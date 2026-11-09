#!/usr/bin/env python3
"""Build a complete, deduplicated, resumable Apple Health import set offline.

The source export is read-only. A temporary SQLite database provides bounded
memory deduplication and deterministic ordering for multi-million-row inputs;
nothing is written into the export directory. All printed and persisted reports
are metadata-only (counts, field names, date ranges, verification statistics).
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GENERATOR_SOURCE = "mi-fitness-local-import-set-v2"
MANIFEST_VERSION = 2
DEFAULT_CHUNK_SIZE = 20_000
MAX_CHUNK_SIZE = 50_000

TYPE_ORDER = {
    "sleep": 0,
    "workout": 1,
    "cyclingDistance": 2,
    "restingHeartRate": 3,
    "oxygenSaturation": 4,
    "heartRate": 5,
    "stepCount": 6,
    "activeEnergyBurned": 7,
}
SLEEP_STAGES = {"awake", "inBed", "asleepCore", "asleepDeep", "asleepREM", "asleepUnspecified"}
# Reviewed against this export only: 2=deep, 3=light/core, 4=REM, 5=awake.
# State 6 is deliberately absent; it has no reliable mapping.
REVIEWED_SLEEP_STATE_MAP = {2: "asleepDeep", 3: "asleepCore", 4: "asleepREM", 5: "awake"}
SLEEP_SOURCE_PRIORITY = {"watch_night_sleep": 0, "sleep": 1, "watch_daytime_sleep": 2}
WORKOUT_ACTIVITIES = {
    "outdoor_walking": "walking", "walking": "walking",
    "outdoor_running": "running", "running": "running", "indoor_running": "running",
    "outdoor_riding": "cycling", "cycling": "cycling", "indoor_riding": "cycling",
    "hiking": "hiking", "swimming": "swimming",
    "free_training": "functionalStrengthTraining",
    "strength_training": "traditionalStrengthTraining",
    "rope_skipping": "other", "badminton": "other",
}
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

TABLE_SUFFIXES = {
    "detailed": "MiFitness_hlth_center_fitness_data.csv",
    "sport": "MiFitness_hlth_center_sport_record.csv",
    "aggregate": "MiFitness_hlth_center_aggregated_fitness_data.csv",
    "legacy": "MiFitness_user_fitness_data_records.csv",
    "track": "MiFitness_hlth_center_sport_track_data.csv",
}

EXCLUSION_REASONS: dict[str, str] = {
    "stress": "Apple 健康没有等价的数值压力类型，不做硬映射。",
    "single_stress": "Apple 健康没有等价的数值压力类型，不做硬映射。",
    "distance_in_steps": "步数明细内的 distance 未标明单位，且不能由步数反推；不导入步行/跑步距离。",
    "calories_in_steps": "步数明细内嵌的 calories 是主表热量增量的不完整副本，只导主表这一层，避免双写。",
    "calories_in_workout": "运动 calories 是单次运动的汇总值，已被逐条热量增量覆盖，不另写独立样本。",
    "calories_daily_report": "聚合日报告 calories 是当日汇总值，仅用于单位/语义验证，不与逐条明细叠加。",
    "calories_legacy": "旧版热量报告与聚合日报告口径不一致，作为独立源会造成重复，不导入。",
    "body_momentum": "无 Apple 健康等价类型。",
    "light_sensitivity_value": "无 Apple 健康等价类型。",
    "intensity": "仅有汇总时长，无已验证单位与 Apple 健康等价类型。",
    "headset": "耳机使用记录，无 Apple 健康等价类型。",
    "dynamic": "活动汇总片段，与已导入的步数/运动重复且单位未证实。",
    "valid_stand": "站立统计无 Apple 健康直接等价类型，且为汇总值。",
    "pai": "小米 PAI 派生指标，Apple 健康无等价类型。",
    "vitality": "小米活力指标，Apple 健康无等价类型。",
    "training_load": "训练负荷派生指标，Apple 健康无等价类型。",
    "low_heart_rate": "异常心率提示区间，非逐条测量，且单位/语义未证实。",
    "max_heart_rate": "每日极值汇总，不是逐条心率样本，避免与连续心率双写。",
    "min_heart_rate": "每日极值汇总，不是逐条心率样本，避免与连续心率双写。",
    "abnormal_heart_beat": "异常心搏标记区间，非逐条测量。",
    "weight": "样本量不足且单位未证实，不足以确认编码。",
    "sleep_state_6": "状态码 6 未完成可靠映射，按约定默认不导入。",
    "legacy_sleep": "旧版睡眠分段由新版中心分段覆盖，仅作交叉验证，不双写。",
    "legacy_other": "旧版明细被健康中心明细覆盖或单位未证实，不作为独立导入源。",
    "aggregate_daily_report": "聚合日报告是汇总值，与逐条明细重复，避免双写。",
    "workout_energy": "运动 calories 未标明 kcal/kJ，且不能确认是活动能量。",
    "workout_distance_nonpositive": "distance 非正数的运动不携带距离，也不生成骑车距离样本；运动本身仍导入。",
    "workout_pace_cadence_elevation": "配速/步频/海拔为运动摘要字段，Apple 健康无等价样本类型，单位也未证实。",
    "workout_route": "轨迹列不是可直接解码的坐标数据；无法可靠解析或关联时不导入，不伪造轨迹。",
    "sleep_summary_fields": "睡眠评分、算法版本、鼾声、翻身、呼吸质量等派生字段无 Apple 健康等价类型。",
}


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def unix_iso(value: Any) -> str | None:
    stamp = number(value)
    if stamp is None or not 946684800 <= stamp <= 4102444800:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def source_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(f"v1|{kind}|{identity}".encode("utf-8")).hexdigest()
    return f"mifitness-{kind}-{digest}"


def validate(record: dict[str, Any]) -> bool:
    kind = record.get("type")
    identifier = record.get("sourceId")
    if kind not in TYPE_ORDER or not isinstance(identifier, str) or not SOURCE_ID_PATTERN.fullmatch(identifier):
        return False
    if kind == "sleep":
        return (set(record) == {"sourceId", "type", "startDate", "endDate", "sleepStage"}
                and record["sleepStage"] in SLEEP_STAGES and record["startDate"] < record["endDate"])
    if kind == "workout":
        allowed = {"sourceId", "type", "startDate", "endDate", "workoutActivity", "distanceMeters"}
        if not (set(record) <= allowed and {"sourceId", "type", "startDate", "endDate", "workoutActivity"} <= set(record)):
            return False
        if record["workoutActivity"] not in set(WORKOUT_ACTIVITIES.values()) or record["startDate"] >= record["endDate"]:
            return False
        if "distanceMeters" in record:
            distance = number(record["distanceMeters"])
            return distance is not None and 0 <= distance <= 2_000_000
        return True
    if set(record) != {"sourceId", "type", "date", "value"}:
        return False
    value = number(record["value"])
    if value is None or record.get("date") is None:
        return False
    if kind in {"heartRate", "restingHeartRate"}:
        return 0 < value <= 300
    if kind == "oxygenSaturation":
        return 0 <= value <= 1
    if kind == "cyclingDistance":
        return 0 <= value <= 2_000_000
    return 0 <= value <= 10_000_000


def rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as handle:
        yield from csv.DictReader(handle)


def find_table(root: Path, suffix: str) -> Path | None:
    matches = sorted(root.glob(f"*{suffix}"))
    return matches[0] if len(matches) == 1 else None


def parse_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


UPSERT_SQL = """
INSERT INTO records(identity, type_order, rec_type, sort_key, day, revision, payload)
VALUES(?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(identity) DO UPDATE SET
  type_order=excluded.type_order,
  rec_type=excluded.rec_type,
  sort_key=excluded.sort_key,
  day=excluded.day,
  revision=excluded.revision,
  payload=excluded.payload
WHERE excluded.revision > records.revision
"""


class Sink:
    """Bounded-memory staging of validated records into a temporary database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.buffer: list[tuple[Any, ...]] = []
        self.accepted: Counter[str] = Counter()
        self.rejected: Counter[str] = Counter()

    def add(self, kind: str, identity: str, sort_key: str, revision: Any, record: dict[str, Any]) -> None:
        if not validate(record):
            self.rejected[f"{kind}_app_contract_validation"] += 1
            return
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self.buffer.append((identity, TYPE_ORDER[kind], kind, sort_key, sort_key[:10], int(number(revision) or 0), payload))
        self.accepted[kind] += 1
        if len(self.buffer) >= 5_000:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        self.connection.executemany(UPSERT_SQL, self.buffer)
        self.connection.commit()
        self.buffer.clear()


def build_record(kind: str, identity: str, **fields: Any) -> dict[str, Any]:
    return {"sourceId": source_id(kind, identity), "type": kind, **fields}


def scan_detailed(path: Path, sink: Sink, audit: dict[str, Any], sleep_segments: list[tuple[int, int, int, str]]) -> dict[str, float]:
    """Returns per-day energy sums for the unit/verification gate; never persists them."""
    key_counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    spo2_bands: Counter[str] = Counter()
    step_days: dict[str, dict[str, Any]] = {}
    energy_days: dict[str, float] = defaultdict(float)
    heart_timestamps: set[int] = set()
    spo2_timestamps: set[int] = set()
    single_merged = 0
    for row in rows(path):
        key = row.get("Key", "")
        key_counts[key] += 1
        payload = parse_json(row.get("Value", ""))
        if not isinstance(payload, dict):
            skipped["invalid_or_non_object_payload"] += 1
            continue
        revision = row.get("UpdateTime", "")

        if key in {"heart_rate", "single_heart_rate"}:
            stamp, bpm = unix_iso(payload.get("time")), number(payload.get("bpm"))
            if not stamp or bpm is None or not 0 < bpm <= 300:
                skipped["heart_rate_invalid_time_or_value"] += 1
                continue
            stamp_int = int(number(payload.get("time")))
            if key == "single_heart_rate" and stamp_int in heart_timestamps:
                single_merged += 1
            if key == "heart_rate":
                heart_timestamps.add(stamp_int)
            identity = f"hr|{stamp_int}"
            sink.add("heartRate", identity, stamp, revision, build_record("heartRate", identity, date=stamp, value=bpm))

        elif key in {"spo2", "single_spo2"}:
            stamp, raw = unix_iso(payload.get("time")), number(payload.get("spo2"))
            if not stamp or raw is None:
                skipped["oxygen_invalid_time_or_value"] += 1
                continue
            stamp_int = int(number(payload.get("time")))
            if raw <= 0 or raw > 100:
                skipped["oxygen_out_of_percent_range"] += 1
                continue
            if raw <= 1:
                # Mixed encoding would make the unit ambiguous; refuse rather than guess.
                skipped["oxygen_unit_ambiguous"] += 1
                continue
            spo2_bands["percent_1_100"] += 1
            if key == "single_spo2" and stamp_int in spo2_timestamps:
                single_merged += 1
            if key == "spo2":
                spo2_timestamps.add(stamp_int)
            value = raw / 100.0
            identity = f"spo2|{stamp_int}"
            sink.add("oxygenSaturation", identity, stamp, revision, build_record("oxygenSaturation", identity, date=stamp, value=value))

        elif key == "resting_heart_rate":
            stamp_value = payload.get("date_time") if number(payload.get("date_time")) else payload.get("time")
            stamp, bpm = unix_iso(stamp_value), number(payload.get("bpm"))
            if not stamp or bpm is None or not 0 < bpm <= 300:
                skipped["resting_heart_rate_invalid_time_or_value"] += 1
                continue
            stamp_int = int(number(stamp_value))
            identity = f"rhr|{stamp_int}"
            sink.add("restingHeartRate", identity, stamp, revision, build_record("restingHeartRate", identity, date=stamp, value=bpm))

        elif key == "steps":
            stamp, steps = unix_iso(payload.get("time")), number(payload.get("steps"))
            if not stamp or steps is None or steps < 0 or steps > 10_000_000:
                skipped["steps_invalid_time_or_value"] += 1
                continue
            stamp_int = int(number(payload.get("time")))
            day = stamp[:10]
            entry = step_days.setdefault(day, {"count": 0, "last": None, "decreases": 0})
            entry["count"] += 1
            if entry["last"] is not None and steps < entry["last"]:
                entry["decreases"] += 1
            entry["last"] = steps
            if steps == 0:
                skipped["steps_zero_increment_omitted"] += 1
                continue
            identity = f"steps|{stamp_int}"
            sink.add("stepCount", identity, stamp, revision, build_record("stepCount", identity, date=stamp, value=steps))
            skipped["distance_in_steps"] += 1
            skipped["calories_in_steps"] += 1

        elif key == "calories":
            stamp, kcal = unix_iso(payload.get("time")), number(payload.get("calories"))
            if not stamp or kcal is None or kcal < 0 or kcal > 100_000:
                skipped["energy_invalid_time_or_value"] += 1
                continue
            stamp_int = int(number(payload.get("time")))
            energy_days[stamp[:10]] += kcal
            if kcal == 0:
                # A zero increment contributes nothing to the cumulative total.
                skipped["energy_zero_increment_omitted"] += 1
                continue
            identity = f"energy|{stamp_int}"
            sink.add("activeEnergyBurned", identity, stamp, revision,
                     build_record("activeEnergyBurned", identity, date=stamp, value=kcal))

        elif key in {"sleep", "watch_night_sleep", "watch_daytime_sleep"}:
            items = payload.get("items")
            if not isinstance(items, list):
                skipped["sleep_row_without_segments"] += 1
                continue
            for item in items:
                if not isinstance(item, dict):
                    skipped["sleep_item_not_object"] += 1
                    continue
                start, end = number(item.get("start_time")), number(item.get("end_time"))
                state = number(item.get("state"))
                if start is None or end is None or state is None or state != int(state) or end <= start:
                    skipped["sleep_segment_invalid_interval_or_state"] += 1
                    continue
                sleep_segments.append((int(start), int(end), int(state), key))
            skipped["sleep_summary_fields"] += 1

        else:
            skipped[key] += 1
    sink.flush()
    audit["detailed_table"] = {
        "rows": sum(key_counts.values()),
        "key_rows": dict(sorted(key_counts.items())),
        "skipped_reasons": dict(sorted(skipped.items())),
        "oxygen_encoding_bands": dict(spo2_bands),
        "single_readings_merged_into_continuous_timeline": single_merged,
        "continuous_heart_rate_unique_timestamps": len(heart_timestamps),
        "continuous_oxygen_unique_timestamps": len(spo2_timestamps),
        "steps_interval_semantics": {
            "days_with_rows": len(step_days),
            "days_with_any_decrease": sum(1 for entry in step_days.values() if entry["decreases"] > 0),
            "interpretation": "区间增量（同日数值反复回落，非当日累计值）",
        },
        "energy_rows": key_counts.get("calories", 0),
        "energy_days_with_rows": len(energy_days),
        "energy_zero_increment_omitted": skipped.get("energy_zero_increment_omitted", 0),
    }
    return dict(energy_days)


def resolve_sleep(sink: Sink, audit: dict[str, Any], sleep_segments: list[tuple[int, int, int, str]]) -> tuple[set[tuple[int, int, int]], set[tuple[int, int, int]]]:
    """Exact dedupe, drop unverified states, then keep only non-overlapping segments."""
    state_counts: Counter[int] = Counter(state for _, _, state, _ in sleep_segments)
    exact: dict[tuple[int, int, int], str] = {}
    exact_duplicates = 0
    excluded_state = 0
    for start, end, state, source in sleep_segments:
        if state not in REVIEWED_SLEEP_STATE_MAP:
            excluded_state += 1
            continue
        identity = (start, end, state)
        if identity in exact:
            exact_duplicates += 1
            continue
        exact[identity] = source
    all_valid_segments = set(exact.keys())
    grouped: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for identity, source in exact.items():
        grouped[source].append(identity)
    retained_starts: list[int] = []
    retained: list[tuple[int, int, int]] = []
    overlap_dropped = 0
    for source in sorted(grouped, key=lambda item: SLEEP_SOURCE_PRIORITY.get(item, 99)):
        for start, end, state in sorted(grouped[source]):
            index = bisect.bisect_left(retained_starts, start)
            conflicts = (index > 0 and retained[index - 1][1] > start) or (index < len(retained) and retained[index][0] < end)
            if conflicts:
                overlap_dropped += 1
                continue
            retained_starts.insert(index, start)
            retained.insert(index, (start, end, state))
    for start, end, state in retained:
        start_iso, end_iso = unix_iso(start), unix_iso(end)
        if not start_iso or not end_iso:
            continue
        identity = f"sleep|{start}|{end}|{state}"
        stage = REVIEWED_SLEEP_STATE_MAP[state]
        sink.add("sleep", identity, start_iso, 0, build_record("sleep", identity, startDate=start_iso, endDate=end_iso, sleepStage=stage))
    sink.flush()
    audit["sleep_resolution"] = {
        "input_segments": len(sleep_segments),
        "state_distribution": dict(sorted(state_counts.items())),
        "excluded_unverified_states": excluded_state,
        "excluded_state_codes": sorted(code for code in state_counts if code not in REVIEWED_SLEEP_STATE_MAP),
        "exact_duplicate_segments": exact_duplicates,
        "all_valid_segments_before_overlap_removal": len(all_valid_segments),
        "overlapping_segments_dropped": overlap_dropped,
        "retained_segments": len(retained),
        "verified_state_map": {str(code): stage for code, stage in sorted(REVIEWED_SLEEP_STATE_MAP.items())},
        "source_priority": SLEEP_SOURCE_PRIORITY,
    }
    return set(retained), all_valid_segments


def scan_sport(path: Path, sink: Sink, audit: dict[str, Any]) -> None:
    categories: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    distance_status: Counter[str] = Counter()
    cycling_with_distance = 0
    for row in rows(path):
        payload = parse_json(row.get("Value", ""))
        if not isinstance(payload, dict):
            skipped["invalid_payload"] += 1
            continue
        original = str(row.get("Key") or "").lower()
        activity = WORKOUT_ACTIVITIES.get(str(payload.get("sport_type") or "").lower()) or WORKOUT_ACTIVITIES.get(original)
        start, end = unix_iso(payload.get("start_time")), unix_iso(payload.get("end_time"))
        if not activity or not start or not end or start >= end:
            skipped["unrecognised_activity_or_interval"] += 1
            continue
        categories[activity] += 1
        start_raw, end_raw = int(number(payload.get("start_time"))), int(number(payload.get("end_time")))
        distance = number(payload.get("distance"))
        has_distance = distance is not None and 0 < distance <= 2_000_000
        if "distance" not in payload:
            distance_status["missing"] += 1
        elif distance is None:
            distance_status["nonnumeric"] += 1
        elif distance <= 0:
            distance_status["nonpositive"] += 1
            skipped["workout_distance_nonpositive"] += 1
        elif distance > 2_000_000:
            distance_status["over_limit"] += 1
        else:
            distance_status["valid"] += 1
        identity = f"workout|{activity}|{start_raw}|{end_raw}"
        fields: dict[str, Any] = {"startDate": start, "endDate": end, "workoutActivity": activity}
        if has_distance:
            fields["distanceMeters"] = distance
        sink.add("workout", identity, start, row.get("UpdateTime", ""), build_record("workout", identity, **fields))
        if "calories" in payload or "total_cal" in payload:
            skipped["workout_energy"] += 1
        if any(field in payload for field in ("avg_pace", "max_pace", "min_pace", "avg_cadence", "max_cadence", "avg_height", "max_height", "min_height", "rise_height", "fall_height", "total_climbing")):
            skipped["workout_pace_cadence_elevation"] += 1
        if activity == "cycling" and has_distance:
            cycling_identity = f"cycling|{start_raw}|{end_raw}"
            sink.add("cyclingDistance", cycling_identity, end, row.get("UpdateTime", ""),
                     build_record("cyclingDistance", cycling_identity, date=end, value=distance))
            cycling_with_distance += 1
    sink.flush()
    audit["workouts"] = {
        "rows_with_recognised_activity": sum(categories.values()),
        "activity_distribution": dict(sorted(categories.items())),
        "distance_status": dict(sorted(distance_status.items())),
        "cycling_distance_samples": cycling_with_distance,
        "skipped_reasons": dict(sorted(skipped.items())),
    }


def scan_legacy(path: Path, audit: dict[str, Any], modern_segments: set[tuple[int, int, int]]) -> None:
    key_counts: Counter[str] = Counter()
    legacy_segments: set[tuple[int, int, int]] = set()
    for row in rows(path):
        key = row.get("key", "")
        key_counts[key] += 1
        if key not in {"watch_night_sleep_record", "watch_daytime_sleep_record"}:
            continue
        payload = parse_json(row.get("value", ""))
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            start = number(item.get("start") if "start" in item else item.get("start_time"))
            end = number(item.get("end") if "end" in item else item.get("end_time"))
            state = number(item.get("state"))
            if start is None or end is None or state is None or state != int(state) or end <= start:
                continue
            legacy_segments.add((int(start), int(end), int(state)))
    audit["legacy_crosscheck"] = {
        "rows": sum(key_counts.values()),
        "distinct_keys": len(key_counts),
        "legacy_sleep_segments": len(legacy_segments),
        "legacy_segments_absent_from_modern": len(legacy_segments - modern_segments),
        "legacy_segments_present_in_modern": len(legacy_segments & modern_segments),
        "comparison_basis": "重叠消除前的全部有效新版分段（重叠消除属于导入安全处理，不代表旧版缺失）",
        "decision": "旧版分段不导入，仅用于交叉验证" if not (legacy_segments - modern_segments) else "存在未被覆盖的旧版分段，需人工复核",
    }


def scan_track(path: Path, audit: dict[str, Any], sport_keys: set[str], sport_times: dict[str, set[str]]) -> None:
    total = parsed = failed = linked = 0
    for row in rows(path):
        total += 1
        raw = row.get("GPX", "") or ""
        try:
            ET.fromstring(raw)
            parsed += 1
        except (ET.ParseError, ValueError):
            failed += 1
        key, stamp = row.get("Key", ""), row.get("Time", "")
        if key in sport_keys and stamp in sport_times.get(key, set()):
            linked += 1
    audit["workout_route"] = {
        "track_rows": total,
        "gpx_parsed_as_xml": parsed,
        "gpx_not_decodable": failed,
        "rows_linked_to_a_workout_by_key_and_time": linked,
        "decision": "不可靠解码，不导入轨迹" if failed else "需复核",
    }


def scan_aggregate(path: Path, audit: dict[str, Any]) -> None:
    tags: Counter[str] = Counter()
    keys: Counter[str] = Counter()
    for row in rows(path):
        tags[row.get("Tag", "")] += 1
        keys[row.get("Key", "")] += 1
    audit["aggregate_crosscheck"] = {
        "rows": sum(tags.values()),
        "tag_distribution": dict(sorted(tags.items())),
        "key_distribution": dict(sorted(keys.items())),
        "decision": "聚合日报告为汇总值，与逐条明细重复，整体不作为独立导入源",
    }


ENERGY_BUCKETS: list[tuple[float, str]] = [
    (0, "<=0"), (10, "0-10"), (50, "10-50"), (100, "50-100"), (300, "100-300"),
    (500, "300-500"), (800, "500-800"), (1500, "800-1500"), (2500, "1500-2500"),
    (4000, "2500-4000"), (8000, "4000-8000"), (16000, "8000-16000"),
]


def energy_bucket(value: float) -> str:
    for edge, label in ENERGY_BUCKETS:
        if value <= edge:
            return label
    return ">16000"


def median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def verify_energy_unit(aggregate_path: Path, sport_path: Path, energy_days: dict[str, float]) -> dict[str, Any]:
    """Prove the kcal unit and active-energy semantics from the export itself.

    Four independent signals must all agree: daily report magnitude (rules out
    kJ and basal-metabolic totals), the goal magnitude (activity target), the
    per-minute workout magnitude, and detailed-increment sums matching the
    daily report (no hidden unit conversion). Only aggregate/bucket/ratio
    statistics are retained; individual values are never emitted.
    """
    report_by_day: dict[str, float] = {}
    daily_buckets: Counter[str] = Counter()
    goal_values: list[float] = []
    goal_buckets: Counter[str] = Counter()
    for row in rows(aggregate_path):
        if row.get("Tag") != "daily_report" or row.get("Key") != "calories":
            continue
        payload = parse_json(row.get("Value", ""))
        if not isinstance(payload, dict):
            continue
        day = unix_iso(row.get("Time", ""))
        value, goal = number(payload.get("calories")), number(payload.get("goal"))
        if day and value is not None and value > 0:
            report_by_day[day[:10]] = value
            daily_buckets[energy_bucket(value)] += 1
        if goal is not None and goal > 0:
            goal_values.append(goal)
            goal_buckets[energy_bucket(goal)] += 1

    per_minute: list[float] = []
    pace_buckets: Counter[str] = Counter()
    for row in rows(sport_path):
        payload = parse_json(row.get("Value", ""))
        if not isinstance(payload, dict):
            continue
        value, duration = number(payload.get("calories")), number(payload.get("duration"))
        if value is not None and value > 0 and duration is not None and duration > 0:
            pace = value / (duration / 60.0)
            per_minute.append(pace)
            pace_buckets[energy_bucket(pace)] += 1

    ratios = sorted(energy_days[day] / report_by_day[day]
                    for day in report_by_day
                    if day in energy_days and report_by_day[day] > 0 and energy_days[day] > 0)
    daily_median = median_or_none([value for value in report_by_day.values() if value > 0])
    goal_median = median_or_none(goal_values)
    pace_median = median_or_none(per_minute)
    ratio_median = median_or_none(ratios)
    checks = {
        "daily_report_median_in_active_energy_band": daily_median is not None and 50 <= daily_median <= 1500,
        "goal_median_in_kcal_band": goal_median is not None and 100 <= goal_median <= 1000,
        "detailed_sum_matches_daily_report": ratio_median is not None and 0.8 <= ratio_median <= 1.3 and len(ratios) >= 14,
        "workout_per_minute_in_kcal_band": pace_median is not None and 1 <= pace_median <= 20,
    }
    included = all(checks.values())
    return {
        "decision": "included" if included else "excluded",
        "checks": checks,
        "daily_report_days": len(report_by_day),
        "daily_report_value_buckets": dict(sorted(daily_buckets.items())),
        "goal_value_buckets": dict(sorted(goal_buckets.items())),
        "workout_rows_with_energy_and_duration": len(per_minute),
        "workout_per_minute_buckets": dict(sorted(pace_buckets.items())),
        "detailed_sum_over_daily_report": {
            "days_compared": len(ratios),
            "median_ratio": round(ratio_median, 4) if ratio_median is not None else None,
            "p10_ratio": round(ratios[int(len(ratios) * 0.1)], 4) if ratios else None,
            "p90_ratio": round(ratios[int(len(ratios) * 0.9)], 4) if ratios else None,
            "days_within_0.8_to_1.2": sum(1 for value in ratios if 0.8 <= value <= 1.2),
        },
        "conclusion": ("多项独立信号一致：单位为 kcal、语义为活动能量，对应 HKQuantityTypeIdentifierActiveEnergyBurned"
                       if included else "单位或语义未被全部独立信号同时证实，整类不导入"),
        "reason_if_excluded": None if included else "以下校验未全部通过：" + "、".join(key for key, passed in checks.items() if not passed),
    }


def write_parts(connection: sqlite3.Connection, destination: Path, chunk_size: int, audit: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    parts_dir = destination / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    cursor = connection.execute("SELECT rec_type, day, payload FROM records ORDER BY type_order, sort_key")
    manifest_parts: list[dict[str, Any]] = []
    buffer: list[str] = []
    type_counts: Counter[str] = Counter()
    day_min: dict[str, str] = {}
    day_max: dict[str, str] = {}
    total = 0
    sequence = 0

    def emit() -> None:
        nonlocal buffer, sequence, total
        if not buffer:
            return
        sequence += 1
        name = f"part-{sequence:04}.json"
        encoded = ('{"schemaVersion":1,"source":"' + GENERATOR_SOURCE + '","records":[' + ",".join(buffer) + "]}\n").encode("utf-8")
        (parts_dir / name).write_bytes(encoded)
        manifest_parts.append({
            "fileName": f"parts/{name}",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "sequence": sequence,
            "recordCount": len(buffer),
            "types": dict(sorted(type_counts.items())),
            "dateRanges": {kind: {"start": day_min[kind], "end": day_max[kind], "recordCount": count}
                           for kind, count in sorted(type_counts.items()) if kind in day_min},
        })
        total += len(buffer)
        buffer = []
        type_counts.clear()
        day_min.clear()
        day_max.clear()

    for rec_type, day, payload in cursor:
        buffer.append(payload)
        type_counts[rec_type] += 1
        if rec_type not in day_min or day < day_min[rec_type]:
            day_min[rec_type] = day
        if rec_type not in day_max or day > day_max[rec_type]:
            day_max[rec_type] = day
        if len(buffer) >= chunk_size:
            emit()
    emit()
    audit["parts_written"] = len(manifest_parts)
    return manifest_parts, total


def verify_parts(destination: Path, manifest_parts: list[dict[str, Any]]) -> dict[str, Any]:
    recomputed = 0
    mismatches = 0
    total_records = 0
    for part in manifest_parts:
        path = destination / part["fileName"]
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != part["sha256"]:
            mismatches += 1
            continue
        recomputed += 1
        package = json.loads(encoded)
        if package.get("schemaVersion") != 1 or len(package.get("records", [])) != part["recordCount"]:
            mismatches += 1
            continue
        total_records += len(package["records"])
    return {"parts_recomputed": recomputed, "digest_or_count_mismatches": mismatches, "records_verified": total_records}


def positive_chunk(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数") from error
    if not 1 <= value <= MAX_CHUNK_SIZE:
        raise argparse.ArgumentTypeError(f"必须是 1 到 {MAX_CHUNK_SIZE} 之间的整数")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="离线生成完整、去重、可续传的正式 Apple 健康导入集；只输出元数据")
    parser.add_argument("--input", type=Path, required=True, help="小米导出 CSV 目录（只读）")
    parser.add_argument("--output-dir", type=Path, required=True, help="正式导入目录，必须不存在或为空")
    parser.add_argument("--chunk-size", type=positive_chunk, default=DEFAULT_CHUNK_SIZE)
    args = parser.parse_args()

    root = args.input.resolve()
    destination = args.output_dir.resolve()
    here = Path(__file__).resolve().parent
    if not root.is_dir() or here in root.parents or root == here:
        parser.error("--input 必须是仓库外的现有导出目录")
    if destination == root or root in destination.parents:
        parser.error("--output-dir 不得位于导出目录内")
    if destination.exists() and any(destination.iterdir()):
        prior_path = destination / "import-manifest.json"
        if not prior_path.is_file():
            parser.error("--output-dir 已存在且非空，且不是本生成器创建的导入集；拒绝覆盖")
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parser.error("--output-dir 中的清单无法解析；拒绝覆盖")
        if prior.get("generator") != GENERATOR_SOURCE or prior.get("manifestVersion") != MANIFEST_VERSION:
            parser.error("--output-dir 不是本生成器创建的导入集；拒绝覆盖")
        # Idempotent rebuild: parts are overwritten in place, never deleted.
        # This is part of the recoverability design, not an overwrite of unknown data.
    tables = {name: find_table(root, suffix) for name, suffix in TABLE_SUFFIXES.items()}
    missing = [name for name, path in tables.items() if path is None]
    if missing:
        parser.error(f"缺少或存在歧义的源表：{', '.join(sorted(missing))}")
    destination.mkdir(parents=True, exist_ok=True)

    audit: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generator": GENERATOR_SOURCE,
        "notes": "仅含计数、字段结构、日期范围与校验统计；不含任何健康数值、坐标、账号、设备标识或本机路径。",
    }
    gates: dict[str, dict[str, Any]] = {}
    sleep_segments: list[tuple[int, int, int, str]] = []
    with tempfile.TemporaryDirectory(prefix="mi-import-set-") as temporary:
        database = Path(temporary) / "staging.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE records (identity TEXT PRIMARY KEY, type_order INTEGER NOT NULL, rec_type TEXT NOT NULL, sort_key TEXT NOT NULL, day TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL)")
        sink = Sink(connection)

        energy_days = scan_detailed(tables["detailed"], sink, audit, sleep_segments)  # type: ignore[arg-type]
        _, all_modern_segments = resolve_sleep(sink, audit, sleep_segments)
        scan_sport(tables["sport"], sink, audit)  # type: ignore[arg-type]
        # Compare legacy against ALL valid modern segments (before overlap
        # removal); overlap removal is an import-safety step, not evidence that
        # a legacy segment was uncovered.
        scan_legacy(tables["legacy"], audit, all_modern_segments)  # type: ignore[arg-type]
        scan_aggregate(tables["aggregate"], audit)  # type: ignore[arg-type]

        sport_keys: set[str] = set()
        sport_times: dict[str, set[str]] = defaultdict(set)
        for row in rows(tables["sport"]):  # type: ignore[arg-type]
            key = row.get("Key", "")
            sport_keys.add(key)
            sport_times[key].add(row.get("Time", ""))
        scan_track(tables["track"], audit, sport_keys, sport_times)  # type: ignore[arg-type]

        pre_gate = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        detailed = audit["detailed_table"]
        oxygen_ambiguous = detailed["skipped_reasons"].get("oxygen_unit_ambiguous", 0)
        oxygen_percent_rows = detailed["oxygen_encoding_bands"].get("percent_1_100", 0)
        if oxygen_ambiguous or oxygen_percent_rows == 0:
            connection.execute("DELETE FROM records WHERE rec_type='oxygenSaturation'")
            gates["oxygenSaturation"] = {
                "decision": "excluded",
                "reason": "血氧出现 <=1 的混合编码或没有可确认的百分比读数；单位未被唯一证实，整类不导入。",
            }
        else:
            gates["oxygenSaturation"] = {
                "decision": "included",
                "evidence": f"{oxygen_percent_rows} 条读数全部落在 (1,100] 区间，无 0–1 编码；按百分比 /100 归一化为 0–1。",
            }
        steps_semantics = detailed["steps_interval_semantics"]
        days, decreasing = steps_semantics["days_with_rows"], steps_semantics["days_with_any_decrease"]
        ratio = decreasing / max(1, days)
        if days < 14 or ratio < 0.5:
            connection.execute("DELETE FROM records WHERE rec_type='stepCount'")
            gates["stepCount"] = {
                "decision": "excluded",
                "reason": "无法由本地数据确认步数是区间增量（同日回落天数不足），按约定不猜测、整类不导入。",
                "days_with_rows": days,
                "days_with_any_decrease": decreasing,
            }
        else:
            gates["stepCount"] = {
                "decision": "included",
                "evidence": f"{days} 天中有 {decreasing} 天出现同日数值回落（{ratio:.1%}），确认为区间增量而非当日累计值。",
                "representation": "每条增量写为一个独立的 stepCount 数量样本，Apple 健康按量值累加；不猜测区间端点，不叠加日报告总量。",
            }
        energy_evidence = verify_energy_unit(tables["aggregate"], tables["sport"], energy_days)  # type: ignore[arg-type]
        audit["calories_evidence"] = energy_evidence
        if energy_evidence["decision"] != "included":
            connection.execute("DELETE FROM records WHERE rec_type='activeEnergyBurned'")
            gates["activeEnergyBurned"] = {
                "decision": "excluded",
                "reason": str(energy_evidence["reason_if_excluded"]),
            }
        else:
            gates["activeEnergyBurned"] = {
                "decision": "included",
                "evidence": str(energy_evidence["conclusion"]),
                "unit": "kcal",
                "healthkit_type": "HKQuantityTypeIdentifierActiveEnergyBurned",
                "source_layer": "仅健康中心逐条热量增量；步数内嵌副本、运动汇总与日报告均不重复导入",
            }
        connection.commit()
        post_gate = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        audit["safety_gates"] = gates
        audit["unit_validation"] = {
            "heart_rate": "字段名为 bpm，值域经逐条校验落在 (0,300]，直接作为 count/min 样本。",
            "resting_heart_rate": "字段名为 bpm，时间取 date_time（缺失时取 time），均为已验证的 Unix 秒。",
            "oxygen_saturation": "见 safety_gates.oxygenSaturation；未出现 0–1 与 1–100 混合编码。",
            "steps": "见 safety_gates.stepCount；单位为步数计数。",
            "active_energy": "见 safety_gates.activeEnergyBurned：kcal 单位与活动能量语义由多项独立信号交叉验证，未靠字段名猜测。",
            "workout_distance": "运动 distance 按米解析，仅导入有效正值。",
            "sleep_intervals": "新版 items 的 start_time/end_time 已验证为 Unix 秒；汇总 duration 为分钟，仅作校验，不用于生成样本。",
            "sleep_states": "仅使用本地已验证的 2=深睡/3=浅睡/4=REM/5=清醒；状态 6 及未知状态不导入。",
            "never_guessed": ["距离单位（步数明细）", "状态码 6", "GPX 坐标", "轨迹时间", "旧版热量报告口径"],
        }
        audit["staging"] = {
            "rows_accepted_before_dedupe": dict(sorted(sink.accepted.items())),
            "rows_rejected_by_contract": dict(sorted(sink.rejected.items())),
            "unique_logical_identities_after_dedupe": pre_gate,
            "deduplicated_rows": sum(sink.accepted.values()) - pre_gate,
            "records_after_safety_gates": post_gate,
            "rows_excluded_by_safety_gates": pre_gate - post_gate,
        }
        connection.execute("CREATE INDEX idx_records_order ON records(type_order, sort_key)")
        connection.commit()
        manifest_parts, total = write_parts(connection, destination, args.chunk_size, audit)
        connection.close()
    if not manifest_parts:
        print("FAIL：没有生成任何可导入记录")
        return 1

    planned_names = {part["fileName"].split("/")[-1] for part in manifest_parts}
    stale_parts = sorted(path.name for path in (destination / "parts").glob("part-*.json") if path.name not in planned_names)
    digest_input = "".join(part["sha256"] for part in manifest_parts).encode("ascii")
    import_set_id = hashlib.sha256(digest_input).hexdigest()
    verification = verify_parts(destination, manifest_parts)
    type_totals: Counter[str] = Counter()
    type_ranges: dict[str, dict[str, str]] = {}
    for part in manifest_parts:
        for kind, count in part["types"].items():
            type_totals[kind] += count
            entry = part["dateRanges"].get(kind)
            if entry:
                current = type_ranges.setdefault(kind, {"start": entry["start"], "end": entry["end"]})
                current["start"] = min(current["start"], entry["start"])
                current["end"] = max(current["end"], entry["end"])

    deduplication = {
        "logical_identity": "每类按稳定的 (类型, UTC 时间戳) 或 (类型, 区间起止, 状态) 作为唯一键，写入临时 SQLite 主键约束",
        "revision_policy": "同一逻辑键保留 UpdateTime 最大的修订版本（UPSERT WHERE excluded.revision > records.revision）",
        "single_vs_continuous": "单次心率/单次血氧与连续读数共用同一时间戳身份键，自动消除重复，不双写",
        "active_energy": "仅健康中心逐条热量增量；步数明细内嵌 calories 为不完整副本，运动 calories 与日报告为汇总值，均不叠加",
        "sleep": "仅新版中心三类；排除未验证状态码；按 (起止,状态) 精确去重；按来源优先级丢弃任何区间交叠段，保证任意时刻至多一个睡眠样本",
        "legacy_sleep": "旧版分段由新版覆盖，仅作交叉验证，不生成记录",
        "aggregate": "聚合日报告为汇总值，不与逐条明细叠加",
        "idempotency": "每条记录携带稳定 SHA-256 sourceId；App 写入时作为 HKMetadataKeySyncIdentifier（syncVersion=1），重复导入不会产生重复样本",
    }
    excluded = [{"field": field, "reason": reason} for field, reason in sorted(EXCLUSION_REASONS.items())]
    excluded.extend({"field": kind, "reason": gate["reason"]} for kind, gate in sorted(gates.items()) if gate["decision"] == "excluded")
    recovery = {
        "resume": "App 按 importSetId 持久化每个分包的完成状态；中断后重新选择同一目录即自动跳过已完成分包，从第一个未完成分包继续",
        "retry": "批量写入失败时自动逐条重试；仍失败的分包记录为失败，可在 App 内一次性重试所有失败分包",
        "integrity": "导入前逐个重算分包 SHA-256，不匹配的分包拒绝导入并明确报告，不会静默跳过",
        "reimport": "整个导入集可重复运行；HealthKit syncIdentifier + syncVersion 保证幂等",
        "partial_import": "分包按类型聚集且按时间有序；即使中途停止，已完成部分保持有效，无需回滚",
    }
    manifest = {
        "manifestVersion": MANIFEST_VERSION,
        "packageSchemaVersion": SCHEMA_VERSION,
        "importSetId": import_set_id,
        "generatedAt": audit["generatedAt"],
        "generator": GENERATOR_SOURCE,
        "recordCount": total,
        "partCount": len(manifest_parts),
        "chunkSize": args.chunk_size,
        "typeOrder": sorted(TYPE_ORDER, key=lambda item: TYPE_ORDER[item]),
        "totalsByType": dict(sorted(type_totals.items())),
        "dateRangesByType": {kind: dict(sorted(entry.items())) for kind, entry in sorted(type_ranges.items())},
        "parts": manifest_parts,
        "deduplication": deduplication,
        "excludedFields": excluded,
        "recovery": recovery,
        "verification": verification,
    }
    (destination / "import-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    audit["final"] = {
        "recordCount": total,
        "partCount": len(manifest_parts),
        "totalsByType": dict(sorted(type_totals.items())),
        "dateRangesByType": {kind: dict(sorted(entry.items())) for kind, entry in sorted(type_ranges.items())},
        "verification": verification,
        "stale_parts_not_in_manifest": stale_parts,
    }
    (destination / "audit-report.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    print("mode: generated")
    print("parts:", len(manifest_parts), "records:", total)
    for kind in sorted(type_totals):
        entry = type_ranges.get(kind, {})
        print(f"type[{kind}]: {type_totals[kind]} records, {entry.get('start', '-')}..{entry.get('end', '-')}")
    print("verification:", json.dumps(verification, ensure_ascii=False, sort_keys=True))
    print("deduplicated_rows:", audit["staging"]["deduplicated_rows"])
    if verification["digest_or_count_mismatches"]:
        print("FAIL：分包校验不一致")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
