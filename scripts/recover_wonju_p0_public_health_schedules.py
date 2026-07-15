"""Recover verified P0-DATA-03 operating schedules from collected page evidence.

This stage is intentionally conservative.  The currently collected sources prove
two institution-level schedules: Wonju Mental Health Welfare Center and Wonju
Addiction Management Center, both weekdays 09:00-18:00.  The mental-health
center-owned header is repeated on several pages, so one page is retained as the
canonical candidate and the remaining pages are emitted as supporting sources.

No network request is made here and no missing schedule is inferred.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]

MENTAL_HEALTH_TARGET_ID = "mh:wonju"
ADDICTION_TARGET_ID = "mh:addiction"
SUICIDE_TARGET_ID = "mh:suicide"
DEMENTIA_TARGET_ID = "mh:dementia"
MENTAL_HEALTH_SITE_ROOT = "https://loveme.yonsei.kr"
MENTAL_HEALTH_CANONICAL_URL = f"{MENTAL_HEALTH_SITE_ROOT}/"
MENTAL_HEALTH_OFFICIAL_URL = (
    "https://www.wonju.go.kr/health/contents.do?key=6230"
)
ADDICTION_SITE_ROOT = "https://www.wonju.go.kr"
ADDICTION_CANONICAL_URL = (
    "https://www.wonju.go.kr/health/contents.do?key=1671"
)

SCHEDULE_TYPE = "general_operation"
DAY_TYPE = "weekday"
OPEN_TIME = "09:00"
CLOSE_TIME = "18:00"
HOURS_NORMALIZED = f"{OPEN_TIME}~{CLOSE_TIME}"

# Deliberately excludes the separate Wonju city page whose wording uses AM/PM.
# This expression identifies the repeated, center-owned header that has already
# been verified as belonging to the mental-health welfare center, not the
# suicide-prevention organizational unit.
VERIFIED_COMMON_HEADER = re.compile(
    r"이용시간\s*평일\s*09\s*:\s*00\s*[~∼-]\s*18\s*:\s*00"
)
VERIFIED_ADDICTION_SCHEDULE = re.compile(
    r"원주시중독관리통합지원센터\s*이용시간\s*:\s*"
    r"평일\s*AM\s*09\s*:\s*00\s*[~∼-]\s*PM\s*18\s*:\s*00",
    re.IGNORECASE,
)
VERIFIED_MENTAL_HEALTH_OFFICIAL_SCHEDULE = re.compile(
    r"원주시정신건강복지센터\s*이용시간\s*:\s*"
    r"평일\s*AM\s*09\s*:\s*00\s*[~∼～-]\s*PM\s*18\s*:\s*00",
    re.IGNORECASE,
)

PROGRAM_OR_EVENT_TERMS = (
    "센터 일정",
    "프로그램",
    "교육",
    "교실",
    "행사",
    "대상",
    "장소",
)
POSTED_TIME_TERMS = ("게시일", "작성일", "등록일", "수정일", "최종수정일")

SCHEDULE_COLUMNS = [
    "candidate_id",
    "target_id",
    "source_url",
    "source_site_root",
    "source_updated_at",
    "schedule_type",
    "day_type",
    "hours_source_raw",
    "hours_normalized",
    "open_time",
    "close_time",
    "closes_next_day",
    "break_start",
    "break_end",
    "break_note",
    "holiday_status",
    "reservation_required",
    "schedule_note",
    "evidence_text",
    "evidence_hash",
    "extraction_method",
    "parse_status",
    "extraction_confidence",
    "review_required",
]

SUPPORTING_SOURCE_COLUMNS = [
    "supporting_source_id",
    "canonical_candidate_id",
    "target_id",
    "source_url",
    "source_site_root",
    "source_updated_at",
    "hours_normalized",
    "evidence_text",
    "evidence_hash",
    "reason",
]

COVERAGE_GAP_COLUMNS = [
    "gap_id",
    "target_id",
    "canonical_name",
    "gap_field",
    "gap_type",
    "coverage_status",
    "reason",
    "recommended_action",
]

TARGET_RESOLUTION_COLUMNS = [
    "target_id",
    "canonical_name",
    "institution_type",
    "target_resolution_status",
    "parent_target_id",
    "schedule_coverage_status",
    "reason",
    "review_required",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_site_root(source_url: str) -> str:
    parsed = urlparse(source_url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = hostname
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    return urlunparse((scheme, netloc, "", "", "", "")).rstrip("/")


def normalize_source_url(source_url: str) -> str:
    parsed = urlparse(source_url.strip())
    path = parsed.path or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            path,
            "",
            parsed.query,
            "",
        )
    )


def compact_text(value: str) -> str:
    return " ".join(str(value).split())


def evidence_context(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 180)
    end = min(len(text), match.end() + 140)
    return compact_text(text[start:end])


def validate_required_columns(
    rows: list[dict[str, str]], required: set[str], label: str
) -> None:
    if not rows:
        raise ValueError(f"{label} is empty")
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"{label} is missing columns: {sorted(missing)}")


def verified_mental_health_sources(
    evidence_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return one verified common-header occurrence per center-site page."""

    found: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for row in sorted(evidence_rows, key=lambda value: value["source_url"]):
        source_url = normalize_source_url(row["source_url"])
        if source_url in seen_urls:
            continue
        if source_site_root(source_url) != MENTAL_HEALTH_SITE_ROOT:
            continue

        text = compact_text(row.get("page_text", ""))
        match = VERIFIED_COMMON_HEADER.search(text)
        if match is None:
            continue

        context = evidence_context(text, match)
        # Ownership guard: the center name and center-wide 이용시간 wording must
        # both occur in the collected center-site page.  A page title mentioning
        # the suicide unit does not transfer ownership of the common header.
        if "원주시정신건강복지센터" not in text:
            continue
        if any(term in context for term in POSTED_TIME_TERMS):
            continue

        seen_urls.add(source_url)
        found.append(
            {
                "source_url": source_url,
                "source_updated_at": row.get("source_updated_at", ""),
                "evidence_text": context,
                "evidence_hash": sha256_text(context),
            }
        )

    return sorted(
        found,
        key=lambda row: (
            0 if row["source_url"] == MENTAL_HEALTH_CANONICAL_URL else 1,
            row["source_url"],
        ),
    )


def verified_addiction_source(
    evidence_rows: list[dict[str, str]],
) -> dict[str, str] | None:
    """Return the explicit institution-owned schedule on Wonju page key 1671."""

    matches: list[dict[str, str]] = []
    for row in evidence_rows:
        source_url = normalize_source_url(row["source_url"])
        if source_url != ADDICTION_CANONICAL_URL:
            continue
        text = compact_text(row.get("page_text", ""))
        match = VERIFIED_ADDICTION_SCHEDULE.search(text)
        if match is None:
            continue
        context = evidence_context(text, match)
        if any(term in context for term in POSTED_TIME_TERMS):
            continue
        matches.append(
            {
                "source_url": source_url,
                "source_updated_at": row.get("source_updated_at", ""),
                "evidence_text": context,
                "evidence_hash": sha256_text(context),
            }
        )

    if len(matches) != 1:
        return None
    return matches[0]


def verified_mental_health_official_source(
    evidence_rows: list[dict[str, str]],
) -> dict[str, str] | None:
    """Return the corroborating Wonju-city mental-center schedule evidence."""

    matches: list[dict[str, str]] = []
    for row in evidence_rows:
        source_url = normalize_source_url(row["source_url"])
        if source_url != MENTAL_HEALTH_OFFICIAL_URL:
            continue
        text = compact_text(row.get("page_text", ""))
        match = VERIFIED_MENTAL_HEALTH_OFFICIAL_SCHEDULE.search(text)
        if match is None:
            continue
        context = evidence_context(text, match)
        if any(term in context for term in POSTED_TIME_TERMS):
            continue
        matches.append(
            {
                "source_url": source_url,
                "source_updated_at": row.get("source_updated_at", ""),
                "evidence_text": context,
                "evidence_hash": sha256_text(context),
            }
        )
    return matches[0] if len(matches) == 1 else None


def make_schedule(
    *,
    target_id: str,
    source: dict[str, str],
    site_root: str,
    hours_source_raw: str,
    extraction_method: str,
    schedule_note: str,
) -> dict[str, object]:
    key = (
        target_id,
        SCHEDULE_TYPE,
        DAY_TYPE,
        HOURS_NORMALIZED,
        site_root,
    )
    return {
        "candidate_id": sha256_text("|".join(key))[:20],
        "target_id": target_id,
        "source_url": source["source_url"],
        "source_site_root": site_root,
        "source_updated_at": source["source_updated_at"],
        "schedule_type": SCHEDULE_TYPE,
        "day_type": DAY_TYPE,
        "hours_source_raw": hours_source_raw,
        "hours_normalized": HOURS_NORMALIZED,
        "open_time": OPEN_TIME,
        "close_time": CLOSE_TIME,
        "closes_next_day": False,
        "break_start": "",
        "break_end": "",
        "break_note": "",
        "holiday_status": "",
        "reservation_required": "",
        "schedule_note": schedule_note,
        "evidence_text": source["evidence_text"],
        "evidence_hash": source["evidence_hash"],
        "extraction_method": extraction_method,
        "parse_status": "parsed",
        "extraction_confidence": "high",
        "review_required": False,
    }


def schedule_key(row: dict[str, object]) -> tuple[str, str, str, str, str]:
    return (
        str(row["target_id"]),
        str(row["schedule_type"]),
        str(row["day_type"]),
        str(row["hours_normalized"]),
        str(row["source_site_root"]),
    )


def build_outputs(
    evidence_rows: list[dict[str, str]], target_rows: list[dict[str, str]]
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    target_by_id = {row["target_id"]: row for row in target_rows}
    verified_sources = verified_mental_health_sources(evidence_rows)
    mental_official_source = verified_mental_health_official_source(evidence_rows)
    addiction_source = verified_addiction_source(evidence_rows)

    schedules: list[dict[str, object]] = []
    supporting_sources: list[dict[str, object]] = []

    if verified_sources and MENTAL_HEALTH_TARGET_ID in target_by_id:
        canonical_source = verified_sources[0]
        mental_schedule = make_schedule(
            target_id=MENTAL_HEALTH_TARGET_ID,
            source=canonical_source,
            site_root=MENTAL_HEALTH_SITE_ROOT,
            hours_source_raw="09:00 ~ 18:00",
            extraction_method="verified_center_common_header",
            schedule_note=(
                "Verified center-wide operating hours repeated on the official "
                "center site."
            ),
        )
        schedules.append(mental_schedule)
        candidate_id = str(mental_schedule["candidate_id"])

        for source in verified_sources[1:]:
            supporting_source_id = "schedule-support:" + sha256_text(
                "|".join(
                    (
                        candidate_id,
                        source["source_url"],
                        source["evidence_hash"],
                    )
                )
            )[:20]
            supporting_sources.append(
                {
                    "supporting_source_id": supporting_source_id,
                    "canonical_candidate_id": candidate_id,
                    "target_id": MENTAL_HEALTH_TARGET_ID,
                    "source_url": source["source_url"],
                    "source_site_root": MENTAL_HEALTH_SITE_ROOT,
                    "source_updated_at": source["source_updated_at"],
                    "hours_normalized": HOURS_NORMALIZED,
                    "evidence_text": source["evidence_text"],
                    "evidence_hash": source["evidence_hash"],
                    "reason": "same_site_common_header",
                }
            )

        if mental_official_source:
            source = mental_official_source
            supporting_source_id = "schedule-support:" + sha256_text(
                "|".join((candidate_id, source["source_url"], source["evidence_hash"]))
            )[:20]
            supporting_sources.append(
                {
                    "supporting_source_id": supporting_source_id,
                    "canonical_candidate_id": candidate_id,
                    "target_id": MENTAL_HEALTH_TARGET_ID,
                    "source_url": source["source_url"],
                    "source_site_root": source_site_root(source["source_url"]),
                    "source_updated_at": source["source_updated_at"],
                    "hours_normalized": HOURS_NORMALIZED,
                    "evidence_text": source["evidence_text"],
                    "evidence_hash": source["evidence_hash"],
                    "reason": "cross_site_official_corroboration",
                }
            )

    if addiction_source and ADDICTION_TARGET_ID in target_by_id:
        schedules.append(
            make_schedule(
                target_id=ADDICTION_TARGET_ID,
                source=addiction_source,
                site_root=ADDICTION_SITE_ROOT,
                hours_source_raw="AM 09:00 ~ PM 18:00",
                extraction_method="verified_institution_schedule_context",
                schedule_note=(
                    "Explicit institution-owned operating hours on the official "
                    "Wonju public-health page."
                ),
            )
        )

    coverage_gaps: list[dict[str, object]] = []
    resolutions: list[dict[str, object]] = []
    canonical_target_ids = {row["target_id"] for row in schedules}

    for target in target_rows:
        target_id = target["target_id"]
        if target_id == SUICIDE_TARGET_ID:
            resolutions.append(
                {
                    "target_id": target_id,
                    "canonical_name": target["canonical_name"],
                    "institution_type": target["institution_type"],
                    "target_resolution_status": "organizational_unit",
                    "parent_target_id": MENTAL_HEALTH_TARGET_ID,
                    "schedule_coverage_status": "inherited_from_parent",
                    "reason": (
                        "No separate address or independent operating-hours "
                        "evidence; managed as a mental-health-center unit."
                    ),
                    "review_required": False,
                }
            )
            continue

        has_canonical_schedule = target_id in canonical_target_ids
        resolutions.append(
            {
                "target_id": target_id,
                "canonical_name": target["canonical_name"],
                "institution_type": target["institution_type"],
                # Entity/master resolution is deliberately deferred to the
                # integration stage.  This file records schedule coverage only.
                "target_resolution_status": "insufficient_evidence",
                "parent_target_id": "",
                "schedule_coverage_status": (
                    "canonical_available"
                    if has_canonical_schedule
                    else "not_present_in_collected_sources"
                ),
                "reason": (
                    "Entity/master resolution is deferred to integration; "
                    "schedule coverage is recorded separately."
                ),
                "review_required": False,
            }
        )

        if has_canonical_schedule:
            continue
        gap_id = "coverage-gap:" + sha256_text(
            f"{target_id}|operation_schedule|not_present_in_collected_sources"
        )[:20]
        coverage_gaps.append(
            {
                "gap_id": gap_id,
                "target_id": target_id,
                "canonical_name": target["canonical_name"],
                "gap_field": "operation_schedule",
                "gap_type": "operating_schedule_evidence_missing",
                "coverage_status": "not_present_in_collected_sources",
                "reason": (
                    "No explicit institution-owned general operating schedule "
                    "was present in the collected sources."
                ),
                "recommended_action": (
                    "Collect an approved institution-specific source in a "
                    "future run."
                ),
            }
        )

    return schedules, supporting_sources, coverage_gaps, resolutions


def validate_outputs(
    target_rows: list[dict[str, str]],
    schedules: list[dict[str, object]],
    supporting_sources: list[dict[str, object]],
    coverage_gaps: list[dict[str, object]],
    resolutions: list[dict[str, object]],
) -> dict[str, bool]:
    target_ids = [row["target_id"] for row in target_rows]
    resolution_ids = [str(row["target_id"]) for row in resolutions]
    candidate_ids = [str(row["candidate_id"]) for row in schedules]
    supporting_ids = [
        str(row["supporting_source_id"]) for row in supporting_sources
    ]
    schedule_keys = [schedule_key(row) for row in schedules]
    canonical_id_set = set(candidate_ids)

    expected_gap_targets = set(target_ids) - {
        MENTAL_HEALTH_TARGET_ID,
        ADDICTION_TARGET_ID,
        SUICIDE_TARGET_ID,
    }
    actual_gap_targets = {str(row["target_id"]) for row in coverage_gaps}
    mental_schedules = [
        row for row in schedules if row["target_id"] == MENTAL_HEALTH_TARGET_ID
    ]
    addiction_schedules = [
        row for row in schedules if row["target_id"] == ADDICTION_TARGET_ID
    ]
    suicide_schedules = [
        row for row in schedules if row["target_id"] == SUICIDE_TARGET_ID
    ]
    dementia_schedules = [
        row for row in schedules if row["target_id"] == DEMENTIA_TARGET_ID
    ]
    suicide_resolutions = [
        row for row in resolutions if row["target_id"] == SUICIDE_TARGET_ID
    ]

    exact_mental_schedule = (
        len(mental_schedules) == 1
        and mental_schedules[0]["schedule_type"] == SCHEDULE_TYPE
        and mental_schedules[0]["day_type"] == DAY_TYPE
        and mental_schedules[0]["hours_normalized"] == HOURS_NORMALIZED
        and mental_schedules[0]["source_site_root"] == MENTAL_HEALTH_SITE_ROOT
        and mental_schedules[0]["source_url"] == MENTAL_HEALTH_CANONICAL_URL
        and mental_schedules[0]["extraction_method"]
        == "verified_center_common_header"
        and mental_schedules[0]["review_required"] is False
    )
    exact_addiction_schedule = (
        len(addiction_schedules) == 1
        and addiction_schedules[0]["schedule_type"] == SCHEDULE_TYPE
        and addiction_schedules[0]["day_type"] == DAY_TYPE
        and addiction_schedules[0]["hours_normalized"] == HOURS_NORMALIZED
        and addiction_schedules[0]["source_site_root"] == ADDICTION_SITE_ROOT
        and addiction_schedules[0]["source_url"] == ADDICTION_CANONICAL_URL
        and addiction_schedules[0]["extraction_method"]
        == "verified_institution_schedule_context"
        and addiction_schedules[0]["review_required"] is False
    )

    schedule_hashes_valid = all(
        row["evidence_hash"] == sha256_text(str(row["evidence_text"]))
        for row in schedules
    )
    supporting_hashes_valid = all(
        row["evidence_hash"] == sha256_text(str(row["evidence_text"]))
        for row in supporting_sources
    )
    no_program_or_timestamp_false_positive = all(
        not any(term in str(row["evidence_text"]) for term in POSTED_TIME_TERMS)
        and not (
            row["target_id"] == DEMENTIA_TARGET_ID
            and any(
                term in str(row["evidence_text"])
                for term in PROGRAM_OR_EVENT_TERMS
            )
        )
        for row in schedules
    )

    return {
        "target_count_is_26": len(target_rows) == 26,
        "target_ids_unique": len(target_ids) == len(set(target_ids)),
        "public_health_branch_count_is_9": sum(
            row["institution_type"] == "public_health_branch"
            for row in target_rows
        )
        == 9,
        "target_resolution_complete": (
            len(resolutions) == 26
            and len(resolution_ids) == len(set(resolution_ids))
            and set(resolution_ids) == set(target_ids)
        ),
        "exact_verified_mental_health_schedule": exact_mental_schedule,
        "exact_verified_addiction_schedule": exact_addiction_schedule,
        "canonical_schedule_count_is_2": len(schedules) == 2,
        "candidate_ids_unique": len(candidate_ids) == len(set(candidate_ids)),
        "canonical_schedule_keys_unique": len(schedule_keys)
        == len(set(schedule_keys)),
        "supporting_source_count_is_4": len(supporting_sources) == 4,
        "supporting_source_ids_unique": len(supporting_ids)
        == len(set(supporting_ids)),
        "supporting_source_foreign_keys_valid": all(
            row["canonical_candidate_id"] in canonical_id_set
            for row in supporting_sources
        ),
        "supporting_sources_match_canonical": all(
            row["target_id"] == MENTAL_HEALTH_TARGET_ID
            and row["hours_normalized"] == HOURS_NORMALIZED
            and row["source_url"] != MENTAL_HEALTH_CANONICAL_URL
            for row in supporting_sources
        )
        and sum(
            row["source_url"] == MENTAL_HEALTH_OFFICIAL_URL
            and row["reason"] == "cross_site_official_corroboration"
            for row in supporting_sources
        ) == 1,
        "schedule_evidence_hashes_valid": schedule_hashes_valid,
        "supporting_evidence_hashes_valid": supporting_hashes_valid,
        "suicide_has_no_direct_schedule": not suicide_schedules,
        "dementia_has_no_general_schedule": not dementia_schedules,
        "no_program_or_timestamp_false_positive": (
            no_program_or_timestamp_false_positive
        ),
        "suicide_is_organizational_unit": (
            len(suicide_resolutions) == 1
            and suicide_resolutions[0]["target_resolution_status"]
            == "organizational_unit"
            and suicide_resolutions[0]["parent_target_id"]
            == MENTAL_HEALTH_TARGET_ID
            and suicide_resolutions[0]["schedule_coverage_status"]
            == "inherited_from_parent"
        ),
        "coverage_gap_targets_complete": actual_gap_targets
        == expected_gap_targets,
        "coverage_gap_count_is_23": len(coverage_gaps) == 23,
        "coverage_gaps_are_not_manual_review": all(
            row["review_required"] is False
            for row in resolutions
            if row["target_id"] in actual_gap_targets
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--page-evidence",
        type=Path,
        default=(
            ROOT
            / "data/collected/public_health/processed/p0_data_03_page_evidence.csv"
        ),
    )
    parser.add_argument(
        "--targets",
        type=Path,
        default=ROOT / "config/p0_data_03_target_institutions.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/processed/public_health",
    )
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence_rows = read_csv(args.page_evidence)
    target_rows = read_csv(args.targets)
    validate_required_columns(
        evidence_rows,
        {"source_url", "source_updated_at", "page_text"},
        "page evidence",
    )
    validate_required_columns(
        target_rows,
        {"target_id", "canonical_name", "institution_type"},
        "target configuration",
    )

    schedules, supporting_sources, coverage_gaps, resolutions = build_outputs(
        evidence_rows, target_rows
    )
    checks = validate_outputs(
        target_rows,
        schedules,
        supporting_sources,
        coverage_gaps,
        resolutions,
    )
    integrity_checks_passed = all(checks.values())

    write_csv(
        args.output_dir / "public_health_schedule_candidates_recovered.csv",
        schedules,
        SCHEDULE_COLUMNS,
    )
    write_csv(
        args.output_dir / "public_health_schedule_supporting_sources.csv",
        supporting_sources,
        SUPPORTING_SOURCE_COLUMNS,
    )
    write_csv(
        args.output_dir / "public_health_coverage_gaps.csv",
        coverage_gaps,
        COVERAGE_GAP_COLUMNS,
    )
    write_csv(
        args.output_dir / "public_health_target_resolution.csv",
        resolutions,
        TARGET_RESOLUTION_COLUMNS,
    )

    report = {
        "target_count": len(target_rows),
        "public_health_branch_target_count": sum(
            row["institution_type"] == "public_health_branch"
            for row in target_rows
        ),
        "recovered_canonical_count": len(schedules),
        "supporting_source_count": len(supporting_sources),
        "coverage_gap_count": len(coverage_gaps),
        "duplicate_candidate_id_count": len(schedules)
        - len({row["candidate_id"] for row in schedules}),
        "duplicate_schedule_key_count": len(schedules)
        - len({schedule_key(row) for row in schedules}),
        "supporting_source_foreign_key_error_count": sum(
            row["canonical_candidate_id"]
            not in {schedule["candidate_id"] for schedule in schedules}
            for row in supporting_sources
        ),
        "schedule_owner_target_ids": sorted(
            str(schedule["target_id"]) for schedule in schedules
        ),
        "integrity_checks": checks,
        "integrity_checks_passed": integrity_checks_passed,
    }
    (args.output_dir / "schedule_recovery_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not integrity_checks_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
