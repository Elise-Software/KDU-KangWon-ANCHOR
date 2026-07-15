from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from p1_rag.common import read_csv, read_json, read_jsonl, sha256_text
from p1_rag.models import classify_risk


P1 = ROOT / "data/p1_rag"


@pytest.fixture(scope="module")
def artifacts() -> dict:
    paths = {
        "manifest": P1 / "raw/document_manifest.csv",
        "documents": P1 / "processed/documents_clean.jsonl",
        "chunks": P1 / "processed/chunks.jsonl",
        "institution_links": P1 / "processed/document_institution_links.csv",
        "service_links": P1 / "processed/document_service_links.csv",
        "evaluation_set": P1 / "evaluation/evaluation_set.csv",
        "evaluation_results": P1 / "evaluation/evaluation_results.jsonl",
        "evaluation_report": P1 / "reports/evaluation_report.json",
        "pipeline_report": P1 / "reports/p1_rag_pipeline_report.json",
        "index_report": P1 / "reports/index_report.json",
        "index": P1 / "index/bge_m3.faiss",
        "embeddings": P1 / "index/chunk_embeddings.npy",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    assert not missing, f"missing P1 artifacts: {missing}"
    return {
        "paths": paths,
        "manifest": read_csv(paths["manifest"]),
        "documents": read_jsonl(paths["documents"]),
        "chunks": read_jsonl(paths["chunks"]),
        "institution_links": read_csv(paths["institution_links"]),
        "service_links": read_csv(paths["service_links"]),
        "evaluation_set": read_csv(paths["evaluation_set"]),
        "evaluation_results": read_jsonl(paths["evaluation_results"]),
        "pipeline_report": read_json(paths["pipeline_report"]),
        "evaluation_report": read_json(paths["evaluation_report"]),
        "index_report": read_json(paths["index_report"]),
    }


def test_official_documents_and_raw_provenance(artifacts: dict) -> None:
    rows = artifacts["manifest"]
    assert len(rows) >= 50
    assert len(rows) == len({row["doc_id"] for row in rows})
    assert all(
        urlparse(row["url"]).netloc in {"www.wonju.go.kr", "loveme.yonsei.kr"}
        for row in rows
    )
    assert all(re.fullmatch(r"20\d{2}-\d{2}-\d{2}", row["reference_date"]) for row in rows)
    for row in rows:
        raw = ROOT / row["raw_path"]
        assert raw.is_file()
        assert hashlib.sha256(raw.read_bytes()).hexdigest() == row["sha256"]


def test_cleaning_deduplication_and_semantic_chunks(artifacts: dict) -> None:
    documents = artifacts["documents"]
    chunks = artifacts["chunks"]
    assert sum(row["dedup_status"] == "canonical" for row in documents) >= 50
    assert len(chunks) >= 50
    assert len(chunks) == len({row["chunk_id"] for row in chunks})
    assert all(row["text"] and row["title"] and row["section_title"] for row in chunks)
    assert all(row["evidence_hash"] == sha256_text(row["text"]) for row in chunks)
    assert all(row["url"] and row["reference_date"] and row["raw_sha256"] for row in chunks)


def test_master_and_service_links_are_valid(artifacts: dict) -> None:
    master = read_csv(ROOT / "data/integrated/wonju/institutions_p0_public_health_enriched.csv")
    services = read_csv(ROOT / "data/integrated/wonju/institution_services.csv")
    master_ids = {row["institution_id"] for row in master}
    service_ids = {row["service_id"] for row in services}
    chunk_ids = {row["chunk_id"] for row in artifacts["chunks"]}
    assert len(master) == 2487
    assert artifacts["institution_links"]
    assert artifacts["service_links"]
    assert all(row["institution_id"] in master_ids for row in artifacts["institution_links"])
    assert all(row["chunk_id"] in chunk_ids for row in artifacts["institution_links"])
    assert all(row["service_id"] in service_ids for row in artifacts["service_links"])
    assert all(row["chunk_id"] in chunk_ids for row in artifacts["service_links"])


def test_embedding_and_faiss_index(artifacts: dict) -> None:
    import faiss
    import numpy as np

    report = artifacts["index_report"]
    index = faiss.read_index(str(artifacts["paths"]["index"]))
    vectors = np.load(artifacts["paths"]["embeddings"])
    assert report["embedding_model"] == "BAAI/bge-m3"
    assert report["backend"] == "faiss.IndexFlatIP"
    assert report["vector_dimension"] == 1024
    assert index.ntotal == vectors.shape[0] == len(artifacts["chunks"])
    assert index.d == vectors.shape[1] == 1024


def test_evaluation_metrics_and_citations(artifacts: dict) -> None:
    cases = artifacts["evaluation_set"]
    results = artifacts["evaluation_results"]
    report = artifacts["evaluation_report"]
    chunk_by_id = {row["chunk_id"]: row for row in artifacts["chunks"]}
    assert len(cases) >= 100
    assert len(results) == len(cases)
    assert report["retrieval_recall_at_5"] >= 0.75
    assert report["mean_reciprocal_rank"] >= 0.55
    assert report["answer_groundedness"] >= 0.55
    assert report["citation_accuracy"] >= 0.99
    assert report["safety_rule_pass_rate"] == 1.0
    assert report["reranker_model"] == "BAAI/bge-reranker-v2-m3"
    for result in results:
        assert result["citations"]
        for citation in result["citations"]:
            chunk = chunk_by_id[citation["chunk_id"]]
            assert citation["url"] == chunk["url"]
            assert citation["doc_id"] == chunk["doc_id"]
            assert citation["document"] == chunk["title"]


def test_safety_rules_take_priority() -> None:
    rules = read_json(ROOT / "config/p1_rag_safety_rules.json")
    assert classify_risk("죽고 싶고 자살할 생각입니다", rules)[0] == "suicide"
    assert classify_risk("의식이 없고 숨을 못 쉬어요", rules)[0] == "emergency"
    assert classify_risk("약물 중독과 과다복용", rules)[0] == "addiction"
    assert classify_risk("임신 중 약 복용량을 정해줘", rules)[0] == "medical_high_risk"


def test_pipeline_report_is_strict_and_p0_is_unchanged(artifacts: dict) -> None:
    report = artifacts["pipeline_report"]
    assert report["python_version"].startswith("3.12.")
    assert report["p0_files_unchanged"]
    assert report["p0_before_sha256"] == report["p0_after_sha256"]
    assert report["failure_or_manual_review_count"] == 0
    assert report["generator_base_url"] == "http://192.168.100.58:8000/v1"
    assert report["generator_model"]
    assert report["generator_temperature"] == 0
    assert report["reranker_model"] == "BAAI/bge-reranker-v2-m3"
    assert report["integrity_checks_passed"]
    assert report["dataset_status"] == "verified"
