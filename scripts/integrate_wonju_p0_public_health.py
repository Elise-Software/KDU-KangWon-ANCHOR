"""Non-destructively integrate normalized P0-DATA-03 profiles into the master."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_RESOLUTION_STATUSES = {
    "matched_existing",
    "created_new",
    "organizational_unit",
    "current_status_unknown",
    "insufficient_evidence",
    "not_present_in_collected_sources",
}
ALLOWED_AUTOMATIC_MATCH_METHODS = {
    "name_phone",
    "name_base_address",
    "official_alias_base_address",
    "phone_base_address",
}
ALLOWED_MANUAL_ISSUES = {
    "identity_conflict",
    "official_phone_conflict",
    "official_address_conflict",
    "organizational_structure_unclear",
    "current_operating_status_unclear",
}
OFFICIAL_HOSTS = {
    "www.wonju.go.kr", "wonju.nid.or.kr", "loveme.yonsei.kr", "www.alja.or.kr",
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
OFFICIAL_ALIASES = {
    "phc:sanhyeon": {"산현보건지료소"},  # typo on the official institution-list page
}
BYEOLJARI_TARGET_ID = "mh:byeoljari"
BYEOLJARI_MASTER_ID = "public:c8ed3721eb29eca1"

PROFILE_COLUMNS = [
    "institution_id", "public_health_id", "target_id", "canonical_name",
    "institution_type", "target_resolution_status", "current_status", "address",
    "representative_phone", "homepage_url", "jurisdiction", "matched_by",
    "match_confidence", "match_evidence", "match_source_url", "match_evidence_hash",
    "source_url", "source_reference", "source_updated_at",
    "evidence_hash", "source_candidate_id", "has_conflict", "review_status",
]
CONTACT_COLUMNS = [
    "contact_id", "source_candidate_id", "institution_id", "public_health_id",
    "target_id", "organizational_unit_id", "contact_type", "contact_label",
    "contact_value", "contact_value_normalized", "department", "purpose",
    "availability_note", "source_url", "source_updated_at", "evidence_hash",
    "review_status",
]
SCHEDULE_COLUMNS = [
    "schedule_id", "candidate_id", "institution_id", "organizational_unit_id",
    "public_health_id", "target_id", "schedule_type", "day_type",
    "hours_source_raw", "hours_normalized", "open_time", "close_time",
    "closes_next_day", "break_start", "break_end", "break_note", "holiday_status",
    "reservation_required", "schedule_note", "source_url", "source_site_root",
    "source_updated_at", "evidence_text", "evidence_hash", "extraction_method",
    "parse_status", "extraction_confidence", "review_status",
]
SUPPORT_COLUMNS = [
    "supporting_source_id", "canonical_schedule_id", "canonical_candidate_id",
    "institution_id", "target_id", "source_url", "source_site_root",
    "source_updated_at", "hours_normalized", "evidence_text", "evidence_hash",
    "reason",
]
SERVICE_COLUMNS = [
    "service_id", "source_candidate_id", "institution_id", "public_health_id",
    "target_id", "service_name", "service_category", "target_population",
    "eligibility", "reservation_required", "application_method", "cost",
    "required_documents", "service_description", "jurisdiction", "source_url",
    "source_updated_at", "evidence_hash", "review_status",
]
UNIT_COLUMNS = [
    "organizational_unit_id", "target_id", "parent_target_id", "parent_institution_id",
    "parent_public_health_id", "unit_name", "unit_type", "current_status",
    "representative_phone", "source_url", "source_updated_at", "evidence_text",
    "evidence_hash", "review_status", "direct_schedule_count", "inherited_schedule",
]
SOURCE_COLUMNS = [
    "source_record_id", "candidate_id", "target_id", "record_type", "disposition",
    "institution_id", "organizational_unit_id", "source_url", "source_reference", "source_updated_at",
    "evidence_hash", "disposition_reason",
]
CONFLICT_COLUMNS = [
    "conflict_id", "target_id", "institution_id", "conflict_scope", "field_name",
    "master_value", "source_value", "source_url", "evidence_hash",
    "resolution_status", "review_required",
]
MANUAL_COLUMNS = [
    "review_id", "target_id", "institution_id", "review_scope", "issue_type",
    "field_name", "master_value", "source_value", "detail", "source_url",
    "source_reference", "evidence_hash", "review_status",
]
GAP_COLUMNS = [
    "gap_id", "target_id", "canonical_name", "institution_id", "public_health_id",
    "field_name", "coverage_status", "gap_reason", "source_scope",
    "recommended_action", "review_required",
]
RESOLUTION_COLUMNS = [
    "target_id", "canonical_name", "institution_type", "target_resolution_status",
    "institution_id", "organizational_unit_id", "parent_target_id",
    "parent_institution_id", "matched_by", "match_confidence", "match_source_url",
    "match_evidence_hash", "source_url", "source_reference", "evidence_hash",
    "current_status", "resolution_reason", "review_required",
    "schedule_coverage_status",
]
AUDIT_COLUMNS = [
    "target_id", "canonical_name", "institution_type", "entity_candidate_count",
    "normalized_profile_count", "integrated_profile_count", "organizational_unit_count",
    "target_resolution_status", "institution_id", "trace_status", "drop_reason",
    "address_status", "phone_status", "homepage_status", "schedule_status",
    "review_status",
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


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def base_address_key(value: str) -> str:
    text = re.sub(r"\([^)]*\)", "", value or "")
    matches = re.findall(r"([0-9A-Za-z가-힣]+(?:로|길))\s*(\d+(?:-\d+)?)", text)
    return "".join(matches[-1]).casefold() if matches else ""


def valid_address(value: str) -> bool:
    if not value:
        return True
    return (
        len(value) <= 200
        and "원주시" in value
        and bool(re.search(r"\d", value))
        and not FORBIDDEN_ADDRESS_TEXT.search(value)
    )


def valid_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value or ""))


def as_bool(value: Any) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def official_source(url: str) -> bool:
    return urlparse(url).hostname in OFFICIAL_HOSTS


def relative_to_repo(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", type=Path, default=ROOT / "config/p0_data_03_target_institutions.csv")
    parser.add_argument("--institutions", type=Path, default=ROOT / "data/integrated/wonju/institutions_pharmacy_enriched.csv")
    parser.add_argument("--institution-source-records", type=Path, default=ROOT / "data/integrated/wonju/institution_source_records.csv")
    parser.add_argument("--normalization-report", type=Path, default=ROOT / "data/normalized/public_health/normalization_report.json")
    parser.add_argument("--public-health-institutions", type=Path, default=ROOT / "data/normalized/public_health/public_health_institutions.csv")
    parser.add_argument("--contacts", type=Path, default=ROOT / "data/normalized/public_health/public_health_contacts.csv")
    parser.add_argument("--schedules", type=Path, default=ROOT / "data/normalized/public_health/public_health_schedules.csv")
    parser.add_argument("--schedule-supporting-sources", type=Path, default=ROOT / "data/normalized/public_health/public_health_schedule_supporting_sources.csv")
    parser.add_argument("--services", type=Path, default=ROOT / "data/normalized/public_health/public_health_services.csv")
    parser.add_argument("--organizational-units", type=Path, default=ROOT / "data/normalized/public_health/public_health_organizational_units.csv")
    parser.add_argument("--source-records", type=Path, default=ROOT / "data/normalized/public_health/public_health_source_records.csv")
    parser.add_argument("--conflicts", type=Path, default=ROOT / "data/normalized/public_health/public_health_conflicts.csv")
    parser.add_argument("--manual-review", type=Path, default=ROOT / "data/normalized/public_health/public_health_manual_review.csv")
    parser.add_argument("--coverage-gaps", type=Path, default=ROOT / "data/normalized/public_health/public_health_coverage_gaps.csv")
    parser.add_argument("--target-resolution", type=Path, default=ROOT / "data/normalized/public_health/public_health_target_resolution.csv")
    parser.add_argument("--entity-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_entity_candidates.csv")
    parser.add_argument("--contact-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_contact_candidates.csv")
    parser.add_argument("--schedule-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_schedule_candidates_recovered.csv")
    parser.add_argument("--service-candidates", type=Path, default=ROOT / "data/processed/public_health/public_health_service_candidates.csv")
    parser.add_argument("--resolution-decisions", type=Path, default=ROOT / "config/p0_data_03_resolution_decisions.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/integrated/wonju")
    parser.add_argument("--audit-output-dir", type=Path, default=ROOT / "data/processed/public_health")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    normalization_report = json.loads(args.normalization_report.read_text(encoding="utf-8"))
    if not normalization_report.get("integrity_checks_passed"):
        raise RuntimeError("P0-DATA-03 normalization did not pass")

    targets = read_csv(args.targets)
    target_by_id = {row["target_id"]: row for row in targets}
    target_ids = set(target_by_id)
    master = read_csv(args.institutions)
    original_columns = list(master[0]) if master else []
    original_by_id = {row["institution_id"]: row for row in master}
    normalized_profiles = read_csv(args.public_health_institutions)
    contacts_in = read_csv(args.contacts)
    schedules_in = read_csv(args.schedules)
    support_in = read_csv(args.schedule_supporting_sources)
    services_in = read_csv(args.services)
    units_in = read_csv(args.organizational_units)
    source_records_in = read_csv(args.source_records)
    normalized_conflicts = read_csv(args.conflicts)
    normalized_manual = read_csv(args.manual_review)
    gaps_in = read_csv(args.coverage_gaps)
    entity_candidates = read_csv(args.entity_candidates)
    contact_candidates = read_csv(args.contact_candidates)
    schedule_candidates = read_csv(args.schedule_candidates)
    service_candidates = read_csv(args.service_candidates)
    master_source_records = read_csv(args.institution_source_records)
    resolution_decisions = read_csv(args.resolution_decisions) if args.resolution_decisions.is_file() else []
    decisions_by_target_field = {
        (row["target_id"], row["field_name"]): row for row in resolution_decisions
    }
    if len(decisions_by_target_field) != len(resolution_decisions):
        raise RuntimeError("P0-DATA-03 resolution decisions contain duplicate target/field rows")

    contact_phones_by_target: dict[str, set[str]] = defaultdict(set)
    contact_evidence_by_target_phone: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in contacts_in:
        if row.get("contact_type") not in {"national_hotline", "organizational_unit_phone"}:
            phone = normalize_phone(row.get("contact_value_normalized", ""))
            if phone:
                contact_phones_by_target[row["target_id"]].add(phone)
                contact_evidence_by_target_phone[(row["target_id"], phone)].append(row)

    master_facts: dict[str, dict[str, str]] = {}
    for row in master:
        master_facts[row["institution_id"]] = {
            "name": normalize_name(row.get("name", "")),
            "address": base_address_key(row.get("normalized_address") or row.get("address", "")),
            "phone": normalize_phone(row.get("phone", "")),
        }

    def find_allowed_match(
        profile: dict[str, str],
    ) -> tuple[dict[str, str] | None, str, str, str, str]:
        target_id = profile["target_id"]
        wanted_name = normalize_name(profile["canonical_name"])
        wanted_address = profile.get("base_address_key") or base_address_key(profile.get("address", ""))
        phones = contact_phones_by_target.get(target_id, set())
        aliases = {normalize_name(value) for value in OFFICIAL_ALIASES.get(target_id, set())}
        matches: dict[str, set[str]] = defaultdict(set)
        for row in master:
            facts = master_facts[row["institution_id"]]
            name_equal = bool(wanted_name and facts["name"] == wanted_name)
            alias_equal = bool(aliases and facts["name"] in aliases)
            address_equal = bool(wanted_address and facts["address"] == wanted_address)
            phone_equal = bool(facts["phone"] and facts["phone"] in phones)
            if name_equal and phone_equal:
                matches[row["institution_id"]].add("name_phone")
            if name_equal and address_equal:
                matches[row["institution_id"]].add("name_base_address")
            if alias_equal and address_equal:
                matches[row["institution_id"]].add("official_alias_base_address")
            if phone_equal and address_equal:
                matches[row["institution_id"]].add("phone_base_address")
        if len(matches) != 1:
            return None, "", "", "", ""
        institution_id, methods = next(iter(matches.items()))
        # Prefer address evidence when both address and phone independently
        # identify the same record; this keeps the matching source aligned with
        # the entity profile.  Phone-only identity linkage remains available for
        # the Sillim address-conflict case.
        priority = ["name_base_address", "official_alias_base_address", "name_phone", "phone_base_address"]
        method = next(item for item in priority if item in methods)
        matching_contact: dict[str, str] = {}
        if method in {"name_phone", "phone_base_address"}:
            matching_rows = contact_evidence_by_target_phone.get(
                (target_id, master_facts[institution_id]["phone"]), []
            )
            matching_contact = matching_rows[0] if matching_rows else {}
        evidence = {
            "source_name": profile["canonical_name"],
            "source_base_address": wanted_address,
            "source_phones": sorted(phones),
            "master_name": original_by_id[institution_id].get("name", ""),
            "master_base_address": master_facts[institution_id]["address"],
            "master_phone": original_by_id[institution_id].get("phone", ""),
            "matching_contact_id": matching_contact.get("contact_id", ""),
            "matching_contact_source_url": matching_contact.get("source_url", ""),
            "matching_contact_evidence_hash": matching_contact.get("evidence_hash", ""),
        }
        match_source_url = (
            matching_contact.get("source_url", "")
            if method in {"name_phone", "phone_base_address"}
            else profile.get("primary_source_url", "")
        )
        match_evidence_hash = (
            matching_contact.get("evidence_hash", "")
            if method in {"name_phone", "phone_base_address"}
            else profile.get("evidence_hash", "")
        )
        return (
            original_by_id[institution_id], method,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            match_source_url, match_evidence_hash,
        )

    mapping_by_public_id: dict[str, str] = {}
    mapping_by_target: dict[str, str] = {}
    resolution_meta: dict[str, dict[str, Any]] = {}
    created_rows: list[dict[str, Any]] = []
    unsupported_new_targets: set[str] = set()

    category_by_type = {
        "public_health_center": "보건소",
        "public_health_branch": "보건지소",
        "public_health_clinic": "보건진료소",
        "health_life_support_center": "건강생활지원센터",
        "mental_health_welfare_center": "정신건강복지센터",
        "addiction_management_center": "중독관리통합지원센터",
        "dementia_safety_center": "치매안심센터",
        "mental_rehabilitation_facility": "정신재활시설",
    }

    for profile in normalized_profiles:
        target_id = profile["target_id"]
        matched, method, evidence, match_source_url, match_evidence_hash = find_allowed_match(profile)
        if matched:
            institution_id = matched["institution_id"]
            status = "matched_existing"
            confidence = "1.0000"
            reason = f"Unique existing institution matched by {method}."
        else:
            valid_new = (
                target_id in target_ids
                and profile["canonical_name"] == target_by_id[target_id]["canonical_name"]
                and valid_address(profile.get("address", ""))
                and bool(profile.get("address"))
                and official_source(profile.get("primary_source_url", ""))
                and valid_sha256(profile.get("evidence_hash", ""))
                and profile.get("current_status") == "source_confirmed"
            )
            if not valid_new:
                unsupported_new_targets.add(target_id)
                resolution_meta[target_id] = {
                    "status": "insufficient_evidence", "institution_id": "",
                    "matched_by": "", "match_confidence": "", "match_evidence": "",
                    "reason": "No unique policy-compliant match and new-institution evidence is insufficient.",
                }
                continue
            institution_id = stable_id("public:p0", target_id, length=16)
            status = "created_new"
            method = "official_name_address_created_new"
            confidence = "1.0000"
            evidence = json.dumps({
                "official_name": profile["canonical_name"],
                "wonju_address": profile["address"],
                "source_url": profile["primary_source_url"],
                "evidence_hash": profile["evidence_hash"],
            }, ensure_ascii=False, sort_keys=True)
            reason = "Created from official institution name, Wonju address, and field-level evidence."
            match_source_url = profile["primary_source_url"]
            match_evidence_hash = profile["evidence_hash"]
            category = category_by_type.get(profile["institution_type"], profile["institution_type"])
            new_row = {column: "" for column in original_columns}
            new_row.update({
                "institution_id": institution_id,
                "source_id": target_id,
                "category": category,
                "normalized_category": category,
                "name": profile["canonical_name"],
                "normalized_name": normalize_name(profile["canonical_name"]),
                "address": profile["address"],
                "normalized_address": profile.get("normalized_address", profile["address"]),
                "phone": profile.get("representative_phone", ""),
                "mental_health_type": profile["institution_type"],
                # A current official listing proves source presence, not an
                # independently verified operating-state assertion.
                "active_status": "source_confirmed",
                "source_status": "p0_data_03_source_confirmed",
                "primary_source": profile["primary_source_url"],
                "primary_source_reference_date": profile.get("primary_source_updated_at", ""),
                "created_at": profile.get("latest_verified_at", ""),
                "updated_at": profile.get("latest_verified_at", ""),
                "is_late_night_pharmacy": "False",
                "is_year_round_pharmacy": "False",
                "is_public_late_night_pharmacy": "False",
                "pharmacy_operation_source_count": "0",
                "pharmacy_operation_has_conflict": "False",
                "pharmacy_operation_review_status": "not_applicable",
                "pharmacy_operation_latest_source_updated_at": "",
            })
            created_rows.append(new_row)
        mapping_by_public_id[profile["public_health_id"]] = institution_id
        mapping_by_target[target_id] = institution_id
        resolution_meta[target_id] = {
            "status": status, "institution_id": institution_id,
            "matched_by": method, "match_confidence": confidence,
            "match_evidence": evidence, "match_source_url": match_source_url,
            "match_evidence_hash": match_evidence_hash, "reason": reason,
        }

    # User-approved historical linkage: the current P0 seed set contains no
    # Byeoljari evidence, so this is not an automatic name-only match.
    bye_master = original_by_id.get(BYEOLJARI_MASTER_ID)
    bye_source_rows = [row for row in master_source_records if row.get("institution_id") == BYEOLJARI_MASTER_ID]
    if bye_master and normalize_name(bye_master.get("name", "")) == normalize_name(target_by_id[BYEOLJARI_TARGET_ID]["canonical_name"]) and bye_source_rows:
        bye_source = bye_source_rows[0]
        bye_evidence_hash = digest(json.dumps(bye_source, ensure_ascii=False, sort_keys=True))
        bye_public_id = stable_id("public_health", BYEOLJARI_TARGET_ID, length=16)
        mapping_by_public_id[bye_public_id] = BYEOLJARI_MASTER_ID
        mapping_by_target[BYEOLJARI_TARGET_ID] = BYEOLJARI_MASTER_ID
        bye_status_decision = decisions_by_target_field.get((BYEOLJARI_TARGET_ID, "current_status"), {})
        bye_is_confirmed_operating = bye_status_decision.get("resolved_value") == "operating_confirmed"
        resolution_meta[BYEOLJARI_TARGET_ID] = {
            "status": "matched_existing" if bye_is_confirmed_operating else "current_status_unknown",
            "institution_id": BYEOLJARI_MASTER_ID,
            "public_health_id": bye_public_id,
            "matched_by": "curated_existing_master_resolution",
            "match_confidence": "historical_identity_only",
            "match_evidence": json.dumps(bye_source, ensure_ascii=False, sort_keys=True),
            "match_source_url": "",
            "match_evidence_hash": bye_evidence_hash,
            "reason": (
                "Linked to the existing historical master record; current operation was confirmed by a recorded user decision."
                if bye_is_confirmed_operating
                else "Linked to the existing historical master record; current operation is unverified."
            ),
            "source_url": "",
            "source_reference": relative_to_repo(args.institution_source_records),
            "source_updated_at": bye_source.get("source_reference_date", ""),
            "evidence_hash": digest(json.dumps(bye_status_decision, ensure_ascii=False, sort_keys=True)) if bye_is_confirmed_operating else bye_evidence_hash,
            "decision_reference": relative_to_repo(args.resolution_decisions) if bye_is_confirmed_operating else "",
        }
    else:
        resolution_meta[BYEOLJARI_TARGET_ID] = {
            "status": "insufficient_evidence", "institution_id": "", "matched_by": "",
            "match_confidence": "", "match_evidence": "",
            "reason": "The required historical Byeoljari master record could not be verified.",
        }

    profile_by_target = {row["target_id"]: row for row in normalized_profiles}
    final_profiles: list[dict[str, Any]] = []
    for target_id, profile in profile_by_target.items():
        meta = resolution_meta.get(target_id, {})
        institution_id = meta.get("institution_id", "")
        if not institution_id:
            continue
        final_profiles.append({
            "institution_id": institution_id,
            "public_health_id": profile["public_health_id"],
            "target_id": target_id,
            "canonical_name": profile["canonical_name"],
            "institution_type": profile["institution_type"],
            "target_resolution_status": meta["status"],
            "current_status": profile["current_status"],
            "address": profile["address"],
            "representative_phone": profile["representative_phone"],
            "homepage_url": profile["homepage_url"],
            "jurisdiction": profile["jurisdiction"],
            "matched_by": meta["matched_by"],
            "match_confidence": meta["match_confidence"],
            "match_evidence": meta["match_evidence"],
            "match_source_url": meta["match_source_url"],
            "match_evidence_hash": meta["match_evidence_hash"],
            "source_url": profile["primary_source_url"],
            "source_reference": "",
            "source_updated_at": profile["primary_source_updated_at"],
            "evidence_hash": profile["evidence_hash"],
            "source_candidate_id": profile["entity_candidate_id"],
            "has_conflict": False,
            "review_status": "verified",
        })

    if resolution_meta.get(BYEOLJARI_TARGET_ID, {}).get("institution_id"):
        meta = resolution_meta[BYEOLJARI_TARGET_ID]
        final_profiles.append({
            "institution_id": meta["institution_id"],
            "public_health_id": meta["public_health_id"],
            "target_id": BYEOLJARI_TARGET_ID,
            "canonical_name": target_by_id[BYEOLJARI_TARGET_ID]["canonical_name"],
            "institution_type": target_by_id[BYEOLJARI_TARGET_ID]["institution_type"],
            "target_resolution_status": meta["status"],
            "current_status": "operating_confirmed" if meta["status"] == "matched_existing" else "unverified",
            "address": "", "representative_phone": "", "homepage_url": "",
            "jurisdiction": "원주시",
            "matched_by": meta["matched_by"],
            "match_confidence": meta["match_confidence"],
            "match_evidence": meta["match_evidence"],
            "match_source_url": meta["match_source_url"],
            "match_evidence_hash": meta["match_evidence_hash"],
            "source_url": "",
            "source_reference": meta.get("decision_reference") or meta["source_reference"],
            "source_updated_at": meta["source_updated_at"],
            "evidence_hash": meta["evidence_hash"],
            "source_candidate_id": "",
            "has_conflict": False,
            "review_status": "verified" if meta["status"] == "matched_existing" else "manual_review_required",
        })

    conflicts: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []

    def add_conflict(profile: dict[str, Any], field_name: str, issue_type: str,
                     master_value: str, source_value: str) -> None:
        target_id = profile["target_id"]
        conflict_id = stable_id("conflict", target_id, field_name, master_value, source_value)
        decision = decisions_by_target_field.get((target_id, field_name), {})
        decision_value = decision.get("resolved_value", "")
        if decision and decision_value != source_value:
            raise RuntimeError(
                f"Resolution decision for {target_id}/{field_name} does not match the current source value"
            )
        resolved = bool(decision)
        conflicts.append({
            "conflict_id": conflict_id,
            "target_id": target_id,
            "institution_id": profile["institution_id"],
            "conflict_scope": "master_vs_current_official_source",
            "field_name": field_name,
            "master_value": master_value,
            "source_value": source_value,
            "source_url": profile["source_url"],
            "source_reference": "",
            "evidence_hash": profile["evidence_hash"],
            "resolution_status": "user_confirmed_current_source" if resolved else "keep_both_no_master_overwrite",
            "review_required": not resolved,
        })
        if resolved:
            return
        manual_review.append({
            "review_id": stable_id("review", conflict_id),
            "target_id": target_id,
            "institution_id": profile["institution_id"],
            "review_scope": "master_identity",
            "issue_type": issue_type,
            "field_name": field_name,
            "master_value": master_value,
            "source_value": source_value,
            "detail": "Current official field differs from the preserved master field; both values are retained.",
            "source_url": profile["source_url"],
            "source_reference": "",
            "evidence_hash": profile["evidence_hash"],
            "review_status": "manual_review_required",
        })

    for profile in final_profiles:
        if profile["target_resolution_status"] != "matched_existing":
            continue
        master_row = original_by_id[profile["institution_id"]]
        if profile["representative_phone"] and normalize_phone(profile["representative_phone"]) != normalize_phone(master_row.get("phone", "")):
            add_conflict(profile, "representative_phone", "official_phone_conflict", master_row.get("phone", ""), profile["representative_phone"])
        source_base = base_address_key(profile["address"])
        master_base = base_address_key(master_row.get("normalized_address") or master_row.get("address", ""))
        if source_base and master_base and source_base != master_base:
            add_conflict(profile, "address", "official_address_conflict", master_row.get("address", ""), profile["address"])

    bye_profile = next((row for row in final_profiles if row["target_id"] == BYEOLJARI_TARGET_ID), None)
    if bye_profile and bye_profile["current_status"] == "unverified":
        manual_review.append({
            "review_id": stable_id("review", BYEOLJARI_TARGET_ID, "current_status"),
            "target_id": BYEOLJARI_TARGET_ID,
            "institution_id": bye_profile["institution_id"],
            "review_scope": "current_status",
            "issue_type": "current_operating_status_unclear",
            "field_name": "current_status",
            "master_value": original_by_id[bye_profile["institution_id"]].get("active_status", ""),
            "source_value": "unverified",
            "detail": "No current P0-DATA-03 source mentions Byeoljari; historical identity is retained without asserting current operation.",
            "source_url": "",
            "source_reference": bye_profile["source_reference"],
            "evidence_hash": bye_profile["evidence_hash"],
            "review_status": "manual_review_required",
        })

    conflict_targets = {row["target_id"] for row in conflicts if row["review_required"]}
    review_targets = {row["target_id"] for row in manual_review}
    for profile in final_profiles:
        if profile["target_id"] in conflict_targets:
            profile["has_conflict"] = True
        if profile["target_id"] in review_targets:
            profile["review_status"] = "manual_review_required"

    profiles_by_institution = defaultdict(list)
    for row in final_profiles:
        profiles_by_institution[row["institution_id"]].append(row)

    contacts: list[dict[str, Any]] = []
    for row in contacts_in:
        target_id = row["target_id"]
        contacts.append({
            **row,
            "institution_id": "" if target_id == "mh:suicide" else mapping_by_target.get(target_id, ""),
        })
    schedules = [
        {**row, "institution_id": mapping_by_target.get(row["target_id"], ""), "organizational_unit_id": ""}
        for row in schedules_in if mapping_by_target.get(row["target_id"])
    ]
    support = [
        {**row, "institution_id": mapping_by_target.get(row["target_id"], "")}
        for row in support_in
    ]
    services = [
        {**row, "institution_id": mapping_by_target.get(row["target_id"], "")}
        for row in services_in if mapping_by_target.get(row["target_id"])
    ]
    units: list[dict[str, Any]] = []
    for row in units_in:
        units.append({
            **row,
            "parent_institution_id": mapping_by_target.get(row["parent_target_id"], ""),
            "direct_schedule_count": sum(s["target_id"] == row["target_id"] for s in schedules),
            "inherited_schedule": False,
        })

    gaps = [
        {**row, "institution_id": mapping_by_target.get(row["target_id"], ""),
         "public_health_id": row.get("public_health_id") or next(
             (p["public_health_id"] for p in final_profiles if p["target_id"] == row["target_id"]), ""
         )}
        for row in gaps_in
    ]
    if bye_profile and bye_profile["current_status"] == "operating_confirmed":
        gaps = [
            row for row in gaps
            if not (row["target_id"] == BYEOLJARI_TARGET_ID and row["field_name"] == "current_status")
        ]

    source_records: list[dict[str, Any]] = []
    status_by_target = {target_id: meta.get("status", "") for target_id, meta in resolution_meta.items()}
    for row in source_records_in:
        target_id = row["target_id"]
        disposition = row["disposition"]
        if row["record_type"] == "entity":
            disposition = status_by_target.get(target_id, disposition)
        elif row["record_type"] == "contact":
            if disposition != "supporting_duplicate_contact":
                disposition = "integrated_organizational_unit_contact" if target_id == "mh:suicide" else "integrated_contact"
        elif row["record_type"] == "schedule":
            disposition = "integrated_schedule"
        elif row["record_type"] == "service":
            disposition = "integrated_service"
        source_records.append({
            "source_record_id": row["source_record_id"],
            "candidate_id": row["candidate_id"],
            "target_id": target_id,
            "record_type": row["record_type"],
            "disposition": disposition,
            "institution_id": mapping_by_target.get(target_id, ""),
            "organizational_unit_id": row.get("organizational_unit_id", ""),
            "source_url": row["source_url"],
            "source_reference": row.get("source_reference", ""),
            "source_updated_at": row.get("source_updated_at", ""),
            "evidence_hash": row["evidence_hash"],
            "disposition_reason": row.get("disposition_reason", ""),
        })
    if bye_profile:
        source_records.append({
            "source_record_id": stable_id("source", "master_historical", BYEOLJARI_MASTER_ID),
            "candidate_id": f"master:{BYEOLJARI_MASTER_ID}",
            "target_id": BYEOLJARI_TARGET_ID,
            "record_type": "master_historical",
            "disposition": bye_profile["target_resolution_status"],
            "institution_id": BYEOLJARI_MASTER_ID,
            "organizational_unit_id": "",
            "source_url": "",
            "source_reference": relative_to_repo(args.institution_source_records),
            "source_updated_at": bye_profile["source_updated_at"],
            "evidence_hash": digest(json.dumps(bye_source_rows[0], ensure_ascii=False, sort_keys=True)),
            "disposition_reason": "User-approved historical master linkage.",
        })
        if bye_profile["current_status"] == "operating_confirmed":
            decision = decisions_by_target_field[(BYEOLJARI_TARGET_ID, "current_status")]
            source_records.append({
                "source_record_id": stable_id("source", "user_resolution", BYEOLJARI_TARGET_ID, "current_status"),
                "candidate_id": "decision:mh:byeoljari:current_status",
                "target_id": BYEOLJARI_TARGET_ID,
                "record_type": "user_resolution_decision",
                "disposition": "operating_confirmed",
                "institution_id": BYEOLJARI_MASTER_ID,
                "organizational_unit_id": "",
                "source_url": "",
                "source_reference": relative_to_repo(args.resolution_decisions),
                "source_updated_at": "2026-07-16",
                "evidence_hash": digest(json.dumps(decision, ensure_ascii=False, sort_keys=True)),
                "disposition_reason": decision["decision_note"],
            })

    resolutions: list[dict[str, Any]] = []
    schedule_targets = {row["target_id"] for row in schedules}
    suicide_unit = units[0] if units else {}
    for target in targets:
        target_id = target["target_id"]
        if target_id == "mh:suicide":
            status = "organizational_unit"
            institution_id = ""
            org_id = suicide_unit.get("organizational_unit_id", "")
            parent_target_id = "mh:wonju"
            parent_institution_id = mapping_by_target.get("mh:wonju", "")
            matched_by = "explicit_parent_unit_evidence"
            confidence = "1.0000"
            source_url = suicide_unit.get("source_url", "")
            source_reference = ""
            evidence_hash = suicide_unit.get("evidence_hash", "")
            match_source_url = source_url
            match_evidence_hash = evidence_hash
            current_status = suicide_unit.get("current_status", "")
            reason = "Managed as a child unit; no independent institution or direct schedule was created."
            review_required = False
            schedule_status = "inherited_context_no_direct_schedule"
        else:
            meta = resolution_meta.get(target_id, {
                "status": "not_present_in_collected_sources", "institution_id": "",
                "matched_by": "", "match_confidence": "", "reason": "No normalized entity profile.",
            })
            status = meta["status"]
            institution_id = meta.get("institution_id", "")
            org_id = ""
            parent_target_id = ""
            parent_institution_id = ""
            matched_by = meta.get("matched_by", "")
            confidence = meta.get("match_confidence", "")
            profile = next((row for row in final_profiles if row["target_id"] == target_id), {})
            source_url = profile.get("source_url", meta.get("source_url", ""))
            source_reference = profile.get("source_reference", meta.get("source_reference", ""))
            evidence_hash = profile.get("evidence_hash", meta.get("evidence_hash", ""))
            match_source_url = profile.get("match_source_url", meta.get("match_source_url", ""))
            match_evidence_hash = profile.get("match_evidence_hash", meta.get("match_evidence_hash", ""))
            current_status = profile.get("current_status", "")
            reason = meta.get("reason", "")
            review_required = target_id in review_targets or status == "insufficient_evidence"
            schedule_status = "canonical_available" if target_id in schedule_targets else "not_present_in_collected_sources"
        resolutions.append({
            "target_id": target_id,
            "canonical_name": target["canonical_name"],
            "institution_type": target["institution_type"],
            "target_resolution_status": status,
            "institution_id": institution_id,
            "organizational_unit_id": org_id,
            "parent_target_id": parent_target_id,
            "parent_institution_id": parent_institution_id,
            "matched_by": matched_by,
            "match_confidence": confidence,
            "match_source_url": match_source_url,
            "match_evidence_hash": match_evidence_hash,
            "source_url": source_url,
            "source_reference": source_reference,
            "evidence_hash": evidence_hash,
            "current_status": current_status,
            "resolution_reason": reason,
            "review_required": review_required,
            "schedule_coverage_status": schedule_status,
        })

    all_master_rows: list[dict[str, Any]] = [dict(row) for row in master] + created_rows
    contact_count = Counter(row["institution_id"] for row in contacts if row.get("institution_id"))
    schedule_count = Counter(row["institution_id"] for row in schedules if row.get("institution_id"))
    service_count = Counter(row["institution_id"] for row in services if row.get("institution_id"))
    gap_targets = {row["target_id"] for row in gaps}
    final_columns = original_columns + [
        "is_public_health_institution", "public_health_institution_type",
        "public_health_service_count", "public_health_contact_count",
        "public_health_schedule_count", "public_health_has_conflict",
        "public_health_review_status", "public_health_coverage_complete",
        "public_health_latest_source_updated_at", "public_health_updated_at",
    ]
    enriched: list[dict[str, Any]] = []
    for row in all_master_rows:
        institution_id = row["institution_id"]
        linked = profiles_by_institution.get(institution_id, [])
        linked_targets = {profile["target_id"] for profile in linked}
        latest_source = max((profile.get("source_updated_at", "") for profile in linked), default="")
        latest_update = max((profile.get("source_updated_at", "") for profile in linked), default="")
        enriched.append({
            **row,
            "is_public_health_institution": bool(linked),
            "public_health_institution_type": " | ".join(sorted({profile["institution_type"] for profile in linked})),
            "public_health_service_count": service_count[institution_id],
            "public_health_contact_count": contact_count[institution_id],
            "public_health_schedule_count": schedule_count[institution_id],
            "public_health_has_conflict": any(profile["has_conflict"] for profile in linked),
            "public_health_review_status": "manual_review_required" if linked_targets & review_targets else "verified" if linked else "not_applicable",
            "public_health_coverage_complete": bool(linked) and not bool(linked_targets & gap_targets),
            "public_health_latest_source_updated_at": latest_source,
            "public_health_updated_at": latest_update,
        })

    enriched_by_id = {row["institution_id"]: row for row in enriched}
    missing_original_ids = set(original_by_id) - set(enriched_by_id)
    existing_field_changes = 0
    changed_by_column = Counter()
    for institution_id, original in original_by_id.items():
        output = enriched_by_id.get(institution_id, {})
        for column in original_columns:
            if output.get(column, "") != original.get(column, ""):
                existing_field_changes += 1
                changed_by_column[column] += 1
    core_field_change_count = sum(changed_by_column[column] for column in ("name", "address", "phone", "category"))
    pharmacy_columns = [column for column in original_columns if column.startswith("pharmacy_") or column.startswith("is_late_night") or column.startswith("is_year_round") or column.startswith("is_public_late_night")]
    pharmacy_field_change_count = sum(changed_by_column[column] for column in pharmacy_columns)

    final_institution_ids = set(enriched_by_id)
    final_schedule_ids = {row["schedule_id"] for row in schedules}
    final_unit_ids = {row["organizational_unit_id"] for row in units}
    foreign_key_errors = 0
    foreign_key_errors += sum(row["institution_id"] not in final_institution_ids for row in final_profiles)
    foreign_key_errors += sum(bool(row.get("institution_id")) and row["institution_id"] not in final_institution_ids for row in contacts)
    foreign_key_errors += sum(row["institution_id"] not in final_institution_ids for row in schedules)
    foreign_key_errors += sum(row["institution_id"] not in final_institution_ids for row in services)
    foreign_key_errors += sum(row["parent_institution_id"] not in final_institution_ids for row in units)
    foreign_key_errors += sum(row["canonical_schedule_id"] not in final_schedule_ids for row in support)
    foreign_key_errors += sum(bool(row.get("institution_id")) and row["institution_id"] not in final_institution_ids for row in source_records)
    foreign_key_errors += sum(bool(row.get("organizational_unit_id")) and row["organizational_unit_id"] not in final_unit_ids for row in source_records)
    foreign_key_errors += sum(row["target_id"] not in target_ids for row in source_records + conflicts + manual_review + gaps)
    foreign_key_errors += sum(bool(row.get("institution_id")) and row["institution_id"] not in final_institution_ids for row in conflicts + manual_review + gaps)

    primary_candidate_ids = {
        row["candidate_id"] for row in entity_candidates + contact_candidates + schedule_candidates + service_candidates
    }
    traced_candidate_ids = {
        row["candidate_id"] for row in source_records
        if row["record_type"] in {"entity", "contact", "schedule", "service"}
    }
    candidate_trace_rate = len(primary_candidate_ids & traced_candidate_ids) / len(primary_candidate_ids) if primary_candidate_ids else 1.0
    unexplained_dropped = primary_candidate_ids - traced_candidate_ids

    polluted_profile_count = sum(
        not valid_address(row["address"])
        or (bool(row["homepage_url"]) and row["homepage_url"] != DIRECT_HOMEPAGE_BY_TARGET.get(row["target_id"]))
        for row in final_profiles
    )
    unsupported_new_count = sum(
        not (
            row["name"] == target_by_id[row["source_id"]]["canonical_name"]
            and valid_address(row["address"])
            and bool(row["address"])
            and official_source(row["primary_source"])
            and row["active_status"] == "source_confirmed"
        )
        for row in created_rows
    )

    automatic_match_policy_errors = 0
    for profile in final_profiles:
        if profile["target_resolution_status"] != "matched_existing":
            continue
        method = profile["matched_by"]
        if (
            profile["target_id"] == BYEOLJARI_TARGET_ID
            and method == "curated_existing_master_resolution"
            and profile["current_status"] == "operating_confirmed"
            and (BYEOLJARI_TARGET_ID, "current_status") in decisions_by_target_field
        ):
            # This is an explicitly recorded human resolution of a historical
            # linkage, not an automatic name-similarity match.
            continue
        if method not in ALLOWED_AUTOMATIC_MATCH_METHODS:
            automatic_match_policy_errors += 1
            continue
        master_row = original_by_id[profile["institution_id"]]
        name_equal = normalize_name(profile["canonical_name"]) == normalize_name(master_row["name"])
        address_equal = bool(base_address_key(profile["address"])) and base_address_key(profile["address"]) == base_address_key(master_row.get("normalized_address") or master_row["address"])
        phone_equal = bool(normalize_phone(master_row["phone"])) and normalize_phone(master_row["phone"]) in contact_phones_by_target.get(profile["target_id"], set())
        alias_equal = normalize_name(master_row["name"]) in {normalize_name(value) for value in OFFICIAL_ALIASES.get(profile["target_id"], set())}
        method_valid = {
            "name_phone": name_equal and phone_equal,
            "name_base_address": name_equal and address_equal,
            "official_alias_base_address": alias_equal and address_equal,
            "phone_base_address": phone_equal and address_equal,
        }[method]
        automatic_match_policy_errors += not method_valid

    schedule_keys = [
        (row["target_id"], row["schedule_type"], row["day_type"], row["hours_normalized"], row["source_site_root"])
        for row in schedules
    ]
    schedule_false_positive_count = sum(
        row["target_id"] not in {"mh:wonju", "mh:addiction"}
        or row["schedule_type"] != "general_operation"
        or row["day_type"] != "weekday"
        or row["hours_normalized"] != "09:00~18:00"
        for row in schedules
    )
    support_source_record_ids = {
        row["candidate_id"] for row in source_records if row["record_type"] == "schedule_support"
    }
    supporting_source_record_errors = sum(row["supporting_source_id"] not in support_source_record_ids for row in support)
    resolution_status_counts = Counter(row["target_resolution_status"] for row in resolutions)
    manual_gap_separation_errors = (
        sum(row["issue_type"] not in ALLOWED_MANUAL_ISSUES for row in manual_review)
        + sum(as_bool(row.get("review_required")) for row in gaps)
    )
    duplicate_id_count = (
        len(enriched) - len(final_institution_ids)
        + len(final_profiles) - len({row["target_id"] for row in final_profiles})
        + len(schedules) - len(final_schedule_ids)
        + len(units) - len(final_unit_ids)
        + len(resolutions) - len({row["target_id"] for row in resolutions})
    )
    profile_evidence_errors = sum(
        not valid_sha256(row["evidence_hash"])
        or (
            row["target_id"] == BYEOLJARI_TARGET_ID
            and row["current_status"] == "operating_confirmed"
            and row["source_url"]
        )
        or (
            row["target_id"] == BYEOLJARI_TARGET_ID
            and row["current_status"] == "operating_confirmed"
            and row["source_reference"] != relative_to_repo(args.resolution_decisions)
        )
        or (
            row["target_id"] != BYEOLJARI_TARGET_ID
            and row["target_resolution_status"] == "current_status_unknown"
            and (bool(row["source_url"]) or not row["source_reference"])
        )
        or (
            row["target_id"] != BYEOLJARI_TARGET_ID
            and row["target_resolution_status"] != "current_status_unknown"
            and (not row["source_url"] or bool(row["source_reference"]))
        )
        for row in final_profiles
    )
    match_evidence_errors = sum(
        not valid_sha256(row["match_evidence_hash"])
        or (
            row["target_id"] != BYEOLJARI_TARGET_ID
            and row["target_resolution_status"] == "current_status_unknown"
            and bool(row["match_source_url"])
        )
        or (
            row["target_id"] != BYEOLJARI_TARGET_ID
            and row["target_resolution_status"] != "current_status_unknown"
            and not row["match_source_url"]
        )
        for row in final_profiles
    )
    suicide_independent_count = (
        sum(row["target_id"] == "mh:suicide" for row in final_profiles)
        + sum(row["target_id"] == "mh:suicide" for row in schedules)
        + sum(row.get("source_id") == "mh:suicide" for row in created_rows)
    )

    integrity_checks = {
        "target_resolution_complete": len(resolutions) == 26 and {row["target_id"] for row in resolutions} == target_ids,
        "target_resolution_unique": len(resolutions) == len({row["target_id"] for row in resolutions}),
        "target_resolution_statuses_allowed": set(resolution_status_counts) <= ALLOWED_RESOLUTION_STATUSES,
        "all_targets_terminally_resolved": not any(row["target_resolution_status"] in {"insufficient_evidence", "not_present_in_collected_sources"} for row in resolutions),
        "candidate_trace_complete": primary_candidate_ids == traced_candidate_ids,
        "master_preserved": len(original_by_id) == 2481 and not missing_original_ids,
        "all_existing_fields_unchanged": existing_field_changes == 0,
        "core_fields_unchanged": core_field_change_count == 0,
        "pharmacy_fields_unchanged": pharmacy_field_change_count == 0,
        "unsupported_new_institutions_absent": unsupported_new_count == 0 and not unsupported_new_targets,
        "polluted_fields_absent": polluted_profile_count == 0,
        "profile_match_evidence_complete": profile_evidence_errors == 0,
        "identity_match_evidence_complete": match_evidence_errors == 0,
        "automatic_matching_policy_satisfied": automatic_match_policy_errors == 0,
        "foreign_keys_valid": foreign_key_errors == 0,
        "identifiers_unique": duplicate_id_count == 0,
        "suicide_structure_valid": suicide_independent_count == 0 and len(units) == 1 and units[0]["target_id"] == "mh:suicide" and units[0]["direct_schedule_count"] == 0,
        "byeoljari_status_resolved": bool(bye_profile) and (
            (bye_profile["current_status"] == "operating_confirmed" and resolution_meta[BYEOLJARI_TARGET_ID]["status"] == "matched_existing")
            or (bye_profile["current_status"] == "unverified" and resolution_meta[BYEOLJARI_TARGET_ID]["status"] == "current_status_unknown")
        ),
        "canonical_schedule_unique": len(schedule_keys) == len(set(schedule_keys)),
        "schedule_false_positive_absent": schedule_false_positive_count == 0,
        "supporting_sources_valid": supporting_source_record_errors == 0,
        "manual_review_coverage_gap_separated": manual_gap_separation_errors == 0,
    }

    audit_rows: list[dict[str, Any]] = []
    entity_candidate_counts = Counter(row["target_id"] for row in entity_candidates)
    normalized_counts = Counter(row["target_id"] for row in normalized_profiles)
    integrated_counts = Counter(row["target_id"] for row in final_profiles)
    unit_counts = Counter(row["target_id"] for row in units)
    gaps_by_target_field = {(row["target_id"], row["field_name"]) for row in gaps}
    resolution_by_target = {row["target_id"]: row for row in resolutions}
    profile_lookup = {row["target_id"]: row for row in final_profiles}
    for target in targets:
        target_id = target["target_id"]
        resolution = resolution_by_target[target_id]
        profile = profile_lookup.get(target_id, {})
        represented = integrated_counts[target_id] == 1 or unit_counts[target_id] == 1
        audit_rows.append({
            "target_id": target_id,
            "canonical_name": target["canonical_name"],
            "institution_type": target["institution_type"],
            "entity_candidate_count": entity_candidate_counts[target_id],
            "normalized_profile_count": normalized_counts[target_id],
            "integrated_profile_count": integrated_counts[target_id],
            "organizational_unit_count": unit_counts[target_id],
            "target_resolution_status": resolution["target_resolution_status"],
            "institution_id": resolution["institution_id"],
            "trace_status": "resolved" if represented else "unresolved",
            "drop_reason": "" if represented else resolution["resolution_reason"],
            "address_status": "present" if profile.get("address") else "coverage_gap" if (target_id, "address") in gaps_by_target_field else "not_applicable",
            "phone_status": "present" if profile.get("representative_phone") else "coverage_gap" if (target_id, "representative_phone") in gaps_by_target_field else "not_applicable",
            "homepage_status": "present" if profile.get("homepage_url") else "coverage_gap" if (target_id, "homepage_url") in gaps_by_target_field else "not_applicable",
            "schedule_status": resolution["schedule_coverage_status"],
            "review_status": "manual_review_required" if target_id in review_targets else "verified",
        })

    report = {
        "input_master_count": len(master),
        "output_master_count": len(enriched),
        "preserved_master_count": len(original_by_id) - len(missing_original_ids),
        "missing_master_count": len(missing_original_ids),
        "existing_field_change_count": existing_field_changes,
        "modified_existing_name_count": changed_by_column["name"],
        "modified_existing_address_count": changed_by_column["address"],
        "modified_existing_phone_count": changed_by_column["phone"],
        "modified_existing_category_count": changed_by_column["category"],
        "modified_existing_pharmacy_field_count": pharmacy_field_change_count,
        "normalized_public_health_count": len(normalized_profiles),
        "integrated_profile_count": len(final_profiles),
        "linked_existing_count": sum(row["institution_id"] in original_by_id for row in final_profiles),
        "matched_existing_resolution_count": resolution_status_counts["matched_existing"],
        "created_new_count": resolution_status_counts["created_new"],
        "current_status_unknown_count": resolution_status_counts["current_status_unknown"],
        "organizational_unit_count": resolution_status_counts["organizational_unit"],
        "unresolved_target_count": resolution_status_counts["insufficient_evidence"] + resolution_status_counts["not_present_in_collected_sources"],
        "target_resolution_count": len(resolutions),
        "contact_count": len(contacts),
        "schedule_count": len(schedules),
        "schedule_supporting_source_count": len(support),
        "service_count": len(services),
        "source_record_count": len(source_records),
        "conflict_count": len(conflicts),
        "resolved_conflict_count": sum(not bool(row["review_required"]) for row in conflicts),
        "unresolved_conflict_count": sum(bool(row["review_required"]) for row in conflicts),
        "manual_review_count": len(manual_review),
        "coverage_gap_count": len(gaps),
        "candidate_count": len(primary_candidate_ids),
        "traced_candidate_count": len(primary_candidate_ids & traced_candidate_ids),
        "candidate_trace_rate": candidate_trace_rate,
        "unexplained_dropped_candidate_count": len(unexplained_dropped),
        "unsupported_new_institution_count": unsupported_new_count + len(unsupported_new_targets),
        "polluted_field_count": polluted_profile_count,
        "automatic_match_policy_error_count": automatic_match_policy_errors,
        "match_evidence_error_count": match_evidence_errors,
        "foreign_key_error_count": foreign_key_errors,
        "duplicate_identifier_count": duplicate_id_count,
        "independent_suicide_institution_count": suicide_independent_count,
        "schedule_false_positive_count": schedule_false_positive_count,
        "integrity_checks": integrity_checks,
    }
    report["integrity_checks_passed"] = all(integrity_checks.values())
    if not report["integrity_checks_passed"]:
        report["dataset_status"] = "failed"
    elif conflicts or gaps or any(row["target_resolution_status"] == "current_status_unknown" for row in resolutions):
        report["dataset_status"] = "conditionally_verified"
    else:
        report["dataset_status"] = "verified"

    audit_report = {
        "target_count": len(targets),
        "represented_target_count": sum(row["trace_status"] == "resolved" for row in audit_rows),
        "unrepresented_target_count": sum(row["trace_status"] != "resolved" for row in audit_rows),
        "entity_candidate_count": len(entity_candidates),
        "candidate_trace_rate": candidate_trace_rate,
        "unexplained_dropped_candidate_count": len(unexplained_dropped),
        "candidate_trace_complete": primary_candidate_ids == traced_candidate_ids,
        "integrity_checks_passed": report["integrity_checks_passed"],
    }

    out = args.output_dir
    write_csv(out / "institutions_p0_public_health_enriched.csv", enriched, final_columns)
    write_csv(out / "institution_public_health_profiles.csv", final_profiles, PROFILE_COLUMNS)
    write_csv(out / "institution_contacts.csv", contacts, CONTACT_COLUMNS)
    write_csv(out / "institution_operation_schedules.csv", schedules, SCHEDULE_COLUMNS)
    write_csv(out / "institution_schedule_supporting_sources.csv", support, SUPPORT_COLUMNS)
    write_csv(out / "institution_services.csv", services, SERVICE_COLUMNS)
    write_csv(out / "institution_organizational_units.csv", units, UNIT_COLUMNS)
    write_csv(out / "public_health_source_records.csv", source_records, SOURCE_COLUMNS)
    write_csv(out / "public_health_conflicts.csv", conflicts, CONFLICT_COLUMNS)
    write_csv(out / "public_health_manual_review.csv", manual_review, MANUAL_COLUMNS)
    write_csv(out / "public_health_coverage_gaps.csv", gaps, GAP_COLUMNS)
    write_csv(out / "public_health_target_resolution.csv", resolutions, RESOLUTION_COLUMNS)
    (out / "p0_data_03_integration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(args.audit_output_dir / "public_health_profile_gap_audit.csv", audit_rows, AUDIT_COLUMNS)
    (args.audit_output_dir / "public_health_profile_gap_report.json").write_text(
        json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["integrity_checks_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
