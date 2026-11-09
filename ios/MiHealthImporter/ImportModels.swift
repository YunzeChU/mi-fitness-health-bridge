import Foundation

enum ImportRecordType: String, Codable, CaseIterable, Identifiable {
    case sleep, workout, cyclingDistance, heartRate, restingHeartRate, oxygenSaturation, stepCount, activeEnergyBurned
    var id: String { rawValue }
    var title: String {
        switch self {
        case .sleep: return "睡眠"
        case .workout: return "锻炼"
        case .cyclingDistance: return "骑车距离"
        case .heartRate: return "心率"
        case .restingHeartRate: return "静息心率"
        case .oxygenSaturation: return "血氧"
        case .stepCount: return "步数"
        case .activeEnergyBurned: return "活动能量"
        }
    }
}

enum SleepStage: String, Codable {
    case awake, inBed, asleepCore, asleepDeep, asleepREM, asleepUnspecified
}

enum WorkoutActivity: String, Codable {
    case walking, running, cycling, hiking, swimming, traditionalStrengthTraining, functionalStrengthTraining, yoga, other
}

struct ImportPackage: Codable {
    let schemaVersion: Int
    let source: String?
    let records: [ImportRecord]
}

struct ImportRecord: Codable, Identifiable, Hashable {
    let sourceId: String
    let type: ImportRecordType
    let date: Date?
    let startDate: Date?
    let endDate: Date?
    let value: Double?
    let sleepStage: SleepStage?
    let workoutActivity: WorkoutActivity?
    let distanceMeters: Double?
    let activeEnergyKilocalories: Double?
    var id: String { sourceId }
}

struct ValidationIssue: Identifiable, Hashable {
    let id = UUID()
    let sourceId: String
    let message: String
}

struct ImportPreview {
    let package: ImportPackage
    let validRecords: [ImportRecord]
    let issues: [ValidationIssue]
    var counts: [ImportRecordType: Int] {
        Dictionary(grouping: validRecords, by: \.type).mapValues(\.count)
    }
}

// MARK: - Complete import set (one directory, resumable)

enum PackageLimits {
    /// A single part is parsed and released before the next one is read, so the
    /// app never holds the full multi-million-record set in memory.
    static let maxRecordsPerPart = 50_000
}

struct ImportPartDescriptor: Codable, Hashable {
    let fileName: String
    let sha256: String
    let sequence: Int
    let recordCount: Int
}

struct ImportSetManifest: Codable {
    let manifestVersion: Int
    let packageSchemaVersion: Int
    let importSetId: String
    let recordCount: Int
    let partCount: Int
    let parts: [ImportPartDescriptor]
    let totalsByType: [String: Int]?
}

struct ImportProgress: Codable {
    var importSetId: String
    var completedParts: Set<String>
    var failedParts: [String: String]
    var updatedAt: String
}

enum RecordOutcome: String { case succeeded, skipped, failed }

/// Aggregate-only results. Keeping one dictionary entry per imported sample
/// would retain millions of strings in memory during a full import.
struct ImportResult {
    private(set) var succeeded: [String: Int] = [:]
    private(set) var skipped: [String: Int] = [:]
    private(set) var failed: [String: Int] = [:]
    private(set) var sampleFailures: [String] = []

    mutating func record(_ outcome: RecordOutcome, type: ImportRecordType, error: String? = nil) {
        let key = type.rawValue
        switch outcome {
        case .succeeded: succeeded[key, default: 0] += 1
        case .skipped: skipped[key, default: 0] += 1
        case .failed:
            failed[key, default: 0] += 1
            if sampleFailures.count < 20, let error { sampleFailures.append(error) }
        }
    }

    mutating func merge(_ other: ImportResult) {
        for (key, value) in other.succeeded { succeeded[key, default: 0] += value }
        for (key, value) in other.skipped { skipped[key, default: 0] += value }
        for (key, value) in other.failed { failed[key, default: 0] += value }
        for message in other.sampleFailures where sampleFailures.count < 20 {
            sampleFailures.append(message)
        }
    }

    func count(_ outcome: RecordOutcome, type: ImportRecordType) -> Int {
        switch outcome {
        case .succeeded: return succeeded[type.rawValue, default: 0]
        case .skipped: return skipped[type.rawValue, default: 0]
        case .failed: return failed[type.rawValue, default: 0]
        }
    }

    var totalSucceeded: Int { succeeded.values.reduce(0, +) }
    var totalSkipped: Int { skipped.values.reduce(0, +) }
    var totalFailed: Int { failed.values.reduce(0, +) }
}
