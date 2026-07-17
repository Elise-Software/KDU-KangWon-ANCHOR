from __future__ import annotations

import re
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .common import P1_ROOT, now_iso, relative, sha256_bytes, stable_id, today, write_csv, write_json


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131 Safari/537.36 "
    "KDU-KangWon-ANCHOR-P1/1.0"
)
MANIFEST_COLUMNS = [
    "doc_id", "title", "category", "url", "reference_date",
    "reference_date_basis", "retrieved_at", "raw_path", "sha256",
    "http_status", "content_type", "content_length", "collection_status",
]


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    query = sorted((key, value) for key, value in parse_qsl(parsed.query) if value)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", urlencode(query), ""))


def get(url: str, timeout: int) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            try:
                response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            except requests.exceptions.SSLError:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, verify=False)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def discover(config: dict) -> list[str]:
    allowed_hosts = set(config["allowed_hosts"])
    sections = tuple(config["allowed_sections"])
    per_section: dict[str, list[str]] = {section: [] for section in sections}
    for seed in config["seed_urls"]:
        response = get(seed, config["request_timeout_seconds"])
        soup = BeautifulSoup(response.content, "lxml")
        for anchor in soup.select("a[href]"):
            url = canonical_url(urljoin(seed, anchor["href"]))
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc not in allowed_hosts:
                continue
            section = next((item for item in sections if parsed.path.startswith(item)), None)
            if not section:
                continue
            params = dict(parse_qsl(parsed.query))
            if parsed.path.endswith("/contents.do") and params.get("key", "").isdigit():
                per_section[section].append(url)
    maximum = int(config["maximum_documents"])
    selected: list[str] = [canonical_url(url) for url in config.get("mandatory_urls", [])]
    while len(selected) < maximum and any(per_section.values()):
        for section in sections:
            values = sorted(set(per_section[section]) - set(selected))
            if values:
                selected.append(values.pop(0))
                per_section[section] = values
                if len(selected) >= maximum:
                    break
    return selected


def extract_metadata(url: str, content: bytes) -> tuple[str, str, str, str]:
    soup = BeautifulSoup(content, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    title = re.sub(r"\s*[-|]\s*원주시.*$", "", title).strip() or title
    content_node = soup.select_one("#contents") or soup.select_one(".sub_contents") or soup.select_one("main")
    text = content_node.get_text(" ", strip=True) if content_node else soup.get_text(" ", strip=True)
    match = re.search(r"최종\s*수정일\s*[:：]?\s*(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", text)
    if match:
        reference_date = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        basis = "official_last_modified"
    else:
        reviewed = re.search(
            r"Page\s+last\s+reviewed\s*[:：]?\s*(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})",
            text,
            re.IGNORECASE,
        )
        if reviewed:
            month = datetime.strptime(reviewed.group(2)[:3], "%b").month
            reference_date = f"{int(reviewed.group(3)):04d}-{month:02d}-{int(reviewed.group(1)):02d}"
            basis = "official_last_reviewed"
        else:
            reference_date = today()
            basis = "collection_date_no_published_date"
    parsed = urlparse(url)
    category = (
        "mental_health" if parsed.netloc == "loveme.yonsei.kr"
        else "clinical_guidance" if parsed.netloc in {
            "health.kdca.go.kr", "www.e-gen.or.kr", "www.nice.org.uk", "www.nhs.uk"
        }
        else "health" if parsed.path.startswith("/health/")
        else "welfare"
    )
    return title, category, reference_date, basis


def collect_one(url: str, raw_dir: Path, timeout: int) -> dict[str, object]:
    response = get(url, timeout)
    content = response.content
    title, category, reference_date, basis = extract_metadata(url, content)
    doc_id = stable_id("p1doc", url)
    raw_path = raw_dir / f"{doc_id.replace(':', '_')}.html"
    raw_path.write_bytes(content)
    return {
        "doc_id": doc_id,
        "title": title,
        "category": category,
        "url": url,
        "reference_date": reference_date,
        "reference_date_basis": basis,
        "retrieved_at": now_iso(),
        "raw_path": relative(raw_path),
        "sha256": sha256_bytes(content),
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "content_length": len(content),
        "collection_status": "collected",
    }


def run(config: dict, strict: bool = False) -> dict:
    raw_dir = P1_ROOT / "raw" / "pages"
    raw_dir.mkdir(parents=True, exist_ok=True)
    urls = discover(config)
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=int(config["workers"])) as executor:
        futures = {
            executor.submit(collect_one, url, raw_dir, int(config["request_timeout_seconds"])): url
            for url in urls
        }
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"url": futures[future], "error": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda row: str(row["url"]))
    write_csv(P1_ROOT / "raw" / "document_manifest.csv", rows, MANIFEST_COLUMNS)
    write_csv(P1_ROOT / "reports" / "collection_errors.csv", errors, ["url", "error"])
    checks = {
        "minimum_document_count": len(rows) >= int(config["minimum_documents"]),
        "urls_unique": len(rows) == len({row["url"] for row in rows}),
        "hashes_valid": all(re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])) for row in rows),
        "raw_files_present": all((Path(__file__).resolve().parents[2] / str(row["raw_path"])).is_file() for row in rows),
        "official_urls_only": all(urlparse(str(row["url"])).netloc in set(config["allowed_hosts"]) for row in rows),
        "collection_errors_absent": not errors,
    }
    report = {
        "discovered_url_count": len(urls),
        "collected_document_count": len(rows),
        "error_count": len(errors),
        "integrity_checks": checks,
        "integrity_checks_passed": all(checks.values()),
    }
    write_json(P1_ROOT / "reports" / "collection_report.json", report)
    if strict and not report["integrity_checks_passed"]:
        raise RuntimeError(f"P1 collection strict checks failed: {report}")
    return report
