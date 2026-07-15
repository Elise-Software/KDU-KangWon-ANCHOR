"""Audit target representation and candidate lineage for final P0-DATA-03 outputs."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
COLUMNS = [
    "target_id", "canonical_name", "institution_type", "entity_candidate_count",
    "normalized_profile_count", "integrated_profile_count", "organizational_unit_count",
    "target_resolution_status", "institution_id", "trace_status", "drop_reason",
    "address_status", "phone_status", "homepage_status", "schedule_status",
    "review_status",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=Path, default=ROOT / "config/p0_data_03_target_institutions.csv")
    parser.add_argument("--entity-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_entity_candidates.csv")
    parser.add_argument("--contact-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_contact_candidates.csv")
    parser.add_argument("--schedule-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_schedule_candidates_recovered.csv")
    parser.add_argument("--service-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_service_candidates.csv")
    parser.add_argument("--normalized-profiles", type=Path, default=ROOT / "data/normalized/public_health/public_health_institutions.csv")
    parser.add_argument("--profiles", type=Path, default=ROOT / "data/integrated/wonju/institution_public_health_profiles.csv")
    parser.add_argument("--units", type=Path, default=ROOT / "data/integrated/wonju/institution_organizational_units.csv")
    parser.add_argument("--resolutions", type=Path, default=ROOT / "data/integrated/wonju/public_health_target_resolution.csv")
    parser.add_argument("--source-records", type=Path, default=ROOT / "data/integrated/wonju/public_health_source_records.csv")
    parser.add_argument("--gaps", type=Path, default=ROOT / "data/integrated/wonju/public_health_coverage_gaps.csv")
    parser.add_argument("--manual-review", type=Path, default=ROOT / "data/integrated/wonju/public_health_manual_review.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/processed/public_health")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    targets = read_csv(args.targets)
    entity_candidates = read_csv(args.entity_candidates)
    contact_candidates = read_csv(args.contact_candidates)
    schedule_candidates = read_csv(args.schedule_candidates)
    service_candidates = read_csv(args.service_candidates)
    normalized_profiles = read_csv(args.normalized_profiles)
    profiles = read_csv(args.profiles)
    units = read_csv(args.units)
    resolutions = read_csv(args.resolutions)
    source_records = read_csv(args.source_records)
    gaps = read_csv(args.gaps)
    manual = read_csv(args.manual_review)

    target_ids = {row["target_id"] for row in targets}
    resolution_by_target = {row["target_id"]: row for row in resolutions}
    profile_by_target = {row["target_id"]: row for row in profiles}
    entity_counts = Counter(row["target_id"] for row in entity_candidates)
    normalized_counts = Counter(row["target_id"] for row in normalized_profiles)
    integrated_counts = Counter(row["target_id"] for row in profiles)
    unit_counts = Counter(row["target_id"] for row in units)
    review_targets = {row["target_id"] for row in manual}
    gap_fields = {(row["target_id"], row["field_name"]) for row in gaps}

    rows: list[dict[str, Any]] = []
    for target in targets:
        target_id = target["target_id"]
        resolution = resolution_by_target.get(target_id, {})
        profile = profile_by_target.get(target_id, {})
        represented = integrated_counts[target_id] == 1 or unit_counts[target_id] == 1
        rows.append({
            "target_id": target_id,
            "canonical_name": target["canonical_name"],
            "institution_type": target["institution_type"],
            "entity_candidate_count": entity_counts[target_id],
            "normalized_profile_count": normalized_counts[target_id],
            "integrated_profile_count": integrated_counts[target_id],
            "organizational_unit_count": unit_counts[target_id],
            "target_resolution_status": resolution.get("target_resolution_status", ""),
            "institution_id": resolution.get("institution_id", ""),
            "trace_status": "resolved" if represented else "unresolved",
            "drop_reason": "" if represented else resolution.get("resolution_reason", "missing final resolution"),
            "address_status": "present" if profile.get("address") else "coverage_gap" if (target_id, "address") in gap_fields else "not_applicable",
            "phone_status": "present" if profile.get("representative_phone") else "coverage_gap" if (target_id, "representative_phone") in gap_fields else "not_applicable",
            "homepage_status": "present" if profile.get("homepage_url") else "coverage_gap" if (target_id, "homepage_url") in gap_fields else "not_applicable",
            "schedule_status": resolution.get("schedule_coverage_status", ""),
            "review_status": "manual_review_required" if target_id in review_targets else "verified",
        })

    primary_candidate_ids = {
        row["candidate_id"]
        for row in entity_candidates + contact_candidates + schedule_candidates + service_candidates
    }
    traced_candidate_ids = {
        row["candidate_id"] for row in source_records
        if row["record_type"] in {"entity", "contact", "schedule", "service"}
    }
    trace_rate = len(primary_candidate_ids & traced_candidate_ids) / len(primary_candidate_ids) if primary_candidate_ids else 1.0
    checks = {
        "target_count_is_26": len(targets) == len(target_ids) == 26,
        "resolution_set_matches_targets": len(resolutions) == 26 and set(resolution_by_target) == target_ids,
        "all_targets_represented": all(row["trace_status"] == "resolved" for row in rows),
        "candidate_trace_complete": primary_candidate_ids == traced_candidate_ids,
        "unexplained_dropped_candidates_absent": not (primary_candidate_ids - traced_candidate_ids),
    }
    report = {
        "target_count": len(targets),
        "represented_target_count": sum(row["trace_status"] == "resolved" for row in rows),
        "unrepresented_target_count": sum(row["trace_status"] != "resolved" for row in rows),
        "entity_candidate_count": len(entity_candidates),
        "normalized_profile_count": len(normalized_profiles),
        "integrated_profile_count": len(profiles),
        "organizational_unit_count": len(units),
        "candidate_count": len(primary_candidate_ids),
        "traced_candidate_count": len(primary_candidate_ids & traced_candidate_ids),
        "candidate_trace_rate": trace_rate,
        "unexplained_dropped_candidate_count": len(primary_candidate_ids - traced_candidate_ids),
        "integrity_checks": checks,
        "integrity_checks_passed": all(checks.values()),
    }
    write_csv(args.output_dir / "public_health_profile_gap_audit.csv", rows)
    (args.output_dir / "public_health_profile_gap_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["integrity_checks_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
