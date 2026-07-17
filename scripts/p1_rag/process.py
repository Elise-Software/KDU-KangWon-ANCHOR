from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from .common import (
    P1_ROOT, ROOT, normalize_key, normalize_space, read_csv, sha256_bytes,
    sha256_text, stable_id, write_csv, write_json, write_jsonl,
)


DOCUMENT_COLUMNS = [
    "doc_id", "canonical_doc_id", "dedup_status", "title", "category", "url",
    "reference_date", "reference_date_basis", "retrieved_at", "raw_path",
    "raw_sha256", "clean_sha256", "clean_character_count", "section_count",
]
CHUNK_COLUMNS = [
    "chunk_id", "doc_id", "chunk_index", "title", "section_title", "text",
    "url", "reference_date", "raw_sha256", "evidence_hash", "character_count",
]
INSTITUTION_LINK_COLUMNS = [
    "link_id", "doc_id", "chunk_id", "institution_id", "institution_name",
    "link_method", "matched_text", "confidence",
]
SERVICE_LINK_COLUMNS = [
    "link_id", "doc_id", "chunk_id", "service_id", "institution_id",
    "service_name", "link_method", "matched_text", "confidence",
]


def extract_sections(html: bytes, title: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("#contents") or soup.select_one(".sub_contents") or soup.select_one("main") or soup.body
    if not root:
        return []
    for node in root.select("script, style, noscript, nav, form, button, svg"):
        node.decompose()
    sections: list[dict[str, str]] = []
    current_heading = title
    seen: set[str] = set()
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "caption", "dt", "dd", "tr"]):
        if not isinstance(node, Tag):
            continue
        text = normalize_space(node.get_text(" ", strip=True))
        if len(text) < 2:
            continue
        if node.name and node.name.startswith("h"):
            current_heading = text
            continue
        key = normalize_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        sections.append({"heading": current_heading, "text": text})
    if not sections:
        text = normalize_space(root.get_text(" ", strip=True))
        if text:
            sections.append({"heading": title, "text": text})
    return sections


def build_chunks(
    doc: dict[str, Any], sections: list[dict[str, str]], chunk_config: dict
) -> list[dict[str, Any]]:
    maximum = int(chunk_config["maximum_characters"])
    overlap = int(chunk_config["overlap_characters"])
    minimum = int(chunk_config["minimum_characters"])
    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    heading = doc["title"]

    def flush() -> None:
        nonlocal buffer
        text = normalize_space(" ".join(buffer))
        if len(text) < minimum:
            return
        chunk_index = len(chunks)
        chunk_id = stable_id("p1chunk", doc["doc_id"], str(chunk_index), text)
        chunks.append({
            "chunk_id": chunk_id,
            "doc_id": doc["doc_id"],
            "chunk_index": chunk_index,
            "title": doc["title"],
            "section_title": heading,
            "text": text,
            "url": doc["url"],
            "reference_date": doc["reference_date"],
            "raw_sha256": doc["raw_sha256"],
            "evidence_hash": sha256_text(text),
            "character_count": len(text),
        })
        tail = text[-overlap:] if overlap else ""
        buffer = [tail] if tail else []

    for section in sections:
        value = section["text"]
        if section["heading"] != heading and buffer:
            flush()
        heading = section["heading"]
        prefix = f"{doc['title']} | {heading} | "
        if not buffer:
            buffer.append(prefix)
        if len(" ".join(buffer)) + len(value) + 1 > maximum and len(buffer) > 1:
            flush()
            buffer.append(prefix)
        if len(value) > maximum:
            sentences = re.split(r"(?<=[.!?다요함됨])\s+", value)
            for sentence in sentences:
                if len(" ".join(buffer)) + len(sentence) + 1 > maximum and len(buffer) > 1:
                    flush()
                    buffer.append(prefix)
                buffer.append(sentence)
        else:
            buffer.append(value)
    flush()
    # A short factual page (for example a compact address/directions page)
    # must remain retrievable even when it is shorter than the normal chunk
    # floor. Dropping the whole canonical document is worse than keeping one
    # small, well-scoped evidence unit.
    if not chunks:
        text = normalize_space(
            f"{doc['title']} | " + " ".join(
                f"{section['heading']} | {section['text']}" for section in sections
            )
        )
        if text:
            chunk_id = stable_id("p1chunk", doc["doc_id"], "0", text)
            chunks.append({
                "chunk_id": chunk_id,
                "doc_id": doc["doc_id"],
                "chunk_index": 0,
                "title": doc["title"],
                "section_title": sections[0]["heading"] if sections else doc["title"],
                "text": text,
                "url": doc["url"],
                "reference_date": doc["reference_date"],
                "raw_sha256": doc["raw_sha256"],
                "evidence_hash": sha256_text(text),
                "character_count": len(text),
            })
    return chunks


def link_entities(chunks: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    institutions = read_csv(ROOT / "data/integrated/wonju/institutions_p0_public_health_enriched.csv")
    services_path = ROOT / "data/integrated/wonju/institution_services.csv"
    services = read_csv(services_path) if services_path.is_file() else []
    institution_terms = []
    for row in institutions:
        term = normalize_key(row.get("name", ""))
        if len(term) >= 4:
            institution_terms.append((term, row))
    service_terms = []
    for row in services:
        term = normalize_key(row.get("service_name", ""))
        if len(term) >= 4:
            service_terms.append((term, row))

    institution_links: list[dict] = []
    service_links: list[dict] = []
    seen_institutions: set[tuple[str, str]] = set()
    seen_services: set[tuple[str, str]] = set()
    for chunk in chunks:
        searchable = normalize_key(chunk["text"])
        for term, institution in institution_terms:
            key = (chunk["chunk_id"], institution["institution_id"])
            if term in searchable and key not in seen_institutions:
                seen_institutions.add(key)
                institution_links.append({
                    "link_id": stable_id("p1instlink", *key),
                    "doc_id": chunk["doc_id"],
                    "chunk_id": chunk["chunk_id"],
                    "institution_id": institution["institution_id"],
                    "institution_name": institution["name"],
                    "link_method": "exact_normalized_institution_name",
                    "matched_text": institution["name"],
                    "confidence": "1.0000",
                })
        for term, service in service_terms:
            key = (chunk["chunk_id"], service["service_id"])
            if term in searchable and key not in seen_services:
                seen_services.add(key)
                service_links.append({
                    "link_id": stable_id("p1servicelink", *key),
                    "doc_id": chunk["doc_id"],
                    "chunk_id": chunk["chunk_id"],
                    "service_id": service["service_id"],
                    "institution_id": service["institution_id"],
                    "service_name": service["service_name"],
                    "link_method": "exact_normalized_service_name",
                    "matched_text": service["service_name"],
                    "confidence": "1.0000",
                })
    return institution_links, service_links


def run(chunk_config: dict, strict: bool = False) -> dict:
    manifest = read_csv(P1_ROOT / "raw" / "document_manifest.csv")
    documents: list[dict[str, Any]] = []
    all_chunks: list[dict[str, Any]] = []
    canonical_by_hash: dict[str, str] = {}
    for row in manifest:
        raw_path = ROOT / row["raw_path"]
        raw = raw_path.read_bytes()
        if sha256_bytes(raw) != row["sha256"]:
            raise RuntimeError(f"raw SHA-256 mismatch: {raw_path}")
        sections = extract_sections(raw, row["title"])
        clean_text = "\n".join(f"{item['heading']}\t{item['text']}" for item in sections)
        clean_hash = sha256_text(normalize_key(clean_text))
        canonical_doc_id = canonical_by_hash.setdefault(clean_hash, row["doc_id"])
        dedup_status = "canonical" if canonical_doc_id == row["doc_id"] else "duplicate"
        document = {
            **row,
            "canonical_doc_id": canonical_doc_id,
            "dedup_status": dedup_status,
            "raw_sha256": row["sha256"],
            "clean_sha256": clean_hash,
            "clean_character_count": len(clean_text),
            "section_count": len(sections),
            "sections": sections,
        }
        documents.append(document)
        if dedup_status == "canonical":
            all_chunks.extend(build_chunks(document, sections, chunk_config))

    institution_links, service_links = link_entities(all_chunks)
    processed = P1_ROOT / "processed"
    write_jsonl(processed / "documents_clean.jsonl", documents)
    write_csv(processed / "document_metadata.csv", documents, DOCUMENT_COLUMNS)
    write_jsonl(processed / "chunks.jsonl", all_chunks)
    write_csv(processed / "chunks.csv", all_chunks, CHUNK_COLUMNS)
    write_csv(processed / "document_institution_links.csv", institution_links, INSTITUTION_LINK_COLUMNS)
    write_csv(processed / "document_service_links.csv", service_links, SERVICE_LINK_COLUMNS)

    canonical_count = sum(row["dedup_status"] == "canonical" for row in documents)
    chunk_ids = [row["chunk_id"] for row in all_chunks]
    master_ids = {
        row["institution_id"]
        for row in read_csv(ROOT / "data/integrated/wonju/institutions_p0_public_health_enriched.csv")
    }
    checks = {
        "canonical_documents_at_least_50": canonical_count >= 50,
        "chunks_present": bool(all_chunks),
        "chunk_ids_unique": len(chunk_ids) == len(set(chunk_ids)),
        "chunk_hashes_valid": all(row["evidence_hash"] == sha256_text(row["text"]) for row in all_chunks),
        "institution_links_present": bool(institution_links),
        "institution_link_foreign_keys_valid": all(row["institution_id"] in master_ids for row in institution_links),
        "service_links_present": bool(service_links),
        "source_provenance_complete": all(row["url"] and row["reference_date"] and row["raw_sha256"] for row in all_chunks),
    }
    report = {
        "input_document_count": len(documents),
        "canonical_document_count": canonical_count,
        "duplicate_document_count": len(documents) - canonical_count,
        "chunk_count": len(all_chunks),
        "institution_link_count": len(institution_links),
        "linked_institution_count": len({row["institution_id"] for row in institution_links}),
        "service_link_count": len(service_links),
        "linked_service_count": len({row["service_id"] for row in service_links}),
        "integrity_checks": checks,
        "integrity_checks_passed": all(checks.values()),
    }
    write_json(P1_ROOT / "reports" / "processing_report.json", report)
    if strict and not report["integrity_checks_passed"]:
        raise RuntimeError(f"P1 processing strict checks failed: {report}")
    return report
