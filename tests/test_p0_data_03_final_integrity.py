"""Independent, offline integrity checks for the final P0-DATA-03 dataset."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATED = ROOT / "data" / "integrated" / "wonju"
NORMALIZED = ROOT / "data" / "normalized" / "public_health"
PROCESSED = ROOT / "data" / "processed" / "public_health"

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
EXPECTED_TYPE_COUNTS = {
    "public_health_center": 1,
    "public_health_branch": 9,
    "public_health_clinic": 8,
    "health_life_support_center": 3,
    "mental_health_welfare_center": 1,
    "suicide_prevention_center": 1,
    "addiction_management_center": 1,
    "dementia_safety_center": 1,
    "mental_rehabilitation_facility": 1,
}
EXPECTED_STATUS_COUNTS = {
    "matched_existing": 19,
    "created_new": 6,
    "organizational_unit": 1,
}
EXPECTED_CREATED_TARGETS = {
    "phb:buro",
    "phc:sanhyeon",
    "hls:namwonju",
    "hls:namwonju-annex",
    "hls:seowonju",
    "mh:dementia",
}
DIRECT_HOMEPAGES = {
    "phc:wonju": "https://www.wonju.go.kr/health/index.do",
    "mh:wonju": "https://loveme.yonsei.kr/",
    "mh:addiction": "http://www.alja.or.kr/",
    "mh:dementia": "https://wonju.nid.or.kr/",
}
OFFICIAL_HOSTS = {
    "www.wonju.go.kr",
    "wonju.nid.or.kr",
    "loveme.yonsei.kr",
    "www.alja.or.kr",
}
OFFICIAL_ALIASES = {
    "phc:sanhyeon": {"산현보건지료소"},
}
FORBIDDEN_ADDRESS_TEXT = re.compile(
    r"이용시간|평일\s*AM|주메뉴|인기검색어|사업안내|공지사항|보도자료|"
    r"모자보건|시설/체육/교육|Copyright|홈페이지\s*:|콘텐츠\s*만족도"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
PHONE = re.compile(r"0\d{1,2}-\d{3,4}-\d{4}")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_columns(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle).fieldnames or [])


def unique_index(
    rows: list[dict[str, str]], key: str, *, label: str
) -> dict[str, dict[str, str]]:
    values = [row[key] for row in rows]
    assert all(values), f"{label} contains a blank {key}"
    assert len(values) == len(set(values)), f"{label} contains duplicate {key}"
    return {row[key]: row for row in rows}


def as_bool(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "").casefold()


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def base_address_key(value: str) -> str:
    text = re.sub(r"\([^)]*\)", "", value or "")
    matches = re.findall(
        r"([0-9A-Za-z가-힣]+(?:로|길))\s*(\d+(?:-\d+)?)", text
    )
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


def valid_hash(value: str) -> bool:
    return bool(SHA256.fullmatch(value or ""))


def evidence_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def data() -> dict[str, object]:
    paths = {
        "targets": ROOT / "config" / "p0_data_03_target_institutions.csv",
        "master": INTEGRATED / "institutions_pharmacy_enriched.csv",
        "enriched": INTEGRATED / "institutions_p0_public_health_enriched.csv",
        "profiles": INTEGRATED / "institution_public_health_profiles.csv",
        "contacts": INTEGRATED / "institution_contacts.csv",
        "schedules": INTEGRATED / "institution_operation_schedules.csv",
        "support": INTEGRATED / "institution_schedule_supporting_sources.csv",
        "services": INTEGRATED / "institution_services.csv",
        "units": INTEGRATED / "institution_organizational_units.csv",
        "sources": INTEGRATED / "public_health_source_records.csv",
        "conflicts": INTEGRATED / "public_health_conflicts.csv",
        "manual": INTEGRATED / "public_health_manual_review.csv",
        "gaps": INTEGRATED / "public_health_coverage_gaps.csv",
        "resolution": INTEGRATED / "public_health_target_resolution.csv",
        "entity_candidates": PROCESSED / "public_health_entity_candidates.csv",
        "contact_candidates": PROCESSED / "public_health_contact_candidates.csv",
        "schedule_candidates": (
            PROCESSED / "public_health_schedule_candidates_recovered.csv"
        ),
        "service_candidates": PROCESSED / "public_health_service_candidates.csv",
        "normalized_profiles": NORMALIZED / "public_health_institutions.csv",
        "master_sources": INTEGRATED / "institution_source_records.csv",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    assert not missing, f"missing P0-DATA-03 artifacts: {missing}"

    loaded: dict[str, object] = {
        name: read_csv(path) for name, path in paths.items()
    }
    loaded["paths"] = paths
    loaded["report"] = json.loads(
        (INTEGRATED / "p0_data_03_integration_report.json").read_text(
            encoding="utf-8"
        )
    )
    return loaded


def test_target_scope_and_terminal_resolution(data: dict[str, object]) -> None:
    targets = data["targets"]
    resolutions = data["resolution"]
    profiles = data["profiles"]
    units = data["units"]
    master = data["master"]

    target_by_id = unique_index(targets, "target_id", label="targets")
    resolution_by_target = unique_index(
        resolutions, "target_id", label="target resolution"
    )
    profile_by_target = unique_index(profiles, "target_id", label="profiles")
    unit_by_target = unique_index(units, "target_id", label="organizational units")
    master_ids = {row["institution_id"] for row in master}

    assert len(target_by_id) == 26
    assert Counter(row["institution_type"] for row in targets) == EXPECTED_TYPE_COUNTS
    assert set(resolution_by_target) == set(target_by_id)
    assert {
        row["target_resolution_status"] for row in resolutions
    } <= ALLOWED_RESOLUTION_STATUSES
    assert Counter(
        row["target_resolution_status"] for row in resolutions
    ) == EXPECTED_STATUS_COUNTS

    for target_id, resolution in resolution_by_target.items():
        status = resolution["target_resolution_status"]
        if status == "organizational_unit":
            assert target_id in unit_by_target
            assert target_id not in profile_by_target
            assert not resolution["institution_id"]
            assert resolution["organizational_unit_id"] == unit_by_target[target_id][
                "organizational_unit_id"
            ]
        else:
            assert target_id in profile_by_target
            assert resolution["institution_id"] == profile_by_target[target_id][
                "institution_id"
            ]
            if status in {"matched_existing", "current_status_unknown"}:
                assert resolution["institution_id"] in master_ids
            elif status == "created_new":
                assert resolution["institution_id"] not in master_ids

    assert not {
        "insufficient_evidence",
        "not_present_in_collected_sources",
    } & {row["target_resolution_status"] for row in resolutions}


def test_candidate_trace_rate_is_exactly_100_percent(
    data: dict[str, object],
) -> None:
    source_records = data["sources"]
    unique_index(source_records, "source_record_id", label="source records")

    candidate_groups = {
        "entity": data["entity_candidates"],
        "contact": data["contact_candidates"],
        "schedule": data["schedule_candidates"],
        "service": data["service_candidates"],
    }
    all_candidate_ids: list[str] = []
    for record_type, candidates in candidate_groups.items():
        candidate_ids = [row["candidate_id"] for row in candidates]
        assert len(candidate_ids) == len(set(candidate_ids))
        all_candidate_ids.extend(candidate_ids)

        traced = [
            row for row in source_records if row["record_type"] == record_type
        ]
        traced_by_candidate = unique_index(
            traced, "candidate_id", label=f"{record_type} source records"
        )
        assert set(traced_by_candidate) == set(candidate_ids)
        for candidate in candidates:
            trace = traced_by_candidate[candidate["candidate_id"]]
            assert trace["target_id"] == candidate["target_id"]
            assert trace["source_url"] == candidate.get("source_url", "")
            assert trace["evidence_hash"] == candidate.get("evidence_hash", "")
            assert trace["disposition_reason"]

    assert len(all_candidate_ids) == len(set(all_candidate_ids))
    assert all_candidate_ids
    traced_primary_ids = {
        row["candidate_id"]
        for row in source_records
        if row["record_type"] in candidate_groups
    }
    assert traced_primary_ids == set(all_candidate_ids)

    support = data["support"]
    support_traces = {
        row["candidate_id"]: row
        for row in source_records
        if row["record_type"] == "schedule_support"
    }
    assert set(support_traces) == {
        row["supporting_source_id"] for row in support
    }


def test_master_is_preserved_and_six_new_rows_are_supported(
    data: dict[str, object],
) -> None:
    master = data["master"]
    enriched = data["enriched"]
    profiles = data["profiles"]
    resolutions = data["resolution"]
    candidates = data["entity_candidates"]
    paths = data["paths"]

    master_by_id = unique_index(master, "institution_id", label="input master")
    enriched_by_id = unique_index(
        enriched, "institution_id", label="P0-enriched master"
    )
    profile_by_target = unique_index(profiles, "target_id", label="profiles")
    candidate_by_id = unique_index(
        candidates, "candidate_id", label="entity candidates"
    )
    resolution_by_target = unique_index(
        resolutions, "target_id", label="target resolution"
    )
    original_columns = csv_columns(paths["master"])

    assert len(master_by_id) == 2481
    assert set(master_by_id) <= set(enriched_by_id)
    for institution_id, before in master_by_id.items():
        after = enriched_by_id[institution_id]
        assert {
            column: after[column] for column in original_columns
        } == before, f"existing master row changed: {institution_id}"

    new_ids = set(enriched_by_id) - set(master_by_id)
    created_resolutions = {
        row["target_id"]: row
        for row in resolutions
        if row["target_resolution_status"] == "created_new"
    }
    assert set(created_resolutions) == EXPECTED_CREATED_TARGETS
    assert len(new_ids) == len(created_resolutions) == 6
    assert new_ids == {
        row["institution_id"] for row in created_resolutions.values()
    }

    for target_id, resolution in created_resolutions.items():
        row = enriched_by_id[resolution["institution_id"]]
        profile = profile_by_target[target_id]
        candidate = candidate_by_id[profile["source_candidate_id"]]
        assert row["source_id"] == target_id
        assert row["name"] == profile["canonical_name"]
        permitted_source_names = {
            normalize_name(profile["canonical_name"]),
            *(
                normalize_name(alias)
                for alias in OFFICIAL_ALIASES.get(target_id, set())
            ),
        }
        assert normalize_name(candidate["institution_name_raw"]) in (
            permitted_source_names
        )
        assert row["address"] == profile["address"] == candidate["address_raw"]
        assert row["phone"] == profile["representative_phone"]
        assert valid_address(row["address"]) and row["address"]
        assert urlparse(row["primary_source"]).hostname in OFFICIAL_HOSTS
        assert row["primary_source"] == profile["source_url"]
        assert candidate["extraction_confidence"] == "high"
        assert not as_bool(candidate["review_required"])
        assert valid_hash(candidate["evidence_hash"])
        assert profile["evidence_hash"] == candidate["evidence_hash"]
        assert profile["matched_by"] == "official_name_address_created_new"
        assert resolution_by_target[target_id]["institution_id"] == row[
            "institution_id"
        ]
        assert row["source_status"] == "p0_data_03_source_confirmed"
        assert row["active_status"] == "source_confirmed"

    assert not any(row.get("source_id") == "mh:suicide" for row in enriched)


def test_automatic_matches_recompute_from_name_address_and_phone_evidence(
    data: dict[str, object],
) -> None:
    master = data["master"]
    profiles = data["profiles"]
    contacts = data["contacts"]
    candidates = data["entity_candidates"]

    master_by_id = unique_index(master, "institution_id", label="input master")
    candidate_by_id = unique_index(
        candidates, "candidate_id", label="entity candidates"
    )
    phones_by_target: dict[str, set[str]] = defaultdict(set)
    for contact in contacts:
        if contact["contact_type"] in {
            "national_hotline",
            "organizational_unit_phone",
        }:
            continue
        phone = normalize_phone(contact["contact_value_normalized"])
        if phone:
            phones_by_target[contact["target_id"]].add(phone)

    matched_profiles = [
        profile
        for profile in profiles
        if profile["target_resolution_status"] == "matched_existing"
    ]
    automatic_profiles = [
        profile
        for profile in matched_profiles
        if profile["matched_by"] != "curated_existing_master_resolution"
    ]
    assert len(automatic_profiles) == 18
    for profile in automatic_profiles:
        method = profile["matched_by"]
        assert method in ALLOWED_AUTOMATIC_MATCH_METHODS
        assert profile["match_confidence"] == "1.0000"
        assert profile["institution_id"] in master_by_id
        assert profile["source_candidate_id"] in candidate_by_id
        candidate = candidate_by_id[profile["source_candidate_id"]]
        assert profile["source_url"] == candidate["source_url"]
        assert profile["evidence_hash"] == candidate["evidence_hash"]
        assert valid_hash(profile["evidence_hash"])

        wanted_name = normalize_name(profile["canonical_name"])
        wanted_address = base_address_key(profile["address"])
        aliases = {
            normalize_name(alias)
            for alias in OFFICIAL_ALIASES.get(profile["target_id"], set())
        }
        eligible: dict[str, set[str]] = defaultdict(set)
        for institution_id, row in master_by_id.items():
            master_name = normalize_name(row["name"])
            master_address = base_address_key(
                row.get("normalized_address") or row["address"]
            )
            master_phone = normalize_phone(row["phone"])
            name_equal = bool(wanted_name and master_name == wanted_name)
            alias_equal = bool(aliases and master_name in aliases)
            address_equal = bool(
                wanted_address and master_address == wanted_address
            )
            phone_equal = bool(
                master_phone
                and master_phone in phones_by_target[profile["target_id"]]
            )
            if name_equal and phone_equal:
                eligible[institution_id].add("name_phone")
            if name_equal and address_equal:
                eligible[institution_id].add("name_base_address")
            if alias_equal and address_equal:
                eligible[institution_id].add("official_alias_base_address")
            if phone_equal and address_equal:
                eligible[institution_id].add("phone_base_address")

        assert set(eligible) == {profile["institution_id"]}
        assert method in eligible[profile["institution_id"]]

        match_evidence = json.loads(profile["match_evidence"])
        master_row = master_by_id[profile["institution_id"]]
        assert match_evidence["source_name"] == profile["canonical_name"]
        assert match_evidence["source_base_address"] == wanted_address
        assert set(match_evidence["source_phones"]) == phones_by_target[
            profile["target_id"]
        ]
        assert match_evidence["master_name"] == master_row["name"]
        assert match_evidence["master_phone"] == master_row["phone"]


def test_profile_fields_are_clean_and_homepages_are_direct(
    data: dict[str, object],
) -> None:
    profiles = data["profiles"]
    gaps = data["gaps"]
    gap_keys = {(row["target_id"], row["field_name"]) for row in gaps}

    assert len(gap_keys) == len(gaps)
    for profile in profiles:
        target_id = profile["target_id"]
        address = profile["address"]
        phone = profile["representative_phone"]
        homepage = profile["homepage_url"]
        assert valid_address(address), f"polluted address for {target_id}: {address}"
        if phone:
            assert PHONE.fullmatch(phone)
        assert homepage == DIRECT_HOMEPAGES.get(target_id, "")
        if homepage:
            assert urlparse(homepage).hostname in OFFICIAL_HOSTS
            assert "contents.do" not in homepage
            assert "selectEmployeeList.do" not in homepage

        for field_name, value in (
            ("address", address),
            ("representative_phone", phone),
            ("homepage_url", homepage),
        ):
            assert ((target_id, field_name) in gap_keys) is (not bool(value))

    wonju_health = next(row for row in profiles if row["target_id"] == "phc:wonju")
    dementia = next(row for row in profiles if row["target_id"] == "mh:dementia")
    assert "정신건강복지센터" not in wonju_health["address"]
    assert not re.search(
        r"원주시전통시장|주메뉴|인기검색어|모자보건|시설/체육/교육",
        dementia["address"],
    )


def test_contacts_and_services_keep_conservative_field_semantics(
    data: dict[str, object],
) -> None:
    contacts = data["contacts"]
    profiles = unique_index(data["profiles"], "target_id", label="profiles")
    services = data["services"]
    service_candidates = unique_index(
        data["service_candidates"], "candidate_id", label="service candidates"
    )

    contact_keys = [
        (
            row["target_id"],
            row["contact_type"],
            normalize_phone(row["contact_value_normalized"]),
        )
        for row in contacts
    ]
    assert len(contact_keys) == len(set(contact_keys))
    assert not any(
        row["target_id"] == "phb:heungeop"
        and normalize_phone(row["contact_value_normalized"]) == "0337374539"
        for row in contacts
    )
    hls_targets = {
        "hls:namwonju",
        "hls:namwonju-annex",
        "hls:seowonju",
    }
    assert all(not profiles[target_id]["representative_phone"] for target_id in hls_targets)
    labelled_inquiry_contacts = [
        row
        for row in contacts
        if row["target_id"] in hls_targets and row["contact_label"] == "문의전화"
    ]
    assert labelled_inquiry_contacts
    assert all(
        row["contact_type"] == "inquiry_phone"
        for row in labelled_inquiry_contacts
    )

    allowed_service_sources = {
        "https://www.wonju.go.kr/health/contents.do?key=3745",
        "https://www.wonju.go.kr/health/contents.do?key=5551",
        "https://www.wonju.go.kr/health/contents.do?key=6230",
        "https://www.wonju.go.kr/health/contents.do?key=1671",
    }
    assert len(services) == len(service_candidates) == 15
    assert {row["source_url"] for row in services} <= allowed_service_sources
    for service in services:
        assert service["source_candidate_id"] in service_candidates
        candidate = service_candidates[service["source_candidate_id"]]
        assert service["target_id"] == candidate["target_id"]
        assert service["source_url"] == candidate["source_url"]
        assert service["evidence_hash"] == candidate["evidence_hash"]
        assert valid_hash(service["evidence_hash"])
        assert service["service_name"]


def test_exact_canonical_schedules_and_supporting_sources(
    data: dict[str, object],
) -> None:
    schedules = data["schedules"]
    support = data["support"]
    source_records = data["sources"]
    profile_by_target = unique_index(
        data["profiles"], "target_id", label="profiles"
    )
    schedule_by_id = unique_index(
        schedules, "schedule_id", label="canonical schedules"
    )
    unique_index(schedules, "candidate_id", label="canonical schedule candidates")
    support_by_id = unique_index(
        support, "supporting_source_id", label="schedule supporting sources"
    )

    assert len(schedules) == 2
    assert {row["target_id"] for row in schedules} == {
        "mh:wonju",
        "mh:addiction",
    }
    expected_sources = {
        "mh:wonju": (
            "https://loveme.yonsei.kr/",
            "https://loveme.yonsei.kr",
            "verified_center_common_header",
        ),
        "mh:addiction": (
            "https://www.wonju.go.kr/health/contents.do?key=1671",
            "https://www.wonju.go.kr",
            "verified_institution_schedule_context",
        ),
    }
    keys = []
    for schedule in schedules:
        expected_url, expected_root, expected_method = expected_sources[
            schedule["target_id"]
        ]
        assert schedule["schedule_type"] == "general_operation"
        assert schedule["day_type"] == "weekday"
        assert schedule["hours_normalized"] == "09:00~18:00"
        assert schedule["open_time"] == "09:00"
        assert schedule["close_time"] == "18:00"
        assert not as_bool(schedule["closes_next_day"])
        assert schedule["source_url"] == expected_url
        assert schedule["source_site_root"] == expected_root
        assert schedule["extraction_method"] == expected_method
        assert schedule["parse_status"] == "parsed"
        assert schedule["extraction_confidence"] == "high"
        assert schedule["review_status"] == "verified"
        assert evidence_hash(schedule["evidence_text"]) == schedule["evidence_hash"]
        assert schedule["institution_id"] == profile_by_target[
            schedule["target_id"]
        ]["institution_id"]
        assert re.search(r"09\s*:\s*00\s*[~∼-]\s*(?:PM\s*)?18\s*:\s*00", schedule["evidence_text"], re.I)
        keys.append(
            (
                schedule["target_id"],
                schedule["schedule_type"],
                schedule["day_type"],
                schedule["hours_normalized"],
                schedule["source_site_root"],
            )
        )
    assert len(keys) == len(set(keys))
    assert not any(
        row["target_id"] in {"mh:suicide", "mh:dementia"}
        for row in schedules
    )

    assert len(support_by_id) == 4
    expected_support_urls = {
        "https://loveme.yonsei.kr/sub.php?menukey=12",
        "https://loveme.yonsei.kr/sub.php?menukey=31",
        "https://loveme.yonsei.kr/sub.php?menukey=14",
        "https://www.wonju.go.kr/health/contents.do?key=6230",
    }
    assert {row["source_url"] for row in support} == expected_support_urls
    support_source_records = {
        row["candidate_id"]: row
        for row in source_records
        if row["record_type"] == "schedule_support"
    }
    for row in support:
        assert row["canonical_schedule_id"] in schedule_by_id
        canonical = schedule_by_id[row["canonical_schedule_id"]]
        assert row["canonical_candidate_id"] == canonical["candidate_id"]
        assert row["institution_id"] == canonical["institution_id"]
        assert row["target_id"] == canonical["target_id"] == "mh:wonju"
        assert row["hours_normalized"] == canonical["hours_normalized"]
        assert row["source_url"] != canonical["source_url"]
        parsed = urlparse(row["source_url"])
        assert row["source_site_root"] == f"{parsed.scheme}://{parsed.netloc}"
        if row["source_site_root"] == canonical["source_site_root"]:
            assert row["reason"] == "same_site_common_header"
        else:
            assert "cross_site" in row["reason"]
            assert "official" in row["reason"]
        assert evidence_hash(row["evidence_text"]) == row["evidence_hash"]
        assert row["supporting_source_id"] in support_source_records
        trace = support_source_records[row["supporting_source_id"]]
        assert trace["institution_id"] == row["institution_id"]
        assert trace["target_id"] == row["target_id"]
        assert trace["source_url"] == row["source_url"]
        assert trace["evidence_hash"] == row["evidence_hash"]


def test_suicide_is_only_an_organizational_unit_and_byeoljari_is_user_confirmed_operating(
    data: dict[str, object],
) -> None:
    resolutions = unique_index(
        data["resolution"], "target_id", label="target resolution"
    )
    profiles = unique_index(data["profiles"], "target_id", label="profiles")
    units = data["units"]
    schedules = data["schedules"]
    enriched = data["enriched"]
    master_ids = {row["institution_id"] for row in data["master"]}
    source_records = data["sources"]
    master_sources = data["master_sources"]

    suicide_resolution = resolutions["mh:suicide"]
    assert suicide_resolution["target_resolution_status"] == "organizational_unit"
    assert suicide_resolution["parent_target_id"] == "mh:wonju"
    assert suicide_resolution["schedule_coverage_status"] == (
        "inherited_context_no_direct_schedule"
    )
    assert "mh:suicide" not in profiles
    assert len(units) == 1
    unit = units[0]
    assert unit["target_id"] == "mh:suicide"
    assert unit["organizational_unit_id"] == suicide_resolution[
        "organizational_unit_id"
    ]
    assert unit["parent_target_id"] == "mh:wonju"
    assert unit["parent_institution_id"] == profiles["mh:wonju"]["institution_id"]
    assert unit["parent_public_health_id"] == profiles["mh:wonju"][
        "public_health_id"
    ]
    assert unit["direct_schedule_count"] == "0"
    assert not any(row["target_id"] == "mh:suicide" for row in schedules)
    assert not any(row.get("source_id") == "mh:suicide" for row in enriched)
    assert any(
        row["target_id"] == "mh:suicide"
        and row["disposition"] == "organizational_unit"
        and row["organizational_unit_id"] == unit["organizational_unit_id"]
        for row in source_records
    )

    bye_resolution = resolutions["mh:byeoljari"]
    bye_profile = profiles["mh:byeoljari"]
    assert bye_resolution["target_resolution_status"] == "matched_existing"
    assert bye_resolution["current_status"] == "operating_confirmed"
    assert bye_profile["current_status"] == "operating_confirmed"
    assert bye_profile["institution_id"] in master_ids
    assert bye_profile["institution_id"] == "public:c8ed3721eb29eca1"
    assert bye_profile["matched_by"] == "curated_existing_master_resolution"
    assert bye_profile["match_confidence"] == "historical_identity_only"
    assert bye_profile["review_status"] == "verified"
    assert bye_profile["source_url"] == ""
    assert bye_profile["source_reference"] == "config/p0_data_03_resolution_decisions.csv"
    bye_sources = [
        row
        for row in master_sources
        if row["institution_id"] == bye_profile["institution_id"]
    ]
    assert bye_sources
    expected_hash = evidence_hash(
        json.dumps(bye_sources[0], ensure_ascii=False, sort_keys=True)
    )
    assert bye_profile["match_evidence_hash"] == expected_hash
    assert any(
        row["target_id"] == "mh:byeoljari"
        and row["record_type"] == "master_historical"
        and row["disposition"] == "matched_existing"
        and row["institution_id"] == bye_profile["institution_id"]
        for row in source_records
    )
    assert any(
        row["target_id"] == "mh:byeoljari"
        and row["record_type"] == "user_resolution_decision"
        and row["disposition"] == "operating_confirmed"
        for row in source_records
    )


def test_manual_review_and_coverage_gaps_are_separated(
    data: dict[str, object],
) -> None:
    manual = data["manual"]
    conflicts = data["conflicts"]
    gaps = data["gaps"]
    targets = {row["target_id"] for row in data["targets"]}
    schedules = {row["target_id"] for row in data["schedules"]}

    unique_index(manual, "review_id", label="manual review")
    unique_index(conflicts, "conflict_id", label="conflicts")
    unique_index(gaps, "gap_id", label="coverage gaps")
    assert manual == []
    assert {row["issue_type"] for row in manual} <= ALLOWED_MANUAL_ISSUES
    assert len(conflicts) == 2
    assert {row["target_id"] for row in conflicts} == {"phc:wonju", "phb:sillim"}
    assert all(row["resolution_status"] == "user_confirmed_current_source" for row in conflicts)
    assert not any(as_bool(row["review_required"]) for row in conflicts)
    profile_by_target = unique_index(data["profiles"], "target_id", label="profiles")
    assert profile_by_target["phc:wonju"]["representative_phone"] == "033-737-4011"
    assert profile_by_target["phb:sillim"]["address"] == "원주시 신림면 치악로 28-2 (신림리 530-9)"

    assert all(row["target_id"] in targets for row in gaps)
    assert all(
        row["coverage_status"] == "not_present_in_collected_sources"
        for row in gaps
    )
    assert not any(as_bool(row["review_required"]) for row in gaps)
    assert all(row["field_name"] and row["gap_reason"] for row in gaps)

    schedule_gap_targets = {
        row["target_id"]
        for row in gaps
        if row["field_name"] == "operation_schedule"
    }
    assert schedule_gap_targets == targets - schedules - {"mh:suicide"}
    assert len(schedule_gap_targets) == 23
    assert not any(
        row["target_id"] == "mh:byeoljari"
        and row["field_name"] == "current_status"
        for row in gaps
    )


def test_all_foreign_keys_and_identifiers_are_valid(
    data: dict[str, object],
) -> None:
    targets = {row["target_id"] for row in data["targets"]}
    institution_ids = {row["institution_id"] for row in data["enriched"]}
    profiles = data["profiles"]
    profile_public_ids = {row["public_health_id"] for row in profiles}
    profile_by_target = {row["target_id"]: row for row in profiles}
    units = data["units"]
    unit_ids = {row["organizational_unit_id"] for row in units}
    schedules = data["schedules"]
    schedule_ids = {row["schedule_id"] for row in schedules}

    tables_with_ids = (
        (data["enriched"], "institution_id"),
        (profiles, "public_health_id"),
        (data["contacts"], "contact_id"),
        (schedules, "schedule_id"),
        (data["support"], "supporting_source_id"),
        (data["services"], "service_id"),
        (units, "organizational_unit_id"),
        (data["sources"], "source_record_id"),
        (data["conflicts"], "conflict_id"),
        (data["manual"], "review_id"),
        (data["gaps"], "gap_id"),
    )
    for rows, identifier in tables_with_ids:
        values = [row[identifier] for row in rows]
        assert all(values)
        assert len(values) == len(set(values)), f"duplicate {identifier}"

    for profile in profiles:
        assert profile["target_id"] in targets
        assert profile["institution_id"] in institution_ids

    for contact in data["contacts"]:
        assert contact["target_id"] in targets
        if contact["organizational_unit_id"]:
            assert contact["organizational_unit_id"] in unit_ids
            assert not contact["institution_id"]
        else:
            assert contact["institution_id"] in institution_ids
            assert contact["public_health_id"] in profile_public_ids

    for schedule in schedules:
        assert schedule["target_id"] in targets
        assert schedule["institution_id"] in institution_ids
        assert schedule["public_health_id"] in profile_public_ids
        assert schedule["institution_id"] == profile_by_target[
            schedule["target_id"]
        ]["institution_id"]

    for service in data["services"]:
        assert service["target_id"] in targets
        assert service["institution_id"] in institution_ids
        assert service["public_health_id"] in profile_public_ids

    for unit in units:
        assert unit["target_id"] in targets
        assert unit["parent_target_id"] in targets
        assert unit["parent_institution_id"] in institution_ids
        assert unit["parent_public_health_id"] in profile_public_ids

    for supporting in data["support"]:
        assert supporting["canonical_schedule_id"] in schedule_ids
        assert supporting["institution_id"] in institution_ids
        assert supporting["target_id"] in targets

    for source in data["sources"]:
        assert source["target_id"] in targets
        assert bool(source["institution_id"]) ^ bool(source["organizational_unit_id"])
        if source["institution_id"]:
            assert source["institution_id"] in institution_ids
        if source["organizational_unit_id"]:
            assert source["organizational_unit_id"] in unit_ids

    for row in data["conflicts"] + data["manual"] + data["gaps"]:
        assert row["target_id"] in targets
        if row.get("institution_id"):
            assert row["institution_id"] in institution_ids

    resolution_by_target = unique_index(
        data["resolution"], "target_id", label="target resolution"
    )
    for target_id, resolution in resolution_by_target.items():
        if resolution["institution_id"]:
            assert resolution["institution_id"] in institution_ids
        if resolution["organizational_unit_id"]:
            assert resolution["organizational_unit_id"] in unit_ids
        if resolution["parent_target_id"]:
            assert resolution["parent_target_id"] in targets
        if resolution["parent_institution_id"]:
            assert resolution["parent_institution_id"] in institution_ids


def test_integration_report_equals_independently_recomputed_metrics(
    data: dict[str, object],
) -> None:
    report = data["report"]
    master = data["master"]
    enriched = data["enriched"]
    profiles = data["profiles"]
    resolutions = data["resolution"]
    source_records = data["sources"]
    paths = data["paths"]

    master_by_id = unique_index(master, "institution_id", label="input master")
    enriched_by_id = unique_index(
        enriched, "institution_id", label="P0-enriched master"
    )
    original_columns = csv_columns(paths["master"])
    changed = Counter()
    for institution_id, before in master_by_id.items():
        after = enriched_by_id.get(institution_id, {})
        for column in original_columns:
            if before.get(column, "") != after.get(column, ""):
                changed[column] += 1

    pharmacy_columns = [
        column
        for column in original_columns
        if column.startswith("pharmacy_")
        or column.startswith("is_late_night")
        or column.startswith("is_year_round")
        or column.startswith("is_public_late_night")
    ]
    primary_candidates = (
        data["entity_candidates"]
        + data["contact_candidates"]
        + data["schedule_candidates"]
        + data["service_candidates"]
    )
    primary_candidate_ids = {row["candidate_id"] for row in primary_candidates}
    traced_candidate_ids = {
        row["candidate_id"]
        for row in source_records
        if row["record_type"] in {"entity", "contact", "schedule", "service"}
    }
    status_counts = Counter(
        row["target_resolution_status"] for row in resolutions
    )
    missing_original_ids = set(master_by_id) - set(enriched_by_id)

    recomputed = {
        "input_master_count": len(master),
        "output_master_count": len(enriched),
        "preserved_master_count": len(master_by_id) - len(missing_original_ids),
        "missing_master_count": len(missing_original_ids),
        "existing_field_change_count": sum(changed.values()),
        "modified_existing_name_count": changed["name"],
        "modified_existing_address_count": changed["address"],
        "modified_existing_phone_count": changed["phone"],
        "modified_existing_category_count": changed["category"],
        "modified_existing_pharmacy_field_count": sum(
            changed[column] for column in pharmacy_columns
        ),
        "normalized_public_health_count": len(data["normalized_profiles"]),
        "integrated_profile_count": len(profiles),
        "linked_existing_count": sum(
            row["institution_id"] in master_by_id for row in profiles
        ),
        "matched_existing_resolution_count": status_counts["matched_existing"],
        "created_new_count": status_counts["created_new"],
        "current_status_unknown_count": status_counts["current_status_unknown"],
        "organizational_unit_count": status_counts["organizational_unit"],
        "unresolved_target_count": status_counts["insufficient_evidence"]
        + status_counts["not_present_in_collected_sources"],
        "target_resolution_count": len(resolutions),
        "contact_count": len(data["contacts"]),
        "schedule_count": len(data["schedules"]),
        "schedule_supporting_source_count": len(data["support"]),
        "service_count": len(data["services"]),
        "source_record_count": len(source_records),
        "conflict_count": len(data["conflicts"]),
        "resolved_conflict_count": sum(
            not as_bool(row["review_required"]) for row in data["conflicts"]
        ),
        "unresolved_conflict_count": sum(
            as_bool(row["review_required"]) for row in data["conflicts"]
        ),
        "manual_review_count": len(data["manual"]),
        "coverage_gap_count": len(data["gaps"]),
        "candidate_count": len(primary_candidate_ids),
        "traced_candidate_count": len(primary_candidate_ids & traced_candidate_ids),
        "candidate_trace_rate": (
            len(primary_candidate_ids & traced_candidate_ids)
            / len(primary_candidate_ids)
        ),
        "unexplained_dropped_candidate_count": len(
            primary_candidate_ids - traced_candidate_ids
        ),
        "unsupported_new_institution_count": 0,
        "polluted_field_count": sum(
            not valid_address(row["address"])
            or (
                bool(row["homepage_url"])
                and row["homepage_url"]
                != DIRECT_HOMEPAGES.get(row["target_id"])
            )
            for row in profiles
        ),
        "automatic_match_policy_error_count": 0,
        "foreign_key_error_count": 0,
        "duplicate_identifier_count": 0,
        "independent_suicide_institution_count": sum(
            row["target_id"] == "mh:suicide" for row in profiles
        )
        + sum(row["target_id"] == "mh:suicide" for row in data["schedules"])
        + sum(row.get("source_id") == "mh:suicide" for row in enriched),
        "schedule_false_positive_count": sum(
            row["target_id"] not in {"mh:wonju", "mh:addiction"}
            or row["schedule_type"] != "general_operation"
            or row["day_type"] != "weekday"
            or row["hours_normalized"] != "09:00~18:00"
            for row in data["schedules"]
        ),
    }
    for key, expected in recomputed.items():
        assert report[key] == expected, f"report metric is stale: {key}"

    assert report["candidate_trace_rate"] == 1.0
    assert report["unsupported_new_institution_count"] == 0
    assert report["polluted_field_count"] == 0
    assert report["automatic_match_policy_error_count"] == 0
    assert report["foreign_key_error_count"] == 0
    assert report["integrity_checks"]
    assert all(report["integrity_checks"].values())
    assert report["integrity_checks_passed"] is all(
        report["integrity_checks"].values()
    )
    assert data["conflicts"] or data["gaps"] or status_counts[
        "current_status_unknown"
    ]
    assert report["dataset_status"] == "conditionally_verified"
