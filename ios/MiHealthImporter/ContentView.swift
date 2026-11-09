import SwiftUI
import UniformTypeIdentifiers

private enum PickerMode { case folder, file }

struct ContentView: View {
    @StateObject private var importer = HealthKitImporter()
    @State private var showingImporter = false
    @State private var pickerMode: PickerMode = .folder
    @State private var manifest: ImportSetManifest?
    @State private var folderURL: URL?
    @State private var singlePreview: ImportPreview?
    @State private var result = ImportResult()
    @State private var alertText: String?
    @State private var isImporting = false
    @State private var progressText = ""
    @State private var completedCount = 0
    @State private var failedParts: [ImportPartDescriptor] = []
    @State private var healthStatus = "先选择正式导入目录；写入前自检不会写入任何健康数据。"

    var body: some View {
        NavigationStack {
            List {
                Section("导入目录（一次选择）") {
                    Button("选择正式导入目录") {
                        pickerMode = .folder
                        showingImporter = true
                    }
                    Text("把含 import-manifest.json 与 parts 文件夹的整个目录放进 iPhone 文件 App，一次选择该目录即可。App 按清单顺序校验 SHA-256 并逐个导入，无需逐个点选 JSON。")
                        .font(.footnote).foregroundStyle(.secondary)
                    if let manifest { manifestSection(manifest) }
                }
                Section("兼容：单个导入包") {
                    Button("选择单个 JSON 导入包") {
                        pickerMode = .file
                        showingImporter = true
                    }
                    if let singlePreview { singlePreviewSection(singlePreview) }
                }
                Section("HealthKit 写入前自检") {
                    Text(healthStatus).font(.footnote)
                    Button("检查写入权限") { checkHealthKit() }.disabled(isImporting)
                    Text("此步骤不写数据。正式导入会以稳定 syncIdentifier 写入，重复运行不会产生重复样本。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if isImporting || !progressText.isEmpty {
                    Section("导入进度") {
                        if isImporting { ProgressView(progressText) }
                        else { Text(progressText).font(.footnote) }
                    }
                }
                if result.totalSucceeded + result.totalSkipped + result.totalFailed > 0 || !failedParts.isEmpty {
                    resultSection
                }
            }
            .navigationTitle("健康数据导入")
            .fileImporter(isPresented: $showingImporter,
                          allowedContentTypes: pickerMode == .folder ? [.folder] : [.json]) { outcome in
                switch outcome {
                case .success(let url):
                    if pickerMode == .folder { loadFolder(url) } else { loadSingle(url) }
                case .failure(let error): alertText = error.localizedDescription
                }
            }
            .onOpenURL { url in
                if url.hasDirectoryPath { loadFolder(url) } else { loadSingle(url) }
            }
            .alert("无法读取", isPresented: Binding(get: { alertText != nil }, set: { if !$0 { alertText = nil } })) {
                Button("好", role: .cancel) {}
            } message: { Text(alertText ?? "") }
        }
    }

    @ViewBuilder private func manifestSection(_ manifest: ImportSetManifest) -> some View {
        Text("记录总数：\(manifest.recordCount)；分包：\(manifest.partCount)")
        if let totals = manifest.totalsByType {
            ForEach(totals.keys.sorted(), id: \.self) { key in
                if let title = ImportRecordType(rawValue: key)?.title {
                    Text("\(title)：\(totals[key] ?? 0) 条").font(.footnote)
                }
            }
        }
        if completedCount > 0 {
            Text("已完成分包：\(completedCount)/\(manifest.partCount)；下次从断点继续")
                .font(.footnote).foregroundStyle(.secondary)
        }
        Button(isImporting ? "正在导入…" : (completedCount == 0 ? "开始完整导入" : "从断点继续导入")) {
            startImportSet(retryOnly: false)
        }.disabled(isImporting)
        if !failedParts.isEmpty {
            Button("重试失败的 \(failedParts.count) 个分包") { startImportSet(retryOnly: true) }
                .disabled(isImporting)
        }
        Text("中断后无需回滚：重新选择同一目录即可从第一个未完成分包继续。")
            .font(.footnote).foregroundStyle(.secondary)
    }

    @ViewBuilder private func singlePreviewSection(_ preview: ImportPreview) -> some View {
        Text("有效记录：\(preview.validRecords.count)；需修正：\(preview.issues.count)")
        if !preview.issues.isEmpty {
            DisclosureGroup("校验问题（\(preview.issues.count)）") {
                ForEach(preview.issues) { issue in
                    Text("\(issue.sourceId)：\(issue.message)").font(.footnote)
                }
            }
        }
        Button("确认并写入 HealthKit") { startSingleImport(preview.validRecords) }
            .disabled(preview.validRecords.isEmpty || isImporting)
    }

    @ViewBuilder private var resultSection: some View {
        Section("导入结果（累计）") {
            Text("成功 \(result.totalSucceeded) · 已存在 \(result.totalSkipped) · 失败 \(result.totalFailed)")
            ForEach(ImportRecordType.allCases) { type in
                if result.count(.succeeded, type: type) + result.count(.skipped, type: type) + result.count(.failed, type: type) > 0 {
                    Text("\(type.title)：成功 \(result.count(.succeeded, type: type))，已存在 \(result.count(.skipped, type: type))，失败 \(result.count(.failed, type: type))")
                        .font(.footnote)
                }
            }
            if !result.sampleFailures.isEmpty {
                DisclosureGroup("失败原因示例") {
                    ForEach(result.sampleFailures, id: \.self) { message in
                        Text(message).font(.footnote)
                    }
                }
            }
            if !failedParts.isEmpty {
                Button("重试失败的 \(failedParts.count) 个分包") { startImportSet(retryOnly: true) }
                    .disabled(isImporting)
            }
        }
    }

    private func loadFolder(_ url: URL) {
        do {
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            let loaded = try PackageLoader.loadManifest(in: url)
            manifest = loaded
            folderURL = url
            singlePreview = nil
            result = ImportResult()
            progressText = ""
            let state = PackageLoader.loadProgress(importSetId: loaded.importSetId)
            completedCount = state.completedParts.count
            failedParts = loaded.parts.filter { state.failedParts[$0.fileName] != nil }
            healthStatus = "已载入导入目录。可先做写入前自检，本步骤不写数据。"
            checkHealthKit()
        } catch {
            alertText = error.localizedDescription
        }
    }

    private func loadSingle(_ url: URL) {
        do {
            singlePreview = try PackageLoader.load(url: url)
            manifest = nil
            folderURL = nil
            result = ImportResult()
            failedParts = []
            completedCount = 0
            progressText = ""
            checkHealthKit()
        } catch {
            alertText = error.localizedDescription
        }
    }

    private func checkHealthKit() {
        healthStatus = "正在检查…"
        Task {
            if manifest != nil {
                healthStatus = await importer.authorizationSummaryForAllTypes()
            } else if let singlePreview {
                healthStatus = await importer.authorizationSummary(for: singlePreview.validRecords)
            } else {
                healthStatus = "请先选择正式导入目录或单个导入包。"
            }
        }
    }

    private func startImportSet(retryOnly: Bool) {
        guard let manifest, let folderURL else { return }
        let targets = retryOnly ? failedParts : manifest.parts.sorted { $0.sequence < $1.sequence }
        guard !targets.isEmpty else { return }
        isImporting = true
        progressText = "准备请求 HealthKit 授权…"
        Task {
            do {
                let types = try HealthKitImporter.allShareTypes()
                try await importer.requestAuthorization(forTypes: types)
                healthStatus = await importer.authorizationSummaryForAllTypes()
            } catch {
                alertText = error.localizedDescription
                isImporting = false
                return
            }
            let access = folderURL.startAccessingSecurityScopedResource()
            defer { if access { folderURL.stopAccessingSecurityScopedResource() } }
            var state = PackageLoader.loadProgress(importSetId: manifest.importSetId)
            var remainingFailures: [ImportPartDescriptor] = []
            for (index, part) in targets.enumerated() {
                if !retryOnly && state.completedParts.contains(part.fileName) { continue }
                progressText = "分包 \(index + 1)/\(targets.count) · 校验 SHA-256…"
                do {
                    let preview = try PackageLoader.loadPart(in: folderURL, descriptor: part)
                    let invalidCount = preview.issues.count
                    let partResult = await importer.importRecords(
                        preview.validRecords,
                        skipExistingLookup: !retryOnly
                    ) { done, total in
                        progressText = "分包 \(index + 1)/\(targets.count) · 本包 \(done)/\(total)"
                    }
                    var merged = result
                    merged.merge(partResult)
                    result = merged
                    if partResult.totalFailed == 0 && invalidCount == 0 {
                        state.completedParts.insert(part.fileName)
                        state.failedParts.removeValue(forKey: part.fileName)
                    } else {
                        state.failedParts[part.fileName] = "失败 \(partResult.totalFailed) 条，无效 \(invalidCount) 条"
                        remainingFailures.append(part)
                    }
                } catch {
                    state.failedParts[part.fileName] = error.localizedDescription
                    remainingFailures.append(part)
                }
                state.updatedAt = PackageLoader.timestamp()
                PackageLoader.saveProgress(state)
                completedCount = state.completedParts.count
                failedParts = remainingFailures
                progressText = "分包 \(index + 1)/\(targets.count) 完成 · 累计成功 \(result.totalSucceeded)，跳过 \(result.totalSkipped)，失败 \(result.totalFailed)"
                await Task.yield()
            }
            progressText = "完成：成功 \(result.totalSucceeded)，已存在 \(result.totalSkipped)，失败 \(result.totalFailed)；待重试分包 \(remainingFailures.count)"
            isImporting = false
        }
    }

    private func startSingleImport(_ records: [ImportRecord]) {
        isImporting = true
        Task {
            do {
                try await importer.requestAuthorization(for: records)
                let partResult = await importer.importRecords(records) { done, total in
                    progressText = "正在写入 \(done)/\(total)…"
                }
                var merged = result
                merged.merge(partResult)
                result = merged
            } catch {
                alertText = error.localizedDescription
            }
            isImporting = false
        }
    }
}
