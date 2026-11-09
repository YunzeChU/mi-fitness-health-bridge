#!/usr/bin/env python3
"""Locally infer whether numeric Mi Fitness sleep states can be mapped safely.

This program is deliberately an *analyser*, not an importer.  It prints only
aggregate fit statistics: no timestamps, measurements, record payloads, or
paths.  A mapping must be reviewed and explicitly copied into the converter;
this utility never writes an ImportPackage.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SUMMARY_KEY = "watch_sleep_report"
SEGMENT_KEY = "watch_night_sleep_record"
TARGETS = {
    "sleep_deep_duration": "asleepDeep",
    "sleep_light_duration": "asleepCore",
    "sleep_rem_duration": "asleepREM",
    "sleep_awake_duration": "awake",
}
# The export does not state duration units.  Test the common interpretations
# rather than silently assuming one.  ``raw seconds -> reported minutes`` is
# the expected winner for many Mi Fitness versions.
SCALES = {
    "raw": 1.0,
    "raw_seconds_to_minutes": 1.0 / 60.0,
    "raw_milliseconds_to_minutes": 1.0 / 60_000.0,
    "raw_milliseconds_to_seconds": 1.0 / 1_000.0,
}


def rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as handle:
        yield from csv.DictReader(handle)


def as_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def parse_object(text: str) -> dict[str, Any] | None:
    try:
        candidate = json.loads(text)
    except (TypeError, ValueError):
        return None
    return candidate if isinstance(candidate, dict) else None


def find_export_file(root: Path) -> Path:
    matches = sorted(root.glob("*_MiFitness_user_fitness_data_records.csv"))
    if len(matches) != 1:
        raise ValueError("expected exactly one *_MiFitness_user_fitness_data_records.csv file")
    return matches[0]


def read_sleep_data(path: Path) -> tuple[dict[str, dict[str, float]], dict[str, Counter[int]], Counter[str]]:
    summaries: dict[str, dict[str, float]] = {}
    segments: dict[str, Counter[int]] = defaultdict(Counter)
    skipped: Counter[str] = Counter()
    for row in rows(path):
        key, tag, stamp = row.get("key"), row.get("tag"), row.get("time")
        if tag != "days" or not stamp:
            continue
        payload = parse_object(row.get("value", ""))
        if payload is None:
            if key in {SUMMARY_KEY, SEGMENT_KEY}:
                skipped["invalid_json"] += 1
            continue
        if key == SUMMARY_KEY:
            values: dict[str, float] = {}
            for field in TARGETS:
                value = as_number(payload.get(field))
                if value is not None and value >= 0:
                    values[field] = value
            if len(values) == len(TARGETS):
                summaries[stamp] = values
            else:
                skipped["incomplete_summary"] += 1
        elif key == SEGMENT_KEY:
            items = payload.get("items")
            if not isinstance(items, list):
                skipped["missing_segment_items"] += 1
                continue
            durations: Counter[int] = Counter()
            for item in items:
                if not isinstance(item, dict):
                    skipped["invalid_segment_item"] += 1
                    continue
                state = as_number(item.get("state"))
                start = as_number(item.get("start"))
                # Some exports include both end and seg_end.  ``end`` is the
                # logical segment boundary; fall back only when it is absent.
                end = as_number(item.get("end"))
                if end is None:
                    end = as_number(item.get("seg_end"))
                if state is None or state != int(state) or start is None or end is None or end <= start:
                    skipped["invalid_segment_interval"] += 1
                    continue
                durations[int(state)] += end - start
            if durations:
                segments[stamp] = durations
            else:
                skipped["empty_valid_segments"] += 1
    return summaries, segments, skipped


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def median_relative_error(observed: list[float], expected: list[float]) -> float | None:
    ratios = [abs(a - b) / max(b, 1.0) for a, b in zip(observed, expected) if b > 0]
    return median(ratios) if ratios else None


def analyse(summaries: dict[str, dict[str, float]], segments: dict[str, Counter[int]]) -> dict[str, Any]:
    joined = sorted(set(summaries) & set(segments))
    state_counts: Counter[int] = Counter()
    for stamp in joined:
        state_counts.update(segments[stamp].keys())
    candidates: list[dict[str, Any]] = []
    for state in sorted(state_counts):
        for summary_field, health_stage in TARGETS.items():
            expected = [summaries[stamp][summary_field] for stamp in joined]
            raw = [segments[stamp][state] for stamp in joined]
            for scale_name, scale in SCALES.items():
                error = median_relative_error([value * scale for value in raw], expected)
                if error is not None:
                    candidates.append({
                        "state": state,
                        "candidate_stage": health_stage,
                        "summary_field": summary_field,
                        "unit_interpretation": scale_name,
                        "median_relative_error": round(error, 4),
                    })
    candidates.sort(key=lambda item: (item["median_relative_error"], item["state"], item["candidate_stage"]))
    # A recommendation requires enough nights and distinctly better fit than
    # the next alternative for that same numeric state.  It remains a review
    # result, never an automatic import decision.
    recommendations: list[dict[str, Any]] = []
    for state in sorted(state_counts):
        ranked = [item for item in candidates if item["state"] == state]
        if not ranked:
            continue
        best = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        distinct = runner_up is None or best["median_relative_error"] * 2 < runner_up["median_relative_error"]
        if len(joined) >= 14 and best["median_relative_error"] <= 0.10 and distinct:
            recommendations.append({**best, "status": "reviewable"})
        else:
            recommendations.append({**best, "status": "insufficient_evidence"})
    return {
        "summary_nights": len(summaries),
        "segment_nights": len(segments),
        "joined_nights": len(joined),
        "states_seen": sorted(state_counts),
        "best_candidates": candidates[: min(len(candidates), 24)],
        "recommendations": recommendations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline aggregate-only Mi Fitness sleep mapping analyser")
    parser.add_argument("--input", required=True, type=Path, help="Mi Fitness CSV export directory")
    parser.add_argument("--report", type=Path, help="optional aggregate-only JSON report")
    args = parser.parse_args()
    root = args.input.resolve()
    if not root.is_dir():
        parser.error("--input must be an existing export directory")
    if args.report and (args.report.resolve().parent == root or root in args.report.resolve().parents):
        parser.error("--report must not be written inside --input")
    summaries, segments, skipped = read_sleep_data(find_export_file(root))
    result = analyse(summaries, segments)
    result["skipped"] = dict(sorted(skipped.items()))
    print("summary_nights:", result["summary_nights"])
    print("segment_nights:", result["segment_nights"])
    print("joined_nights:", result["joined_nights"])
    print("states_seen:", ",".join(str(item) for item in result["states_seen"]))
    for item in result["recommendations"]:
        print("state", item["state"], "->", item["candidate_stage"], item["unit_interpretation"], "fit", item["median_relative_error"], item["status"])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
