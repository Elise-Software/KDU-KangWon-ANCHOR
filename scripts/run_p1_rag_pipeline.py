"""Run the complete Wonju P1 RAG pipeline from collection through evaluation."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

from p1_rag import collect, process
from p1_rag.common import P1_ROOT, ROOT, read_json, read_jsonl, write_csv, write_json
from p1_rag.evaluate import run as run_evaluation
from p1_rag.models import EmbeddingIndex, OpenAICompatibleGenerator, Reranker


def protected_data_digest() -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    protected_roots = [
        ROOT / "data/collected/public_health",
        ROOT / "data/processed/public_health",
        ROOT / "data/normalized/public_health",
        ROOT / "data/integrated/wonju",
    ]
    protected_configs = sorted((ROOT / "config").glob("p0_data_03_*"))
    files = protected_configs[:]
    for directory in protected_roots:
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    for path in sorted(set(files)):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
        count += 1
    return digest.hexdigest(), count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config/p1_rag_config.json")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--skip-collection", action="store_true")
    parser.add_argument("--skip-evaluation", action="store_true")
    args = parser.parse_args()
    config = read_json(args.config)
    if args.strict and sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"P1 strict execution requires Python 3.12, got {platform.python_version()}")

    before_digest, protected_file_count = protected_data_digest()
    collection_report = (
        read_json(P1_ROOT / "reports/collection_report.json")
        if args.skip_collection
        else collect.run(config["collection"], strict=args.strict)
    )
    processing_report = process.run(config["chunking"], strict=args.strict)
    chunks = read_jsonl(P1_ROOT / "processed/chunks.jsonl")
    index = EmbeddingIndex(config["embedding"])
    index_report = index.use_existing(chunks) or index.build(chunks)
    reranker = Reranker(config["reranker"])
    generator = OpenAICompatibleGenerator(config["generation"])
    evaluation_report = (
        {"skipped": True, "integrity_checks_passed": True}
        if args.skip_evaluation
        else run_evaluation(config, index, reranker, generator, strict=args.strict)
    )
    after_digest, after_file_count = protected_data_digest()
    p0_unchanged = before_digest == after_digest and protected_file_count == after_file_count
    failures = []
    for stage, report in (
        ("collection", collection_report),
        ("processing", processing_report),
        ("index", index_report),
        ("evaluation", evaluation_report),
    ):
        if report.get("integrity_checks_passed") is False:
            failures.append({"stage": stage, "detail": "integrity checks failed"})
    if not p0_unchanged:
        failures.append({"stage": "p0_protection", "detail": "protected P0 files changed during P1 execution"})
    write_csv(P1_ROOT / "reports/failures_and_manual_review.csv", failures, ["stage", "detail"])
    final_report = {
        "python_version": platform.python_version(),
        "p0_protected_file_count": protected_file_count,
        "p0_before_sha256": before_digest,
        "p0_after_sha256": after_digest,
        "p0_files_unchanged": p0_unchanged,
        "collection": collection_report,
        "processing": processing_report,
        "index": index_report,
        "evaluation": evaluation_report,
        "reranker_model": config["reranker"]["model"],
        "generator_base_url": generator.base_url,
        "generator_model": generator.model_name,
        "generator_temperature": config["generation"]["temperature"],
        "failure_or_manual_review_count": len(failures),
        "source_and_config_files": [
            ".gitignore",
            "README.md",
            "requirements-p1.txt",
            "config/p1_rag_config.json",
            "config/p1_rag_safety_rules.json",
            "scripts/run_p1_rag_pipeline.py",
            "scripts/query_wonju_p1_rag.py",
            "scripts/p1_rag/common.py",
            "scripts/p1_rag/collect.py",
            "scripts/p1_rag/process.py",
            "scripts/p1_rag/models.py",
            "scripts/p1_rag/evaluate.py",
            "tests/test_p1_rag_pipeline.py",
        ],
        "artifact_locations": {
            "raw_documents": "data/p1_rag/raw/pages/",
            "document_manifest": "data/p1_rag/raw/document_manifest.csv",
            "clean_documents": "data/p1_rag/processed/documents_clean.jsonl",
            "chunks": "data/p1_rag/processed/chunks.jsonl",
            "institution_links": "data/p1_rag/processed/document_institution_links.csv",
            "service_links": "data/p1_rag/processed/document_service_links.csv",
            "faiss_index": "data/p1_rag/index/bge_m3.faiss",
            "embeddings": "data/p1_rag/index/chunk_embeddings.npy",
            "evaluation_set": "data/p1_rag/evaluation/evaluation_set.csv",
            "evaluation_results": "data/p1_rag/evaluation/evaluation_results.jsonl",
            "push_readiness_report": "data/p1_rag/reports/push_readiness_report.json",
            "reports": "data/p1_rag/reports/",
        },
        "execution_command": ".\\.venv-p1\\Scripts\\python.exe scripts\\run_p1_rag_pipeline.py --strict",
        "pytest_command": ".\\.venv-p1\\Scripts\\python.exe -m pytest -q",
        "integrity_checks_passed": not failures and p0_unchanged,
    }
    final_report["dataset_status"] = "verified" if final_report["integrity_checks_passed"] else "failed"
    write_json(P1_ROOT / "reports/p1_rag_pipeline_report.json", final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not final_report["integrity_checks_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
