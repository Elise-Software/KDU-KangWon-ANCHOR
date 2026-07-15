"""Attach verified Wonju pharmacy-operation source data to the institution master."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def true(value: str | bool) -> bool:
    return str(value).strip().lower() == "true"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", type=Path, default=REPO_ROOT / "data/integrated/wonju/institutions.csv")
    parser.add_argument("--sources", type=Path, default=REPO_ROOT / "data/processed/pharmacy_operations/pharmacy_operation_sources_processed.csv")
    parser.add_argument("--segments", type=Path, default=REPO_ROOT / "data/processed/pharmacy_operations/pharmacy_operation_schedule_segments.csv")
    parser.add_argument("--source-conflicts", type=Path, default=REPO_ROOT / "data/processed/pharmacy_operations/pharmacy_operation_source_conflicts.csv")
    parser.add_argument("--master-matches", type=Path, default=REPO_ROOT / "data/collected/pharmacy_operations/processed/master_matches.csv")
    parser.add_argument("--master-conflicts", type=Path, default=REPO_ROOT / "data/collected/pharmacy_operations/processed/master_conflicts.csv")
    parser.add_argument("--review-decisions", type=Path, default=REPO_ROOT / "config/pharmacy_operation_review_decisions.csv")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "data/integrated/wonju")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    master = read_csv(args.master)
    source_rows = read_csv(args.sources)
    segments = read_csv(args.segments)
    source_conflicts = read_csv(args.source_conflicts)
    master_matches = read_csv(args.master_matches)
    master_conflicts = read_csv(args.master_conflicts)
    review_decisions = read_csv(args.review_decisions)
    output_dir = args.output_dir

    match_by_name = {row["pharmacy_name"]: row for row in master_matches}
    source_by_id = {row["source_record_id"]: row for row in source_rows}
    source_names_by_institution: dict[str, list[dict[str, str]]] = defaultdict(list)
    for source in source_rows:
        match = match_by_name.get(source["pharmacy_name"])
        if match:
            source_names_by_institution[match["master_institution_id"]].append(source)

    source_conflict_names = {row["pharmacy_name"] for row in source_conflicts}
    master_conflict_names = {row["pharmacy_name"] for row in master_conflicts}
    conflict_names = source_conflict_names | master_conflict_names
    decision_by_name = {row["pharmacy_name"]: row for row in review_decisions}
    if set(decision_by_name) != conflict_names:
        raise RuntimeError("Review decisions must cover exactly the pharmacy records with retained conflicts.")
    source_count_by_institution = {key: len(value) for key, value in source_names_by_institution.items()}

    enriched: list[dict[str, object]] = []
    for institution in master:
        records = source_names_by_institution.get(institution["institution_id"], [])
        names = {row["pharmacy_name"] for row in records}
        pharmacy_name = next(iter(names), "")
        has_conflict = bool(names & conflict_names)
        matched = bool(records)
        enriched.append({
            **institution,
            "is_late_night_pharmacy": any(row["source_type"] == "late_night" for row in records),
            "is_year_round_pharmacy": any(row["source_type"] == "year_round" for row in records),
            "is_public_late_night_pharmacy": any(true(row.get("is_public_late_night", "")) for row in records),
            "pharmacy_operation_source_count": source_count_by_institution.get(institution["institution_id"], 0),
            "pharmacy_operation_has_conflict": has_conflict,
            "pharmacy_operation_review_status": (
                "decision_recorded" if has_conflict else ("auto_matched" if matched else "not_applicable")
            ),
            "pharmacy_operation_latest_source_updated_at": max((row["source_updated_at"] for row in records), default=""),
        })

    operation_rows: list[dict[str, object]] = []
    source_enriched: list[dict[str, object]] = []
    for source in source_rows:
        match = match_by_name.get(source["pharmacy_name"])
        if not match:
            continue
        source_enriched.append({
            **source,
            "institution_id": match["master_institution_id"],
            "match_status": match["match_status"],
            "resolution": match["resolution"],
            "institution_match_status": decision_by_name.get(source["pharmacy_name"], {}).get("institution_match_status", "auto_matched"),
            "review_required": source["pharmacy_name"] in conflict_names,
        })
    for segment in segments:
        source = source_by_id.get(segment["source_record_id"])
        match = match_by_name.get(source["pharmacy_name"]) if source else None
        if match:
            decision = decision_by_name.get(segment["pharmacy_name"], {})
            operation_rows.append({
                **segment,
                "institution_id": match["master_institution_id"],
                "institution_match_status": decision.get("institution_match_status", "auto_matched"),
                "effective_schedule_status": decision.get("effective_status", "source_values"),
                "review_required": segment["pharmacy_name"] in conflict_names,
            })

    combined_conflicts: list[dict[str, object]] = []
    for row in source_conflicts:
        match = match_by_name.get(row["pharmacy_name"], {})
        combined_conflicts.append({**row, "conflict_scope": "source_schedule", "institution_id": match.get("master_institution_id", ""), "review_required": True})
    for row in master_conflicts:
        combined_conflicts.append({**row, "conflict_scope": "master_identity", "institution_id": row["master_institution_id"], "review_required": True})

    manual_reviews: list[dict[str, object]] = []
    for pharmacy_name in sorted(conflict_names):
        match = match_by_name[pharmacy_name]
        decision = decision_by_name[pharmacy_name]
        related = [row for row in combined_conflicts if row["pharmacy_name"] == pharmacy_name]
        scopes = sorted({row["conflict_scope"] for row in related})
        manual_reviews.append({
            "pharmacy_name": pharmacy_name,
            "institution_id": match["master_institution_id"],
            "review_status": "decision_recorded",
            "institution_match_status": decision["institution_match_status"],
            "review_scope": decision["review_scope"],
            "resolution": decision["resolution"],
            "effective_status": decision["effective_status"],
            "conflict_scopes": ";".join(scopes),
            "conflict_field_count": len(related),
            "source_address": match["source_address"],
            "source_phone": match["source_phone"],
            "master_address": match["master_address"],
            "master_phone": match["master_phone"],
            "phone_status": (
                "unresolved_official_conflict"
                if decision["review_scope"] == "master_identity"
                else "not_applicable"
            ),
            "user_response_policy": (
                "Do not select one phone number. Present source and master phone numbers, then advise confirmation before visiting."
                if decision["review_scope"] == "master_identity"
                else "Present the institution normally; preserve both source schedules and advise confirmation with the pharmacy, E-GEN, or 119 before visiting."
            ),
        })

    enriched_path = output_dir / "institutions_pharmacy_enriched.csv"
    operations_path = output_dir / "institution_pharmacy_operations.csv"
    sources_path = output_dir / "institution_pharmacy_operation_sources.csv"
    conflicts_path = output_dir / "pharmacy_operation_conflicts.csv"
    reviews_path = output_dir / "pharmacy_operation_manual_review.csv"
    report_path = output_dir / "pharmacy_integration_report.json"
    write_csv(enriched_path, enriched)
    write_csv(operations_path, operation_rows)
    write_csv(sources_path, source_enriched)
    write_csv(conflicts_path, combined_conflicts)
    write_csv(reviews_path, manual_reviews)

    master_ids = {row["institution_id"] for row in master}
    foreign_key_errors = [row for row in operation_rows if row["institution_id"] not in master_ids]
    source_conflict_pharmacies = {row["pharmacy_name"] for row in source_conflicts}
    report = {
        "dataset": "P0-DATA-02 Wonju pharmacy operations integration",
        "master_institution_count_before": len(master),
        "master_institution_count_after": len(enriched),
        "source_record_count": len(source_rows),
        "unique_pharmacy_count": len(match_by_name),
        "schedule_segment_count": len(operation_rows),
        "institution_auto_match_count": len(master_matches),
        "institution_unmatched_count": len(source_rows) - len(source_enriched),
        "source_schedule_conflicts": {"pharmacy_count": len(source_conflict_pharmacies), "field_count": len(source_conflicts)},
        "master_identity_conflicts": {"pharmacy_count": len(master_conflict_names), "field_count": len(master_conflicts)},
        "master_address_variants": {"pharmacy_count": sum(true(row["address_detail_difference"]) for row in master_matches), "review_required_count": 0},
        "parse_loss_count": 0,
        "foreign_key_error_count": len(foreign_key_errors),
        "manual_review_count": len(manual_reviews),
        "review_decision_count": len(review_decisions),
        "integrity_checks": {
            "master_count_unchanged": len(master) == len(enriched),
            "all_sources_matched": len(source_rows) == len(source_enriched),
            "three_segments_per_source": len(operation_rows) == len(source_rows) * 3,
            "no_foreign_key_errors": not foreign_key_errors,
            "all_conflicts_have_review_decisions": len(review_decisions) == len(conflict_names),
        },
        "dataset_status": "conditionally_verified",
        "files": {
            "enriched_institutions": relative(enriched_path),
            "operations": relative(operations_path),
            "operation_sources": relative(sources_path),
            "conflicts": relative(conflicts_path),
            "manual_review": relative(reviews_path),
            "review_decisions": relative(args.review_decisions),
        },
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    report["integrity_checks_passed"] = all(report["integrity_checks"].values())
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (report["integrity_checks_passed"] or not args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
