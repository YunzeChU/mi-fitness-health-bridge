import Foundation
import CoreFoundation
import CryptoKit

enum PackageLoaderError: LocalizedError {
    case invalidFile(String)
    case invalidManifest(String)
    case digestMismatch(String)
    var errorDescription: String? {
        switch self {
        case .invalidFile(let text): return "导入包无效：\(text)"
        case .invalidManifest(let text): return "导入清单无效：\(text)"
        case .digestMismatch(let text): return "分包校验失败：\(text)"
        }
    }
}

enum PackageLoader {
    /// Legacy single-file entry point, kept for compatibility with small trials.
    static func load(url: URL) throws -> ImportPreview {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: url)
        return try load(data: data)
    }

    static func load(data: Data) throws -> ImportPreview {
        let raw: Any
        do { raw = try JSONSerialization.jsonObject(with: data, options: []) }
        catch { throw PackageLoaderError.invalidFile("文件不是 JSON 对象") }
        try validateRawPackage(raw)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = strictDate(text) else {
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "时间必须是带时区的 ISO-8601 时间")
            }
            return date
        }
        let package: ImportPackage
        do { package = try decoder.decode(ImportPackage.self, from: data) }
        catch { throw PackageLoaderError.invalidFile(error.localizedDescription) }
        guard package.schemaVersion == 1 else { throw PackageLoaderError.invalidFile("只支持 schemaVersion 1") }
        guard !package.records.isEmpty else { throw PackageLoaderError.invalidFile("records 不能为空") }
        guard package.records.count <= PackageLimits.maxRecordsPerPart else {
            throw PackageLoaderError.invalidFile("records 最多 \(PackageLimits.maxRecordsPerPart) 条")
        }
        let duplicated = Set(package.records.map(\.sourceId)).count != package.records.count
        guard !duplicated else { throw PackageLoaderError.invalidFile("sourceId 必须在包内唯一") }
        var valid: [ImportRecord] = []
        var issues: [ValidationIssue] = []
        for record in package.records {
            if let error = validate(record) { issues.append(.init(sourceId: record.sourceId, message: error)) }
            else { valid.append(record) }
        }
        return ImportPreview(package: package, validRecords: valid, issues: issues)
    }

    // MARK: - Complete import set

    static func loadManifest(in folder: URL) throws -> ImportSetManifest {
        let url = folder.appendingPathComponent("import-manifest.json")
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw PackageLoaderError.invalidManifest("所选目录中缺少 import-manifest.json，请选择包含清单与 parts 目录的整个文件夹")
        }
        let data: Data
        do { data = try Data(contentsOf: url) }
        catch { throw PackageLoaderError.invalidManifest("无法读取清单：\(error.localizedDescription)") }
        let manifest: ImportSetManifest
        do { manifest = try JSONDecoder().decode(ImportSetManifest.self, from: data) }
        catch { throw PackageLoaderError.invalidManifest("清单格式错误：\(error.localizedDescription)") }
        guard manifest.manifestVersion == 2, manifest.packageSchemaVersion == 1 else {
            throw PackageLoaderError.invalidManifest("清单版本不受支持")
        }
        guard !manifest.parts.isEmpty, manifest.parts.count == manifest.partCount else {
            throw PackageLoaderError.invalidManifest("分包数量与清单不一致")
        }
        guard manifest.importSetId.count == 64 else {
            throw PackageLoaderError.invalidManifest("导入集标识无效")
        }
        guard Set(manifest.parts.map(\.sequence)).count == manifest.parts.count else {
            throw PackageLoaderError.invalidManifest("分包序号重复")
        }
        for part in manifest.parts {
            guard part.sha256.count == 64, part.recordCount > 0, part.recordCount <= PackageLimits.maxRecordsPerPart else {
                throw PackageLoaderError.invalidManifest("分包描述无效：\(part.fileName)")
            }
            guard safePartName(part.fileName) != nil else {
                throw PackageLoaderError.invalidManifest("分包文件名不安全：\(part.fileName)")
            }
        }
        return manifest
    }

    /// Only simple `parts/xxx.json` or `xxx.json` names are accepted; no path traversal.
    static func safePartName(_ fileName: String) -> String? {
        let name = (fileName as NSString).lastPathComponent
        guard fileName == name || fileName == "parts/\(name)" else { return nil }
        guard name.hasSuffix(".json"), !name.contains(".."), !name.contains("/") else { return nil }
        return name
    }

    static func partURL(in folder: URL, fileName: String) -> URL? {
        guard let name = safePartName(fileName) else { return nil }
        return folder.appendingPathComponent("parts", isDirectory: true).appendingPathComponent(name)
    }

    /// Reads one part, verifies its digest, and returns records for this part only.
    /// Callers release the preview before loading the next part.
    static func loadPart(in folder: URL, descriptor: ImportPartDescriptor) throws -> ImportPreview {
        guard let url = partURL(in: folder, fileName: descriptor.fileName) else {
            throw PackageLoaderError.digestMismatch("分包文件名不安全：\(descriptor.fileName)")
        }
        let data: Data
        do { data = try Data(contentsOf: url) }
        catch { throw PackageLoaderError.digestMismatch("无法读取 \(descriptor.fileName)：\(error.localizedDescription)") }
        guard sha256Hex(data) == descriptor.sha256 else {
            throw PackageLoaderError.digestMismatch("\(descriptor.fileName) 的 SHA-256 与清单不一致，已拒绝导入")
        }
        let preview = try load(data: data)
        guard preview.package.records.count == descriptor.recordCount else {
            throw PackageLoaderError.digestMismatch("\(descriptor.fileName) 的记录数与清单不一致")
        }
        return preview
    }

    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - Resume state (app container only)

    private static func progressFileURL(importSetId: String) -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let directory = base.appendingPathComponent("ImportProgress", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("\(importSetId).json")
    }

    static func loadProgress(importSetId: String) -> ImportProgress {
        guard let data = try? Data(contentsOf: progressFileURL(importSetId: importSetId)),
              let progress = try? JSONDecoder().decode(ImportProgress.self, from: data),
              progress.importSetId == importSetId else {
            return ImportProgress(importSetId: importSetId, completedParts: [], failedParts: [:], updatedAt: "")
        }
        return progress
    }

    static func saveProgress(_ progress: ImportProgress) {
        guard let data = try? JSONEncoder().encode(progress) else { return }
        try? data.write(to: progressFileURL(importSetId: progress.importSetId), options: .atomic)
    }

    static func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }

    // MARK: - Validation

    private static func validate(_ r: ImportRecord) -> String? {
        guard matchesIdentifier(r.sourceId) else { return "sourceId 必须是 1–128 位的 ASCII 字母/数字开头标识，可含 . _ : -" }
        switch r.type {
        case .sleep:
            guard let start = r.startDate, let end = r.endDate, start < end, r.sleepStage != nil else { return "睡眠需要有效的 startDate、endDate 和 sleepStage" }
            guard r.date == nil && r.value == nil && r.workoutActivity == nil && r.distanceMeters == nil && r.activeEnergyKilocalories == nil else { return "睡眠记录不能包含数量或锻炼字段" }
        case .workout:
            guard let start = r.startDate, let end = r.endDate, start < end, r.workoutActivity != nil else { return "锻炼需要有效的 startDate、endDate 和 workoutActivity" }
            guard r.date == nil && r.value == nil && r.sleepStage == nil else { return "锻炼记录不能包含数量或睡眠字段" }
            if let distance = r.distanceMeters, (!distance.isFinite || !(0...2_000_000).contains(distance)) { return "距离必须是 0–2000000 米之间的有限数" }
            if let energy = r.activeEnergyKilocalories, (!energy.isFinite || !(0...100_000).contains(energy)) { return "活动能量必须是 0–100000 千卡之间的有限数" }
        case .heartRate, .restingHeartRate:
            guard let value = r.value, value.isFinite, (0...300).contains(value), value > 0, r.date != nil else { return "心率需要 date 和大于 0 且不超过 300 的 bpm value" }
            guard r.startDate == nil && r.endDate == nil && r.sleepStage == nil && r.workoutActivity == nil && r.distanceMeters == nil && r.activeEnergyKilocalories == nil else { return "数量记录包含不适用字段" }
        case .oxygenSaturation:
            guard let value = r.value, value.isFinite, (0...1).contains(value), r.date != nil else { return "血氧需要 date 和 0–1 的有限 value" }
            guard r.startDate == nil && r.endDate == nil && r.sleepStage == nil && r.workoutActivity == nil && r.distanceMeters == nil && r.activeEnergyKilocalories == nil else { return "数量记录包含不适用字段" }
        case .stepCount, .cyclingDistance, .activeEnergyBurned:
            let upperBound = r.type == .stepCount ? 10_000_000.0 : (r.type == .cyclingDistance ? 2_000_000.0 : 100_000.0)
            guard let value = r.value, value.isFinite, (0...upperBound).contains(value), r.date != nil else { return "数量记录需要 date 和范围内的有限 value" }
            guard r.startDate == nil && r.endDate == nil && r.sleepStage == nil && r.workoutActivity == nil && r.distanceMeters == nil && r.activeEnergyKilocalories == nil else { return "数量记录包含不适用字段" }
        }
        return nil
    }

    private static let identifierPattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    private static let timestampPattern = try! NSRegularExpression(pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,3})?(Z|[+-][0-9]{2}:[0-9]{2})$")

    private static func validateRawPackage(_ raw: Any) throws {
        guard let object = raw as? [String: Any] else { throw PackageLoaderError.invalidFile("顶层必须是对象") }
        let allowedTop = Set(["schemaVersion", "source", "records"])
        let unexpectedTop = Set(object.keys).subtracting(allowedTop)
        guard unexpectedTop.isEmpty else { throw PackageLoaderError.invalidFile("顶层不允许未知字段：\(unexpectedTop.sorted().joined(separator: ", "))") }
        guard let version = object["schemaVersion"] as? NSNumber, isJSONNumber(version), version.intValue == 1, version.doubleValue == 1 else { throw PackageLoaderError.invalidFile("schemaVersion 必须为数字 1") }
        if let source = object["source"] {
            guard let text = source as? String, matchesIdentifier(text) else { throw PackageLoaderError.invalidFile("source 必须符合标识符规范") }
        }
        guard let records = object["records"] as? [Any], !records.isEmpty, records.count <= PackageLimits.maxRecordsPerPart else {
            throw PackageLoaderError.invalidFile("records 必须是 1–\(PackageLimits.maxRecordsPerPart) 条的数组")
        }
        for (index, element) in records.enumerated() {
            guard let record = element as? [String: Any] else { throw PackageLoaderError.invalidFile("records[\(index)] 必须是对象") }
            let allowed = Set(["sourceId", "type", "date", "startDate", "endDate", "value", "sleepStage", "workoutActivity", "distanceMeters", "activeEnergyKilocalories"])
            let unknown = Set(record.keys).subtracting(allowed)
            guard unknown.isEmpty else { throw PackageLoaderError.invalidFile("records[\(index)] 不允许未知字段：\(unknown.sorted().joined(separator: ", "))") }
            guard let sourceId = record["sourceId"] as? String, matchesIdentifier(sourceId) else { throw PackageLoaderError.invalidFile("records[\(index)].sourceId 不符合规范") }
            guard record["type"] is String else { throw PackageLoaderError.invalidFile("records[\(index)].type 必须是字符串") }
            for key in ["date", "startDate", "endDate"] where record[key] != nil {
                guard let text = record[key] as? String, strictDate(text) != nil else { throw PackageLoaderError.invalidFile("records[\(index)].\(key) 必须是带时区的 ISO-8601 时间") }
            }
            for key in ["value", "distanceMeters", "activeEnergyKilocalories"] where record[key] != nil {
                guard let number = record[key] as? NSNumber, isJSONNumber(number), number.doubleValue.isFinite else { throw PackageLoaderError.invalidFile("records[\(index)].\(key) 必须是有限数") }
            }
            for key in ["sleepStage", "workoutActivity"] where record[key] != nil {
                guard record[key] is String else { throw PackageLoaderError.invalidFile("records[\(index)].\(key) 必须是字符串") }
            }
        }
    }

    private static func matchesIdentifier(_ text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        return identifierPattern.firstMatch(in: text, range: range) != nil
    }

    private static func strictDate(_ text: String) -> Date? {
        let range = NSRange(text.startIndex..., in: text)
        guard timestampPattern.firstMatch(in: text, range: range) != nil else { return nil }
        let regular = ISO8601DateFormatter()
        regular.formatOptions = [.withInternetDateTime]
        if let date = regular.date(from: text) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: text)
    }

    private static func isJSONNumber(_ number: NSNumber) -> Bool {
        CFGetTypeID(number) != CFBooleanGetTypeID()
    }
}
