#!/usr/bin/env python3
"""
P0-DATA-01 원주시 관내의료기관 전처리 및 무결성 검증기.

입력:
- 새 크롤러의 raw_rows.jsonl 또는 raw_rows.csv
- 이전 버전의 wonju_medical_institutions.jsonl/csv도 호환

처리:
- 표시 문자열 정리
- 전화번호 정규화
- source_id 및 상세 URL 상호 검증
- 번호/페이지 관계 검증
- 중복 제거 및 거부 행 분리
- 잘못 추출된 기존 latitude/longitude 무효화
- CSV/JSONL 및 검증 보고서 생성

좌표 정책:
- 목록의 onclick 주소 문자열 속 숫자는 좌표가 아니다.
- 본 전처리기는 latitude/longitude를 항상 null로 만든다.
- 좌표는 별도의 주소 지오코딩 파이프라인에서 생성해야 한다.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


PHONE_RE = re.compile(
    r"(?<!\d)(?:02|0\d{2}|1[568]\d{2})[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
)


@dataclass(slots=True)
class Institution:
    source: str
    source_id: str
    number: int
    category: str
    name: str
    address: str
    phone: str
    phone_raw: str
    detail_url: str
    map_action_raw: str
    latitude: None
    longitude: None
    page: int
    source_url: str
    collected_at: str
    processed_at: str


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(
        r"\s+",
        " ",
        str(value).replace("\xa0", " "),
    ).strip()


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value).replace(",", ""))
    return int(match.group()) if match else None


def normalize_phone(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""

    match = PHONE_RE.search(raw)
    if not match:
        return raw

    digits = re.sub(r"\D", "", match.group())

    if digits.startswith("02"):
        if len(digits) == 9:
            return f"{digits[:2]}-{digits[2:5]}-{digits[5:]}"
        if len(digits) == 10:
            return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"

    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"

    return match.group().replace(" ", "-")


def get_first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def extract_source_id_from_url(detail_url: str) -> str:
    if not detail_url:
        return ""

    query = parse_qs(urlparse(detail_url).query)
    for key in ("resrceNo", "resourceNo", "seq", "idx", "no"):
        values = query.get(key)
        if values:
            return clean_text(values[0])
    return ""


def load_records(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()

    if suffix == ".jsonl":
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"JSONL {line_number}행 파싱 실패: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise RuntimeError(
                        f"JSONL {line_number}행이 객체가 아닙니다."
                    )
                records.append(value)
        return records

    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))

    raise ValueError("입력은 .jsonl 또는 .csv 파일이어야 합니다.")


def load_crawl_report(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}

    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RuntimeError("crawl_report.json 형식이 올바르지 않습니다.")
    return value


def canonicalize(
    record: dict[str, Any],
    processed_at: str,
) -> tuple[Institution | None, dict[str, Any] | None, dict[str, Any]]:
    number = parse_int(
        get_first(record, "number", "list_number", "number_raw")
    )
    category = clean_text(
        get_first(record, "category", "category_raw")
    )
    name = clean_text(get_first(record, "name", "name_raw"))
    address = clean_text(
        get_first(record, "address", "address_raw", "road_address")
    )
    phone_raw = clean_text(
        get_first(record, "phone_raw", "phone")
    )
    detail_url = clean_text(get_first(record, "detail_url"))
    source_id = clean_text(get_first(record, "source_id", "source_key"))
    source_id_from_url = extract_source_id_from_url(detail_url)

    if not source_id:
        source_id = source_id_from_url

    page = parse_int(get_first(record, "page"))
    source = clean_text(get_first(record, "source")) or (
        "wonju_health_medical_institution"
    )
    map_action_raw = clean_text(
        get_first(record, "map_action_raw", "map_action")
    )
    source_url = clean_text(get_first(record, "source_url"))
    collected_at = clean_text(get_first(record, "collected_at"))

    audit = {
        "number": number,
        "source_id": source_id,
        "source_id_from_url": source_id_from_url,
        "source_id_url_match": (
            not source_id_from_url
            or not source_id
            or source_id == source_id_from_url
        ),
        "incoming_latitude": get_first(record, "latitude"),
        "incoming_longitude": get_first(record, "longitude"),
    }

    required_missing = [
        field
        for field, value in {
            "number": number,
            "category": category,
            "name": name,
            "address": address,
            "source_id": source_id,
            "detail_url": detail_url,
            "page": page,
        }.items()
        if value in (None, "")
    ]

    if required_missing:
        rejected = {
            "reason": "required_field_missing",
            "missing_fields": required_missing,
            "record": record,
        }
        return None, rejected, audit

    assert number is not None
    assert page is not None

    return (
        Institution(
            source=source,
            source_id=source_id,
            number=number,
            category=category,
            name=name,
            address=address,
            phone=normalize_phone(phone_raw),
            phone_raw=phone_raw,
            detail_url=detail_url,
            map_action_raw=map_action_raw,
            latitude=None,
            longitude=None,
            page=page,
            source_url=source_url,
            collected_at=collected_at,
            processed_at=processed_at,
        ),
        None,
        audit,
    )


def dedupe_key(row: Institution) -> tuple[str, ...]:
    if row.source_id:
        return ("source_id", row.source_id)

    return (
        "fallback",
        row.category.casefold(),
        row.name.casefold(),
        row.address.casefold(),
        re.sub(r"\D", "", row.phone),
    )


def deduplicate(
    rows: list[Institution],
) -> tuple[list[Institution], list[dict[str, Any]]]:
    seen: dict[tuple[str, ...], Institution] = {}
    output: list[Institution] = []
    duplicates: list[dict[str, Any]] = []

    for row in sorted(rows, key=lambda item: (item.number, item.page)):
        key = dedupe_key(row)
        if key in seen:
            duplicates.append(
                {
                    "dedupe_key": list(key),
                    "kept": asdict(seen[key]),
                    "removed": asdict(row),
                }
            )
            continue

        seen[key] = row
        output.append(row)

    return output, duplicates


def coverage(rows: list[Institution], field: str) -> float:
    if not rows:
        return 0.0

    present = sum(
        getattr(row, field) not in (None, "")
        for row in rows
    )
    return round(present / len(rows), 6)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def write_jsonl(path: Path, rows: list[Institution]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    temp.replace(path)


def write_csv(path: Path, rows: list[Institution]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [asdict(row) for row in rows]
    if not records:
        raise RuntimeError("출력할 정상 행이 없습니다.")

    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    temp.replace(path)


def write_optional_parquet(path: Path, rows: list[Institution]) -> str:
    try:
        import pandas as pd
    except ImportError:
        return "skipped: pandas/pyarrow 미설치"

    try:
        pd.DataFrame([asdict(row) for row in rows]).to_parquet(
            path,
            index=False,
        )
    except ImportError:
        return "skipped: pyarrow 미설치"
    return "written"


def build_report(
    input_count: int,
    rows_before_dedupe: list[Institution],
    rows: list[Institution],
    duplicates: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    audits: list[dict[str, Any]],
    crawl_report: dict[str, Any],
    page_size: int,
    parquet_status: str,
) -> dict[str, Any]:
    numbers = [row.number for row in rows]
    pages = [row.page for row in rows]
    source_ids = [row.source_id for row in rows]

    expected_total = (
        parse_int(crawl_report.get("source_reported_count"))
        or parse_int(crawl_report.get("source_reported_pages"))
        and None
    )
    expected_pages = parse_int(
        crawl_report.get("source_reported_pages")
    )

    if expected_total is None and numbers:
        expected_total = max(numbers)
    if expected_pages is None and pages:
        expected_pages = max(pages)

    missing_numbers: list[int] = []
    if expected_total:
        missing_numbers = sorted(
            set(range(1, expected_total + 1)) - set(numbers)
        )

    duplicate_number_count = len(numbers) - len(set(numbers))
    duplicate_source_id_count = len(source_ids) - len(set(source_ids))

    page_relation_errors = [
        {
            "number": row.number,
            "page": row.page,
            "expected_page": ((row.number - 1) // page_size) + 1,
            "source_id": row.source_id,
        }
        for row in rows
        if row.page != ((row.number - 1) // page_size) + 1
    ]

    source_id_url_mismatches = [
        audit
        for audit in audits
        if not audit["source_id_url_match"]
    ]

    invalidated_coordinate_rows = sum(
        audit["incoming_latitude"] not in (None, "", "nan", "NaN")
        or audit["incoming_longitude"] not in (None, "", "nan", "NaN")
        for audit in audits
    )

    category_counts = Counter(row.category for row in rows)
    phone_blank_by_category: dict[str, dict[str, int | float]] = {}
    grouped: dict[str, list[Institution]] = defaultdict(list)

    for row in rows:
        grouped[row.category].append(row)

    for category, category_rows in sorted(grouped.items()):
        blank = sum(not row.phone for row in category_rows)
        phone_blank_by_category[category] = {
            "total": len(category_rows),
            "blank": blank,
            "coverage": round(
                (len(category_rows) - blank) / len(category_rows),
                6,
            ),
        }

    checks = {
        "input_rows_accounted_for": (
            input_count
            == len(rows_before_dedupe) + len(rejected)
        ),
        "normalized_count_matches_expected": (
            expected_total is None or len(rows) == expected_total
        ),
        "all_pages_present": (
            expected_pages is None or len(set(pages)) == expected_pages
        ),
        "number_sequence_complete": len(missing_numbers) == 0,
        "number_unique": duplicate_number_count == 0,
        "source_id_unique": duplicate_source_id_count == 0,
        "page_number_relation_valid": len(page_relation_errors) == 0,
        "source_id_matches_detail_url": (
            len(source_id_url_mismatches) == 0
        ),
        "core_fields_complete": all(
            coverage(rows, field) == 1.0
            for field in (
                "source_id",
                "category",
                "name",
                "address",
                "detail_url",
            )
        ),
        "coordinates_cleared": all(
            row.latitude is None and row.longitude is None
            for row in rows
        ),
    }

    return {
        "dataset": "P0-DATA-01 원주시 관내의료기관 전처리",
        "input_row_count": input_count,
        "valid_before_dedupe_count": len(rows_before_dedupe),
        "processed_row_count": len(rows),
        "rejected_count": len(rejected),
        "duplicate_count": len(duplicates),
        "expected_source_count": expected_total,
        "expected_source_pages": expected_pages,
        "collected_page_count": len(set(pages)),
        "number_min": min(numbers) if numbers else None,
        "number_max": max(numbers) if numbers else None,
        "missing_list_numbers": missing_numbers,
        "duplicate_number_count": duplicate_number_count,
        "duplicate_source_id_count": duplicate_source_id_count,
        "page_relation_error_count": len(page_relation_errors),
        "page_relation_error_examples": page_relation_errors[:20],
        "source_id_url_mismatch_count": len(source_id_url_mismatches),
        "source_id_url_mismatch_examples": (
            source_id_url_mismatches[:20]
        ),
        "invalidated_incoming_coordinate_rows": (
            invalidated_coordinate_rows
        ),
        "coordinate_policy": (
            "latitude/longitude는 모두 null. "
            "목록 주소를 별도 지오코딩한 결과만 후속 테이블에 저장."
        ),
        "coverage": {
            "source_id": coverage(rows, "source_id"),
            "category": coverage(rows, "category"),
            "name": coverage(rows, "name"),
            "address": coverage(rows, "address"),
            "phone": coverage(rows, "phone"),
            "detail_url": coverage(rows, "detail_url"),
            "coordinates": 0.0,
        },
        "category_counts": dict(sorted(category_counts.items())),
        "phone_stats_by_category": phone_blank_by_category,
        "parquet_status": parquet_status,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "processed_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
    }


def preprocess(args: argparse.Namespace) -> int:
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    crawl_report_path: Path | None = None
    if args.crawl_report:
        crawl_report_path = Path(args.crawl_report).resolve()
    else:
        candidate = input_path.parent / "crawl_report.json"
        if candidate.exists():
            crawl_report_path = candidate

    crawl_report = load_crawl_report(crawl_report_path)
    records = load_records(input_path)
    processed_at = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    canonical_rows: list[Institution] = []
    rejected: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    for record in records:
        row, rejection, audit = canonicalize(
            record=record,
            processed_at=processed_at,
        )
        audits.append(audit)
        if rejection is not None:
            rejected.append(rejection)
        elif row is not None:
            canonical_rows.append(row)

    rows, duplicates = deduplicate(canonical_rows)

    csv_path = output_dir / "wonju_medical_institutions.csv"
    jsonl_path = output_dir / "wonju_medical_institutions.jsonl"
    parquet_path = output_dir / "wonju_medical_institutions.parquet"
    duplicates_path = output_dir / "wonju_medical_duplicates.json"
    rejected_path = output_dir / "wonju_medical_rejected.json"
    report_path = output_dir / "validation_report.json"

    write_csv(csv_path, rows)
    write_jsonl(jsonl_path, rows)
    write_json(duplicates_path, duplicates)
    write_json(rejected_path, rejected)

    parquet_status = (
        write_optional_parquet(parquet_path, rows)
        if args.parquet
        else "disabled"
    )

    report = build_report(
        input_count=len(records),
        rows_before_dedupe=canonical_rows,
        rows=rows,
        duplicates=duplicates,
        rejected=rejected,
        audits=audits,
        crawl_report=crawl_report,
        page_size=args.page_size,
        parquet_status=parquet_status,
    )
    report["files"] = {
        "csv": str(csv_path),
        "jsonl": str(jsonl_path),
        "parquet": (
            str(parquet_path)
            if parquet_status == "written"
            else None
        ),
        "duplicates": str(duplicates_path),
        "rejected": str(rejected_path),
        "validation_report": str(report_path),
    }
    write_json(report_path, report)

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.strict and not report["all_checks_passed"]:
        failed = [
            name
            for name, passed in report["checks"].items()
            if not passed
        ]
        raise RuntimeError(
            "전처리 엄격 검증 실패: " + ", ".join(failed)
        )

    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="원주시 관내의료기관 데이터 전처리 및 무결성 검사"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="raw_rows.jsonl/csv 또는 이전 수집 결과 JSONL/CSV",
    )
    parser.add_argument(
        "--crawl-report",
        default=None,
        help="원천 수집 crawl_report.json. 생략 시 입력 파일 옆에서 자동 탐색",
    )
    parser.add_argument(
        "--output-dir",
        default="data/processed/wonju_medical",
        help="전처리 결과 출력 디렉터리",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=10,
        help="원천 목록 페이지당 행 수",
    )
    parser.add_argument(
        "--parquet",
        action="store_true",
        help="pandas+pyarrow가 설치된 경우 Parquet도 생성",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="무결성 검사 하나라도 실패하면 오류 종료",
    )
    return parser.parse_args(argv)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()

    try:
        return preprocess(args)
    except Exception:
        logging.exception("전처리 실패")
        return 1


if __name__ == "__main__":
    sys.exit(main())
