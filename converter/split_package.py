#!/usr/bin/env python3
"""Split one validated import package into Files-app-friendly local chunks."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def positive(value: str) -> int:
    try: result = int(value)
    except ValueError as error: raise argparse.ArgumentTypeError("must be a positive integer") from error
    if not 1 <= result <= 10_000: raise argparse.ArgumentTypeError("must be 1 through 10000")
    return result


def date_ranges(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return metadata-only ranges; never include a source measurement."""
    dates: dict[str, list[str]] = defaultdict(list)
    for record in records:
        kind, date = record.get("type"), record.get("date") or record.get("startDate")
        if isinstance(kind, str) and isinstance(date, str):
            # Package validation owns exact ISO-8601 validation.  This check
            # keeps a malformed external file out of a manifest.
            try:
                datetime.fromisoformat(date.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("record date is not ISO-8601") from error
            dates[kind].append(date)
    return {
        kind: {"start": min(values)[:10], "end": max(values)[:10], "recordCount": len(values)}
        for kind, values in sorted(dates.items())
    }


def write_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline import package splitter; never alters the original package")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--chunk-size", type=positive, default=10_000)
    parser.add_argument("--manifest-name", default="import-manifest.json", help="metadata-only manifest filename")
    args = parser.parse_args()
    source, destination = args.input.resolve(), args.output_dir.resolve()
    if not source.is_file(): parser.error("--input must be an existing JSON package")
    if destination == source.parent: parser.error("--output-dir must differ from the input package directory")
    if Path(args.manifest_name).name != args.manifest_name or not args.manifest_name.endswith(".json"):
        parser.error("--manifest-name must be a simple .json filename")
    try: package = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error: parser.error(f"cannot read JSON package: {error}")
    records = package.get("records") if isinstance(package, dict) else None
    if package.get("schemaVersion") != 1 or not isinstance(records, list) or not records:
        parser.error("input must be a non-empty schemaVersion 1 package")
    destination.mkdir(parents=True, exist_ok=True)
    chunks = [records[index:index + args.chunk_size] for index in range(0, len(records), args.chunk_size)]
    prefix = source.stem
    planned = [destination / f"{prefix}-part-{index:03}.json" for index in range(1, len(chunks) + 1)]
    manifest_path = destination / args.manifest_name
    if any(path.exists() for path in [*planned, manifest_path]): parser.error("a planned part or manifest already exists; refusing to overwrite")
    # Validate all metadata before creating any output, so a malformed package
    # never leaves a half-written directory that looks importable.
    planned_payloads = []
    for path, chunk in zip(planned, chunks):
        planned_payloads.append((path, chunk, {"schemaVersion": 1, "source": package.get("source", "mi-fitness-local-converter-v1"), "records": chunk}, date_ranges(chunk)))
    manifest_parts: list[dict[str, Any]] = []
    for path, chunk, result, ranges in planned_payloads:
        digest = write_json(path, result)
        manifest_parts.append({
            "fileName": path.name,
            "sha256": digest,
            "recordCount": len(chunk),
            "types": dict(sorted(Counter(str(record.get("type")) for record in chunk).items())),
            "dateRanges": ranges,
        })
    manifest = {
        "manifestVersion": 1,
        "packageSchemaVersion": 1,
        "partCount": len(manifest_parts),
        "recordCount": len(records),
        "parts": manifest_parts,
    }
    write_json(manifest_path, manifest)
    print("parts_written:", len(planned), "records:", len(records), "max_per_part:", args.chunk_size, "manifest:", manifest_path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
