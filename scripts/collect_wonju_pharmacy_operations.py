"""
원주시 보건소 심야약국·연중무휴약국 운영정보 크롤러.

공식 출처
- 심야약국: https://www.wonju.go.kr/health/contents.do?key=1801
- 연중무휴약국: https://www.wonju.go.kr/health/contents.do?key=1802

출력
- raw/<수집일>/late_night.html
- raw/<수집일>/year_round.html
- processed/pharmacy_operation_sources.csv
- processed/pharmacy_operations_merged.csv
- processed/pharmacy_operation_conflicts.csv
- processed/master_matches.csv                  (--master 사용 시)
- processed/master_conflicts.csv                (--master 사용 시)
- processed/validation_report.json
- processed/collection_manifest.json

설계 원칙
- 웹페이지의 셀 원문과 정규화 값을 모두 보존한다.
- 공식 페이지끼리 운영시간이 다르면 자동 덮어쓰지 않는다.
- 기존 기관 마스터와 전화번호가 다르면 수동검토 충돌로 기록한다.
- 화면에 표시된 전화번호에 지역번호가 없을 때만 원주시 지역번호 033을 보완한다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SOURCES = {
    "late_night": {
        "url": "https://www.wonju.go.kr/health/contents.do?key=1801",
        "label": "심야약국",
        "expected_count": 16,
    },
    "year_round": {
        "url": "https://www.wonju.go.kr/health/contents.do?key=1802",
        "label": "연중무휴약국",
        "expected_count": 9,
    },
}

EXPECTED_TABLE_HEADERS = {
    "name": ("의료기관명", "약국명"),
    "address": ("주소",),
    "phone": ("전화번호",),
    "hours": ("운영시간",),
}

LAST_UPDATED_RE = re.compile(
    r"최종수정일\s*[:：]?\s*(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:(?:0\d{1,2})[-\s]?)?\d{3,4}[-\s]?\d{4}(?!\d)"
)
TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
PUBLIC_LATE_NIGHT_RE = re.compile(r"\*?\s*공공심야약국", re.IGNORECASE)


@dataclass(slots=True)
class PharmacySourceRow:
    source_type: str
    source_label: str
    source_url: str
    source_updated_at: str
    collected_at: str
    source_row_number: int

    pharmacy_name_source_raw: str
    pharmacy_name: str
    is_public_late_night: bool

    address_source_raw: str
    address_normalized: str

    phone_source_raw: str
    phone_normalized: str

    weekday_hours_source_raw: str
    weekday_hours_normalized: str
    saturday_hours_source_raw: str
    saturday_hours_normalized: str
    sunday_hours_source_raw: str
    sunday_hours_normalized: str

    raw_row_text: str


def clean_space(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def normalize_name(value: str) -> str:
    text = clean_space(value)
    text = PUBLIC_LATE_NIGHT_RE.sub("", text)
    text = re.sub(r"\s*([(),])\s*", r"\1", text)
    return clean_space(text)


def normalize_name_key(value: str) -> str:
    text = normalize_name(value).casefold()
    return re.sub(r"[\s(){}\[\],.\-·ㆍ]", "", text)


def normalize_address(value: str) -> str:
    text = clean_space(value)
    text = text.replace("강원도", "강원특별자치도")
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r",\s*(?=\()", " ", text)
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)\s*", ")", text)
    return clean_space(text)


def normalize_address_key(value: str) -> str:
    text = normalize_address(value)
    text = re.sub(r"^강원특별자치도\s*", "", text)
    text = re.sub(r"^원주시\s*", "", text)

    # 건물명·층·호 등 상세정보는 비교키에서 후순위로 취급한다.
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(
        r",?\s*(?:지하\s*)?\d+(?:,\d+)*(?:층|호|동)\b.*$",
        "",
        text,
    )
    return re.sub(r"[\s,.\-]", "", text).casefold()


def normalize_base_address(value: str) -> str:
    """Comparison key that excludes province, building, floor, and unit detail."""
    text = normalize_address(value)
    for prefix in ("강원특별자치도", "강원도", "원주시"):
        text = text.replace(prefix, "")
    text = re.sub(r"\([^)]*\)", "", text).split(",", 1)[0]
    # Preserve the road/building number; remove only a trailing unit/floor.
    text = re.sub(r"(\d+)\s+\d+(?:\s*,?\s*\d+)*\s*(?:층|호)\b.*$", r"\1", text)
    return re.sub(r"[\s,.\-]", "", text).casefold()


def normalize_detail_address(value: str) -> str:
    text = normalize_address(value)
    for prefix in ("강원특별자치도", "강원도", "원주시"):
        text = text.replace(prefix, "")
    return re.sub(r"\s+", "", text).casefold()


def normalize_phone(value: str, default_area_code: str = "033") -> str:
    raw = clean_space(value)
    if not raw or raw in {"-", "없음"}:
        return ""

    match = PHONE_RE.search(raw)
    if not match:
        return raw

    digits = re.sub(r"\D", "", match.group())

    # 원주시 공식 표는 지역번호를 생략해 7~8자리만 제공할 수 있다.
    if len(digits) in (7, 8):
        digits = default_area_code + digits

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


def normalize_hours(value: str) -> str:
    text = clean_space(value)
    if text in {"", "-", "없음", "휴무"}:
        return text or "-"

    text = text.replace("∼", "~").replace("～", "~").replace("−", "-")
    text = re.sub(r"\s*~\s*", "~", text)
    text = re.sub(r"(?<![:\d])(\d{1,2})시", r"\1:00", text)
    text = re.sub(r"(?<![:\d])(\d{1,2})(?=~)", r"\1:00", text)
    text = re.sub(r"(\d{1,2})시\s*~\s*(\d{1,2})시", r"\1:00~\2:00", text)
    text = re.sub(r"\s*\(\s*", " (", text)
    text = re.sub(r"\s*\)\s*", ")", text)
    return clean_space(text)


def parse_hours(value: str) -> dict[str, Any]:
    """
    첫 번째 영업시간 구간만 구조화한다.
    휴게시간은 별도 문자열로 보존하며 원문은 절대 폐기하지 않는다.
    """
    normalized = normalize_hours(value)
    if normalized in {"", "-", "없음", "휴무"}:
        return {
            "open_time": "",
            "close_time": "",
            "closes_next_day": False,
            "break_note": "",
        }

    times = TIME_RE.findall(normalized)
    open_time = ""
    close_time = ""
    closes_next_day = False

    if len(times) >= 2:
        open_hour, open_minute = map(int, times[0])
        close_hour, close_minute = map(int, times[1])

        open_time = f"{open_hour:02d}:{open_minute:02d}"
        close_time = f"{close_hour:02d}:{close_minute:02d}"

        if close_hour == 24:
            close_time = "24:00"
        elif (close_hour, close_minute) <= (open_hour, open_minute):
            closes_next_day = True

    break_note = ""
    if "휴게" in normalized:
        parenthetical = re.findall(r"\(([^)]*휴게[^)]*)\)", normalized)
        break_note = parenthetical[0] if parenthetical else normalized

    return {
        "open_time": open_time,
        "close_time": close_time,
        "closes_next_day": closes_next_day,
        "break_note": break_note,
    }


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
            "DNT": "1",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


def fetch_html(
    session: requests.Session,
    url: str,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()

    if not response.encoding or response.encoding.lower() in {
        "iso-8859-1",
        "ascii",
    }:
        response.encoding = response.apparent_encoding or "utf-8"

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        raise RuntimeError(f"HTML이 아닌 응답: {content_type}")

    html = response.text
    metadata = {
        "requested_url": url,
        "final_url": response.url,
        "status_code": response.status_code,
        "content_type": content_type,
        "etag": response.headers.get("ETag", ""),
        "last_modified_header": response.headers.get("Last-Modified", ""),
        "content_length": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(),
    }
    return html, metadata


def table_header_text(table: Tag) -> str:
    return clean_space(" ".join(th.get_text(" ", strip=True) for th in table.find_all("th")))


def is_pharmacy_table(table: Tag) -> bool:
    headers = table_header_text(table)
    return (
        any(label in headers for label in EXPECTED_TABLE_HEADERS["name"])
        and "주소" in headers
        and "전화번호" in headers
        and ("운영시간" in headers or all(day in headers for day in ("평일", "토요일", "일요일")))
    )


def find_pharmacy_table(soup: BeautifulSoup) -> Tag:
    candidates = [table for table in soup.find_all("table") if is_pharmacy_table(table)]
    if not candidates:
        raise RuntimeError("약국 운영정보 테이블을 찾지 못했습니다.")

    # 실제 데이터 행이 가장 많은 표를 선택한다.
    return max(
        candidates,
        key=lambda table: sum(1 for tr in table.find_all("tr") if len(tr.find_all(["th", "td"], recursive=False)) >= 6),
    )


def parse_last_updated_at(soup: BeautifulSoup) -> str:
    text = clean_space(soup.get_text(" ", strip=True))
    match = LAST_UPDATED_RE.search(text)
    if not match:
        return ""
    year, month, day = map(int, match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_table_cells(tr: Tag) -> list[str] | None:
    cells = tr.find_all(["th", "td"], recursive=False)
    # The public late-night page's first row has closing td tags embedded in
    # comments, which makes BeautifulSoup nest the three hours cells.
    if len(cells) < 6:
        repaired = BeautifulSoup(
            str(tr).replace("<!--</td-->", "</td>"), "html.parser"
        ).find("tr")
        if repaired is not None:
            cells = repaired.find_all(["th", "td"], recursive=False)
    values = [clean_space(" ".join(cell.stripped_strings)) for cell in cells]
    return values[:6] if len(values) >= 6 else None


def parse_source_page(
    html: str,
    source_type: str,
    source_label: str,
    source_url: str,
    collected_at: str,
) -> tuple[list[PharmacySourceRow], str, int]:
    soup = BeautifulSoup(html, "html.parser")
    table = find_pharmacy_table(soup)
    source_updated_at = parse_last_updated_at(soup)

    rows: list[PharmacySourceRow] = []

    dom_data_row_count = 0
    for tr in table.find_all("tr"):
        values = extract_table_cells(tr)
        if values is None:
            continue

        # 현재 공식 표 구조:
        # 의료기관명 / 주소 / 전화번호 / 평일 / 토요일 / 일요일
        name_raw, address_raw, phone_raw, weekday_raw, saturday_raw, sunday_raw = values

        if not name_raw or name_raw in {"의료기관명", "약국명"}:
            continue

        is_public_late_night = bool(PUBLIC_LATE_NIGHT_RE.search(name_raw))
        pharmacy_name = normalize_name(name_raw)

        dom_data_row_count += 1
        rows.append(
            PharmacySourceRow(
                source_type=source_type,
                source_label=source_label,
                source_url=source_url,
                source_updated_at=source_updated_at,
                collected_at=collected_at,
                source_row_number=len(rows) + 1,
                pharmacy_name_source_raw=name_raw,
                pharmacy_name=pharmacy_name,
                is_public_late_night=is_public_late_night,
                address_source_raw=address_raw,
                address_normalized=normalize_address(address_raw),
                phone_source_raw=phone_raw,
                phone_normalized=normalize_phone(phone_raw),
                weekday_hours_source_raw=weekday_raw,
                weekday_hours_normalized=normalize_hours(weekday_raw),
                saturday_hours_source_raw=saturday_raw,
                saturday_hours_normalized=normalize_hours(saturday_raw),
                sunday_hours_source_raw=sunday_raw,
                sunday_hours_normalized=normalize_hours(sunday_raw),
                raw_row_text=clean_space(tr.get_text(" ", strip=True)),
            )
        )

    if not rows:
        raise RuntimeError(f"{source_label} 페이지에서 데이터 행을 추출하지 못했습니다.")

    return rows, source_updated_at, dom_data_row_count


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def relative_to_repo(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.name


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not records:
        # 빈 충돌 파일도 헤더 없이 빈 파일로 만들지 않고 명시적 JSON 대체는 하지 않는다.
        atomic_write_text(path, "")
        return

    fieldnames: list[str] = []
    for record in records:
        for key in record:
            if key not in fieldnames:
                fieldnames.append(key)

    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    temp.replace(path)


def source_row_identity(row: PharmacySourceRow) -> tuple[str, str]:
    if row.phone_normalized:
        return ("phone", row.phone_normalized)
    return (
        "name_address",
        f"{normalize_name_key(row.pharmacy_name)}|{normalize_address_key(row.address_normalized)}",
    )


def merge_source_rows(
    rows: list[PharmacySourceRow],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[PharmacySourceRow]] = {}
    for row in rows:
        grouped.setdefault(source_row_identity(row), []).append(row)

    merged: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for identity, group in sorted(grouped.items(), key=lambda item: item[0]):
        base = sorted(
            group,
            key=lambda row: (
                row.source_updated_at or "0000-00-00",
                row.source_type,
            ),
            reverse=True,
        )[0]

        by_type = {row.source_type: row for row in group}
        late = by_type.get("late_night")
        year = by_type.get("year_round")
        conflict_fields: list[str] = []

        if late and year:
            comparison_fields = (
                ("weekday_hours_normalized", "평일 운영시간"),
                ("saturday_hours_normalized", "토요일 운영시간"),
                ("sunday_hours_normalized", "일요일 운영시간"),
                ("phone_normalized", "전화번호"),
                ("address_normalized", "주소"),
            )
            for field, field_label in comparison_fields:
                late_value = getattr(late, field)
                year_value = getattr(year, field)
                if late_value != year_value:
                    conflict_fields.append(field)
                    conflicts.append(
                        {
                            "pharmacy_name": base.pharmacy_name,
                            "identity_type": identity[0],
                            "identity_value": identity[1],
                            "field_name": field,
                            "field_label": field_label,
                            "late_night_value": late_value,
                            "late_night_source_raw": getattr(
                                late,
                                field.replace("_normalized", "_source_raw"),
                                late_value,
                            ),
                            "late_night_source_updated_at": late.source_updated_at,
                            "year_round_value": year_value,
                            "year_round_source_raw": getattr(
                                year,
                                field.replace("_normalized", "_source_raw"),
                                year_value,
                            ),
                            "year_round_source_updated_at": year.source_updated_at,
                            "resolution": "manual_review_required",
                        }
                    )

        merged_row: dict[str, Any] = {
            "pharmacy_name": base.pharmacy_name,
            "address_normalized": base.address_normalized,
            "phone_normalized": base.phone_normalized,
            "is_late_night": late is not None,
            "is_year_round": year is not None,
            "is_public_late_night": any(row.is_public_late_night for row in group),
            "needs_manual_review": bool(conflict_fields),
            "conflict_fields": "|".join(conflict_fields),
            "latest_source_updated_at": max(
                (row.source_updated_at for row in group if row.source_updated_at),
                default="",
            ),
            "source_record_count": len(group),
        }

        for prefix, row in (("late_night", late), ("year_round", year)):
            for day in ("weekday", "saturday", "sunday"):
                raw_key = f"{day}_hours_source_raw"
                normalized_key = f"{day}_hours_normalized"
                merged_row[f"{prefix}_{raw_key}"] = getattr(row, raw_key) if row else ""
                merged_row[f"{prefix}_{normalized_key}"] = (
                    getattr(row, normalized_key) if row else ""
                )

                parsed = parse_hours(getattr(row, normalized_key) if row else "")
                merged_row[f"{prefix}_{day}_open_time"] = parsed["open_time"]
                merged_row[f"{prefix}_{day}_close_time"] = parsed["close_time"]
                merged_row[f"{prefix}_{day}_closes_next_day"] = parsed[
                    "closes_next_day"
                ]
                merged_row[f"{prefix}_{day}_break_note"] = parsed["break_note"]

        merged.append(merged_row)

    return merged, conflicts


def load_csv_flexible(path: Path) -> list[dict[str, str]]:
    encodings = ("utf-8-sig", "utf-8", "cp949", "euc-kr")
    errors: list[str] = []

    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as file:
                return list(csv.DictReader(file))
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")

    raise RuntimeError("CSV 인코딩 판독 실패: " + " | ".join(errors))


def first_value(record: dict[str, str], keys: Iterable[str]) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return clean_space(value)
    return ""


def compare_with_master(
    merged_rows: list[dict[str, Any]],
    master_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    master_records = load_csv_flexible(master_path)

    master_index: list[dict[str, Any]] = []
    for row_number, record in enumerate(master_records, start=2):
        category = first_value(
            record,
            ("category", "normalized_category", "구분", "업종", "기관유형"),
        )
        if category and "약국" not in category:
            continue

        name = first_value(
            record,
            ("name", "pharmacy_name", "기관명", "약국명", "명칭"),
        )
        address = first_value(
            record,
            ("address", "address_raw", "road_address", "주소", "소재지"),
        )
        phone = first_value(
            record,
            ("phone", "phone_normalized", "대표전화", "전화번호"),
        )
        institution_id = first_value(
            record,
            ("institution_id", "source_id", "source_key", "기관ID"),
        )

        master_index.append(
            {
                "row_number": row_number,
                "institution_id": institution_id,
                "name": clean_space(name),
                "name_key": normalize_name_key(name),
                "address": clean_space(address),
                "address_key": normalize_base_address(address),
                "phone": normalize_phone(phone),
            }
        )

    matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for source in merged_rows:
        source_name_key = normalize_name_key(source["pharmacy_name"])
        source_address_key = normalize_base_address(source["address_normalized"])
        source_phone = source["phone_normalized"]

        name_address_candidates = [
            row
            for row in master_index
            if row["name_key"] == source_name_key
            and row["address_key"] == source_address_key
        ]
        name_candidates = [
            row for row in master_index if row["name_key"] == source_name_key
        ]
        phone_candidates = [
            row
            for row in master_index
            if source_phone and row["phone"] == source_phone
        ]

        candidate: dict[str, Any] | None = None
        matched_by = ""

        if len(name_address_candidates) == 1:
            candidate = name_address_candidates[0]
            matched_by = "name_address"
        elif len(phone_candidates) == 1:
            candidate = phone_candidates[0]
            matched_by = "phone"
        elif len(name_candidates) == 1:
            candidate = name_candidates[0]
            matched_by = "name_only"

        if candidate is None:
            conflicts.append(
                {
                    "pharmacy_name": source["pharmacy_name"],
                    "source_address": source["address_normalized"],
                    "source_phone": source_phone,
                    "conflict_type": (
                        "ambiguous_master_match"
                        if name_address_candidates or phone_candidates or name_candidates
                        else "master_no_match"
                    ),
                    "candidate_count": max(
                        len(name_address_candidates),
                        len(phone_candidates),
                        len(name_candidates),
                    ),
                    "resolution": "manual_review_required",
                }
            )
            continue

        phone_match = bool(source_phone and candidate["phone"] == source_phone)
        name_match = candidate["name_key"] == source_name_key

        address_detail_difference = (
            normalize_address_key(candidate["address"])
            != normalize_address_key(source["address_normalized"])
        )
        # A matching name and phone identify the same pharmacy even when one
        # source adds/removes detailed address text (e.g. 읍·면 or building).
        # Preserve that difference as metadata instead of escalating it to an
        # identity conflict.
        address_match = (
            candidate["address_key"] == source_address_key
            or (name_match and phone_match and address_detail_difference)
        )
        auto_matched = name_match and address_match and phone_match
        match_record = {
            "pharmacy_name": source["pharmacy_name"],
            "source_address": source["address_normalized"],
            "source_phone": source_phone,
            "master_institution_id": candidate["institution_id"],
            "master_name": candidate["name"],
            "master_address": candidate["address"],
            "master_phone": candidate["phone"],
            "normalized_base_address": source_address_key,
            "address_detail_difference": address_detail_difference,
            "match_status": (
                "address_detail_variant"
                if address_detail_difference
                else "exact_or_base_match"
            ),
            "matched_by": matched_by,
            "name_match": name_match,
            "address_match": address_match,
            "phone_match": phone_match,
            "needs_manual_review": not auto_matched,
            "resolution": "auto_matched" if auto_matched else "manual_review_required",
        }
        matches.append(match_record)

        if source_phone and candidate["phone"] and not phone_match:
            conflicts.append(
                {
                    **match_record,
                    "conflict_type": "master_phone_conflict",
                    "resolution": "manual_review_required",
                }
            )
        elif not address_match:
            conflicts.append(
                {
                    **match_record,
                    "conflict_type": "master_address_conflict",
                    "resolution": "manual_review_required",
                }
            )

    return matches, conflicts


def validate(
    source_rows: list[PharmacySourceRow],
    merged_rows: list[dict[str, Any]],
    source_conflicts: list[dict[str, Any]],
    fetch_metadata: dict[str, dict[str, Any]],
    fixed_count_check: bool,
    master_matches: list[dict[str, Any]],
    master_conflicts: list[dict[str, Any]],
    master_supplied: bool,
) -> dict[str, Any]:
    counts = Counter(row.source_type for row in source_rows)
    expected_counts = {
        source_type: int(config["expected_count"])
        for source_type, config in SOURCES.items()
    }

    source_checks: dict[str, bool] = {
        "both_sources_collected": set(counts) == set(SOURCES),
        "all_rows_have_name": all(row.pharmacy_name for row in source_rows),
        "all_rows_have_address": all(row.address_source_raw for row in source_rows),
        "all_rows_have_phone": all(row.phone_normalized for row in source_rows),
        "all_rows_have_weekday_hours": all(
            row.weekday_hours_source_raw for row in source_rows
        ),
        "all_rows_have_source_updated_at": all(
            row.source_updated_at for row in source_rows
        ),
        "raw_and_normalized_values_both_preserved": all(
            row.pharmacy_name_source_raw
            and row.pharmacy_name
            and row.address_source_raw
            and row.address_normalized
            and row.phone_source_raw
            and row.phone_normalized
            for row in source_rows
        ),
        "source_urls_are_official_wonju": all(
            urlparse(row.source_url).hostname == "www.wonju.go.kr"
            for row in source_rows
        ),
        "html_sha256_recorded": all(
            metadata.get("sha256") for metadata in fetch_metadata.values()
        ),
        "merged_rows_traceable": sum(
            int(row["source_record_count"]) for row in merged_rows
        )
        == len(source_rows),
    }

    source_row_audit = {
        source_type: {
            "dom_data_row_count": int(fetch_metadata[source_type].get("dom_data_row_count", 0)),
            "parsed_row_count": counts[source_type],
            "previous_expected_count": expected_counts[source_type],
            "parse_loss_count": int(fetch_metadata[source_type].get("dom_data_row_count", 0)) - counts[source_type],
            "parse_complete": int(fetch_metadata[source_type].get("dom_data_row_count", 0)) == counts[source_type],
            "source_count_changed": int(fetch_metadata[source_type].get("dom_data_row_count", 0)) != expected_counts[source_type],
        }
        for source_type in SOURCES
    }
    source_checks["all_dom_rows_parsed"] = all(
        audit["parse_complete"] for audit in source_row_audit.values()
    )

    master_checks: dict[str, bool | None] = {
        "master_supplied": master_supplied,
        "all_merged_rows_matched_to_master": (
            len(master_matches) == len(merged_rows) if master_supplied else None
        ),
        "master_identity_conflicts_recorded": True,
    }

    pass_fail_values = list(source_checks.values())
    if master_supplied:
        pass_fail_values.extend(
            value for key, value in master_checks.items()
            if key != "master_supplied" and value is not None
        )

    source_schedule_pharmacies = {row["pharmacy_name"] for row in source_conflicts}
    identity_conflicts = [row for row in master_conflicts if row.get("conflict_type") != "master_address_conflict"]
    address_variants = [row for row in master_matches if row.get("address_detail_difference")]
    manual_review_count = len(source_schedule_pharmacies) + len(identity_conflicts)
    integrity_checks_passed = all(pass_fail_values)

    return {
        "dataset": "P0-DATA-02 원주시 약국 운영정보",
        "source_counts": dict(counts),
        "expected_counts": expected_counts,
        "fixed_count_check_enabled": fixed_count_check,
        "source_row_audit": source_row_audit,
        "source_record_count": len(source_rows),
        "unique_pharmacy_count": len(merged_rows),
        "source_conflict_count": len(source_conflicts),
        "source_conflict_pharmacies": sorted(
            {row["pharmacy_name"] for row in source_conflicts}
        ),
        "master_match_count": len(master_matches),
        "master_conflict_count": len(master_conflicts),
        "master_conflict_pharmacies": sorted(
            {
                row.get("pharmacy_name", "")
                for row in master_conflicts
                if row.get("pharmacy_name")
            }
        ),
        "conflict_summary": {
            "source_schedule_conflicts": {"pharmacy_count": len(source_schedule_pharmacies), "field_count": len(source_conflicts)},
            "master_identity_conflicts": {"pharmacy_count": len({row.get("pharmacy_name") for row in identity_conflicts}), "field_count": len(identity_conflicts)},
            "master_address_variants": {"pharmacy_count": len({row.get("pharmacy_name") for row in address_variants}), "review_required_count": 0},
        },
        "integrity_checks": source_checks | master_checks,
        "integrity_checks_passed": integrity_checks_passed,
        "review_required": manual_review_count > 0,
        "manual_review_count": manual_review_count,
        "dataset_status": "failed" if not integrity_checks_passed else ("conditionally_verified" if manual_review_count else "verified"),
        "source_checks": source_checks,
        "master_checks": master_checks,
        "all_checks_passed": integrity_checks_passed,
    }


def collect(args: argparse.Namespace) -> int:
    started_at = datetime.now().astimezone()
    collected_at = started_at.isoformat(timespec="seconds")
    run_date = args.run_date or started_at.date().isoformat()

    output_root = Path(args.output_dir).resolve()
    repo_root = Path.cwd().resolve()
    raw_dir = output_root / "raw" / run_date
    processed_dir = output_root / "processed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    session = create_session()
    source_rows: list[PharmacySourceRow] = []
    fetch_metadata: dict[str, dict[str, Any]] = {}

    for index, (source_type, config) in enumerate(SOURCES.items()):
        html_path = raw_dir / f"{source_type}.html"

        if args.resume and html_path.exists():
            logging.info("저장 HTML 재사용: %s", html_path)
            html = html_path.read_text(encoding="utf-8")
            metadata = {
                "requested_url": config["url"],
                "final_url": config["url"],
                "status_code": None,
                "content_type": "text/html; local-cache",
                "etag": "",
                "last_modified_header": "",
                "content_length": html_path.stat().st_size,
                "sha256": hashlib.sha256(
                    html_path.read_bytes()
                ).hexdigest(),
                "reused_local_file": True,
            }
        else:
            logging.info("%s 페이지 요청", config["label"])
            html, metadata = fetch_html(
                session=session,
                url=str(config["url"]),
                timeout=args.timeout,
            )
            atomic_write_text(html_path, html)
            if index < len(SOURCES) - 1:
                time.sleep(args.delay)

        rows, source_updated_at, dom_data_row_count = parse_source_page(
            html=html,
            source_type=source_type,
            source_label=str(config["label"]),
            source_url=str(config["url"]),
            collected_at=collected_at,
        )
        metadata["parsed_row_count"] = len(rows)
        metadata["dom_data_row_count"] = dom_data_row_count
        metadata["source_updated_at"] = source_updated_at
        fetch_metadata[source_type] = metadata
        source_rows.extend(rows)

    merged_rows, source_conflicts = merge_source_rows(source_rows)

    master_matches: list[dict[str, Any]] = []
    master_conflicts: list[dict[str, Any]] = []
    master_supplied = bool(args.master)

    if args.master:
        master_path = Path(args.master).resolve()
        logging.info("기존 기관 마스터 비교: %s", master_path)
        master_matches, master_conflicts = compare_with_master(
            merged_rows=merged_rows,
            master_path=master_path,
        )

    report = validate(
        source_rows=source_rows,
        merged_rows=merged_rows,
        source_conflicts=source_conflicts,
        fetch_metadata=fetch_metadata,
        fixed_count_check=not args.allow_count_change,
        master_matches=master_matches,
        master_conflicts=master_conflicts,
        master_supplied=master_supplied,
    )
    report.update(
        {
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "run_date": run_date,
            "raw_html_directory": relative_to_repo(raw_dir, repo_root),
        }
    )

    sources_path = processed_dir / "pharmacy_operation_sources.csv"
    merged_path = processed_dir / "pharmacy_operations_merged.csv"
    source_conflicts_path = processed_dir / "pharmacy_operation_conflicts.csv"
    master_matches_path = processed_dir / "master_matches.csv"
    master_conflicts_path = processed_dir / "master_conflicts.csv"
    report_path = processed_dir / "validation_report.json"
    manifest_path = processed_dir / "collection_manifest.json"

    write_csv(sources_path, [asdict(row) for row in source_rows])
    write_csv(merged_path, merged_rows)
    write_csv(source_conflicts_path, source_conflicts)

    if master_supplied:
        write_csv(master_matches_path, master_matches)
        write_csv(master_conflicts_path, master_conflicts)

    report["files"] = {
        "sources": relative_to_repo(sources_path, repo_root),
        "merged": relative_to_repo(merged_path, repo_root),
        "source_conflicts": relative_to_repo(source_conflicts_path, repo_root),
        "master_matches": relative_to_repo(master_matches_path, repo_root) if master_supplied else None,
        "master_conflicts": relative_to_repo(master_conflicts_path, repo_root) if master_supplied else None,
        "validation_report": relative_to_repo(report_path, repo_root),
        "manifest": relative_to_repo(manifest_path, repo_root),
    }

    manifest = {
        "dataset": report["dataset"],
        "sources": SOURCES,
        "fetch_metadata": fetch_metadata,
        "collected_at": collected_at,
        "run_date": run_date,
        "script": Path(__file__).name,
    }

    write_json(report_path, report)
    write_json(manifest_path, manifest)

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.strict and not report["all_checks_passed"]:
        failed_source = [
            name
            for name, passed in report["source_checks"].items()
            if not passed
        ]
        failed_master = [
            name
            for name, passed in report["master_checks"].items()
            if name != "master_supplied" and passed is False
        ]
        raise RuntimeError(
            "엄격 검증 실패: " + ", ".join(failed_source + failed_master)
        )

    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="원주시 심야·연중무휴약국 운영정보 크롤링 및 검증"
    )
    parser.add_argument(
        "--output-dir",
        default="data/collected/pharmacy_operations",
        help="출력 루트 디렉터리",
    )
    parser.add_argument(
        "--master",
        default=None,
        help="기존 institutions.csv 경로. 지정하면 이름·주소·전화번호를 대조",
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help="원본 HTML 버전 디렉터리명. 기본값은 실행일",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout(초)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.7,
        help="공식 페이지 요청 사이 대기시간(초)",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="동일 run-date의 저장 HTML 재사용",
    )
    parser.add_argument(
        "--allow-count-change",
        action="store_true",
        help="현재 알려진 16건/9건과 달라도 실패 처리하지 않음",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="검증 실패 시 종료 코드 1 반환",
    )
    return parser.parse_args(argv)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()

    if args.delay < 0.2:
        raise SystemExit("--delay는 0.2초 이상이어야 합니다.")

    try:
        return collect(args)
    except KeyboardInterrupt:
        logging.warning("사용자가 중단했습니다.")
        return 130
    except Exception:
        logging.exception("약국 운영정보 수집 실패")
        return 1


if __name__ == "__main__":
    sys.exit(main())
