#!/usr/bin/env python3
"""Offline, read-only Mi Fitness CSV -> MiHealthImporter ImportPackage converter."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
TYPES = {"sleep", "workout", "cyclingDistance", "heartRate", "restingHeartRate", "oxygenSaturation", "stepCount", "activeEnergyBurned"}
SLEEP_STAGES = {"awake", "inBed", "asleepCore", "asleepDeep", "asleepREM", "asleepUnspecified"}
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ACTIVITIES = {
    "outdoor_walking": "walking", "walking": "walking", "outdoor_running": "running", "running": "running",
    "indoor_running": "running", "outdoor_riding": "cycling", "cycling": "cycling", "indoor_riding": "cycling",
    "hiking": "hiking", "swimming": "swimming", "yoga": "yoga", "free_training": "functionalStrengthTraining",
    "strength_training": "traditionalStrengthTraining", "rope_skipping": "other", "badminton": "other",
}


class Report:
    def __init__(self) -> None:
        self.accepted: Counter[str] = Counter()
        self.skipped: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.dates: dict[str, list[datetime]] = defaultdict(list)

    def skip(self, reason: str) -> None: self.skipped[reason] += 1
    def error(self, reason: str) -> None: self.errors[reason] += 1
    def accept(self, rec: dict[str, Any]) -> None:
        kind = rec["type"]; self.accepted[kind] += 1
        iso = rec.get("date") or rec.get("startDate")
        self.dates[kind].append(datetime.fromisoformat(iso.replace("Z", "+00:00")))

    def replace_accepted(self, records: list[dict[str, Any]]) -> None:
        """Make report counts/ranges describe final deduplicated output only."""
        self.accepted = Counter()
        self.dates = defaultdict(list)
        for record in records: self.accept(record)


def parse_json(text: str, report: Report, reason: str) -> Any | None:
    try: return json.loads(text)
    except (TypeError, ValueError): report.skip(reason); return None


def unix_iso(value: Any) -> str | None:
    try:
        n = float(value)
        if not (946684800 <= n <= 4102444800): return None  # 2000-01-01..2100-01-01, Unix seconds only
        return datetime.fromtimestamp(n, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError): return None


def number(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else None
    except (TypeError, ValueError): return None


def source_id(kind: str, table: str, identity: str) -> str:
    digest = hashlib.sha256(f"v1|{kind}|{table}|{identity}".encode("utf-8")).hexdigest()
    return f"mifitness-{kind}-{digest}"


def add(records: list[dict[str, Any]], report: Report, kind: str, table: str, identity: str, **fields: Any) -> None:
    rec = {"sourceId": source_id(kind, table, identity), "type": kind, **fields}
    if validate(rec): records.append(rec); report.accept(rec)
    else: report.skip("app_contract_validation")


def validate(r: dict[str, Any]) -> bool:
    kind = r.get("type")
    if kind not in TYPES or not isinstance(r.get("sourceId"), str) or not SOURCE_ID_PATTERN.fullmatch(r["sourceId"]): return False
    if kind == "sleep":
        return set(r) == {"sourceId", "type", "startDate", "endDate", "sleepStage"} and r["sleepStage"] in SLEEP_STAGES and r["startDate"] < r["endDate"]
    if kind == "workout":
        allowed = {"sourceId", "type", "startDate", "endDate", "workoutActivity", "distanceMeters", "activeEnergyKilocalories"}
        return set(r) <= allowed and {"sourceId", "type", "startDate", "endDate", "workoutActivity"} <= set(r) and r["workoutActivity"] in set(ACTIVITIES.values()) and r["startDate"] < r["endDate"] and all(number(r[x]) is not None and 0 <= number(r[x]) <= limit for x, limit in (("distanceMeters", 2_000_000), ("activeEnergyKilocalories", 100_000)) if x in r)
    if set(r) != {"sourceId", "type", "date", "value"} or r.get("date") is None or number(r.get("value")) is None: return False
    value = number(r["value"])
    if kind in {"heartRate", "restingHeartRate"}: return 0 < value <= 300
    if kind == "oxygenSaturation": return 0 <= value <= 1
    if kind == "cyclingDistance": return 0 <= value <= 2_000_000
    return 0 <= value <= (10_000_000 if kind == "stepCount" else 100_000)


def rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as handle:
        yield from csv.DictReader(handle)


def convert_aggregate(path: Path, records: list[dict[str, Any]], report: Report) -> None:
    # One authority only: daily_report. daily_mark is intentionally ignored to avoid duplicate daily totals.
    for row in rows(path):
        if row.get("Tag") != "daily_report": continue
        key, date = row.get("Key"), unix_iso(row.get("Time"))
        payload = parse_json(row.get("Value", ""), report, "aggregate_invalid_json")
        if not date or not isinstance(payload, dict): report.skip("aggregate_invalid_time_or_shape"); continue
        identity = f"{key}|{row.get('Time')}|{row.get('UpdateTime','')}"
        if key == "steps" and (v := number(payload.get("steps"))) is not None and v >= 0:
            add(records, report, "stepCount", path.name, identity, date=date, value=v)
        elif key == "calories":
            # The export omits kcal/kJ metadata; the App requires kcal, so never infer it.
            report.skip("active_energy_unit_unverified")
        elif key not in {"steps", "calories"}: report.skip("aggregate_unsupported_key")
        else: report.skip("aggregate_missing_or_invalid_value")


def convert_workouts(path: Path, records: list[dict[str, Any]], report: Report, include_validated_distance: bool = False, write_cycling_distance_samples: bool = False, max_per_source_key: int | None = None) -> None:
    accepted_source_keys: Counter[str] = Counter()
    for row in rows(path):
        payload = parse_json(row.get("Value", ""), report, "workout_invalid_json")
        if not isinstance(payload, dict): continue
        # Some Mi Fitness exports use a numeric payload sport_type but retain
        # the descriptive, documented category in the CSV Key column.
        activity = ACTIVITIES.get(str(payload.get("sport_type") or "").lower()) or ACTIVITIES.get(str(row.get("Key") or "").lower())
        start, end = unix_iso(payload.get("start_time")), unix_iso(payload.get("end_time"))
        if not activity or not start or not end or start >= end: report.skip("workout_unrecognised_activity_or_time"); continue
        source_key = str(row.get("Key") or "")
        if max_per_source_key is not None and accepted_source_keys[source_key] >= max_per_source_key:
            report.skip("workout_source_key_limit")
            continue
        identity = f"{row.get('Key')}|{payload.get('start_time')}|{payload.get('end_time')}|{row.get('UpdateTime','')}"
        fields: dict[str, Any] = {"startDate": start, "endDate": end, "workoutActivity": activity}
        validated_distance: float | None = None
        if "distance" in payload:
            distance = number(payload.get("distance"))
            if include_validated_distance and distance is not None and distance >= 0:
                fields["distanceMeters"] = distance
                validated_distance = distance
            else:
                report.skip("workout_distance_unit_unverified")
        if "calories" in payload or "total_cal" in payload: report.skip("workout_energy_unit_unverified")
        add(records, report, "workout", path.name, identity, **fields)
        accepted_source_keys[source_key] += 1
        # HKWorkout.totalDistance is an exercise summary, not a standalone
        # distanceCycling sample.  Apple Health's cycling-distance view needs
        # the latter to show source data when drilled into.
        if write_cycling_distance_samples and activity == "cycling" and validated_distance is not None:
            add(records, report, "cyclingDistance", path.name, f"{identity}|cyclingDistance", date=end, value=validated_distance)


def explicit_point(payload: Any, value_names: tuple[str, ...], time_names: tuple[str, ...]) -> tuple[float, str] | None:
    if not isinstance(payload, dict): return None
    value = next((number(payload[x]) for x in value_names if x in payload), None)
    stamp = next((unix_iso(payload[x]) for x in time_names if x in payload), None)
    return (value, stamp) if value is not None and stamp else None


def convert_legacy(path: Path, records: list[dict[str, Any]], report: Report, validated_sleep_map: dict[int, str] | None = None) -> None:
    for row in rows(path):
        key, tag, metric = row.get("key", ""), row.get("tag", ""), row.get("metric", "")
        payload = parse_json(row.get("value", ""), report, "legacy_invalid_json")
        row_date = unix_iso(row.get("time")); identity = f"{key}|{tag}|{metric}|{row.get('time')}|{row.get('last_modify','')}"
        if key == "huami_manual_heartrate_record" and tag == "once" and isinstance(payload, dict) and row_date:
            v = number(payload.get("hrm"))
            if v and v > 0: add(records, report, "heartRate", path.name, identity, date=row_date, value=v)
            else: report.skip("heart_rate_invalid_manual_value")
        elif key == "watch_single_spo2_report" and tag == "once":
            point = explicit_point(payload, ("MeasureValue",), ("Timestamp",))
            if point:
                v, date = point
                if 0 <= v <= 1: normalized = v
                elif 1 < v <= 100: normalized = v / 100
                else: report.skip("oxygen_out_of_range"); continue
                add(records, report, "oxygenSaturation", path.name, identity, date=date, value=normalized)
            else: report.skip("oxygen_missing_explicit_point")
        elif key == "watch_hrm_report" and tag == "days" and row_date and isinstance(payload, dict):
            v = number(payload.get("rhr_avg"))
            if v and v > 0: add(records, report, "restingHeartRate", path.name, identity, date=row_date, value=v)
            else: report.skip("resting_hr_missing_rhr_avg")
        elif key == "watch_night_sleep_record" and tag == "days":
            if validated_sleep_map:
                convert_sleep_items(payload, path.name, identity, records, report, validated_sleep_map)
            else:
                report.skip("sleep_requires_explicit_validated_mapping")
        elif key.startswith(("watch_steps_", "watch_calories_", "watch_sport_", "watch_daytime_sleep_", "watch_sleep_")):
            report.skip("legacy_shadowed_by_authoritative_source")


def convert_sleep_items(payload: Any, table: str, identity: str, records: list[dict[str, Any]], report: Report, validated_sleep_map: dict[int, str]) -> None:
    # Numeric state codes are accepted only after a caller explicitly passes a
    # mapping which was locally validated against matching Mi Fitness summaries.
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list): report.skip("sleep_items_unrecognised"); return
    accepted = 0
    for index, item in enumerate(payload["items"]):
        if not isinstance(item, dict): report.skip("sleep_item_not_object"); continue
        raw_state = number(item.get("state"))
        stage = validated_sleep_map.get(int(raw_state)) if raw_state is not None and raw_state == int(raw_state) else None
        start = next((unix_iso(item[k]) for k in ("start_time", "startTime", "start") if k in item), None)
        end = next((unix_iso(item[k]) for k in ("end_time", "endTime", "end", "seg_end") if k in item), None)
        if stage not in SLEEP_STAGES or not start or not end or start >= end: report.skip("sleep_stage_or_interval_unverified"); continue
        add(records, report, "sleep", table, f"{identity}|{index}", startDate=start, endDate=end, sleepStage=stage); accepted += 1
    if not accepted: report.skip("sleep_no_safe_stage_records")


def report_data(report: Report, records: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": "dry-run" if dry_run else "generated", "accepted": dict(sorted(report.accepted.items())), "skipped": dict(sorted(report.skipped.items())), "errors": dict(sorted(report.errors.items())), "date_ranges": {}, "unique_source_ids": len({r["sourceId"] for r in records}), "record_count": len(records)}
    for kind in sorted(report.dates):
        dates = report.dates[kind]
        result["date_ranges"][kind] = {"start": min(dates).date().isoformat(), "end": max(dates).date().isoformat(), "record_count": len(dates)}
    return result


def output_report(data: dict[str, Any]) -> None:
    print("mode:", data["mode"])
    print("accepted:", json.dumps(data["accepted"], ensure_ascii=False))
    print("skipped:", json.dumps(data["skipped"], ensure_ascii=False))
    print("errors:", json.dumps(data["errors"], ensure_ascii=False))
    for kind, item in sorted(data["date_ranges"].items()): print(f"date_range[{kind}]: {item['start']}..{item['end']} ({item['record_count']} records)")
    print("unique_source_ids:", data["unique_source_ids"], "/", data["record_count"])


def find_export_file(root: Path, suffix: str) -> Path | None:
    matches = sorted(root.glob(f"*{suffix}"))
    return matches[0] if len(matches) == 1 else None


def stable_record_key(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Stable order independent of CSV row or table traversal order."""
    return (
        record["type"], record.get("date", ""), record.get("startDate", ""),
        record.get("endDate", ""), record["sourceId"],
    )


def deduplicate_and_sort(records: list[dict[str, Any]], report: Report) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in sorted(records, key=stable_record_key):
        if record["sourceId"] in seen:
            report.skip("duplicate_source_id")
            continue
        seen.add(record["sourceId"])
        retained.append(record)
    return retained


def positive_record_limit(raw: str) -> int:
    try: value = int(raw)
    except ValueError as error: raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value < 1: raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def sleep_state_map(raw: str) -> dict[int, str]:
    """Parse an explicitly reviewed numeric Mi Fitness state mapping."""
    mapping: dict[int, str] = {}
    try:
        for part in raw.split(","):
            state_text, stage = part.strip().split(":", 1)
            state = int(state_text.strip())
            stage = stage.strip()
            if state < 0 or stage not in SLEEP_STAGES or state in mapping or stage in mapping.values():
                raise ValueError
            mapping[state] = stage
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be unique STATE:STAGE pairs, e.g. 2:asleepDeep,3:asleepCore,4:asleepREM,5:awake") from error
    if not mapping:
        raise argparse.ArgumentTypeError("must contain at least one STATE:STAGE pair")
    return mapping


def record_types(raw: str) -> set[str]:
    selected = {item.strip() for item in raw.split(",") if item.strip()}
    if not selected or selected - TYPES:
        raise argparse.ArgumentTypeError("must be one or more known package types")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Mi Fitness converter; never prints health values.")
    parser.add_argument("--input", type=Path, required=True, help="Mi Fitness CSV export directory")
    parser.add_argument("--output", type=Path, help="output ImportPackage JSON; any writable local directory except --input")
    parser.add_argument("--report", type=Path, help="optional value-free conversion report JSON")
    parser.add_argument("--dry-run", action="store_true", help="report counts/ranges/errors only; never write a package")
    parser.add_argument("--max-records", type=positive_record_limit, help="after validation/deduplication, emit at most this many stably sorted records")
    parser.add_argument("--latest-records", type=positive_record_limit, help="after validation/deduplication, retain the latest N records by type/date; mutually exclusive with --max-records")
    parser.add_argument("--sleep-state-map", type=sleep_state_map, help="explicit locally validated Mi Fitness numeric sleep-state mapping; sleep is otherwise omitted")
    parser.add_argument("--only-types", type=record_types, help="comma-separated package types to retain after validation, e.g. sleep")
    parser.add_argument("--workout-distance-meters", action="store_true", help="include workout distance only after local pace/duration cross-validation supports metres")
    parser.add_argument("--cycling-distance-samples", action="store_true", help="for validated cycling workouts, also emit Apple Health distanceCycling samples")
    parser.add_argument("--max-workouts-per-source-key", type=positive_record_limit, help="retain at most N recognised workouts for each original Mi Fitness workout category")
    args = parser.parse_args()
    if args.max_records is not None and args.latest_records is not None:
        parser.error("--max-records and --latest-records cannot be used together")
    root = args.input.resolve(); here = Path(__file__).resolve().parent
    if not root.is_dir() or here in root.parents or root == here: parser.error("--input must be an existing export directory outside MiFitnessConverter")
    if int(args.dry_run) + int(args.output is not None) != 1: parser.error("use exactly one of --dry-run or --output")
    for destination in (args.output, args.report):
        if destination and (destination.resolve().parent == root or root in destination.resolve().parents): parser.error("--output/--report must not be written inside --input")
    records: list[dict[str, Any]] = []; report = Report()
    targets = [
        ("_MiFitness_hlth_center_aggregated_fitness_data.csv", convert_aggregate),
        ("_MiFitness_hlth_center_sport_record.csv", convert_workouts),
        ("_MiFitness_user_fitness_data_records.csv", convert_legacy),
    ]
    for suffix, worker in targets:
        path = find_export_file(root, suffix)
        if path:
            if worker is convert_legacy: worker(path, records, report, args.sleep_state_map)
            elif worker is convert_workouts: worker(path, records, report, args.workout_distance_meters, args.cycling_distance_samples, args.max_workouts_per_source_key)
            else: worker(path, records, report)
        else: report.error(f"missing_or_ambiguous_required_file:{suffix}")
    records = deduplicate_and_sort(records, report)
    if args.only_types:
        retained = [record for record in records if record["type"] in args.only_types]
        report.skipped["record_type_filtered"] += len(records) - len(retained)
        records = retained
    if args.max_records is not None and len(records) > args.max_records:
        report.skipped["max_records_truncated"] += len(records) - args.max_records
        records = records[:args.max_records]
    if args.latest_records is not None and len(records) > args.latest_records:
        report.skipped["latest_records_truncated"] += len(records) - args.latest_records
        records = records[-args.latest_records:]
    report.replace_accepted(records)
    data = report_data(report, records, args.dry_run)
    output_report(data)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as f:
            json.dump({"schemaVersion": SCHEMA_VERSION, "source": "mi-fitness-local-converter-v1", "records": records}, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        print("package_written:", args.output.name, "records:", len(records))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8", newline="\n") as f: json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
