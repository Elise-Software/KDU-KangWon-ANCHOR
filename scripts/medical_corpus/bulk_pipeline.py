from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from lxml import etree

from .pipeline import (
    html_to_text, make_chunks, normalize_space, parse_medlineplus_medical_test,
    parse_korean_public_document, parse_pmc_article, sha256_bytes, sha256_text, stable_id,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0


def parse_apache_size(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMG])", value.strip(), re.I)
    if not match:
        return 0
    return int(float(match.group(1)) * {"K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2).upper()])


def discover_packages(config: dict[str, Any], output: Path) -> list[dict[str, Any]]:
    pmc = config["pmc"]
    catalog_dir = output / "catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    index_path = catalog_dir / "pmc_oa_bulk_index.html"
    if index_path.is_file() and index_path.stat().st_size > 1000:
        index_content = index_path.read_bytes()
    else:
        response = requests.get(
            pmc["bulk_index_url"], headers={"User-Agent": pmc["user_agent"]},
            timeout=(20, int(pmc["request_timeout_seconds"])),
        )
        response.raise_for_status()
        index_content = response.content
        index_path.write_bytes(index_content)
    soup = BeautifulSoup(index_content, "lxml")
    candidates: list[dict[str, Any]] = []
    for anchor in soup.find_all("a"):
        name = str(anchor.get("href", ""))
        if not name.endswith(".tar.gz"):
            continue
        tail = " ".join(str(node).strip() for node in anchor.next_siblings if str(node).strip())
        size_match = re.search(r"\b(\d+(?:\.\d+)?[KMG])\b", tail, re.I)
        size = parse_apache_size(size_match.group(1)) if size_match else 0
        baseline = re.match(r"oa_comm_xml\.(PMC\d+xxxxxx)\.baseline\.(\d{4}-\d{2}-\d{2})\.tar\.gz", name)
        incremental = re.match(r"oa_comm_xml\.incr\.(\d{4}-\d{2}-\d{2})\.tar\.gz", name)
        if baseline:
            candidates.append({"name": name, "kind": "baseline", "range": baseline.group(1), "date": baseline.group(2), "listed_bytes": size})
        elif incremental:
            candidates.append({"name": name, "kind": "incremental", "range": "all", "date": incremental.group(1), "listed_bytes": size})
    latest: dict[str, dict[str, Any]] = {}
    for row in candidates:
        if row["kind"] != "baseline":
            continue
        previous = latest.get(row["range"])
        if previous is None or row["date"] > previous["date"]:
            latest[row["range"]] = row
    earliest_baseline = min(row["date"] for row in latest.values())
    selected = list(latest.values()) + [
        row for row in candidates if row["kind"] == "incremental" and row["date"] >= earliest_baseline
    ]
    selected.sort(key=lambda row: (row["date"], 0 if row["kind"] == "baseline" else 1, row["name"]))
    budget = int(config["storage"]["raw_download_budget_bytes"])
    projected = sum(row["listed_bytes"] for row in selected)
    if projected > budget:
        raise RuntimeError(f"PMC archive plan exceeds raw budget: {projected} > {budget}")
    for sequence, row in enumerate(selected):
        row.update({
            "sequence": sequence, "url": urljoin(pmc["bulk_index_url"], row["name"]),
            "status": "pending", "downloaded_bytes": 0, "sha256": "", "updated_at": now(),
        })
    plan = {
        "schema_version": config["schema_version"], "generated_at": now(),
        "package_count": len(selected), "projected_archive_bytes": projected,
        "raw_download_budget_bytes": budget, "earliest_baseline_date": earliest_baseline,
        "packages": selected,
    }
    write_json(catalog_dir / "pmc_package_plan.json", plan)
    return selected


def load_plan(output: Path) -> dict[str, Any]:
    return json.loads((output / "catalogs" / "pmc_package_plan.json").read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def save_plan(output: Path, plan: dict[str, Any]) -> None:
    with MANIFEST_LOCK:
        plan["updated_at"] = now()
        write_json(output / "catalogs" / "pmc_package_plan.json", plan)


def download_one(row: dict[str, Any], plan: dict[str, Any], config: dict[str, Any], output: Path) -> None:
    destination_dir = output / "raw" / "pmc_oa_comm" / "packages"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / row["name"]
    partial = destination.with_suffix(destination.suffix + ".part")
    expected = int(row.get("listed_bytes") or 0)
    if destination.is_file() and (not expected or destination.stat().st_size >= int(expected * 0.98)):
        row.update({"status": "complete", "downloaded_bytes": destination.stat().st_size, "sha256": sha256_file(destination), "updated_at": now()})
        save_plan(output, plan)
        return
    attempts = int(config["pmc"].get("download_retry_attempts", 5))
    last_error: Exception | None = None
    for attempt in range(attempts):
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": config["pmc"]["user_agent"]}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        try:
            with requests.get(
                row["url"], headers=headers,
                timeout=int(config["pmc"]["request_timeout_seconds"]), stream=True,
            ) as response:
                response.raise_for_status()
                append = existing > 0 and response.status_code == 206
                mode = "ab" if append else "wb"
                with partial.open(mode) as handle:
                    for block in response.iter_content(8 * 1024 * 1024):
                        if block:
                            handle.write(block)
            last_error = None
            break
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            row.update({
                "status": "retrying", "retry_attempt": attempt + 1,
                "downloaded_bytes": partial.stat().st_size if partial.exists() else 0,
                "error": repr(exc), "updated_at": now(),
            })
            save_plan(output, plan)
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 16))
    if last_error is not None:
        raise last_error
    os.replace(partial, destination)
    actual = destination.stat().st_size
    row.update({"status": "complete", "downloaded_bytes": actual, "sha256": sha256_file(destination), "updated_at": now()})
    save_plan(output, plan)


def download_packages(config: dict[str, Any], output: Path) -> dict[str, Any]:
    plan = load_plan(output)
    stop_before = int(config["storage"]["stop_before_bytes"])
    projected_remaining = sum(
        int(row.get("listed_bytes") or 0) for row in plan["packages"] if row.get("status") != "complete"
    )
    current = directory_bytes(output)
    if current + projected_remaining > stop_before:
        raise RuntimeError(f"capacity guard: {current} + {projected_remaining} > {stop_before}")
    failures: list[dict[str, str]] = []
    pending = [row for row in plan["packages"] if row.get("status") != "complete"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(config["pmc"]["download_workers"])) as executor:
        futures = {executor.submit(download_one, row, plan, config, output): row for row in pending}
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            try:
                future.result()
            except Exception as exc:
                row.update({"status": "failed", "error": repr(exc), "updated_at": now()})
                failures.append({"name": row["name"], "error": repr(exc)})
                save_plan(output, plan)
    report = {
        "generated_at": now(), "package_count": len(plan["packages"]),
        "complete_count": sum(row.get("status") == "complete" for row in plan["packages"]),
        "failed_count": len(failures), "downloaded_bytes": sum(int(row.get("downloaded_bytes") or 0) for row in plan["packages"]),
        "directory_bytes": directory_bytes(output), "stop_before_bytes": stop_before,
        "failures": failures,
    }
    write_json(output / "reports" / "bulk_download_report.json", report)
    return report


SCAN_FIELDS = (
    "pmcid", "archive_name", "archive_sequence", "member_name", "domain",
    "stable_rank", "title", "journal", "language", "scan_error",
)


def xml_value(root: etree._Element, expression: str) -> str:
    value = root.xpath(f"string({expression})")
    return re.sub(r"\s+", " ", str(value or "")).strip()


def classify_domain(text: str, balance: dict[str, Any]) -> str:
    normalized = normalize_space(text).casefold()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    best_domain = "general_other"
    best_score = 0
    for domain, terms in balance["domains"].items():
        if not terms:
            continue
        score = 0
        for raw_term in terms:
            term = normalize_space(raw_term).casefold()
            if not term:
                continue
            if " " in term:
                # Multi-word concepts must occur as complete phrases.
                pattern = r"(?<![a-z0-9])" + r"\s+".join(
                    re.escape(part) for part in term.split()
                ) + r"(?![a-z0-9])"
                score += len(re.findall(pattern, normalized))
            elif len(term) <= 4:
                # Short concepts such as ear/eye/HIV are exact tokens; otherwise
                # they create large false-positive groups (for example ear in research).
                score += sum(token == term for token in tokens)
            else:
                # Longer configured forms intentionally act as medical stems:
                # neurolog -> neurology/neurological, diabet -> diabetes/diabetic.
                score += sum(token.startswith(term) for token in tokens)
        if score > best_score:
            best_domain, best_score = domain, score
    return best_domain


def pmcid_from_archive_member(member_name: str) -> str:
    match = re.search(r"(?:^|/)(PMC\d+)\.(?:n?xml)$", member_name, re.I)
    return match.group(1).upper() if match else ""


def scan_archive_worker(arguments: tuple[str, int, str, str, dict[str, Any]]) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    archive_value, sequence, archive_name, output_value, balance = arguments
    archive = Path(archive_value)
    destination = Path(output_value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".parquet.tmp")
    schema = pa.schema([
        ("pmcid", pa.string()), ("archive_name", pa.string()), ("archive_sequence", pa.int32()),
        ("member_name", pa.string()), ("domain", pa.string()), ("stable_rank", pa.string()),
        ("title", pa.string()), ("journal", pa.string()), ("language", pa.string()),
        ("scan_error", pa.string()),
    ])
    writer = pq.ParquetWriter(temporary, schema=schema, compression="zstd")
    rows: list[dict[str, Any]] = []
    parsed = errors = 0
    try:
        with tarfile.open(archive, mode="r|gz") as bundle:
            for member in bundle:
                if not member.isfile() or Path(member.name).suffix.lower() not in {".nxml", ".xml"}:
                    continue
                row = {field: "" for field in SCAN_FIELDS}
                row["archive_sequence"] = sequence
                row["archive_name"] = archive_name
                row["member_name"] = member.name
                try:
                    extracted = bundle.extractfile(member)
                    if extracted is None:
                        raise ValueError("member cannot be read")
                    root = etree.fromstring(extracted.read(), parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=True))
                    pmcid = xml_value(root, ".//*[local-name()='article-id' and @pub-id-type='pmc'][1]")
                    if pmcid and not pmcid.upper().startswith("PMC"):
                        pmcid = f"PMC{pmcid}"
                    if not pmcid:
                        filename_pmcid = pmcid_from_archive_member(member.name)
                        if filename_pmcid:
                            pmcid = filename_pmcid
                        else:
                            raise ValueError("PMCID missing from XML and archive member name")
                    title = xml_value(root, ".//*[local-name()='article-title'][1]")
                    abstract = xml_value(root, ".//*[local-name()='abstract'][1]")
                    keywords = " ".join(root.xpath(".//*[local-name()='kwd']//text()"))
                    journal = xml_value(root, ".//*[local-name()='journal-title'][1]")
                    subject = " ".join(root.xpath(".//*[local-name()='subject']//text()"))
                    row.update({
                        "pmcid": pmcid, "title": title, "journal": journal,
                        "language": root.get("{http://www.w3.org/XML/1998/namespace}lang", "en"),
                        "domain": classify_domain(f"{title} {title} {keywords} {subject} {abstract} {journal}", balance),
                        "stable_rank": hashlib.sha256(pmcid.encode()).hexdigest(),
                    })
                    parsed += 1
                except Exception as exc:
                    errors += 1
                    row["scan_error"] = repr(exc)[:1000]
                rows.append(row)
                if len(rows) >= 5000:
                    writer.write_table(pa.Table.from_pylist(rows, schema=schema))
                    rows.clear()
        if rows:
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    finally:
        writer.close()
    os.replace(temporary, destination)
    return {
        "archive_name": archive_name, "archive_sequence": sequence, "parsed_count": parsed,
        "error_count": errors, "metadata_path": str(destination), "generated_at": now(),
    }


def scan_archives(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    plan = load_plan(output)
    incomplete = [row["name"] for row in plan["packages"] if row.get("status") != "complete"]
    if incomplete:
        raise RuntimeError(f"cannot scan before all downloads complete: {len(incomplete)} packages")
    scan_dir = output / "staging" / "pmc_scan"
    reports_dir = output / "reports" / "pmc_scan_packages"
    scan_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    arguments = []
    for row in plan["packages"]:
        archive = output / "raw" / "pmc_oa_comm" / "packages" / row["name"]
        destination = scan_dir / f"{row['sequence']:04d}_{row['name']}.parquet"
        report_path = reports_dir / f"{row['sequence']:04d}.json"
        if destination.is_file() and report_path.is_file():
            continue
        arguments.append((str(archive), int(row["sequence"]), row["name"], str(destination), config["processing"]["balance"]))
    results = []
    failures = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=int(config["processing"]["process_workers"])) as executor:
        futures = {executor.submit(scan_archive_worker, value): value for value in arguments}
        for future in concurrent.futures.as_completed(futures):
            value = futures[future]
            try:
                result = future.result()
                results.append(result)
                write_json(reports_dir / f"{int(value[1]):04d}.json", result)
            except Exception as exc:
                failures.append({"archive_name": value[2], "error": repr(exc)})
            if directory_bytes(output) >= int(config["storage"]["stop_before_bytes"]):
                raise RuntimeError("capacity guard reached during metadata scan")
    existing_reports = [json.loads(path.read_text(encoding="utf-8")) for path in reports_dir.glob("*.json")]
    report = {
        "generated_at": now(), "package_count": len(plan["packages"]),
        "scanned_package_count": len(existing_reports),
        "parsed_document_versions": sum(int(row["parsed_count"]) for row in existing_reports),
        "scan_error_count": sum(int(row["error_count"]) for row in existing_reports),
        "failed_package_count": len(failures), "failures": failures,
        "directory_bytes": directory_bytes(output),
    }
    write_json(output / "reports" / "pmc_scan_report.json", report)
    if strict and (failures or report["scanned_package_count"] != len(plan["packages"])):
        raise RuntimeError("PMC metadata scan incomplete")
    return report


def collect_pmc_catalog_supplement(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    """Fetch current-catalog XML objects not yet present in the published bulk bundles."""
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    scan_glob = (output / "staging" / "pmc_scan" / "*.parquet").as_posix()
    catalog = output / "catalogs" / "pmc_oa_comm.filelist.csv"
    if not catalog.is_file() or not list((output / "staging" / "pmc_scan").glob("*.parquet")):
        raise RuntimeError("PMC current catalog and completed bulk scan are required")
    connection = duckdb.connect()
    allowed = tuple(config["pmc"]["allowed_licenses"])
    missing = connection.execute(
        "WITH scanned AS (SELECT DISTINCT CASE WHEN pmcid<>'' THEN pmcid "
        "WHEN scan_error LIKE '%PMCID missing%' THEN regexp_extract(member_name,'(PMC[0-9]+)\\.(n?xml)$',1) "
        "ELSE '' END AS pmcid FROM read_parquet(?)), catalog AS ("
        "SELECT \"Key\" AS object_key, ETag AS etag, AccessionID AS pmcid, PMID AS pmid, "
        "License AS license, Retracted AS retracted, "
        "\"Last Updated UTC (YYYY-MM-DD HH:MM:SS)\" AS modified_at "
        "FROM read_csv(?,header=true,all_varchar=true,delim=',',quote='\"',escape='\"',ignore_errors=true)"
        ") SELECT c.* FROM catalog c ANTI JOIN scanned s USING(pmcid) "
        "WHERE upper(c.license) IN (?, ?) AND lower(c.retracted) IN ('no','false','0') "
        "ORDER BY c.pmcid",
        [scan_glob, str(catalog), allowed[0].upper(), allowed[1].upper()],
    ).fetch_arrow_table().to_pylist()
    connection.close()
    report_path = output / "reports" / "pmc_catalog_supplement_report.json"
    if not missing:
        if report_path.is_file():
            return json.loads(report_path.read_text(encoding="utf-8"))
        report = {"generated_at": now(), "missing_catalog_document_count": 0, "status": "not_needed"}
        write_json(report_path, report)
        return report

    catalog_path = output / "catalogs" / "pmc_catalog_supplement.parquet"
    pq.write_table(pa.Table.from_pylist(missing), catalog_path, compression="zstd")
    raw_dir = output / "raw" / "pmc_oa_comm" / "supplemental_xml"
    raw_dir.mkdir(parents=True, exist_ok=True)
    base_url = str(config["pmc"]["object_base_url"]).rstrip("/") + "/"
    user_agent = config["pmc"]["user_agent"]
    timeout = int(config["pmc"]["request_timeout_seconds"])
    manifests: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    completed = 0

    def fetch_row(row: dict[str, Any]) -> dict[str, Any]:
        destination = raw_dir / f"{row['pmcid']}.xml"
        manifest = fetch_to_path(base_url + str(row["object_key"]), destination, user_agent, timeout)
        manifest.update({
            "pmcid": row["pmcid"], "object_key": row["object_key"], "catalog_etag": row.get("etag", ""),
            "catalog_license": row["license"], "catalog_modified_at": row["modified_at"],
        })
        return manifest

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(fetch_row, row): row for row in missing}
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            try:
                manifests.append(future.result())
            except Exception as exc:
                failures.append({"pmcid": str(row["pmcid"]), "url": base_url + str(row["object_key"]), "error": repr(exc)})
            completed += 1
            if completed % 50 == 0 and directory_bytes(output) >= int(config["storage"]["stop_before_bytes"]):
                raise RuntimeError("capacity guard reached during PMC catalog supplement download")
    manifests.sort(key=lambda row: str(row["pmcid"]))
    write_json(raw_dir / "manifest.json", manifests)
    if failures:
        write_json(output / "reports" / "pmc_catalog_supplement_failures.json", failures)
        if strict:
            raise RuntimeError(f"PMC catalog supplement download failures: {len(failures)}")

    bundle = output / "raw" / "pmc_oa_comm" / "pmc_catalog_supplement.tar.gz"
    temporary = bundle.with_suffix(bundle.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        for row in missing:
            path = raw_dir / f"{row['pmcid']}.xml"
            if path.is_file():
                archive.add(path, arcname=path.name)
    os.replace(temporary, bundle)
    sequence = max(int(row["sequence"]) for row in load_plan(output)["packages"]) + 1
    scan_path = output / "staging" / "pmc_scan" / f"{sequence:04d}_pmc_catalog_supplement.parquet"
    scan_report = scan_archive_worker((
        str(bundle), sequence, bundle.name, str(scan_path), config["processing"]["balance"],
    ))
    report = {
        "generated_at": now(), "missing_catalog_document_count": len(missing),
        "downloaded_document_count": len(manifests), "download_failure_count": len(failures),
        "bundle_path": bundle.relative_to(output).as_posix(), "bundle_sha256": sha256_file(bundle),
        "catalog_path": catalog_path.relative_to(output).as_posix(),
        "scan_path": scan_path.relative_to(output).as_posix(), "scan_report": scan_report,
        "status": "complete" if not failures and scan_report["error_count"] == 0 else "failed",
    }
    write_json(report_path, report)
    if strict and (
        failures or scan_report["error_count"] or scan_report["parsed_count"] != len(missing)
    ):
        raise RuntimeError("PMC catalog supplement strict validation failed")
    return report


def build_balanced_selection(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    import duckdb
    catalog = output / "catalogs" / "pmc_oa_comm.filelist.csv"
    if not catalog.is_file():
        source_catalog = ROOT / "data" / "commercial_medical_corpus" / "catalogs" / "pmc_oa_comm.filelist.csv"
        if source_catalog.is_file():
            try:
                os.link(source_catalog, catalog)
            except OSError:
                shutil.copy2(source_catalog, catalog)
        else:
            raise RuntimeError("current PMC oa_comm file list is missing")
    scan_glob = (output / "staging" / "pmc_scan" / "*.parquet").as_posix()
    selection = output / "staging" / "pmc_balanced_selection.parquet"
    database = output / "staging" / "selection.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("PRAGMA threads=%d" % int(config["processing"]["process_workers"]))
    connection.execute("PRAGMA temp_directory='%s'" % (output / "staging" / "duckdb_tmp").as_posix().replace("'", "''"))
    connection.execute("DROP TABLE IF EXISTS current_catalog")
    connection.execute(
        "CREATE TABLE current_catalog AS SELECT \"Key\" AS object_key, ETag AS etag, AccessionID AS pmcid, PMID AS pmid, License AS license, "
        "Retracted AS retracted, \"Last Updated UTC (YYYY-MM-DD HH:MM:SS)\" AS modified_at "
        "FROM read_csv(?, header=true, all_varchar=true, delim=',', quote='\"', escape='\"', ignore_errors=true)",
        [str(catalog)]
    )
    target = int(config["processing"]["balance"]["target_documents_per_domain"])
    allowed = tuple(config["pmc"]["allowed_licenses"])
    connection.execute("DROP TABLE IF EXISTS eligible_latest")
    connection.execute(
        "CREATE TABLE eligible_latest AS WITH normalized_scan AS ("
        " SELECT s.* EXCLUDE(pmcid, scan_error, domain, stable_rank),"
        " CASE WHEN s.pmcid <> '' THEN s.pmcid"
        "      WHEN s.scan_error LIKE '%PMCID missing%' THEN regexp_extract(s.member_name, '(PMC[0-9]+)\\.(n?xml)$', 1)"
        "      ELSE '' END AS pmcid,"
        " CASE WHEN s.domain <> '' THEN s.domain ELSE 'general_other' END AS domain,"
        " CASE WHEN s.stable_rank <> '' THEN s.stable_rank"
        "      ELSE sha256(regexp_extract(s.member_name, '(PMC[0-9]+)\\.(n?xml)$', 1)) END AS stable_rank,"
        " CASE WHEN s.scan_error LIKE '%PMCID missing%' AND regexp_extract(s.member_name, '(PMC[0-9]+)\\.(n?xml)$', 1) <> ''"
        "      THEN '' ELSE s.scan_error END AS scan_error"
        " FROM read_parquet(?) s"
        "), versions AS ("
        " SELECT s.*, row_number() OVER (PARTITION BY s.pmcid ORDER BY s.archive_sequence DESC) AS version_rank"
        " FROM normalized_scan s WHERE s.scan_error = '' AND s.pmcid <> ''"
        ") SELECT v.*, c.pmid, c.license, c.retracted, c.modified_at "
        "FROM versions v JOIN current_catalog c USING (pmcid) "
        "WHERE v.version_rank=1 AND upper(c.license) IN (?, ?) AND lower(c.retracted) IN ('no','false','0')",
        [scan_glob, allowed[0].upper(), allowed[1].upper()],
    )
    recovered_filename_pmcid_count = int(connection.execute(
        "SELECT count(*) FROM read_parquet(?) WHERE scan_error LIKE '%PMCID missing%' "
        "AND regexp_extract(member_name, '(PMC[0-9]+)\\.(n?xml)$', 1) <> ''",
        [scan_glob],
    ).fetchone()[0])
    scan_issues_path = output / "reports" / "pmc_scan_issues.parquet"
    scan_issues_literal = scan_issues_path.as_posix().replace("'", "''")
    connection.execute(
        "COPY (SELECT archive_name, archive_sequence, member_name, scan_error, "
        "CASE WHEN scan_error LIKE '%PMCID missing%' AND regexp_extract(member_name, '(PMC[0-9]+)\\.(n?xml)$', 1) <> '' "
        "THEN 'recovered_from_archive_member_and_catalog' ELSE 'excluded_parse_error' END AS resolution "
        "FROM read_parquet(?) WHERE scan_error <> '') "
        f"TO '{scan_issues_literal}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE)",
        [scan_glob],
    )
    scan_issue_counts = dict(connection.execute(
        "SELECT resolution, count(*) FROM read_parquet(?) GROUP BY resolution ORDER BY resolution",
        [str(scan_issues_path)],
    ).fetchall())
    selection_literal = str(selection).replace("'", "''")
    connection.execute(
        "COPY (WITH ranked AS (SELECT *, row_number() OVER (PARTITION BY domain ORDER BY stable_rank) AS domain_rank "
        "FROM eligible_latest) SELECT * EXCLUDE(version_rank) FROM ranked WHERE domain_rank <= ?) "
        f"TO '{selection_literal}' (FORMAT PARQUET, COMPRESSION ZSTD)", [target]
    )
    partition_dir = output / "staging" / "pmc_selection_by_archive"
    # DuckDB's OVERWRITE_OR_IGNORE does not remove stale partition fragments.
    # Reusing them can mix selections from older runs, so rebuild the partition
    # tree from the authoritative balanced-selection file every time.
    if partition_dir.exists():
        shutil.rmtree(partition_dir)
    partition_dir.mkdir(parents=True, exist_ok=True)
    partition_literal = str(partition_dir).replace("'", "''")
    connection.execute(
        f"COPY (SELECT * FROM read_parquet(?)) TO '{partition_literal}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (archive_sequence), OVERWRITE_OR_IGNORE)",
        [str(selection)],
    )
    before = connection.execute("SELECT domain, count(*) count FROM eligible_latest GROUP BY domain ORDER BY domain").fetchall()
    after = connection.execute("SELECT domain, count(*) count FROM read_parquet(?) GROUP BY domain ORDER BY domain", [str(selection)]).fetchall()
    license_counts = dict(connection.execute(
        "SELECT coalesce(license, ''), count(*) FROM current_catalog GROUP BY license ORDER BY license"
    ).fetchall())
    exclusions_path = output / "reports" / "pmc_exclusions.parquet"
    exclusions_literal = exclusions_path.as_posix().replace("'", "''")
    connection.execute(
        "COPY (SELECT pmcid, pmid, license, retracted, modified_at, "
        "CASE WHEN coalesce(lower(retracted), '') NOT IN ('no','false','0') THEN 'retracted_or_retraction_status_unknown' "
        "ELSE 'license_not_allowed' END AS exclusion_reason FROM current_catalog "
        "WHERE coalesce(upper(license), '') NOT IN (?, ?) "
        "OR coalesce(lower(retracted), '') NOT IN ('no','false','0')) "
        f"TO '{exclusions_literal}' (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE)",
        [allowed[0].upper(), allowed[1].upper()],
    )
    exclusion_counts = dict(connection.execute(
        "SELECT exclusion_reason, count(*) FROM read_parquet(?) GROUP BY exclusion_reason ORDER BY exclusion_reason",
        [str(exclusions_path)],
    ).fetchall())
    total_selected = sum(row[1] for row in after)
    max_share = max((row[1] / total_selected for row in after), default=0.0)
    report = {
        "generated_at": now(), "eligible_document_count": sum(row[1] for row in before),
        "selected_document_count": total_selected, "target_documents_per_domain": target,
        "selection_method": config["processing"]["balance"]["selection_method"],
        "before_domain_counts": dict(before), "selected_domain_counts": dict(after),
        "current_catalog_license_counts": license_counts,
        "recovered_filename_pmcid_count": recovered_filename_pmcid_count,
        "scan_issue_resolution_counts": scan_issue_counts,
        "scan_issues_path": scan_issues_path.relative_to(output).as_posix(),
        "exclusion_reason_counts": exclusion_counts,
        "exclusions_path": exclusions_path.relative_to(output).as_posix(),
        "maximum_selected_domain_share": max_share,
        "maximum_allowed_domain_share": config["processing"]["balance"]["maximum_dominant_domain_share"],
        "balance_check_passed": max_share <= float(config["processing"]["balance"]["maximum_dominant_domain_share"]),
        "selection_path": selection.relative_to(output).as_posix(),
        "partitioned_selection_path": partition_dir.relative_to(output).as_posix(),
    }
    write_json(output / "reports" / "pmc_balance_report.json", report)
    connection.close()
    if strict and not report["balance_check_passed"]:
        raise RuntimeError(f"domain balance check failed: {max_share}")
    return report


def _parquet_flush(writer: Any, path: Path, rows: list[dict[str, Any]]) -> Any:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not rows:
        return writer
    table = pa.Table.from_pylist(rows)
    if writer is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = pq.ParquetWriter(path, table.schema, compression="zstd")
    writer.write_table(table)
    rows.clear()
    return writer


def limit_chunks_per_document(chunks: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    """Keep an even, deterministic sample so long papers cannot dominate retrieval."""
    if maximum <= 0 or len(chunks) <= maximum:
        return chunks
    if maximum == 1:
        positions = [0]
    else:
        positions = [round(index * (len(chunks) - 1) / (maximum - 1)) for index in range(maximum)]
    selected = [chunks[position] for position in positions]
    for index, chunk in enumerate(selected, start=1):
        chunk["chunk_index"] = index
        chunk["chunk_id"] = stable_id(
            "medchunk", str(chunk["document_id"]), str(index), str(chunk["text_sha256"])
        )
    return selected


def _selection_files_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def materialize_archive_worker(arguments: tuple[str, list[str], str, int, str, dict[str, Any]]) -> dict[str, Any]:
    import pyarrow.parquet as pq

    archive_value, selection_values, archive_name, sequence, output_value, processing = arguments
    archive = Path(archive_value)
    selection_paths = [Path(value) for value in selection_values]
    output = Path(output_value)
    selection_rows = pq.read_table([str(path) for path in selection_paths]).to_pylist()
    selection_sha256 = _selection_files_sha256(selection_paths)
    selected = {str(row["member_name"]): row for row in selection_rows}
    documents_path = output / "staging" / "pmc_documents" / f"{sequence:04d}.parquet"
    chunks_path = output / "staging" / "pmc_chunks" / f"{sequence:04d}.parquet"
    documents_tmp = documents_path.with_suffix(".parquet.tmp")
    chunks_tmp = chunks_path.with_suffix(".parquet.tmp")
    doc_writer = chunk_writer = None
    doc_rows: list[dict[str, Any]] = []
    chunk_rows: list[dict[str, Any]] = []
    document_count = chunk_count = parse_errors = 0
    parse_error_records: list[dict[str, str]] = []
    collected_at = now()
    with tarfile.open(archive, mode="r|gz") as bundle:
        for member in bundle:
            metadata = selected.get(member.name)
            if metadata is None or not member.isfile():
                continue
            try:
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise ValueError("selected archive member cannot be read")
                raw_xml = extracted.read()
                pmcid = str(metadata["pmcid"])
                article_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
                document = parse_pmc_article(raw_xml, article_url, str(metadata["license"]), collected_at)
                if not document.get("pmcid"):
                    filename_pmcid = pmcid_from_archive_member(member.name)
                    if filename_pmcid == pmcid.upper():
                        document["pmcid"] = pmcid
                if document["pmcid"] != pmcid:
                    raise ValueError(f"PMCID mismatch: {document['pmcid']} != {pmcid}")
                if document.get("retracted"):
                    raise ValueError("article XML indicates a retracted publication")
                if not document.get("title") or not document.get("sections"):
                    raise ValueError("article has no title or searchable abstract/body section")
                document.update({
                    "source": "pmc_oa_comm", "pmid": str(metadata.get("pmid") or document.get("pmid") or "not_provided"),
                    "modified_at": str(metadata.get("modified_at") or "not_provided"),
                    "domain": str(metadata["domain"]), "collection_group": "oa_comm",
                    "artifact_url": archive.name, "archive_member": member.name,
                    "raw_sha256": hashlib.sha256(raw_xml).hexdigest(),
                    "license_evidence": json.dumps({
                        "catalog_license": metadata["license"], "catalog_retracted": metadata["retracted"],
                        "catalog_modified_at": metadata.get("modified_at", ""),
                    }, ensure_ascii=False, sort_keys=True),
                })
                for field in ("authors", "pmid", "published_at", "modified_at", "language"):
                    document[field] = normalize_space(document.get(field)) or "not_provided"
                full_text = "\n\n".join(row["text"] for row in document["sections"])
                document["text_sha256"] = sha256_text(full_text)
                document["document_id"] = stable_id("meddoc", document["source_url"], document["text_sha256"])
                chunks = make_chunks(
                    [document], int(processing["maximum_chunk_characters"]), int(processing["minimum_chunk_characters"])
                )
                chunks = limit_chunks_per_document(
                    chunks, int(processing.get("maximum_chunks_per_document", 24))
                )
                for chunk in chunks:
                    chunk.update({
                        "domain": document["domain"], "archive_name": archive_name,
                        "archive_member": member.name, "raw_sha256": document["raw_sha256"],
                    })
                parquet_document = dict(document)
                parquet_document["sections_json"] = json.dumps(parquet_document.pop("sections"), ensure_ascii=False)
                parquet_document["license_evidence"] = str(parquet_document["license_evidence"])
                doc_rows.append(parquet_document)
                chunk_rows.extend(chunks)
                document_count += 1
                chunk_count += len(chunks)
                if len(doc_rows) >= 1000:
                    doc_writer = _parquet_flush(doc_writer, documents_tmp, doc_rows)
                if len(chunk_rows) >= 10000:
                    chunk_writer = _parquet_flush(chunk_writer, chunks_tmp, chunk_rows)
            except Exception as exc:
                parse_errors += 1
                parse_error_records.append({
                    "member_name": member.name,
                    "pmcid": str(metadata.get("pmcid") or ""),
                    "reason": repr(exc)[:2000],
                })
    doc_writer = _parquet_flush(doc_writer, documents_tmp, doc_rows)
    chunk_writer = _parquet_flush(chunk_writer, chunks_tmp, chunk_rows)
    if doc_writer is not None:
        doc_writer.close()
        documents_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(documents_tmp, documents_path)
    if chunk_writer is not None:
        chunk_writer.close()
        chunks_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(chunks_tmp, chunks_path)
    return {
        "archive_name": archive_name, "archive_sequence": sequence,
        "selected_count": len(selected), "document_count": document_count,
        "chunk_count": chunk_count, "parse_error_count": parse_errors,
        "parse_errors": parse_error_records,
        "selection_sha256": selection_sha256,
        "generated_at": now(),
    }


def materialize_selected(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    plan = load_plan(output)
    reports_dir = output / "reports" / "pmc_materialize_packages"
    reports_dir.mkdir(parents=True, exist_ok=True)
    source_archives = [
        {**row, "archive_path": output / "raw" / "pmc_oa_comm" / "packages" / row["name"]}
        for row in plan["packages"]
    ]
    supplement_report_path = output / "reports" / "pmc_catalog_supplement_report.json"
    if supplement_report_path.is_file():
        supplement_report = json.loads(supplement_report_path.read_text(encoding="utf-8"))
        if supplement_report.get("status") == "complete":
            sequence = int(supplement_report["scan_report"]["archive_sequence"])
            bundle = output / str(supplement_report["bundle_path"])
            source_archives.append({"sequence": sequence, "name": bundle.name, "archive_path": bundle})
    arguments = []
    expected_sequences: list[int] = []
    for row in source_archives:
        sequence = int(row["sequence"])
        selection_files = list((output / "staging" / "pmc_selection_by_archive" / f"archive_sequence={sequence}").glob("*.parquet"))
        if not selection_files:
            continue
        expected_sequences.append(sequence)
        report_path = reports_dir / f"{sequence:04d}.json"
        documents_path = output / "staging" / "pmc_documents" / f"{sequence:04d}.parquet"
        chunks_path = output / "staging" / "pmc_chunks" / f"{sequence:04d}.parquet"
        selection_files = sorted(selection_files, key=lambda value: value.as_posix())
        current_selection_sha256 = _selection_files_sha256(selection_files)
        if report_path.is_file() and documents_path.is_file() and chunks_path.is_file():
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            if previous.get("selection_sha256") == current_selection_sha256:
                continue
        arguments.append((
            str(row["archive_path"]),
            [str(path) for path in selection_files], row["name"], sequence, str(output), config["processing"],
        ))
    failures = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=int(config["processing"]["process_workers"])) as executor:
        futures = {executor.submit(materialize_archive_worker, value): value for value in arguments}
        for future in concurrent.futures.as_completed(futures):
            value = futures[future]
            try:
                result = future.result()
                write_json(reports_dir / f"{int(value[3]):04d}.json", result)
            except Exception as exc:
                failures.append({"archive_name": value[2], "error": repr(exc)})
            if directory_bytes(output) >= int(config["storage"]["stop_before_bytes"]):
                raise RuntimeError("capacity guard reached during document materialization")
    package_reports = [
        json.loads((reports_dir / f"{sequence:04d}.json").read_text(encoding="utf-8"))
        for sequence in expected_sequences if (reports_dir / f"{sequence:04d}.json").is_file()
    ]
    report = {
        "generated_at": now(), "materialized_package_count": len(package_reports),
        "document_count": sum(int(row["document_count"]) for row in package_reports),
        "chunk_count": sum(int(row["chunk_count"]) for row in package_reports),
        "parse_error_count": sum(int(row["parse_error_count"]) for row in package_reports),
        "failed_package_count": len(failures), "failures": failures,
        "expected_package_count": len(expected_sequences),
        "directory_bytes": directory_bytes(output),
    }
    exclusion_reason_counts: dict[str, int] = {}
    exclusions_path = output / "reports" / "pmc_materialization_exclusions.jsonl"
    with exclusions_path.open("w", encoding="utf-8", newline="\n") as exclusions_handle:
        for package_report in package_reports:
            for error in package_report.get("parse_errors", []):
                reason_text = str(error.get("reason", ""))
                if "retracted publication" in reason_text:
                    category = "xml_retraction_evidence"
                elif "no title or searchable" in reason_text:
                    category = "missing_searchable_content"
                else:
                    category = "parse_error"
                exclusion_reason_counts[category] = exclusion_reason_counts.get(category, 0) + 1
                exclusions_handle.write(json.dumps({
                    "archive_sequence": package_report.get("archive_sequence"),
                    "archive_name": package_report.get("archive_name", ""),
                    "member_name": error.get("member_name", ""),
                    "pmcid": error.get("pmcid", ""),
                    "category": category,
                    "reason": reason_text,
                }, ensure_ascii=False) + "\n")
    report["exclusion_reason_counts"] = exclusion_reason_counts
    report["exclusions_path"] = exclusions_path.relative_to(output).as_posix()
    report["selected_document_count"] = sum(int(row["selected_count"]) for row in package_reports)
    report["accounted_document_count"] = report["document_count"] + report["parse_error_count"]
    report["selection_accounting_passed"] = (
        report["selected_document_count"] == report["accounted_document_count"]
    )
    balance_report_path = output / "reports" / "pmc_balance_report.json"
    balanced_selected_count = int(json.loads(balance_report_path.read_text(encoding="utf-8"))["selected_document_count"])
    report["balanced_selected_document_count"] = balanced_selected_count
    report["all_balanced_selections_materialized"] = report["selected_document_count"] == balanced_selected_count
    write_json(output / "reports" / "pmc_materialization_report.json", report)
    if strict and (
        failures or len(package_reports) != len(expected_sequences)
        or not report["selection_accounting_passed"] or not report["all_balanced_selections_materialized"]
    ):
        raise RuntimeError("PMC materialization failed strict validation")
    return report


def _write_jsonl_zstd_shards(
    parquet_path: Path, destination: Path, rows_per_shard: int,
    capacity_root: Path | None = None, stop_before_bytes: int = 0,
) -> dict[str, Any]:
    import duckdb
    import pyarrow.parquet as pq

    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("*.jsonl.zst"):
        stale.unlink()
    parquet = pq.ParquetFile(parquet_path)
    row_count = int(parquet.metadata.num_rows)
    if row_count == 0:
        return {"row_count": 0, "shard_count": 0}
    if capacity_root is not None and stop_before_bytes and directory_bytes(capacity_root) >= stop_before_bytes:
        raise RuntimeError("capacity guard reached while writing compressed JSONL")

    # DuckDB streams Parquet directly to newline-delimited JSON and compresses it
    # in native code.  This avoids converting tens of millions of rows to Python
    # dictionaries one at a time.  A single compressed stream is intentional:
    # Parquet remains the splittable analytics format, while JSONL is the portable
    # interchange copy.
    output_path = destination / "part-00000.jsonl.zst"
    parquet_literal = parquet_path.resolve().as_posix().replace("'", "''")
    output_literal = output_path.resolve().as_posix().replace("'", "''")
    connection = duckdb.connect()
    try:
        connection.execute("PRAGMA threads=2")
        connection.execute("PRAGMA memory_limit='8GB'")
        connection.execute("SET preserve_insertion_order=false")
        connection.execute(
            f"COPY (SELECT * FROM read_parquet('{parquet_literal}')) "
            f"TO '{output_literal}' (FORMAT JSON, ARRAY false, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()
    if capacity_root is not None and stop_before_bytes and directory_bytes(capacity_root) >= stop_before_bytes:
        raise RuntimeError("capacity guard reached while writing compressed JSONL")
    return {"row_count": row_count, "shard_count": 1}


def integrate_corpus(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    """Create one deduplicated, balanced, license-audited corpus in both formats."""
    import duckdb

    pmc_documents = output / "staging" / "pmc_documents" / "*.parquet"
    pmc_chunks = output / "staging" / "pmc_chunks" / "*.parquet"
    medline_documents = output / "staging" / "medlineplus_documents.parquet"
    medline_chunks = output / "staging" / "medlineplus_chunks.parquet"
    korean_documents = output / "staging" / "korean_public_documents.parquet"
    korean_chunks = output / "staging" / "korean_public_chunks.parquet"
    required_inputs = [medline_documents, medline_chunks]
    if any(not path.is_file() for path in required_inputs) or not list(pmc_documents.parent.glob("*.parquet")):
        raise RuntimeError("materialized PMC and MedlinePlus staging datasets are required")

    final_dir = output / "integrated"
    parquet_dir = final_dir / "parquet"
    jsonl_dir = final_dir / "jsonl"
    reports_dir = output / "reports"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    documents_path = parquet_dir / "documents.parquet"
    chunks_path = parquet_dir / "chunks.parquet"
    database = output / "staging" / "integration.duckdb"
    connection = duckdb.connect(str(database))
    integration_threads = int(config["processing"].get("integration_threads", 2))
    connection.execute("PRAGMA threads=%d" % integration_threads)
    memory_limit = str(config["processing"].get("integration_memory_limit", "24GB"))
    connection.execute("PRAGMA memory_limit='%s'" % memory_limit.replace("'", "''"))
    connection.execute("SET preserve_insertion_order=false")
    connection.execute("PRAGMA temp_directory='%s'" % (output / "staging" / "duckdb_tmp").as_posix().replace("'", "''"))
    pmc_doc_literal = pmc_documents.as_posix().replace("'", "''")
    pmc_chunk_literal = pmc_chunks.as_posix().replace("'", "''")
    pmc_selection_literal = (output / "staging" / "pmc_balanced_selection.parquet").as_posix().replace("'", "''")
    med_doc_literal = medline_documents.as_posix().replace("'", "''")
    med_chunk_literal = medline_chunks.as_posix().replace("'", "''")
    korean_doc_literal = korean_documents.as_posix().replace("'", "''")
    korean_chunk_literal = korean_chunks.as_posix().replace("'", "''")
    document_queries = [
        f"SELECT d.* FROM read_parquet('{pmc_doc_literal}', union_by_name=true) d "
        f"JOIN (SELECT DISTINCT pmcid FROM read_parquet('{pmc_selection_literal}')) s USING(pmcid)",
        f"SELECT * FROM read_parquet('{med_doc_literal}', union_by_name=true)",
    ]
    chunk_queries = [
        f"SELECT * FROM read_parquet('{pmc_chunk_literal}', union_by_name=true)",
        f"SELECT * FROM read_parquet('{med_chunk_literal}', union_by_name=true)",
    ]
    if korean_documents.is_file() and korean_chunks.is_file():
        document_queries.append(f"SELECT * FROM read_parquet('{korean_doc_literal}', union_by_name=true)")
        chunk_queries.append(f"SELECT * FROM read_parquet('{korean_chunk_literal}', union_by_name=true)")
    connection.execute("CREATE OR REPLACE VIEW all_documents AS " + " UNION ALL BY NAME ".join(document_queries))
    documents_literal = documents_path.resolve().as_posix().replace("'", "''")
    chunks_literal = chunks_path.resolve().as_posix().replace("'", "''")
    winners_path = output / "staging" / "document_deduplication_winners.parquet"
    winners_literal = winners_path.resolve().as_posix().replace("'", "''")
    reuse_integrated_parquet = bool(config["processing"].get("reuse_integrated_parquet", False)) and (
        documents_path.is_file() and documents_path.stat().st_size > 0
        and chunks_path.is_file() and chunks_path.stat().st_size > 0
    )

    # Keep the wide document rows out of DuckDB's persistent database.  Ranking
    # complete article bodies can exceed RAM.  Finish the compact hash/id winner
    # aggregation first, then release its hash table before streaming wide rows.
    if not reuse_integrated_parquet:
        documents_path.unlink(missing_ok=True)
        chunks_path.unlink(missing_ok=True)
        winners_path.unlink(missing_ok=True)
        connection.execute(
            "COPY ("
            "SELECT text_sha256, arg_min(document_id, source_url || ':' || document_id) AS document_id "
            "FROM all_documents WHERE text_sha256 <> '' AND document_id <> '' GROUP BY text_sha256"
            f") TO '{winners_literal}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        connection.execute(
            "COPY (SELECT d.* FROM all_documents d "
            f"JOIN read_parquet('{winners_literal}') w USING(text_sha256, document_id)) "
            f"TO '{documents_literal}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    connection.execute(
        f"CREATE OR REPLACE VIEW final_documents AS SELECT * FROM read_parquet('{documents_literal}')"
    )
    connection.execute("CREATE OR REPLACE VIEW all_chunks AS " + " UNION ALL BY NAME ".join(chunk_queries))

    # Chunk creation already de-duplicates text within each document.  Joining to
    # the surviving document ids removes chunks belonging to duplicate documents;
    # the strict duplicate query below still rejects any upstream invariant break.
    if not reuse_integrated_parquet:
        connection.execute(
            "COPY (SELECT c.* FROM all_chunks c JOIN final_documents d USING(document_id)) "
            f"TO '{chunks_literal}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    connection.execute(
        f"CREATE OR REPLACE VIEW final_chunks AS SELECT * FROM read_parquet('{chunks_literal}')"
    )
    if directory_bytes(output) >= int(config["storage"]["stop_before_bytes"]):
        connection.close()
        raise RuntimeError("capacity guard reached while writing integrated Parquet")

    document_count = int(connection.execute("SELECT count(*) FROM final_documents").fetchone()[0])
    chunk_count = int(connection.execute("SELECT count(*) FROM final_chunks").fetchone()[0])
    duplicate_documents = int(connection.execute("SELECT count(*) FROM all_documents").fetchone()[0]) - document_count
    final_document_duplicates = int(connection.execute(
        "SELECT count(*) FROM (SELECT text_sha256 FROM final_documents GROUP BY text_sha256 HAVING count(*) > 1)"
    ).fetchone()[0])
    orphan_chunks = int(connection.execute(
        "SELECT count(*) FROM final_chunks c ANTI JOIN final_documents d USING(document_id)"
    ).fetchone()[0])
    duplicate_chunks = int(connection.execute(
        "SELECT count(*)-count(DISTINCT document_id || ':' || text_sha256) FROM final_chunks"
    ).fetchone()[0])
    invalid_licenses = int(connection.execute(
        "SELECT count(*) FROM final_documents WHERE upper(license) NOT IN "
        "('CC0','CC BY','U.S. PUBLIC DOMAIN','KOGL TYPE 0','KOGL TYPE 1')"
    ).fetchone()[0])
    retracted_count = int(connection.execute("SELECT count(*) FROM final_documents WHERE retracted").fetchone()[0])
    required_columns = [
        "document_id", "source_url", "title", "institution", "authors", "pmid", "pmcid",
        "license", "published_at", "modified_at", "collected_at", "language", "text_sha256",
    ]
    missing_fields = {
        column: int(connection.execute(
            f"SELECT count(*) FROM final_documents WHERE {column} IS NULL OR trim(cast({column} AS varchar))=''"
        ).fetchone()[0]) for column in required_columns
    }
    required_chunk_columns = [
        "chunk_id", "document_id", "text", "source_url", "title", "institution", "authors",
        "pmid", "pmcid", "license", "published_at", "modified_at", "collected_at", "language",
        "text_sha256", "domain",
    ]
    missing_chunk_fields = {
        column: int(connection.execute(
            f"SELECT count(*) FROM final_chunks WHERE {column} IS NULL OR trim(cast({column} AS varchar))=''"
        ).fetchone()[0]) for column in required_chunk_columns
    }
    documents_without_chunks = int(connection.execute(
        "SELECT count(*) FROM final_documents d ANTI JOIN final_chunks c USING(document_id)"
    ).fetchone()[0])
    maximum_chunks_for_one_document = int(connection.execute(
        "SELECT coalesce(max(chunk_count), 0) FROM (SELECT document_id, count(*) AS chunk_count "
        "FROM final_chunks GROUP BY document_id)"
    ).fetchone()[0])
    domain_rows = connection.execute(
        "SELECT domain, count(*) AS count FROM final_documents GROUP BY domain ORDER BY domain"
    ).fetchall()
    domain_counts = {str(domain): int(count) for domain, count in domain_rows}
    maximum_share = max(domain_counts.values(), default=0) / max(document_count, 1)
    chunk_domain_rows = connection.execute(
        "SELECT domain, count(*) AS count FROM final_chunks GROUP BY domain ORDER BY domain"
    ).fetchall()
    chunk_domain_counts = {str(domain): int(count) for domain, count in chunk_domain_rows}
    maximum_chunk_share = max(chunk_domain_counts.values(), default=0) / max(chunk_count, 1)
    maximum_allowed = float(config["processing"]["balance"]["maximum_dominant_domain_share"])
    source_counts = dict(connection.execute(
        "SELECT source, count(*) FROM final_documents GROUP BY source ORDER BY source"
    ).fetchall())
    connection.close()

    balance_report = json.loads((output / "reports" / "pmc_balance_report.json").read_text(encoding="utf-8"))
    pmc_catalog_document_count = sum(int(value) for value in balance_report["current_catalog_license_counts"].values())
    pmc_catalog_excluded_count = sum(int(value) for value in balance_report["exclusion_reason_counts"].values())
    pmc_catalog_accounted_count = int(balance_report["eligible_document_count"]) + pmc_catalog_excluded_count
    pmc_catalog_accounting_passed = pmc_catalog_accounted_count == pmc_catalog_document_count

    rows_per_shard = int(config["processing"]["documents_per_shard"])
    stop_before = int(config["storage"]["stop_before_bytes"])
    document_jsonl = _write_jsonl_zstd_shards(
        documents_path, jsonl_dir / "documents", rows_per_shard, output, stop_before
    )
    chunk_jsonl = _write_jsonl_zstd_shards(
        chunks_path, jsonl_dir / "chunks", rows_per_shard * 5, output, stop_before
    )
    storage_bytes = directory_bytes(output)
    checks = {
        "document_count_positive": document_count > 0,
        "chunk_count_positive": chunk_count > 0,
        "chunk_foreign_key_errors_zero": orphan_chunks == 0,
        "duplicate_documents_zero_after_deduplication": final_document_duplicates == 0,
        "duplicate_chunks_zero": duplicate_chunks == 0,
        "invalid_licenses_zero": invalid_licenses == 0,
        "retracted_documents_zero": retracted_count == 0,
        "required_fields_complete": all(value == 0 for value in missing_fields.values()),
        "required_chunk_fields_complete": all(value == 0 for value in missing_chunk_fields.values()),
        "every_document_has_a_chunk": documents_without_chunks == 0,
        "per_document_chunk_limit_passed": maximum_chunks_for_one_document <= int(
            config["processing"].get("maximum_chunks_per_document", 24)
        ),
        "domain_balance_passed": maximum_share <= maximum_allowed,
        "chunk_domain_balance_passed": maximum_chunk_share <= maximum_allowed,
        "jsonl_document_count_matches": document_jsonl["row_count"] == document_count,
        "jsonl_chunk_count_matches": chunk_jsonl["row_count"] == chunk_count,
        "capacity_guard_passed": storage_bytes < int(config["storage"]["stop_before_bytes"]),
        "pmc_current_catalog_accounting_passed": pmc_catalog_accounting_passed,
    }
    report = {
        "generated_at": now(), "schema_version": config["schema_version"],
        "document_count": document_count, "chunk_count": chunk_count,
        "duplicate_input_document_count": duplicate_documents,
        "duplicate_final_document_count": final_document_duplicates,
        "orphan_chunk_count": orphan_chunks, "duplicate_chunk_count": duplicate_chunks,
        "invalid_license_count": invalid_licenses, "retracted_document_count": retracted_count,
        "missing_required_field_counts": missing_fields, "source_counts": source_counts,
        "missing_required_chunk_field_counts": missing_chunk_fields,
        "documents_without_chunks": documents_without_chunks,
        "maximum_chunks_for_one_document": maximum_chunks_for_one_document,
        "domain_counts": domain_counts, "maximum_domain_share": maximum_share,
        "chunk_domain_counts": chunk_domain_counts, "maximum_chunk_domain_share": maximum_chunk_share,
        "maximum_allowed_domain_share": maximum_allowed,
        "documents_jsonl": document_jsonl, "chunks_jsonl": chunk_jsonl,
        "storage_bytes": storage_bytes, "hard_limit_bytes": int(config["storage"]["hard_limit_bytes"]),
        "pmc_current_catalog_document_count": pmc_catalog_document_count,
        "pmc_current_catalog_accounted_count": pmc_catalog_accounted_count,
        "pmc_current_catalog_excluded_count": pmc_catalog_excluded_count,
        "pmc_recovered_filename_pmcid_count": int(balance_report["recovered_filename_pmcid_count"]),
        "integrity_checks": checks, "integrity_checks_passed": all(checks.values()),
        "dataset_status": "verified" if all(checks.values()) else "failed",
        "files": {
            "documents_parquet": documents_path.relative_to(output).as_posix(),
            "chunks_parquet": chunks_path.relative_to(output).as_posix(),
            "documents_jsonl": (jsonl_dir / "documents").relative_to(output).as_posix(),
            "chunks_jsonl": (jsonl_dir / "chunks").relative_to(output).as_posix(),
        },
    }
    write_json(reports_dir / "bulk_integration_report.json", report)
    if strict and not report["integrity_checks_passed"]:
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(f"bulk corpus integration checks failed: {failed}")
    return report


def canonical_medline_document(row: dict[str, Any], balance: dict[str, Any]) -> dict[str, Any]:
    document = dict(row)
    document["authors"] = normalize_space(document.get("authors")) or "MedlinePlus, U.S. National Library of Medicine"
    document["pmid"] = normalize_space(document.get("pmid")) or "not_applicable"
    document["pmcid"] = normalize_space(document.get("pmcid")) or "not_applicable"
    document["published_at"] = normalize_space(document.get("published_at")) or "not_provided"
    document["modified_at"] = normalize_space(document.get("modified_at")) or "not_provided"
    document["collected_at"] = normalize_space(document.get("collected_at")) or now()
    document["retracted"] = False
    document["license"] = "U.S. Public Domain"
    document["license_url"] = "https://medlineplus.gov/about/using/usingcontent/"
    document["license_evidence"] = json.dumps({"policy_url": document["license_url"], "content_area": document["source"]}, sort_keys=True)
    document["institution"] = "U.S. National Library of Medicine"
    document["collection_group"] = "medlineplus_public_domain"
    document["artifact_url"] = document.get("artifact_url", "not_applicable")
    full_text = "\n\n".join(section["text"] for section in document["sections"])
    document["text_sha256"] = sha256_text(full_text)
    document["document_id"] = stable_id("meddoc", document["source_url"], document["text_sha256"])
    document["domain"] = classify_domain(f"{document['title']} {document.get('abstract','')} {full_text[:8000]}", balance)
    return document


def fetch_to_path(url: str, destination: Path, user_agent: str, timeout: int) -> dict[str, Any]:
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        with requests.get(url, headers={"User-Agent": user_agent}, timeout=(20, timeout), stream=True) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for block in response.iter_content(1024 * 1024):
                    if block:
                        handle.write(block)
        os.replace(temporary, destination)
    return {
        "url": url, "raw_path": str(destination), "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination), "collected_at": now(),
    }


def collect_medlineplus_bulk(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    medline = config["medlineplus"]
    user_agent = config["pmc"]["user_agent"]
    timeout = int(config["pmc"]["request_timeout_seconds"])
    raw_dir = output / "raw" / "medlineplus"
    health_path = raw_dir / "health_topics.xml"
    genetics_path = raw_dir / "genetics.xml"
    raw_manifest = [
        fetch_to_path(medline["health_topics_xml_url"], health_path, user_agent, timeout),
        fetch_to_path(medline["genetics_xml_url"], genetics_path, user_agent, timeout),
    ]
    documents: list[dict[str, Any]] = []
    collected_at = now()
    for _, topic in etree.iterparse(str(health_path), events=("end",), tag="{*}health-topic", recover=True, huge_tree=True):
        summary_nodes = topic.xpath("./*[local-name()='full-summary'][1]")
        summary = normalize_space(" ".join(summary_nodes[0].itertext())) if summary_nodes else ""
        if summary:
            language_value = topic.get("language", "English")
            documents.append(canonical_medline_document({
                "source": "medlineplus_health_topics", "source_url": topic.get("url", ""),
                "title": topic.get("title", ""), "authors": "MedlinePlus, U.S. National Library of Medicine",
                "published_at": topic.get("date-created", ""), "modified_at": topic.get("date-modified", ""),
                "collected_at": collected_at, "language": "en" if language_value == "English" else "es",
                "abstract": summary, "sections": [{"section_title": "Summary", "text": summary}],
                "artifact_url": medline["health_topics_xml_url"],
            }, config["processing"]["balance"]))
        topic.clear()
        while topic.getprevious() is not None:
            del topic.getparent()[0]
    summary_tags = {"health-condition-summary", "gene-summary", "chromosome-summary", "mtdna-summary"}
    for _, summary_node in etree.iterparse(str(genetics_path), events=("end",), recover=True, huge_tree=True):
        tag = str(summary_node.tag).rsplit("}", 1)[-1]
        if tag not in summary_tags:
            continue
        name_nodes = summary_node.xpath("./*[local-name()='name'][1]")
        page_nodes = summary_node.xpath("./*[local-name()='ghr-page'][1]")
        title = normalize_space(" ".join(name_nodes[0].itertext())) if name_nodes else ""
        source_url = normalize_space(" ".join(page_nodes[0].itertext())) if page_nodes else ""
        sections = []
        for text_node in summary_node.xpath(".//*[local-name()='text-list']/*[local-name()='text']"):
            role_nodes = text_node.xpath("./*[local-name()='text-role'][1]")
            html_nodes = text_node.xpath("./*[local-name()='html'][1]")
            role = normalize_space(" ".join(role_nodes[0].itertext())) if role_nodes else "Description"
            text = normalize_space(" ".join(html_nodes[0].itertext())) if html_nodes else ""
            if text:
                sections.append({"section_title": role or "Description", "text": text})
        if title and source_url and sections:
            published_nodes = summary_node.xpath("./*[local-name()='published'][1]")
            reviewed_nodes = summary_node.xpath("./*[local-name()='reviewed'][1]")
            documents.append(canonical_medline_document({
                "source": "medlineplus_genetics", "source_url": source_url, "title": title,
                "authors": "MedlinePlus Genetics, U.S. National Library of Medicine",
                "published_at": normalize_space(" ".join(published_nodes[0].itertext())) if published_nodes else "",
                "modified_at": normalize_space(" ".join(reviewed_nodes[0].itertext())) if reviewed_nodes else "",
                "collected_at": collected_at, "language": "en", "abstract": sections[0]["text"],
                "sections": sections, "artifact_url": medline["genetics_xml_url"],
            }, config["processing"]["balance"]))
        summary_node.clear()
        while summary_node.getprevious() is not None:
            del summary_node.getparent()[0]
    index_response = requests.get(medline["medical_tests_index_url"], headers={"User-Agent": user_agent}, timeout=(20, timeout))
    index_response.raise_for_status()
    index_path = raw_dir / "medical_tests_index.html"
    index_path.write_bytes(index_response.content)
    raw_manifest.append({
        "url": medline["medical_tests_index_url"], "raw_path": str(index_path), "bytes": len(index_response.content),
        "sha256": sha256_bytes(index_response.content), "collected_at": collected_at,
    })
    soup = BeautifulSoup(index_response.content, "lxml")
    test_urls = sorted({
        str(anchor.get("href")) for anchor in soup.find_all("a")
        if anchor.get("href") and str(anchor.get("href")).startswith("https://medlineplus.gov/lab-tests/")
        and str(anchor.get("href")) != medline["medical_tests_index_url"]
    })
    failures = []
    def fetch_test(url: str) -> tuple[str, Path, dict[str, Any]]:
        path = raw_dir / "medical_tests" / f"{sha256_text(url)[:24]}.html"
        return url, path, fetch_to_path(url, path, user_agent, timeout)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_test, url): url for url in test_urls}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                _, path, manifest = future.result()
                raw_manifest.append(manifest)
                parsed = parse_medlineplus_medical_test(path.read_bytes(), url, collected_at)
                parsed["artifact_url"] = url
                documents.append(canonical_medline_document(parsed, config["processing"]["balance"]))
            except Exception as exc:
                failures.append({"url": url, "error": repr(exc)})
    collected_document_count = len(documents)
    unique = {}
    for document in documents:
        unique.setdefault(document["text_sha256"], document)
    documents = list(unique.values())
    chunks = make_chunks(
        documents, int(config["processing"]["maximum_chunk_characters"]),
        int(config["processing"]["minimum_chunk_characters"]),
    )
    chunks_by_document: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        chunks_by_document.setdefault(str(chunk["document_id"]), []).append(chunk)
    chunks = [
        chunk
        for document_chunks in chunks_by_document.values()
        for chunk in limit_chunks_per_document(
            document_chunks, int(config["processing"].get("maximum_chunks_per_document", 24))
        )
    ]
    document_by_id = {row["document_id"]: row for row in documents}
    for chunk in chunks:
        document = document_by_id[chunk["document_id"]]
        chunk["domain"] = document["domain"]
    staging = output / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    document_rows = []
    for document in documents:
        row = dict(document)
        row["sections_json"] = json.dumps(row.pop("sections"), ensure_ascii=False)
        document_rows.append(row)
    pq.write_table(pa.Table.from_pylist(document_rows), staging / "medlineplus_documents.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(chunks), staging / "medlineplus_chunks.parquet", compression="zstd")
    write_json(output / "raw" / "medlineplus" / "manifest.json", raw_manifest)
    domain_counts: dict[str, int] = {}
    for document in documents:
        domain_counts[document["domain"]] = domain_counts.get(document["domain"], 0) + 1
    report = {
        "generated_at": now(), "health_topic_and_genetics_and_test_document_count": len(documents),
        "medical_test_discovered_count": len(test_urls), "chunk_count": len(chunks),
        "duplicate_document_count": collected_document_count - len(documents), "failure_count": len(failures),
        "failures": failures, "domain_counts": domain_counts,
        "license": "U.S. Public Domain", "directory_bytes": directory_bytes(output),
    }
    write_json(output / "reports" / "medlineplus_bulk_report.json", report)
    if strict and failures:
        raise RuntimeError(f"MedlinePlus collection failures: {len(failures)}")
    return report


def collect_korean_public_documents(config: dict[str, Any], output: Path, strict: bool) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    source_config = config.get("korean_public", {})
    seeds = list(source_config.get("documents", []))
    raw_dir = output / "raw" / "korean_public"
    collected_at = now()
    user_agent = config["pmc"]["user_agent"]
    timeout = int(config["pmc"]["request_timeout_seconds"])
    documents: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    manifest = []
    navigation_markers = ("민원신청", "조직도", "부서안내", "오시는 길", "상담센터", "부정비리")
    for seed in seeds:
        url = str(seed["url"])
        raw_path = raw_dir / f"{sha256_text(url)[:24]}.html"
        try:
            manifest_row = fetch_to_path(url, raw_path, user_agent, timeout)
            manifest.append(manifest_row)
            document = parse_korean_public_document(raw_path.read_bytes(), url, collected_at)
            if document.get("license") not in {"KOGL Type 0", "KOGL Type 1"}:
                raise ValueError("individual document has no explicit KOGL Type 0 or Type 1 marker")
            cleaned_sections = [
                section for section in document.get("sections", [])
                if sum(marker in section.get("text", "") for marker in navigation_markers) < 3
            ]
            document["sections"] = cleaned_sections
            if not document.get("title") or not document.get("sections"):
                raise ValueError("document has no title or searchable body section")
            page_text = normalize_space(" ".join(BeautifulSoup(raw_path.read_bytes(), "lxml").stripped_strings))
            published_match = re.search(r"작성일\s*[:：]?\s*(20\d{2}[.-]\d{1,2}[.-]\d{1,2})", page_text)
            author_match = re.search(r"담당자\s*[:：]?\s*(.+?)\s+담당부서\s*[:：]", page_text)
            document.update({
                "source": "korean_public_documents",
                "title": normalize_space(cleaned_sections[0].get("section_title")) or normalize_space(document["title"]),
                "institution": str(seed.get("institution", "보건복지부")),
                "authors": normalize_space(author_match.group(1)) if author_match else "not_provided",
                "pmid": "not_applicable", "pmcid": "not_applicable",
                "published_at": published_match.group(1).replace(".", "-") if published_match else "not_provided",
                "modified_at": normalize_space(document.get("modified_at")) or "not_provided",
                "artifact_url": url, "collection_group": "korean_public_kogl",
                "domain": str(seed.get("domain") or "general_other"),
                "license_evidence": json.dumps({
                    "document_url": url, "detected_marker": document["license"],
                    "policy_url": document["license_url"],
                }, ensure_ascii=False, sort_keys=True),
            })
            full_text = "\n\n".join(section["text"] for section in document["sections"])
            document["text_sha256"] = sha256_text(full_text)
            document["document_id"] = stable_id("meddoc", document["source_url"], document["text_sha256"])
            documents.append(document)
        except Exception as exc:
            exclusions.append({"url": url, "reason": repr(exc), "collected_at": collected_at})
    unique: dict[str, dict[str, Any]] = {}
    for document in documents:
        unique.setdefault(str(document["text_sha256"]), document)
    documents = list(unique.values())
    chunks: list[dict[str, Any]] = []
    for document in documents:
        document_chunks = make_chunks(
            [document], int(config["processing"]["maximum_chunk_characters"]),
            int(config["processing"]["minimum_chunk_characters"]),
        )
        document_chunks = limit_chunks_per_document(
            document_chunks, int(config["processing"].get("maximum_chunks_per_document", 24))
        )
        for chunk in document_chunks:
            chunk["domain"] = document["domain"]
        chunks.extend(document_chunks)
    staging = output / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    document_rows = []
    for document in documents:
        row = dict(document)
        row["sections_json"] = json.dumps(row.pop("sections"), ensure_ascii=False)
        document_rows.append(row)
    documents_path = staging / "korean_public_documents.parquet"
    chunks_path = staging / "korean_public_chunks.parquet"
    if document_rows:
        pq.write_table(pa.Table.from_pylist(document_rows), documents_path, compression="zstd")
        pq.write_table(pa.Table.from_pylist(chunks), chunks_path, compression="zstd")
    navigation_contamination_count = sum(
        1 for document in documents for section in document["sections"]
        if sum(marker in section.get("text", "") for marker in navigation_markers) >= 3
    ) if seeds else 0
    write_json(raw_dir / "manifest.json", manifest)
    write_json(output / "reports" / "korean_public_exclusions.json", exclusions)
    report = {
        "generated_at": now(), "configured_document_count": len(seeds),
        "included_document_count": len(documents), "chunk_count": len(chunks),
        "excluded_document_count": len(exclusions), "exclusions": exclusions,
        "navigation_contamination_count": navigation_contamination_count,
        "allowed_licenses": ["KOGL Type 0", "KOGL Type 1"],
        "files": {
            "documents": documents_path.relative_to(output).as_posix() if document_rows else "",
            "chunks": chunks_path.relative_to(output).as_posix() if chunks else "",
        },
    }
    write_json(output / "reports" / "korean_public_report.json", report)
    if strict and (exclusions or len(documents) != len(seeds) or not chunks or navigation_contamination_count):
        raise RuntimeError("Korean public document collection failed strict validation")
    return report


def run(config_path: Path, stage: str, strict: bool) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = ROOT / config["output_dir"]
    output.mkdir(parents=True, exist_ok=True)
    if directory_bytes(output) >= int(config["storage"]["hard_limit_bytes"]):
        raise RuntimeError("storage hard limit already reached")
    if stage in {"plan", "all"}:
        discover_packages(config, output)
    report = None
    if stage in {"download", "all"}:
        if not (output / "catalogs" / "pmc_package_plan.json").is_file():
            discover_packages(config, output)
        report = download_packages(config, output)
        if strict and report["failed_count"]:
            raise RuntimeError(f"bulk download failures: {report['failed_count']}")
    if stage in {"scan", "process", "all"}:
        scan_archives(config, output, strict)
    if stage in {"supplement", "process", "all"}:
        collect_pmc_catalog_supplement(config, output, strict)
    if stage in {"select", "process", "all"}:
        build_balanced_selection(config, output, strict)
    if stage in {"medlineplus", "process", "all"}:
        collect_medlineplus_bulk(config, output, strict)
    if stage in {"korean", "process", "all"}:
        collect_korean_public_documents(config, output, strict)
    if stage in {"materialize", "process", "all"}:
        materialize_selected(config, output, strict)
    if stage in {"integrate", "process", "all"}:
        integrate_corpus(config, output, strict)
    return 0
