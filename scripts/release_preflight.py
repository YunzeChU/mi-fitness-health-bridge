#!/usr/bin/env python3
"""Reject personal-data and generated-artifact risks before public release.

This is intentionally a conservative local gate. It scans the full working tree
as well as the Git candidate list; it does not inspect any external export.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_JSON = {Path("ios/MiHealthImporter/ImportPackage.schema.json")}
FORBIDDEN_SUFFIXES = {".csv", ".xml", ".zip", ".sqlite", ".db", ".ipa", ".tipa"}
FORBIDDEN_DIRECTORY_NAMES = {"__pycache__", "output", "outputs", "reports", "private-imports", "deriveddata", "build"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".xml", ".tsv", ".ndjson"}

# Build these markers from fragments so the guard can inspect its own source.
REAL_PATH_MARKER = re.compile(
    r"(?i)(?:"
    + re.escape("D:" + "\\" + "CODEX")
    + r"|[A-Z]:[\\/](?:users|documents|desktop)[\\/]"
    + r"|/(?:Users|home)/"
    + r"|\\\\[^\\]+\\(?:Users|Documents|Desktop)\\)"
)
EXPORT_HEADER = re.compile(
    r"(?im)^\s*(?:uid|user_id)\s*,\s*(?:sid|session_id)\s*,\s*(?:tag|type)\s*,\s*(?:key|metric)\s*,\s*(?:time|timestamp)\s*,\s*(?:value|data)\b"
)


def relative(path: Path) -> Path:
    return path.resolve().relative_to(ROOT)


def git_candidate_files() -> set[Path]:
    command = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    try:
        result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"Git candidate scan unavailable: {error}") from error
    return {Path(item.decode("utf-8", "surrogateescape")) for item in result.stdout.split(b"\0") if item}


def scan_file(path: Path, findings: list[str]) -> None:
    rel = relative(path)
    suffix = path.suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES:
        findings.append(f"forbidden data or generated artifact: {rel}")
    if suffix == ".json" and rel not in ALLOWED_JSON:
        findings.append(f"JSON is not an approved public contract file: {rel}")
    if suffix not in TEXT_SUFFIXES:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        findings.append(f"non-UTF-8 data-like text file: {rel}")
        return
    if REAL_PATH_MARKER.search(text):
        findings.append(f"real-machine path marker found: {rel}")
    if EXPORT_HEADER.search(text):
        findings.append(f"likely raw-export table header found: {rel}")


def scan_working_tree() -> tuple[set[Path], list[str]]:
    files: set[Path] = set()
    findings: list[str] = []
    for directory, directories, filenames in os.walk(ROOT):
        current = Path(directory)
        directories[:] = [name for name in directories if name != ".git"]
        for name in list(directories):
            if name.lower() in FORBIDDEN_DIRECTORY_NAMES:
                findings.append(f"forbidden generated/private directory: {relative(current / name)}")
                directories.remove(name)
        for name in filenames:
            files.add(relative(current / name))
    return files, findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Local source-only release gate")
    parser.add_argument("--ci", action="store_true", help="identical checks, labelled for CI output")
    args = parser.parse_args()
    try:
        candidate_files = git_candidate_files()
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 2
    working_files, findings = scan_working_tree()
    for rel in sorted(working_files | candidate_files):
        path = ROOT / rel
        if not path.is_file():
            findings.append(f"Git candidate is not a regular file: {rel}")
            continue
        scan_file(path, findings)
    if findings:
        print("Release preflight failed:", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1
    label = "CI preflight" if args.ci else "Local preflight"
    print(f"{label} passed: {len(working_files)} working-tree files, {len(candidate_files)} Git candidate files scanned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
