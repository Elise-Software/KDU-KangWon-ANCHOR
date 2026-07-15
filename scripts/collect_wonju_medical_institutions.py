#!/usr/bin/env python3
"""
P0-DATA-01 원주시 관내의료기관 원천 수집기.

역할:
- 원주시 보건소 공개 목록의 전체 페이지를 순회한다.
- 응답 HTML을 페이지별로 그대로 보존한다.
- 화면 표의 원문 값을 최소 가공 상태로 JSONL/CSV에 저장한다.
- 좌표 추정, 전화번호 정규화, 중복 제거 등 전처리는 수행하지 않는다.

페이지 URL:
https://www.wonju.go.kr/health/selectResrceListPA3.do?key=1787

기본 동작:
- GET 요청을 먼저 사용한다.
- 사이트가 GET 페이지 이동을 거부하면 사용자가 제공한 POST 폼으로 자동 재시도한다.
- 브라우저 Cookie/JSESSIONID는 사용하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://www.wonju.go.kr/health/selectResrceListPA3.do"
BASE_PARAMS = {
    "key": "1787",
    "sc1": "10",
    "si1": "27",
}
POST_FORM = {
    "ad1": "",
    "si2": "",
    "sc3": "",
    "sc10": "ALL",
    "sc2": "",
}
EXPECTED_HEADERS = ("번호", "구분", "명칭", "주소", "대표전화")
TOTAL_RE = re.compile(
    r"전체\s*([\d,]+)\s*건\s*\[\s*(\d+)\s*/\s*(\d+)\s*페이지\s*\]"
)


@dataclass(slots=True)
class RawInstitution:
    source: str
    source_id: str | None
    number_raw: str
    category_raw: str
    name_raw: str
    address_raw: str
    phone_raw: str
    detail_url: str | None
    map_action_raw: str | None
    page: int
    row_index: int
    source_url: str
    http_method: str
    collected_at: str


def clean_display_text(value: str) -> str:
    """HTML 표시 문자열의 줄바꿈과 연속 공백만 정리한다."""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def parse_int(value: str) -> int | None:
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def create_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
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
            "Referer": f"{BASE_URL}?key=1787",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


def request_html(
    session: requests.Session,
    page: int,
    timeout: float,
    method: str,
) -> tuple[str, str, str]:
    params = {**BASE_PARAMS, "cpn": str(page)}

    if method == "get":
        response = session.get(BASE_URL, params=params, timeout=timeout)
    elif method == "post":
        response = session.post(
            BASE_URL,
            params=params,
            data=POST_FORM,
            timeout=timeout,
        )
    else:
        raise ValueError(f"지원하지 않는 요청 방식: {method}")

    response.raise_for_status()

    if not response.encoding or response.encoding.lower() in {
        "iso-8859-1",
        "ascii",
    }:
        response.encoding = response.apparent_encoding or "utf-8"

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        raise RuntimeError(f"HTML이 아닌 응답: {content_type}")

    return response.text, response.url, method.upper()


def parse_total_information(soup: BeautifulSoup) -> tuple[int, int, int]:
    text = clean_display_text(soup.get_text(" ", strip=True))
    match = TOTAL_RE.search(text)
    if not match:
        raise RuntimeError("'전체 N건 [현재/전체 페이지]' 문구를 찾지 못했습니다.")

    return (
        int(match.group(1).replace(",", "")),
        int(match.group(2)),
        int(match.group(3)),
    )


def find_result_table(soup: BeautifulSoup) -> Tag:
    for table in soup.find_all("table"):
        headers = [
            clean_display_text(th.get_text(" ", strip=True))
            for th in table.find_all("th")
        ]
        matched = sum(
            1
            for expected in EXPECTED_HEADERS
            if any(expected in header for header in headers)
        )
        if matched >= 4:
            return table

    raise RuntimeError("의료기관 목록 테이블을 찾지 못했습니다.")


def extract_detail_url(name_cell: Tag) -> str | None:
    link = name_cell.find("a", href=True)
    return urljoin(BASE_URL, link["href"]) if link else None


def extract_source_id(detail_url: str | None) -> str | None:
    if not detail_url:
        return None

    query = parse_qs(urlparse(detail_url).query)
    for key in ("resrceNo", "resourceNo", "seq", "idx", "no"):
        values = query.get(key)
        if values:
            return values[0]
    return None


def extract_map_action_raw(map_cell: Tag) -> str | None:
    """
    위치보기 요소의 속성을 원문 그대로 보존한다.

    중요:
    이 사이트의 onclick에는 위·경도가 아니라 주소/기관명이 포함될 수 있다.
    따라서 여기서는 숫자를 좌표로 해석하지 않는다.
    """
    fragments: list[str] = []

    for element in [map_cell, *map_cell.find_all(True)]:
        for attr in (
            "href",
            "onclick",
            "data-lat",
            "data-lng",
            "data-x",
            "data-y",
            "data-address",
        ):
            value = element.get(attr)
            if value:
                fragments.append(f"{attr}={value}")

    result = " | ".join(dict.fromkeys(fragments))
    return result or None


def parse_page(
    html: str,
    requested_page: int,
    source_url: str,
    http_method: str,
    collected_at: str,
) -> tuple[list[RawInstitution], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    total_count, current_page, total_pages = parse_total_information(soup)

    if current_page != requested_page:
        raise RuntimeError(
            f"요청 페이지와 응답 페이지 불일치: "
            f"requested={requested_page}, response={current_page}"
        )

    table = find_result_table(soup)
    rows: list[RawInstitution] = []

    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 5:
            continue

        values = [
            clean_display_text(cell.get_text(" ", strip=True))
            for cell in cells
        ]

        if parse_int(values[0]) is None:
            continue

        detail_url = extract_detail_url(cells[2])
        map_cell = cells[5] if len(cells) >= 6 else cells[-1]

        rows.append(
            RawInstitution(
                source="wonju_health_medical_institution",
                source_id=extract_source_id(detail_url),
                number_raw=values[0],
                category_raw=values[1],
                name_raw=values[2],
                address_raw=values[3],
                phone_raw=values[4],
                detail_url=detail_url,
                map_action_raw=extract_map_action_raw(map_cell),
                page=requested_page,
                row_index=len(rows) + 1,
                source_url=source_url,
                http_method=http_method,
                collected_at=collected_at,
            )
        )

    if not rows:
        raise RuntimeError(f"{requested_page}페이지에서 데이터 행을 찾지 못했습니다.")

    return rows, total_count, total_pages


def fetch_and_parse_page(
    session: requests.Session,
    page: int,
    timeout: float,
    method_option: str,
    collected_at: str,
) -> tuple[str, list[RawInstitution], int, int, str, str]:
    methods = ["get", "post"] if method_option == "auto" else [method_option]
    errors: list[str] = []

    for method in methods:
        try:
            html, source_url, used_method = request_html(
                session=session,
                page=page,
                timeout=timeout,
                method=method,
            )
            rows, total_count, total_pages = parse_page(
                html=html,
                requested_page=page,
                source_url=source_url,
                http_method=used_method,
                collected_at=collected_at,
            )
            return html, rows, total_count, total_pages, source_url, used_method
        except Exception as exc:
            errors.append(f"{method.upper()}: {exc}")

    raise RuntimeError(
        f"{page}페이지 GET/POST 수집 실패: " + " | ".join(errors)
    )


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def write_json(path: Path, value: object) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2),
    )


def write_jsonl(path: Path, rows: list[RawInstitution]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    temp.replace(path)


def write_csv(path: Path, rows: list[RawInstitution]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [asdict(row) for row in rows]
    if not records:
        raise RuntimeError("CSV로 저장할 데이터가 없습니다.")

    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    temp.replace(path)


def load_page_checkpoint(path: Path) -> list[RawInstitution]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [RawInstitution(**item) for item in data]


def validate_raw_collection(
    rows: list[RawInstitution],
    source_total_count: int,
    source_total_pages: int,
) -> dict:
    numbers = [parse_int(row.number_raw) for row in rows]
    valid_numbers = [number for number in numbers if number is not None]
    unique_pages = sorted({row.page for row in rows})
    source_ids = [row.source_id for row in rows if row.source_id]

    missing_numbers: list[int] = []
    if valid_numbers:
        missing_numbers = sorted(
            set(range(min(valid_numbers), max(valid_numbers) + 1))
            - set(valid_numbers)
        )

    duplicate_numbers = len(valid_numbers) - len(set(valid_numbers))
    duplicate_source_ids = len(source_ids) - len(set(source_ids))

    return {
        "dataset": "P0-DATA-01 원주시 관내의료기관 원천 수집",
        "source_url": f"{BASE_URL}?key=1787",
        "source_reported_count": source_total_count,
        "source_reported_pages": source_total_pages,
        "collected_row_count": len(rows),
        "collected_page_count": len(unique_pages),
        "page_numbers": unique_pages,
        "number_min": min(valid_numbers) if valid_numbers else None,
        "number_max": max(valid_numbers) if valid_numbers else None,
        "missing_list_numbers": missing_numbers,
        "duplicate_number_count": duplicate_numbers,
        "duplicate_source_id_count": duplicate_source_ids,
        "field_missing_counts": {
            "number_raw": sum(not row.number_raw for row in rows),
            "category_raw": sum(not row.category_raw for row in rows),
            "name_raw": sum(not row.name_raw for row in rows),
            "address_raw": sum(not row.address_raw for row in rows),
            "phone_raw": sum(not row.phone_raw for row in rows),
            "detail_url": sum(not row.detail_url for row in rows),
            "source_id": sum(not row.source_id for row in rows),
        },
        "checks": {
            "row_count_matches_source": len(rows) == source_total_count,
            "all_pages_collected": len(unique_pages) == source_total_pages,
            "number_sequence_complete": len(missing_numbers) == 0,
            "number_unique": duplicate_numbers == 0,
            "source_id_unique": duplicate_source_ids == 0,
        },
    }


def collect(args: argparse.Namespace) -> int:
    started_at = datetime.now().astimezone()
    collected_at = started_at.isoformat(timespec="seconds")
    run_date = args.run_date or started_at.date().isoformat()

    output_root = Path(args.output_dir).resolve()
    raw_root = output_root / "raw" / "medical_institutions" / "runs" / run_date
    pages_dir = raw_root / "pages"
    checkpoints_dir = raw_root / "page_rows"
    pages_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_dir.mkdir(parents=True, exist_ok=True)

    session = create_session()
    all_rows: list[RawInstitution] = []

    first_html_path = pages_dir / "page_001.html"
    first_checkpoint_path = checkpoints_dir / "page_001.json"

    if args.resume and first_checkpoint_path.exists() and first_html_path.exists():
        logging.info("저장된 1페이지 체크포인트 재사용")
        first_rows = load_page_checkpoint(first_checkpoint_path)
        first_html = first_html_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(first_html, "html.parser")
        source_total_count, _, source_total_pages = parse_total_information(soup)
    else:
        logging.info("1페이지 요청")
        (
            first_html,
            first_rows,
            source_total_count,
            source_total_pages,
            _,
            _,
        ) = fetch_and_parse_page(
            session=session,
            page=1,
            timeout=args.timeout,
            method_option=args.method,
            collected_at=collected_at,
        )
        atomic_write_text(first_html_path, first_html)
        write_json(first_checkpoint_path, [asdict(row) for row in first_rows])

    all_rows.extend(first_rows)

    last_page = (
        min(source_total_pages, args.max_pages)
        if args.max_pages
        else source_total_pages
    )

    logging.info(
        "원천 표시: %s건 / %s페이지, 이번 실행: 1~%s페이지",
        source_total_count,
        source_total_pages,
        last_page,
    )

    for page in range(2, last_page + 1):
        html_path = pages_dir / f"page_{page:03d}.html"
        checkpoint_path = checkpoints_dir / f"page_{page:03d}.json"

        if args.resume and checkpoint_path.exists() and html_path.exists():
            logging.info("[%s/%s] 체크포인트 재사용", page, last_page)
            all_rows.extend(load_page_checkpoint(checkpoint_path))
            continue

        logging.info("[%s/%s] 페이지 요청", page, last_page)
        (
            html,
            page_rows,
            page_total_count,
            page_total_pages,
            _,
            _,
        ) = fetch_and_parse_page(
            session=session,
            page=page,
            timeout=args.timeout,
            method_option=args.method,
            collected_at=collected_at,
        )

        if (
            page_total_count != source_total_count
            or page_total_pages != source_total_pages
        ):
            logging.warning(
                "수집 중 목록 건수 변경: first=%s/%s, page%s=%s/%s",
                source_total_count,
                source_total_pages,
                page,
                page_total_count,
                page_total_pages,
            )

        atomic_write_text(html_path, html)
        write_json(checkpoint_path, [asdict(row) for row in page_rows])
        all_rows.extend(page_rows)
        time.sleep(args.delay)

    raw_jsonl = raw_root / "raw_rows.jsonl"
    raw_csv = raw_root / "raw_rows.csv"
    crawl_report_path = raw_root / "crawl_report.json"

    write_jsonl(raw_jsonl, all_rows)
    write_csv(raw_csv, all_rows)

    report = validate_raw_collection(
        rows=all_rows,
        source_total_count=source_total_count,
        source_total_pages=source_total_pages,
    )
    report.update(
        {
            "run_date": run_date,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "partial_collection": bool(
                args.max_pages and args.max_pages < source_total_pages
            ),
            "request_method_option": args.method,
            "coordinate_policy": (
                "원천 지도 속성의 숫자를 좌표로 해석하지 않음. "
                "좌표는 별도 지오코딩 단계에서 생성."
            ),
            "files": {
                "raw_jsonl": str(raw_jsonl),
                "raw_csv": str(raw_csv),
                "pages_dir": str(pages_dir),
            },
        }
    )
    write_json(crawl_report_path, report)

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.strict and not report["partial_collection"]:
        failed = [
            name
            for name, passed in report["checks"].items()
            if not passed
        ]
        if failed:
            raise RuntimeError(
                "원천 수집 엄격 검증 실패: " + ", ".join(failed)
            )

    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="원주시 관내의료기관 원천 HTML 및 표 데이터 수집"
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="출력 루트 디렉터리 (기본값: data)",
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help="원천 데이터 버전 디렉터리명 (기본값: 실행일 YYYY-MM-DD)",
    )
    parser.add_argument(
        "--method",
        choices=("auto", "get", "post"),
        default="auto",
        help="HTTP 방식. auto는 GET 실패 시 POST 재시도",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.7,
        help="페이지 신규 요청 사이 대기시간(초)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout(초)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="시험용 최대 페이지 수. 전체 수집 시 생략",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="저장된 HTML/체크포인트 재사용",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="전체 수집 검증 실패 시 오류 종료",
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
    if args.max_pages is not None and args.max_pages < 1:
        raise SystemExit("--max-pages는 1 이상이어야 합니다.")

    try:
        return collect(args)
    except KeyboardInterrupt:
        logging.warning("중단됨. 동일 명령으로 재실행하면 체크포인트에서 이어집니다.")
        return 130
    except Exception:
        logging.exception("원천 수집 실패")
        return 1


if __name__ == "__main__":
    sys.exit(main())
