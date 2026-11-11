"""Synthetic-only smoke tests for the public converter."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONVERTER = ROOT / "converter" / "convert.py"


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


class PublicConverterTests(unittest.TestCase):
    def test_synthetic_export_generates_a_schema_v1_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "export"; output = root / "output"
            source.mkdir(); output.mkdir()
            write_csv(source / "x_MiFitness_hlth_center_aggregated_fitness_data.csv", ["Uid", "Sid", "Tag", "Key", "Time", "Value", "UpdateTime"], [{"Uid": "fictional", "Sid": "fictional", "Tag": "daily_report", "Key": "steps", "Time": "1700000000", "Value": json.dumps({"steps": 1234}), "UpdateTime": "1700000001"}])
            write_csv(source / "x_MiFitness_hlth_center_sport_record.csv", ["Uid", "Sid", "Key", "Time", "Category", "Value", "UpdateTime"], [])
            write_csv(source / "x_MiFitness_user_fitness_data_records.csv", ["uid", "tag", "key", "time", "did", "value", "metric", "last_modify"], [])
            package = output / "package.json"
            result = subprocess.run([sys.executable, str(CONVERTER), "--input", str(source), "--output", str(package)], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(package.read_text(encoding="utf-8"))
            self.assertEqual(data["schemaVersion"], 1)
            self.assertEqual(len(data["records"]), 1)
            self.assertEqual(data["records"][0]["type"], "stepCount")
            self.assertNotIn("1234", result.stdout)


if __name__ == "__main__":
    unittest.main()
