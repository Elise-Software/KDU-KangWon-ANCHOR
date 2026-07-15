"""Normalize P0-DATA-03 evidence without deciding master-record identity.

This stage deliberately keeps field coverage separate from target resolution.
Only bounded, target-owned evidence is promoted to a profile field.  Master
matching and any new institution creation are performed by the integration
stage, where the complete 2,481-row master is available.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_RESOLUTION_STATUSES = {
    "matched_existing",
    "created_new",
    "organizational_unit",
    "current_status_unknown",
    "insufficient_evidence",
    "not_present_in_collected_sources",
}
FORBIDDEN_ADDRESS_TEXT = re.compile(
    r"이용시간|주메뉴|인기검색어|사업안내|공지사항|보도자료|"
    r"모자보건|Copyright|홈페이지\s*:|콘텐츠\s*만족도"
)
DIRECT_HOMEPAGE_BY_TARGET = {
    "phc:wonju": "https://www.wonju.go.kr/health/index.do",
    "mh:wonju": "https://loveme.yonsei.kr/",
    "mh:addiction": "http://www.alja.or.kr/",
    "mh:dementia": "https://wonju.nid.or.kr/",
}

PROFILE_COLUMNS = [
    "public_health_id", "target_id", "canonical_name", "normalized_name",
    "institution_type", "parent_organization", "address", "normalized_address",
    "base_address_key", "current_status", "representative_phone", "homepage_url",
    "jurisdiction", "primary_source_url", "primary_source_updated_at",
    "latest_verified_at", "source_count", "has_conflict", "review_status",
    "entity_candidate_id", "evidence_hash", "extraction_method",
    "extraction_confidence",
]
CONTACT_COLUMNS = [
    "contact_id", "source_candidate_id", "public_health_id", "target_id",
    "organizational_unit_id", "contact_type", "contact_label", "contact_value",
    "contact_value_normalized", "department", "purpose", "availability_note",
    "source_url", "source_updated_at", "evidence_hash", "review_status",
]
SCHEDULE_COLUMNS = [
    "schedule_id", "candidate_id", "public_health_id", "target_id", "schedule_type",
    "day_type", "hours_source_raw", "hours_normalized", "open_time", "close_time",
    "closes_next_day", "break_start", "break_end", "break_note", "holiday_status",
    "reservation_required", "schedule_note", "source_url", "source_site_root",
    "source_updated_at", "evidence_text", "evidence_hash", "extraction_method",
    "parse_status", "extraction_confidence", "review_status",
]
SUPPORT_COLUMNS = [
    "supporting_source_id", "canonical_schedule_id", "canonical_candidate_id",
    "target_id", "source_url", "source_site_root", "source_updated_at",
    "hours_normalized", "evidence_text", "evidence_hash", "reason",
]
SERVICE_COLUMNS = [
    "service_id", "source_candidate_id", "public_health_id", "target_id",
    "service_name", "service_category", "target_population", "eligibility",
    "reservation_required", "application_method", "cost", "required_documents",
    "service_description", "jurisdiction", "source_url", "source_updated_at",
    "evidence_hash", "review_status",
]
UNIT_COLUMNS = [
    "organizational_unit_id", "target_id", "parent_target_id", "parent_public_health_id",
    "unit_name", "unit_type", "current_status", "representative_phone", "source_url",
    "source_updated_at", "evidence_text", "evidence_hash", "review_status",
]
SOURCE_COLUMNS = [
    "source_record_id", "candidate_id", "target_id", "record_type", "disposition",
    "public_health_id", "organizational_unit_id", "source_url", "source_updated_at",
    "evidence_hash", "disposition_reason",
]
CONFLICT_COLUMNS = [
    "conflict_id", "target_id", "conflict_scope", "field_name", "values",
    "source_candidate_ids", "resolution_status", "review_required",
]
MANUAL_COLUMNS = [
    "review_id", "target_id", "review_scope", "issue_type", "detail",
    "source_url", "evidence_hash", "review_status",
]
GAP_COLUMNS = [
    "gap_id", "target_id", "canonical_name", "public_health_id", "field_name",
    "coverage_status", "gap_reason", "source_scope", "recommended_action",
    "review_required",
]
RESOLUTION_COLUMNS = [
    "target_id", "canonical_name", "institution_type", "target_resolution_status",
    "public_health_id", "organizational_unit_id", "parent_target_id",
    "schedule_coverage_status", "resolution_reason", "review_required",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str, length: int = 20) -> str:
    return f"{prefix}:{digest('|'.join(parts))[:length]}"


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "").casefold()


def valid_address(value: str) -> bool:
    if not value:
        return True
    return (
        len(value) <= 200
        and "원주시" in value
        and bool(re.search(r"\d", value))
        and not FORBIDDEN_ADDRESS_TEXT.search(value)
    )


def as_bool(value: Any) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=Path, default=ROOT / "config/p0_data_03_target_institutions.csv")
    parser.add_argument("--entities", type=Path, default=ROOT / "data/processed/public_health/public_health_entity_candidates.csv")
    parser.add_argument("--contacts", type=Path, default=ROOT / "data/processed/public_health/public_health_contact_candidates.csv")
    parser.add_argument("--schedules", type=Path, default=ROOT / "data/processed/public_health/public_health_schedule_candidates_recovered.csv")
    parser.add_argument("--schedule-supporting-sources", type=Path, default=ROOT / "data/processed/public_health/public_health_schedule_supporting_sources.csv")
    parser.add_argument("--services", type=Path, default=ROOT / "data/processed/public_health/public_health_service_candidates.csv")
    parser.add_argument("--target-resolution", type=Path, default=ROOT / "data/processed/public_health/public_health_target_resolution.csv")
    parser.add_argument("--coverage-gaps", type=Path, default=ROOT / "data/processed/public_health/public_health_coverage_gaps.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/normalized/public_health")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    targets = read_csv(args.targets)
    target_by_id = {row["target_id"]: row for row in targets}
    target_ids = set(target_by_id)
    entities = read_csv(args.entities)
    contacts_raw = read_csv(args.contacts)
    schedules_raw = read_csv(args.schedules)
    support_raw = read_csv(args.schedule_supporting_sources)
    services_raw = read_csv(args.services)

    entities_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    contacts_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in entities:
        entities_by_target[row["target_id"]].append(row)
    for row in contacts_raw:
        contacts_by_target[row["target_id"]].append(row)

    profiles: list[dict[str, Any]] = []
    public_id_by_target: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []

    for target in targets:
        target_id = target["target_id"]
        if target_id == "mh:suicide" or not entities_by_target.get(target_id):
            continue
        target_entities = entities_by_target[target_id]
        # The extractor is expected to produce one canonical entity candidate.
        # If multiple records appear, keep the strongest one but surface any
        # actual field disagreement as a conflict.
        target_entities.sort(
            key=lambda row: (
                row.get("extraction_confidence") == "high",
                bool(row.get("address_normalized")),
                row.get("source_updated_at", ""),
            ),
            reverse=True,
        )
        entity = target_entities[0]
        addresses = sorted({row.get("address_normalized", "") for row in target_entities if row.get("address_normalized")})
        if len(addresses) > 1:
            conflicts.append({
                "conflict_id": stable_id("conflict", target_id, "address", *addresses),
                "target_id": target_id, "conflict_scope": "official_source",
                "field_name": "address", "values": " | ".join(addresses),
                "source_candidate_ids": " | ".join(row["candidate_id"] for row in target_entities),
                "resolution_status": "unresolved", "review_required": True,
            })

        representative_values = sorted({
            row["contact_value_normalized"] for row in contacts_by_target.get(target_id, [])
            if row.get("contact_type") == "representative_phone" and row.get("contact_value_normalized")
        })
        representative_phone = representative_values[0] if len(representative_values) == 1 else ""
        if len(representative_values) > 1:
            conflicts.append({
                "conflict_id": stable_id("conflict", target_id, "representative_phone", *representative_values),
                "target_id": target_id, "conflict_scope": "official_source",
                "field_name": "representative_phone", "values": " | ".join(representative_values),
                "source_candidate_ids": " | ".join(
                    row["candidate_id"] for row in contacts_by_target[target_id]
                    if row.get("contact_type") == "representative_phone"
                ),
                "resolution_status": "unresolved", "review_required": True,
            })

        address = entity.get("address_normalized", "")
        homepage = entity.get("homepage_url", "")
        if not valid_address(address):
            address = ""
        if homepage and homepage != DIRECT_HOMEPAGE_BY_TARGET.get(target_id):
            homepage = ""
        public_health_id = stable_id("public_health", target_id, length=16)
        public_id_by_target[target_id] = public_health_id
        profiles.append({
            "public_health_id": public_health_id,
            "target_id": target_id,
            "canonical_name": target["canonical_name"],
            "normalized_name": normalize_name(target["canonical_name"]),
            "institution_type": target["institution_type"],
            "parent_organization": target["expected_parent"],
            "address": address,
            "normalized_address": address,
            "base_address_key": entity.get("base_address_key", "") if address else "",
            "current_status": entity.get("current_status_normalized", "") or "unverified",
            "representative_phone": representative_phone,
            "homepage_url": homepage,
            "jurisdiction": "원주시",
            "primary_source_url": entity["source_url"],
            "primary_source_updated_at": entity.get("source_updated_at", ""),
            "latest_verified_at": entity.get("collected_at", ""),
            "source_count": len(target_entities),
            "has_conflict": any(row["target_id"] == target_id for row in conflicts),
            "review_status": "manual_review_required" if any(row["target_id"] == target_id for row in conflicts) else "verified",
            "entity_candidate_id": entity["candidate_id"],
            "evidence_hash": entity["evidence_hash"],
            "extraction_method": entity.get("extraction_method", ""),
            "extraction_confidence": entity.get("extraction_confidence", ""),
        })

    suicide_entity = (entities_by_target.get("mh:suicide") or [{}])[0]
    suicide_contacts = [
        row for row in contacts_by_target.get("mh:suicide", [])
        if row.get("contact_type") == "organizational_unit_phone"
    ]
    suicide_phone = suicide_contacts[0].get("contact_value_normalized", "") if suicide_contacts else ""
    unit_id = "unit:mh:suicide"
    units = [{
        "organizational_unit_id": unit_id,
        "target_id": "mh:suicide",
        "parent_target_id": "mh:wonju",
        "parent_public_health_id": public_id_by_target.get("mh:wonju", ""),
        "unit_name": target_by_id["mh:suicide"]["canonical_name"],
        "unit_type": "suicide_prevention_center",
        "current_status": "source_confirmed_as_organizational_unit",
        "representative_phone": suicide_phone,
        "source_url": suicide_entity.get("source_url", ""),
        "source_updated_at": suicide_entity.get("source_updated_at", ""),
        "evidence_text": suicide_entity.get("evidence_text", ""),
        "evidence_hash": suicide_entity.get("evidence_hash", ""),
        "review_status": "verified_organizational_unit",
    }]

    contacts: list[dict[str, Any]] = []
    seen_contact_keys: set[tuple[str, str, str, str]] = set()
    duplicate_contact_candidate_ids: set[str] = set()
    for row in contacts_raw:
        target_id = row["target_id"]
        is_unit = target_id == "mh:suicide"
        contact_key = (
            target_id, row.get("contact_type", ""),
            row.get("contact_value_normalized", ""), row.get("source_url", ""),
        )
        if contact_key in seen_contact_keys:
            duplicate_contact_candidate_ids.add(row["candidate_id"])
            continue
        seen_contact_keys.add(contact_key)
        contacts.append({
            "contact_id": stable_id("contact", row["candidate_id"]),
            "source_candidate_id": row["candidate_id"],
            "public_health_id": "" if is_unit else public_id_by_target.get(target_id, ""),
            "target_id": target_id,
            "organizational_unit_id": unit_id if is_unit else "",
            "contact_type": row.get("contact_type", ""),
            "contact_label": row.get("contact_label", ""),
            "contact_value": row.get("contact_value_raw", ""),
            "contact_value_normalized": row.get("contact_value_normalized", ""),
            "department": row.get("department", ""),
            "purpose": row.get("purpose", ""),
            "availability_note": row.get("availability_note", ""),
            "source_url": row.get("source_url", ""),
            "source_updated_at": row.get("source_updated_at", ""),
            "evidence_hash": row.get("evidence_hash", ""),
            "review_status": "verified",
        })

    schedules: list[dict[str, Any]] = []
    schedule_id_by_candidate: dict[str, str] = {}
    for row in schedules_raw:
        target_id = row["target_id"]
        if target_id not in public_id_by_target:
            continue
        schedule_id = stable_id("schedule", row["candidate_id"])
        schedule_id_by_candidate[row["candidate_id"]] = schedule_id
        schedules.append({
            "schedule_id": schedule_id,
            "candidate_id": row["candidate_id"],
            "public_health_id": public_id_by_target[target_id],
            "target_id": target_id,
            "schedule_type": row.get("schedule_type", ""),
            "day_type": row.get("day_type", ""),
            "hours_source_raw": row.get("hours_source_raw", row.get("hours_raw", "")),
            "hours_normalized": row.get("hours_normalized", ""),
            "open_time": row.get("open_time", ""),
            "close_time": row.get("close_time", ""),
            "closes_next_day": row.get("closes_next_day", ""),
            "break_start": row.get("break_start", ""),
            "break_end": row.get("break_end", ""),
            "break_note": row.get("break_note", ""),
            "holiday_status": row.get("holiday_status", ""),
            "reservation_required": row.get("reservation_required", ""),
            "schedule_note": row.get("schedule_note", ""),
            "source_url": row.get("source_url", ""),
            "source_site_root": row.get("source_site_root", ""),
            "source_updated_at": row.get("source_updated_at", ""),
            "evidence_text": row.get("evidence_text", ""),
            "evidence_hash": row.get("evidence_hash", ""),
            "extraction_method": row.get("extraction_method", ""),
            "parse_status": row.get("parse_status", ""),
            "extraction_confidence": row.get("extraction_confidence", ""),
            "review_status": "manual_review_required" if as_bool(row.get("review_required")) else "verified",
        })

    supporting: list[dict[str, Any]] = []
    for row in support_raw:
        canonical_candidate_id = row["canonical_candidate_id"]
        supporting.append({
            "supporting_source_id": row.get("supporting_source_id") or stable_id(
                "schedule_support", canonical_candidate_id, row.get("source_url", "")
            ),
            "canonical_schedule_id": schedule_id_by_candidate.get(canonical_candidate_id, ""),
            "canonical_candidate_id": canonical_candidate_id,
            "target_id": row.get("target_id", ""),
            "source_url": row.get("source_url", ""),
            "source_site_root": row.get("source_site_root", ""),
            "source_updated_at": row.get("source_updated_at", ""),
            "hours_normalized": row.get("hours_normalized", ""),
            "evidence_text": row.get("evidence_text", ""),
            "evidence_hash": row.get("evidence_hash") or digest(
                "|".join([canonical_candidate_id, row.get("source_url", ""), row.get("hours_normalized", "")])
            ),
            "reason": row.get("reason", "same_site_common_schedule"),
        })

    services: list[dict[str, Any]] = []
    for row in services_raw:
        target_id = row["target_id"]
        if target_id not in public_id_by_target:
            continue
        services.append({
            "service_id": stable_id("service", row["candidate_id"]),
            "source_candidate_id": row["candidate_id"],
            "public_health_id": public_id_by_target[target_id],
            "target_id": target_id,
            "service_name": row.get("service_name_normalized", row.get("service_name_raw", "")),
            "service_category": row.get("service_category", ""),
            "target_population": row.get("target_population", ""),
            "eligibility": row.get("eligibility", ""),
            "reservation_required": row.get("reservation_required", ""),
            "application_method": row.get("application_method", ""),
            "cost": row.get("cost", ""),
            "required_documents": row.get("required_documents", ""),
            "service_description": row.get("service_description", ""),
            "jurisdiction": row.get("jurisdiction", ""),
            "source_url": row.get("source_url", ""),
            "source_updated_at": row.get("source_updated_at", ""),
            "evidence_hash": row.get("evidence_hash", ""),
            "review_status": "manual_review_required" if as_bool(row.get("review_required")) else "verified",
        })

    source_records: list[dict[str, Any]] = []

    def trace(row: dict[str, str], record_type: str, disposition: str,
              public_health_id: str = "", organizational_unit_id: str = "") -> None:
        candidate_id = row["candidate_id"]
        source_records.append({
            "source_record_id": stable_id("source", record_type, candidate_id),
            "candidate_id": candidate_id,
            "target_id": row["target_id"],
            "record_type": record_type,
            "disposition": disposition,
            "public_health_id": public_health_id,
            "organizational_unit_id": organizational_unit_id,
            "source_url": row.get("source_url", ""),
            "source_updated_at": row.get("source_updated_at", ""),
            "evidence_hash": row.get("evidence_hash", ""),
            "disposition_reason": "normalized target-owned evidence",
        })

    for row in entities:
        if row["target_id"] == "mh:suicide":
            trace(row, "entity", "organizational_unit", organizational_unit_id=unit_id)
        else:
            trace(row, "entity", "normalized_profile", public_id_by_target.get(row["target_id"], ""))
    for row in contacts_raw:
        if row["target_id"] == "mh:suicide":
            trace(row, "contact", "normalized_organizational_unit_contact", organizational_unit_id=unit_id)
        elif row["candidate_id"] in duplicate_contact_candidate_ids:
            trace(row, "contact", "supporting_duplicate_contact", public_id_by_target.get(row["target_id"], ""))
        else:
            trace(row, "contact", "normalized_contact", public_id_by_target.get(row["target_id"], ""))
    for row in schedules_raw:
        trace(row, "schedule", "normalized_schedule", public_id_by_target.get(row["target_id"], ""))
    for row in services_raw:
        trace(row, "service", "normalized_service", public_id_by_target.get(row["target_id"], ""))
    for row in supporting:
        source_records.append({
            "source_record_id": stable_id("source", "schedule_support", row["supporting_source_id"]),
            "candidate_id": row["supporting_source_id"],
            "target_id": row["target_id"],
            "record_type": "schedule_support",
            "disposition": "supporting_source",
            "public_health_id": public_id_by_target.get(row["target_id"], ""),
            "organizational_unit_id": "",
            "source_url": row["source_url"],
            "source_updated_at": row["source_updated_at"],
            "evidence_hash": row["evidence_hash"],
            "disposition_reason": "same-site repeated evidence linked to canonical schedule",
        })

    schedule_targets = {row["target_id"] for row in schedules}
    gaps: list[dict[str, Any]] = []

    def add_gap(target_id: str, field_name: str, reason: str,
                status: str = "not_present_in_collected_sources") -> None:
        gaps.append({
            "gap_id": stable_id("gap", target_id, field_name),
            "target_id": target_id,
            "canonical_name": target_by_id[target_id]["canonical_name"],
            "public_health_id": public_id_by_target.get(target_id, ""),
            "field_name": field_name,
            "coverage_status": status,
            "gap_reason": reason,
            "source_scope": "approved P0-DATA-03 collected sources",
            "recommended_action": "Verify through a future approved institution-specific official source.",
            "review_required": False,
        })

    for target in targets:
        target_id = target["target_id"]
        if target_id != "mh:suicide" and target_id not in schedule_targets:
            add_gap(target_id, "operation_schedule", "No explicit institution-owned general operating hours were found.")
    for profile in profiles:
        target_id = profile["target_id"]
        if not profile["address"]:
            add_gap(target_id, "address", "No clean institution-specific address evidence was found.")
        if not profile["representative_phone"]:
            add_gap(target_id, "representative_phone", "No explicitly labelled representative phone was found.")
        if not profile["homepage_url"]:
            add_gap(target_id, "homepage_url", "No explicitly linked institution homepage was found.")
    service_targets = {row["target_id"] for row in services}
    for target in targets:
        target_id = target["target_id"]
        if target_id != "mh:suicide" and target_id not in service_targets:
            add_gap(target_id, "provided_services", "No bounded institution-level service description was found.")
    if "mh:byeoljari" not in public_id_by_target:
        for field_name in ("address", "representative_phone", "homepage_url", "current_status"):
            add_gap(
                "mh:byeoljari", field_name,
                "The facility was not present in the approved current collected sources.",
            )

    resolutions: list[dict[str, Any]] = []
    for target in targets:
        target_id = target["target_id"]
        if target_id == "mh:suicide":
            status = "organizational_unit"
            reason = "No independent address or schedule; explicit unit phone is shown under the parent center."
        elif target_id in public_id_by_target:
            status = "insufficient_evidence"
            reason = "Entity evidence normalized; master identity resolution is deferred to integration."
        else:
            status = "not_present_in_collected_sources"
            reason = "No current entity evidence was found in the approved collected sources."
        resolutions.append({
            "target_id": target_id,
            "canonical_name": target["canonical_name"],
            "institution_type": target["institution_type"],
            "target_resolution_status": status,
            "public_health_id": public_id_by_target.get(target_id, ""),
            "organizational_unit_id": unit_id if target_id == "mh:suicide" else "",
            "parent_target_id": "mh:wonju" if target_id == "mh:suicide" else "",
            "schedule_coverage_status": (
                "inherited_from_parent" if target_id == "mh:suicide"
                else "canonical_available" if target_id in schedule_targets
                else "not_present_in_collected_sources"
            ),
            "resolution_reason": reason,
            "review_required": False,
        })

    primary_candidate_ids = {
        row["candidate_id"] for row in entities + contacts_raw + schedules_raw + services_raw
    }
    traced_primary_ids = {
        row["candidate_id"] for row in source_records
        if row["record_type"] in {"entity", "contact", "schedule", "service"}
    }
    profile_public_ids = {row["public_health_id"] for row in profiles}
    schedule_keys = [
        (row["target_id"], row["schedule_type"], row["day_type"],
         row["hours_normalized"], row["source_site_root"])
        for row in schedules
    ]
    support_fk_errors = sum(
        not row["canonical_schedule_id"] or row["canonical_schedule_id"] not in {s["schedule_id"] for s in schedules}
        for row in supporting
    )
    internal_fk_errors = (
        sum(bool(row["public_health_id"]) and row["public_health_id"] not in profile_public_ids for row in contacts)
        + sum(row["public_health_id"] not in profile_public_ids for row in schedules)
        + sum(row["public_health_id"] not in profile_public_ids for row in services)
        + sum(row["parent_public_health_id"] not in profile_public_ids for row in units)
        + support_fk_errors
    )
    polluted_field_count = sum(not valid_address(row["address"]) for row in profiles)
    invalid_homepage_count = sum(
        bool(row["homepage_url"]) and row["homepage_url"] != DIRECT_HOMEPAGE_BY_TARGET.get(row["target_id"])
        for row in profiles
    )
    resolution_statuses = {row["target_resolution_status"] for row in resolutions}
    candidate_trace_rate = len(traced_primary_ids & primary_candidate_ids) / len(primary_candidate_ids) if primary_candidate_ids else 1.0
    integrity_checks = {
        "target_set_is_exact_and_unique": len(targets) == 26 and len(target_ids) == 26,
        "resolution_set_matches_targets": len(resolutions) == 26 and {row["target_id"] for row in resolutions} == target_ids,
        "resolution_statuses_are_allowed": resolution_statuses <= ALLOWED_RESOLUTION_STATUSES,
        "entity_candidate_ids_unique": len(entities) == len({row["candidate_id"] for row in entities}),
        "contact_candidate_ids_unique": len(contacts_raw) == len({row["candidate_id"] for row in contacts_raw}),
        "service_candidate_ids_unique": len(services_raw) == len({row["candidate_id"] for row in services_raw}),
        "candidate_trace_complete": primary_candidate_ids == traced_primary_ids,
        "unexplained_dropped_candidates_absent": primary_candidate_ids <= traced_primary_ids,
        "polluted_profile_fields_absent": polluted_field_count == 0 and invalid_homepage_count == 0,
        "canonical_schedule_keys_unique": len(schedule_keys) == len(set(schedule_keys)),
        "supporting_source_foreign_keys_valid": support_fk_errors == 0,
        "internal_foreign_keys_valid": internal_fk_errors == 0,
        "suicide_is_only_organizational_unit": (
            len(units) == 1
            and not any(row["target_id"] == "mh:suicide" for row in profiles)
            and not any(row["target_id"] == "mh:suicide" for row in schedules)
        ),
        "dementia_program_not_general_operation": not any(
            row["target_id"] == "mh:dementia" and row["schedule_type"] == "general_operation"
            for row in schedules
        ),
        "coverage_gaps_are_not_manual_review": not any(as_bool(row["review_required"]) for row in gaps),
    }
    report = {
        "target_count": len(targets),
        "normalized_institution_count": len(profiles),
        "organizational_unit_count": len(units),
        "contact_count": len(contacts),
        "canonical_schedule_count": len(schedules),
        "schedule_supporting_source_count": len(supporting),
        "service_count": len(services),
        "source_record_count": len(source_records),
        "coverage_gap_count": len(gaps),
        "manual_review_count": len(manual_review),
        "conflict_count": len(conflicts),
        "candidate_count": len(primary_candidate_ids),
        "traced_candidate_count": len(traced_primary_ids & primary_candidate_ids),
        "candidate_trace_rate": candidate_trace_rate,
        "unexplained_dropped_candidate_count": len(primary_candidate_ids - traced_primary_ids),
        "polluted_field_count": polluted_field_count,
        "invalid_homepage_count": invalid_homepage_count,
        "foreign_key_error_count": internal_fk_errors,
        "integrity_checks": integrity_checks,
    }
    report["integrity_checks_passed"] = all(integrity_checks.values())
    report["dataset_status"] = "conditionally_verified" if report["integrity_checks_passed"] else "failed"

    out = args.output_dir
    write_csv(out / "public_health_institutions.csv", profiles, PROFILE_COLUMNS)
    write_csv(out / "public_health_contacts.csv", contacts, CONTACT_COLUMNS)
    write_csv(out / "public_health_schedules.csv", schedules, SCHEDULE_COLUMNS)
    write_csv(out / "public_health_schedule_supporting_sources.csv", supporting, SUPPORT_COLUMNS)
    write_csv(out / "public_health_services.csv", services, SERVICE_COLUMNS)
    write_csv(out / "public_health_organizational_units.csv", units, UNIT_COLUMNS)
    write_csv(out / "public_health_source_records.csv", source_records, SOURCE_COLUMNS)
    write_csv(out / "public_health_conflicts.csv", conflicts, CONFLICT_COLUMNS)
    write_csv(out / "public_health_manual_review.csv", manual_review, MANUAL_COLUMNS)
    write_csv(out / "public_health_coverage_gaps.csv", gaps, GAP_COLUMNS)
    write_csv(out / "public_health_target_resolution.csv", resolutions, RESOLUTION_COLUMNS)
    (out / "normalization_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["integrity_checks_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
