"""Normalize collected Wonju pharmacy operation records into traceable schedules."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_MODULE = Path(__file__).with_name("collect_wonju_pharmacy_operations.py")
spec = importlib.util.spec_from_file_location("pharmacy_collector", COLLECTOR_MODULE)
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)

DAYS = (
    ("weekday", "weekday_hours_source_raw", "weekday_hours_normalized"),
    ("saturday", "saturday_hours_source_raw", "saturday_hours_normalized"),
    ("sunday", "sunday_hours_source_raw", "sunday_hours_normalized"),
)
PARSE_ERROR_FIELDS = [
    "source_record_id", "pharmacy_name", "source_type", "day_type", "raw_hours", "normalized_hours", "error_type", "error_detail"
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def preprocess(source_rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    processed: list[dict[str, object]] = []
    segments: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    for index, source in enumerate(source_rows, start=1):
        record_id = f"pharmacy-source:{index:03d}"
        processed.append({
            **source,
            "source_record_id": record_id,
            "preprocessing_status": "processed",
        })
        for day_type, raw_key, normalized_key in DAYS:
            raw_hours = source.get(raw_key, "")
            normalized_hours = source.get(normalized_key, "")
            parsed = collector.parse_hours(normalized_hours)
            not_provided = collector.normalize_hours(normalized_hours) == "-"
            if not not_provided and (not parsed["open_time"] or not parsed["close_time"]):
                errors.append({
                    "source_record_id": record_id,
                    "pharmacy_name": source.get("pharmacy_name", ""),
                    "source_type": source.get("source_type", ""),
                    "day_type": day_type,
                    "raw_hours": raw_hours,
                    "normalized_hours": normalized_hours,
                    "error_type": "unparseable_schedule",
                    "error_detail": "Opening or closing time was not detected; source record retained.",
                })
            segments.append({
                "schedule_segment_id": f"{record_id}:{day_type}",
                "source_record_id": record_id,
                "pharmacy_name": source.get("pharmacy_name", ""),
                "source_type": source.get("source_type", ""),
                "source_url": source.get("source_url", ""),
                "source_updated_at": source.get("source_updated_at", ""),
                "day_type": day_type,
                "hours_source_raw": raw_hours,
                "hours_normalized": normalized_hours,
                "schedule_status": "not_provided" if not_provided else ("parsed" if parsed["open_time"] and parsed["close_time"] else "parse_error"),
                "open_time": parsed["open_time"],
                "close_time": parsed["close_time"],
                "closes_next_day": parsed["closes_next_day"],
                "break_note": parsed["break_note"],
            })
    return processed, segments, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=REPO_ROOT / "data/collected/pharmacy_operations/processed/pharmacy_operation_sources.csv")
    parser.add_argument("--conflicts", type=Path, default=REPO_ROOT / "data/collected/pharmacy_operations/processed/pharmacy_operation_conflicts.csv")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "data/processed/pharmacy_operations")
    args = parser.parse_args()

    source_rows = read_csv(args.input)
    processed, segments, errors = preprocess(source_rows)
    output_dir = args.output_dir
    processed_path = output_dir / "pharmacy_operation_sources_processed.csv"
    segment_path = output_dir / "pharmacy_operation_schedule_segments.csv"
    conflicts_path = output_dir / "pharmacy_operation_source_conflicts.csv"
    errors_path = output_dir / "pharmacy_operation_parse_errors.csv"
    report_path = output_dir / "preprocessing_report.json"
    write_csv(processed_path, processed)
    write_csv(segment_path, segments)
    write_csv(conflicts_path, read_csv(args.conflicts))
    write_csv(errors_path, errors, PARSE_ERROR_FIELDS)
    report = {
        "dataset": "P0-DATA-02 Wonju pharmacy operations preprocessing",
        "source_record_count": len(source_rows),
        "schedule_segment_count": len(segments),
        "parse_error_count": len(errors),
        "source_schedule_conflict_count": len(read_csv(args.conflicts)),
        "integrity_checks": {
            "all_source_records_retained": len(processed) == len(source_rows),
            "three_segments_per_source_record": len(segments) == len(source_rows) * 3,
            "parse_errors_retained": True,
        },
        "integrity_checks_passed": len(processed) == len(source_rows) and len(segments) == len(source_rows) * 3,
        "files": {
            "sources_processed": relative(processed_path),
            "schedule_segments": relative(segment_path),
            "source_conflicts": relative(conflicts_path),
            "parse_errors": relative(errors_path),
        },
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["integrity_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
