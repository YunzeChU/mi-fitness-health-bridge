import Foundation
import HealthKit

enum HealthKitImportError: LocalizedError {
    case unavailable(String)
    case unsupported(String)
    var errorDescription: String? {
        switch self {
        case .unavailable(let text), .unsupported(let text): return text
        }
    }
}

@MainActor
final class HealthKitImporter: ObservableObject {
    private let store = HKHealthStore()
    private let syncVersion = 1
    // Bounded arrays keep XPC traffic proportional to part count instead of
    // making one call per reading. A part is released before the next is read.
    private let saveBatchSize = 500
    private let lookupBatchSize = 500

    /// Authorise every type used by a complete import set once, without having
    /// to materialise millions of records first.
    static func allShareTypes() throws -> Set<HKSampleType> {
        var types: Set<HKSampleType> = [HKObjectType.workoutType()]
        let quantities: [HKQuantityTypeIdentifier] = [
            .heartRate, .restingHeartRate, .oxygenSaturation, .stepCount,
            .distanceCycling, .activeEnergyBurned
        ]
        for identifier in quantities {
            guard let type = HKObjectType.quantityType(forIdentifier: identifier) else {
                throw HealthKitImportError.unavailable("此设备不支持 \(identifier.rawValue)")
            }
            types.insert(type)
        }
        guard let sleep = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else {
            throw HealthKitImportError.unavailable("此设备不支持睡眠类别")
        }
        types.insert(sleep)
        return types
    }

    func requestAuthorization(forTypes types: Set<HKSampleType>) async throws {
        try await store.requestAuthorization(toShare: types, read: [])
    }

    func requestAuthorization(for records: [ImportRecord]) async throws {
        let types = try Set(records.map { try sampleType(for: $0) })
        try await requestAuthorization(forTypes: types)
    }

    /// Checks availability and authorization only; it writes no health sample.
    func authorizationSummary(for records: [ImportRecord]) async -> String {
        guard HKHealthStore.isHealthDataAvailable() else { return "此设备当前不可使用 HealthKit。" }
        do {
            let types = try Set(records.map { try sampleType(for: $0) })
            return try await summary(forTypes: types)
        } catch {
            return "HealthKit 自检失败：\(error.localizedDescription)"
        }
    }

    func authorizationSummaryForAllTypes() async -> String {
        guard HKHealthStore.isHealthDataAvailable() else { return "此设备当前不可使用 HealthKit。" }
        do { return try await summary(forTypes: Self.allShareTypes()) }
        catch { return "HealthKit 自检失败：\(error.localizedDescription)" }
    }

    private func summary(forTypes types: Set<HKSampleType>) async throws -> String {
        let requestStatus = try await store.statusForAuthorizationRequest(toShare: types, read: [])
        let saving = types.map { "\($0.identifier)：\(savingStatus(store.authorizationStatus(for: $0)))" }.sorted().joined(separator: "；")
        return "HealthKit 可用。系统授权请求状态：\(requestStatusText(requestStatus))。写入权限：\(saving)"
    }

    /// `skipExistingLookup` uses HealthKit's sync identifier/version as the
    /// idempotency mechanism on a first pass (much faster for millions of
    /// readings). Retrying failed parts keeps the lookup so already-written
    /// samples are reported as skipped rather than written again.
    func importRecords(_ records: [ImportRecord], skipExistingLookup: Bool = false, progress: @escaping (Int, Int) -> Void) async -> ImportResult {
        var result = ImportResult()
        var completed = 0
        for packageBatch in records.chunked(into: saveBatchSize) {
            let groups = Dictionary(grouping: packageBatch, by: \.type)
            for (_, typedRecords) in groups {
                var prepared: [(record: ImportRecord, sample: HKSample)] = []
                for record in typedRecords {
                    do { prepared.append((record, try makeSample(for: record))) }
                    catch { result.record(.failed, type: record.type, error: error.localizedDescription) }
                }
                guard let first = prepared.first else { continue }
                do {
                    let existing: Set<String> = skipExistingLookup ? [] :
                        try await existingSourceIDs(for: prepared.map(\.record), sampleType: try sampleType(for: first.record))
                    let pending = prepared.filter { !existing.contains($0.record.sourceId) }
                    for item in prepared where existing.contains(item.record.sourceId) {
                        result.record(.skipped, type: item.record.type)
                    }
                    await saveWithFallback(pending, result: &result)
                } catch {
                    for item in prepared {
                        result.record(.failed, type: item.record.type, error: error.localizedDescription)
                    }
                }
            }
            completed += packageBatch.count
            progress(completed, records.count)
        }
        return result
    }

    /// Query sync identifiers in bounded sets rather than once per record.
    private func existingSourceIDs(for records: [ImportRecord], sampleType: HKSampleType) async throws -> Set<String> {
        var existing: Set<String> = []
        for identifiers in records.map(\.sourceId).chunked(into: lookupBatchSize) {
            let found: Set<String> = try await withCheckedThrowingContinuation { continuation in
                let predicate = HKQuery.predicateForObjects(withMetadataKey: HKMetadataKeySyncIdentifier, allowedValues: identifiers)
                let query = HKSampleQuery(sampleType: sampleType, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, error in
                    if let error { continuation.resume(throwing: error) }
                    else {
                        let found = Set((samples ?? []).compactMap { $0.metadata?[HKMetadataKeySyncIdentifier] as? String })
                        continuation.resume(returning: found)
                    }
                }
                store.execute(query)
            }
            existing.formUnion(found)
        }
        return existing
    }

    private func saveWithFallback(_ pending: [(record: ImportRecord, sample: HKSample)], result: inout ImportResult) async {
        guard !pending.isEmpty else { return }
        do {
            try await save(pending.map(\.sample))
            for item in pending { result.record(.succeeded, type: item.record.type) }
        } catch {
            // A batch failure is isolated by retrying its members one by one.
            for item in pending {
                do {
                    try await save([item.sample])
                    result.record(.succeeded, type: item.record.type)
                } catch {
                    result.record(.failed, type: item.record.type, error: error.localizedDescription)
                }
            }
        }
    }

    private func save(_ samples: [HKSample]) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            store.save(samples) { success, error in
                if let error { continuation.resume(throwing: error) }
                else if success { continuation.resume() }
                else { continuation.resume(throwing: HealthKitImportError.unavailable("HealthKit 未确认写入成功")) }
            }
        }
    }

    private func metadata(for record: ImportRecord) -> [String: Any] {
        [HKMetadataKeySyncIdentifier: record.sourceId, HKMetadataKeySyncVersion: syncVersion]
    }

    private func sampleType(for record: ImportRecord) throws -> HKSampleType {
        switch record.type {
        case .sleep:
            guard let type = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) else { throw HealthKitImportError.unavailable("此设备不支持睡眠类别") }
            return type
        case .workout: return HKObjectType.workoutType()
        case .cyclingDistance:
            guard let type = HKObjectType.quantityType(forIdentifier: .distanceCycling) else { throw HealthKitImportError.unavailable("此设备不支持骑车距离") }; return type
        case .heartRate:
            guard let type = HKObjectType.quantityType(forIdentifier: .heartRate) else { throw HealthKitImportError.unavailable("此设备不支持心率类别") }; return type
        case .restingHeartRate:
            guard let type = HKObjectType.quantityType(forIdentifier: .restingHeartRate) else { throw HealthKitImportError.unavailable("此设备不支持静息心率类别") }; return type
        case .oxygenSaturation:
            guard let type = HKObjectType.quantityType(forIdentifier: .oxygenSaturation) else { throw HealthKitImportError.unavailable("此设备不支持血氧类别") }; return type
        case .stepCount:
            guard let type = HKObjectType.quantityType(forIdentifier: .stepCount) else { throw HealthKitImportError.unavailable("此设备不支持步数类别") }; return type
        case .activeEnergyBurned:
            guard let type = HKObjectType.quantityType(forIdentifier: .activeEnergyBurned) else { throw HealthKitImportError.unavailable("此设备不支持活动能量类别") }; return type
        }
    }

    private func makeSample(for record: ImportRecord) throws -> HKSample {
        let metadata = metadata(for: record)
        switch record.type {
        case .sleep:
            guard let type = try sampleType(for: record) as? HKCategoryType,
                  let start = record.startDate, let end = record.endDate, let stage = record.sleepStage else { throw HealthKitImportError.unsupported("无效的睡眠记录") }
            return HKCategorySample(type: type, value: sleepValue(stage), start: start, end: end, metadata: metadata)
        case .workout:
            guard let activity = record.workoutActivity, let start = record.startDate, let end = record.endDate else { throw HealthKitImportError.unsupported("无效的锻炼记录") }
            let energy = record.activeEnergyKilocalories.map { HKQuantity(unit: .kilocalorie(), doubleValue: $0) }
            let distance = record.distanceMeters.map { HKQuantity(unit: .meter(), doubleValue: $0) }
            return HKWorkout(activityType: workoutType(activity), start: start, end: end, duration: end.timeIntervalSince(start), totalEnergyBurned: energy, totalDistance: distance, metadata: metadata)
        default:
            guard let type = try sampleType(for: record) as? HKQuantityType, let date = record.date, let value = record.value else { throw HealthKitImportError.unsupported("无效的数量记录") }
            return HKQuantitySample(type: type, quantity: HKQuantity(unit: unit(for: record.type), doubleValue: value), start: date, end: date, metadata: metadata)
        }
    }

    private func sleepValue(_ stage: SleepStage) -> Int {
        switch stage {
        case .awake: return HKCategoryValueSleepAnalysis.awake.rawValue
        case .inBed: return HKCategoryValueSleepAnalysis.inBed.rawValue
        case .asleepCore: return HKCategoryValueSleepAnalysis.asleepCore.rawValue
        case .asleepDeep: return HKCategoryValueSleepAnalysis.asleepDeep.rawValue
        case .asleepREM: return HKCategoryValueSleepAnalysis.asleepREM.rawValue
        case .asleepUnspecified: return HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue
        }
    }

    private func unit(for type: ImportRecordType) -> HKUnit {
        switch type {
        case .heartRate, .restingHeartRate: return HKUnit.count().unitDivided(by: .minute())
        case .oxygenSaturation: return .percent()
        case .stepCount: return .count()
        case .cyclingDistance: return .meter()
        case .activeEnergyBurned: return .kilocalorie()
        case .sleep, .workout: fatalError("No quantity unit")
        }
    }

    private func workoutType(_ activity: WorkoutActivity) -> HKWorkoutActivityType {
        switch activity {
        case .walking: return .walking
        case .running: return .running
        case .cycling: return .cycling
        case .hiking: return .hiking
        case .swimming: return .swimming
        case .traditionalStrengthTraining: return .traditionalStrengthTraining
        case .functionalStrengthTraining: return .functionalStrengthTraining
        case .yoga: return .yoga
        case .other: return .other
        }
    }

    private func requestStatusText(_ status: HKAuthorizationRequestStatus) -> String {
        switch status {
        case .unknown: return "未知"
        case .shouldRequest: return "需要请求"
        case .unnecessary: return "无需再次请求"
        @unknown default: return "系统返回了未知状态"
        }
    }

    private func savingStatus(_ status: HKAuthorizationStatus) -> String {
        switch status {
        case .notDetermined: return "尚未决定"
        case .sharingDenied: return "已拒绝"
        case .sharingAuthorized: return "已允许"
        @unknown default: return "未知"
        }
    }
}

private extension Array {
    func chunked(into size: Int) -> [[Element]] {
        stride(from: 0, to: count, by: size).map { Array(self[$0..<Swift.min($0 + size, count)]) }
    }
}
