"""Collect P0-DATA-03 evidence only from the approved seed URL list."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning


REPO_ROOT = Path(__file__).resolve().parents[1]
PHONE_RE = re.compile(r"(?<!\d)(?:0\d{1,2}[-.\s]?)?\d{3,4}[-.\s]?\d{4}(?!\d)")
UPDATED_RE = re.compile(r"(?:최종\s*수정|수정일|등록일)\s*[:：]?\s*(20\d{2}[.\-/년\s]+\d{1,2}[.\-/월\s]+\d{1,2})")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader(); writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=Path, default=REPO_ROOT / "config/p0_data_03_seed_urls.json")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "data/collected/public_health")
    parser.add_argument("--run-date", default=datetime.now().date().isoformat())
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    seeds = json.loads(args.seeds.read_text(encoding="utf-8"))
    raw_dir = args.output_dir / "raw" / args.run_date
    pages: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    session = requests.Session()
    session.headers["User-Agent"] = "KDU-KangWon-ANCHOR P0-DATA-03 collector (+public-service-data)"
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for index, seed in enumerate(seeds, start=1):
        url, scope = seed["url"], seed["scope"]
        try:
            tls_verification = "verified"
            original_tls_error = ""
            try:
                response = session.get(url, timeout=args.timeout)
            except requests.exceptions.SSLError as error:
                # Some approved public-sector sites provide a certificate chain
                # unavailable in this runtime. Retry only the same approved seed;
                # record the weaker transport verification in the manifest.
                disable_warnings(InsecureRequestWarning)
                response = session.get(url, timeout=args.timeout, verify=False)
                tls_verification = "unverified_fallback"
                original_tls_error = str(error)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            html = response.text
            raw_path = raw_dir / f"seed_{index:02d}.html"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(html.encode("utf-8"))
            soup = BeautifulSoup(html, "html.parser")
            text = " ".join(soup.stripped_strings)
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            phones = sorted(set(PHONE_RE.findall(text)))
            updated = UPDATED_RE.search(text)
            pages.append({"seed_id": index, "scope": scope, "source_url": url, "final_url": response.url, "status": "collected", "final_fetch_status": "collected", "http_status": response.status_code, "tls_compat_retry_used": tls_verification == "unverified_fallback", "tls_verification_mode": tls_verification, "original_tls_error": original_tls_error, "tls_verification": tls_verification, "title": title, "source_updated_at": updated.group(1) if updated else "", "phone_candidates": ";".join(phones), "raw_html": raw_path.resolve().relative_to(REPO_ROOT).as_posix(), "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(), "collected_at": collected_at})
            evidence.append({"seed_id": index, "scope": scope, "source_url": url, "title": title, "source_updated_at": updated.group(1) if updated else "", "page_text": text[:12000], "phone_candidates": ";".join(phones), "collected_at": collected_at})
        except requests.RequestException as error:
            pages.append({"seed_id": index, "scope": scope, "source_url": url, "status": "fetch_error", "error": str(error), "collected_at": collected_at})
    processed = args.output_dir / "processed"
    pages_path, evidence_path = processed / "p0_data_03_source_pages.csv", processed / "p0_data_03_page_evidence.csv"
    write_csv(pages_path, pages); write_csv(evidence_path, evidence)
    report = {"dataset":"P0-DATA-03 Wonju public-health source collection", "seed_count":len(seeds), "collected_page_count":sum(p["status"] == "collected" for p in pages), "fetch_error_count":sum(p["status"] != "collected" for p in pages), "scope_restricted_to_seed_urls":True, "files":{"source_pages":pages_path.resolve().relative_to(REPO_ROOT).as_posix(), "page_evidence":evidence_path.resolve().relative_to(REPO_ROOT).as_posix(), "raw_html_directory":raw_dir.resolve().relative_to(REPO_ROOT).as_posix()}}
    (processed / "p0_data_03_collection_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
