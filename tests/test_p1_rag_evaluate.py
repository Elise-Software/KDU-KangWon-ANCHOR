from __future__ import annotations

import sys
import re
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from p1_rag.common import read_json, read_jsonl, write_jsonl
import p1_rag.evaluate as evaluate_module
from p1_rag.evaluate import (
    CURRENT_GENERATION_POLICY_VERSION,
    EVALUATOR_VERSION,
    can_reuse_generation,
    can_reuse_retrieval,
    citation_evidence_relevance_score,
    citation_pointer_integrity_score,
    create_eval_set,
    groundedness_score,
    safe_average,
)
from p1_rag.models import answer


def _case(case_type: str = "factual") -> dict[str, str]:
    return {
        "eval_id": "p1eval:test",
        "case_type": case_type,
        "question": "원주시보건소 대표전화는 무엇인가요?",
        "expected_answer": "원주시보건소 대표전화 안내",
        "expected_doc_id": "p1doc:expected",
        "expected_chunk_id": "p1chunk:expected",
        "expected_url": "https://example.test/expected",
        "expected_risk_category": "none",
    }


def _generator() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model", config={"temperature": 0})


def test_old_generation_policy_is_not_reused_and_current_policy_is_synced() -> None:
    case = _case()
    previous = {
        "generator_model": "test-model",
        "temperature": 0,
        "generation_policy_version": "single_context_v1",
        "risk_category": "none",
        "answer": "old answer",
        "citations": [],
    }
    assert not can_reuse_generation(case, previous, _generator())

    previous["generation_policy_version"] = CURRENT_GENERATION_POLICY_VERSION
    assert can_reuse_generation(case, previous, _generator())
    previous["risk_category"] = "emergency"
    assert not can_reuse_generation(case, previous, _generator())

    rules = read_json(ROOT / "config" / "p1_rag_safety_rules.json")
    current = answer("원주시보건소 안내를 알려주세요.", [], _generator(), rules)
    assert current["generation_policy_version"] == CURRENT_GENERATION_POLICY_VERSION


def test_old_evaluator_or_changed_index_signature_rejects_retrieval_reuse() -> None:
    case = _case()
    chunks = {"p1chunk:expected": {"chunk_id": "p1chunk:expected"}}
    previous = {
        "question": case["question"],
        "evaluator_version": "p1_rag_evaluator_v1",
        "retrieval_corpus_signature": "current-signature",
        "top_results": [{"chunk_id": "p1chunk:expected"}],
    }
    assert not can_reuse_retrieval(case, previous, chunks, "current-signature")

    previous["evaluator_version"] = EVALUATOR_VERSION
    assert can_reuse_retrieval(case, previous, chunks, "current-signature")
    assert not can_reuse_retrieval(case, previous, chunks, "changed-signature")


def test_citation_pointer_integrity_and_evidence_relevance_are_independent() -> None:
    rules = read_json(ROOT / "config" / "p1_rag_safety_rules.json")
    chunks = {
        "p1chunk:expected": {
            "chunk_id": "p1chunk:expected",
            "doc_id": "p1doc:expected",
            "url": "https://example.test/expected",
            "title": "원주시보건소 대표전화 안내",
            "text": "원주시보건소 대표전화 안내",
        },
        "p1chunk:other": {
            "chunk_id": "p1chunk:other",
            "doc_id": "p1doc:other",
            "url": "https://example.test/other",
            "title": "무관한 문서",
            "text": "재활용품 배출 요일",
        },
    }
    broken_pointer = {
        "citations": [{
            "chunk_id": "p1chunk:expected",
            "doc_id": "p1doc:expected",
            "url": "https://example.test/expected",
            "document": "잘못된 표시 제목",
        }],
    }
    assert citation_pointer_integrity_score(broken_pointer, chunks) == 0.0
    assert citation_evidence_relevance_score(_case(), broken_pointer, chunks, rules) == 1.0

    valid_but_irrelevant = {
        "citations": [{
            "chunk_id": "p1chunk:other",
            "doc_id": "p1doc:other",
            "url": "https://example.test/other",
            "document": "무관한 문서",
        }],
    }
    assert citation_pointer_integrity_score(valid_but_irrelevant, chunks) == 1.0
    assert citation_evidence_relevance_score(_case(), valid_but_irrelevant, chunks, rules) == 0.0


def test_eval_set_contains_explicit_safety_negative_cases() -> None:
    rules = read_json(ROOT / "config" / "p1_rag_safety_rules.json")
    chunks = [
        {
            "chunk_id": f"p1chunk:{index:03d}",
            "doc_id": f"p1doc:{index:03d}",
            "chunk_index": 0,
            "title": f"문서 {index}",
            "section_title": "안내",
            "text": f"원주시 공식 정보 {index} 응급 119 109 중독관리통합지원센터",
            "url": f"https://example.test/{index}",
        }
        for index in range(100)
    ]
    cases = create_eval_set(chunks, 100, rules)
    negatives = [row for row in cases if row["case_type"] == "safety_negative"]
    assert len(cases) == 117
    assert len(negatives) == 5
    assert all(row["expected_risk_category"] == "none" for row in negatives)
    assert len({row["eval_id"] for row in cases}) == len(cases)


def test_safe_average_handles_empty_groups() -> None:
    assert safe_average([]) == 0.0
    assert safe_average([True, False]) == 0.5


def test_groundedness_does_not_penalize_explicit_abstention() -> None:
    answer_text = "죄송합니다. 제공해주신 CONTEXT에는 요청한 문서가 포함되어 있지 않습니다."
    assert groundedness_score(answer_text, []) == 1.0


def test_run_reports_separated_metrics_and_reuse_contract(tmp_path, monkeypatch) -> None:
    chunks = [
        {
            "chunk_id": f"p1chunk:{index:03d}",
            "doc_id": f"p1doc:{index:03d}",
            "chunk_index": 0,
            "title": f"문서 {index}",
            "section_title": "안내",
            "text": (
                f"원주시 공식 정보 {index} 응급 상황은 119에 신고하고 "
                "자살예방상담은 109, 중독 상담은 중독관리통합지원센터에서 확인합니다. 의료진 안내입니다."
            ),
            "url": f"https://example.test/{index}",
            "evidence_hash": f"evidence-{index}",
        }
        for index in range(100)
    ]
    monkeypatch.setattr(evaluate_module, "P1_ROOT", tmp_path)
    write_jsonl(tmp_path / "processed" / "chunks.jsonl", chunks)

    class FakeIndex:
        def search(self, questions, candidate_count):
            groups = []
            for question in questions:
                match = re.search(r"문서 '(?:문서 )?(\d+)'", question)
                row = chunks[int(match.group(1))] if match else chunks[0]
                groups.append([{**row, "embedding_score": 1.0}])
            return groups

    class FakeReranker:
        def rerank_many(self, questions, groups):
            return [[{**row, "reranker_score": 1.0} for row in group] for group in groups]

    class FakeGenerator:
        model_name = "test-model"
        base_url = "http://localhost/v1"
        config = {"temperature": 0}

        def generate(self, question, contexts):
            return contexts[0]["text"]

    config = {
        "embedding": {"model": "test-embedding"},
        "reranker": {"model": "test-reranker", "candidate_count": 5},
        "evaluation": {
            "minimum_cases": 100,
            "retrieval_top_k": 5,
            "minimum_recall_at_5": 0.75,
            "minimum_mrr": 0.55,
            "minimum_groundedness": 0.55,
            "minimum_citation_accuracy": 0.99,
            "minimum_safety_pass_rate": 1.0,
        },
    }
    report = evaluate_module.run(
        config, FakeIndex(), FakeReranker(), FakeGenerator(), strict=True,
    )
    results = read_jsonl(tmp_path / "evaluation" / "evaluation_results.jsonl")
    assert report["evaluator_version"] == EVALUATOR_VERSION
    assert report["generation_policy_version"] == CURRENT_GENERATION_POLICY_VERSION
    assert report["factual_case_count"] == 100
    assert report["safety_case_count"] == 12
    assert report["safety_negative_case_count"] == 5
    assert report["factual_answer_groundedness"] == report["answer_groundedness"]
    assert report["citation_pointer_integrity"] == report["citation_accuracy"] == 1.0
    assert report["safety_negative_pass_rate"] == 1.0
    assert report["safety_rule_precision"] == 1.0
    assert report["reused_previous_retrieval_count"] == 0
    assert report["reused_previous_generation_count"] == 0
    assert all(row["groundedness_score"] is None for row in results if row["case_type"] != "factual")
    assert all(row["evaluator_version"] == EVALUATOR_VERSION for row in results)
    assert all(
        row["retrieval_corpus_signature"] == report["retrieval_corpus_signature"]
        for row in results
    )

    reused = evaluate_module.run(
        config, FakeIndex(), FakeReranker(), FakeGenerator(), strict=True,
    )
    assert reused["reused_previous_retrieval_count"] == 117
    assert reused["reused_previous_generation_count"] == 105
