from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
import requests

from .common import P1_ROOT, ROOT, read_json, read_jsonl, sha256_bytes, write_json, write_jsonl


MODEL_CACHE = ROOT / ".cache" / "p1_rag_models"


class EmbeddingIndex:
    def __init__(self, embedding_config: dict[str, Any]) -> None:
        import faiss
        from sentence_transformers import SentenceTransformer

        self.faiss = faiss
        self.config = embedding_config
        self.model = SentenceTransformer(
            embedding_config["model"],
            device="cpu",
            cache_folder=str(MODEL_CACHE),
        )
        self.index = None
        self.chunks: list[dict[str, Any]] = []

    def build(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        index_dir = P1_ROOT / "index"
        metadata_path = index_dir / "chunk_metadata.jsonl"
        vectors_path = index_dir / "chunk_embeddings.npy"
        reusable: dict[tuple[str, str], np.ndarray] = {}
        if metadata_path.is_file() and vectors_path.is_file():
            old_chunks = read_jsonl(metadata_path)
            old_vectors = np.load(vectors_path)
            if len(old_chunks) == len(old_vectors):
                reusable = {
                    (row["chunk_id"], row["evidence_hash"]): old_vectors[position]
                    for position, row in enumerate(old_chunks)
                }
        vectors = np.empty((len(chunks), 1024), dtype="float32")
        missing_positions = [
            position for position, row in enumerate(chunks)
            if (row["chunk_id"], row["evidence_hash"]) not in reusable
        ]
        for position, row in enumerate(chunks):
            key = (row["chunk_id"], row["evidence_hash"])
            if key in reusable:
                vectors[position] = reusable[key]
        if missing_positions:
            texts = [
                f"{chunks[position]['title']}\n{chunks[position]['section_title']}\n{chunks[position]['text']}"
                for position in missing_positions
            ]
            encoded = self.model.encode(
                texts,
                batch_size=int(self.config["batch_size"]),
                normalize_embeddings=bool(self.config["normalize_embeddings"]),
                convert_to_numpy=True,
                show_progress_bar=True,
            ).astype("float32")
            if encoded.shape[1] != 1024:
                raise RuntimeError(f"BAAI/bge-m3 returned unexpected dimension {encoded.shape[1]}")
            for position, vector in zip(missing_positions, encoded):
                vectors[position] = vector
        self.index = self.faiss.IndexFlatIP(vectors.shape[1])
        self.index.add(vectors)
        self.chunks = chunks
        index_dir.mkdir(parents=True, exist_ok=True)
        self.faiss.write_index(self.index, str(index_dir / "bge_m3.faiss"))
        np.save(index_dir / "chunk_embeddings.npy", vectors)
        write_jsonl(index_dir / "chunk_metadata.jsonl", chunks)
        config = getattr(self.model[0].auto_model, "config", None)
        model_revision = getattr(config, "_commit_hash", "") if config else ""
        report = {
            "embedding_model": self.config["model"],
            "model_revision": model_revision or "resolved_from_huggingface_cache",
            "backend": "faiss.IndexFlatIP",
            "metric": "cosine_via_normalized_inner_product",
            "vector_count": int(vectors.shape[0]),
            "vector_dimension": int(vectors.shape[1]),
            "normalized_embeddings": bool(self.config["normalize_embeddings"]),
            "index_file": "data/p1_rag/index/bge_m3.faiss",
            "embedding_file": "data/p1_rag/index/chunk_embeddings.npy",
            "index_sha256": sha256_bytes((index_dir / "bge_m3.faiss").read_bytes()),
            "reused_embedding_count": len(chunks) - len(missing_positions),
            "new_embedding_count": len(missing_positions),
        }
        report["integrity_checks"] = {
            "vector_count_matches_chunks": report["vector_count"] == len(chunks),
            "bge_m3_dimension_is_1024": report["vector_dimension"] == 1024,
            "faiss_index_count_matches": int(self.index.ntotal) == len(chunks),
            "index_sha256_valid": len(report["index_sha256"]) == 64,
        }
        report["integrity_checks_passed"] = all(report["integrity_checks"].values())
        write_json(P1_ROOT / "reports" / "index_report.json", report)
        return report

    def use_existing(self, chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
        index_dir = P1_ROOT / "index"
        index_path = index_dir / "bge_m3.faiss"
        vectors_path = index_dir / "chunk_embeddings.npy"
        metadata_path = index_dir / "chunk_metadata.jsonl"
        if not (index_path.is_file() and vectors_path.is_file() and metadata_path.is_file()):
            return None
        stored = read_jsonl(metadata_path)
        signature = [(row["chunk_id"], row["evidence_hash"]) for row in chunks]
        stored_signature = [(row["chunk_id"], row["evidence_hash"]) for row in stored]
        if signature != stored_signature:
            return None
        self.load()
        vectors = np.load(vectors_path, mmap_mode="r")
        report = {
            "embedding_model": self.config["model"],
            "model_revision": "resolved_from_huggingface_cache",
            "backend": "faiss.IndexFlatIP",
            "metric": "cosine_via_normalized_inner_product",
            "vector_count": int(vectors.shape[0]),
            "vector_dimension": int(vectors.shape[1]),
            "normalized_embeddings": bool(self.config["normalize_embeddings"]),
            "index_file": "data/p1_rag/index/bge_m3.faiss",
            "embedding_file": "data/p1_rag/index/chunk_embeddings.npy",
            "index_sha256": sha256_bytes(index_path.read_bytes()),
            "reused_verified_index": True,
        }
        report["integrity_checks"] = {
            "vector_count_matches_chunks": report["vector_count"] == len(chunks),
            "bge_m3_dimension_is_1024": report["vector_dimension"] == 1024,
            "faiss_index_count_matches": int(self.index.ntotal) == len(chunks),
            "index_sha256_valid": len(report["index_sha256"]) == 64,
        }
        report["integrity_checks_passed"] = all(report["integrity_checks"].values())
        write_json(P1_ROOT / "reports" / "index_report.json", report)
        return report

    def load(self) -> None:
        self.index = self.faiss.read_index(str(P1_ROOT / "index" / "bge_m3.faiss"))
        self.chunks = read_jsonl(P1_ROOT / "index" / "chunk_metadata.jsonl")

    def search(self, queries: list[str], count: int) -> list[list[dict[str, Any]]]:
        if self.index is None:
            self.load()
        vectors = self.model.encode(
            queries,
            batch_size=int(self.config["batch_size"]),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype("float32")
        scores, positions = self.index.search(vectors, min(count, len(self.chunks)))
        output: list[list[dict[str, Any]]] = []
        for query_scores, query_positions in zip(scores, positions):
            rows = []
            for score, position in zip(query_scores, query_positions):
                if position < 0:
                    continue
                rows.append({**self.chunks[int(position)], "embedding_score": float(score)})
            output.append(rows)
        return output


class Reranker:
    def __init__(self, reranker_config: dict[str, Any]) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        self.config = reranker_config
        self.tokenizer = AutoTokenizer.from_pretrained(
            reranker_config["model"], cache_dir=str(MODEL_CACHE)
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            reranker_config["model"], cache_dir=str(MODEL_CACHE)
        )
        self.model.eval()

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.rerank_many([query], [candidates])[0]

    def rerank_many(
        self,
        queries: list[str],
        candidate_groups: list[list[dict[str, Any]]],
    ) -> list[list[dict[str, Any]]]:
        if len(queries) != len(candidate_groups):
            raise ValueError("queries and candidate_groups must have the same length")
        flat_pairs: list[list[str]] = []
        group_sizes: list[int] = []
        for query, candidates in zip(queries, candidate_groups):
            group_sizes.append(len(candidates))
            flat_pairs.extend([[query, row["text"]] for row in candidates])
        if not flat_pairs:
            return [[] for _ in queries]
        scores: list[float] = []
        batch_size = int(self.config["batch_size"])
        with self.torch.inference_mode():
            for start in range(0, len(flat_pairs), batch_size):
                encoded = self.tokenizer(
                    flat_pairs[start:start + batch_size],
                    padding=True,
                    truncation=True,
                    max_length=int(self.config["maximum_length"]),
                    return_tensors="pt",
                )
                logits = self.model(**encoded, return_dict=True).logits.view(-1)
                scores.extend(float(value) for value in logits.cpu())
        output: list[list[dict[str, Any]]] = []
        offset = 0
        for candidates, size in zip(candidate_groups, group_sizes):
            group_scores = scores[offset:offset + size]
            offset += size
            reranked = [dict(row, reranker_score=score) for row, score in zip(candidates, group_scores)]
            reranked.sort(key=lambda row: row["reranker_score"], reverse=True)
            output.append(reranked)
        return output


class OpenAICompatibleGenerator:
    def __init__(self, generation_config: dict[str, Any]) -> None:
        self.config = generation_config
        self.base_url = os.getenv("VLLM_BASE_URL", generation_config["base_url"]).strip().rstrip("/")
        self.model_name = self.discover_model()

    def discover_model(self) -> str:
        response = requests.get(
            f"{self.base_url}/models", timeout=int(self.config["timeout_seconds"])
        )
        response.raise_for_status()
        models = [row["id"] for row in response.json().get("data", []) if row.get("id")]
        if not models:
            raise RuntimeError("OpenAI-compatible /v1/models returned no model")
        return models[0]

    def generate(self, question: str, contexts: list[dict[str, Any]]) -> str:
        context_text = "\n\n".join(
            f"[{row['chunk_id']}] 문서={row['title']} URL={row['url']}\n{row['text']}"
            for row in contexts
        )
        system = (
            "당신은 원주시 주민에게 친절하고 따뜻하게 설명하는 생활건강 안내자다. "
            "먼저 사용자의 질문 의도를 정확히 파악하고, 제공된 CONTEXT에 명시된 사실만 사용한다. "
            "증상이나 건강 고민 질문에는 아래 네 제목을 순서대로 사용해 답한다. "
            "'1. 먼저 마음부터'에서는 걱정되는 마음과 불편함에 짧게 공감한다. 이 공감 문장에서는 "
            "증상의 시작 방식·시각·원인을 다시 묘사하지 않는다. "
            "'2. 생각해볼 수 있는 원인'에서는 가능한 원인을 단정하지 말고 일반적인 가능성으로 설명한다. "
            "CONTEXT의 설명이 금연, 임신, 특정 질환, 특정 연령처럼 별도 조건을 전제로 한다면 사용자가 그 조건을 "
            "직접 말한 경우에만 원인 설명에 사용한다. 사용자가 말하지 않은 상태를 추정하거나 끼워 맞추지 않는다. "
            "QUESTION에 '처음 증상'과 '확인 답변'이 있으면 하나의 문진 기록으로 읽는다. 뒤의 확인 답변은 앞의 "
            "모호한 표현을 구체화한 것이므로, 서로 충돌할 때는 뒤에서 사용자가 명시한 답을 우선한다. 사용자가 "
            "서서히 심해졌다고 답했다면 갑자기 시작됐다고 바꾸지 말고, 없다고 답한 위험 신호를 있다고 가정하지 않는다. "
            "'3. 지금 할 수 있는 대처'에서는 집에서 할 수 있는 안전한 생활 대처와 진료가 필요한 신호를 안내한다. "
            "'4. 상비의약품 안내'에서는 CONTEXT에 근거가 있을 때만 일반의약품 또는 상비의약품 범위의 선택지를 설명하고, "
            "개인별 복용 가능 여부와 용량은 약사나 의료진에게 확인하도록 안내한다. 근거가 없으면 약을 지어내지 말고 "
            "'현재 근거만으로 특정 상비약을 권하기 어렵습니다'라고 짧게 밝힌다. "
            "가까운 의료기관 안내는 애플리케이션이 검증된 기관 마스터로 별도 생성하므로, 답변에 5번 제목이나 "
            "병원·약국 검색 결과, 기관 데이터가 없다는 문장을 직접 쓰지 않는다. 답변은 4번 제목에서 끝낸다. "
            "주소·전화번호 같은 단순 정보 질문에는 불필요한 건강 설명 없이 바로 핵심 정보를 친절하게 답한다. "
            "의학적 진단, 처방, 복용 지시, 치료 효과 보장, 공포를 유발하는 단정은 하지 않는다. "
            "응급 위험 신호가 보이면 다른 설명보다 119 또는 해당 위기 연락 안내를 먼저 한다. "
            "근거가 부족하면 딱딱한 문구만 출력하지 말고, 무엇을 확인하지 못했는지 설명한 뒤 "
            "확인에 필요한 위치나 증상을 한 가지씩 질문한다. 내부 지식이나 추측으로 빈칸을 채우지 않는다. "
            "쉬운 존댓말과 짧은 문단을 사용한다."
        )
        payload = {
            "model": self.model_name,
            "temperature": self.config["temperature"],
            "max_tokens": int(self.config["maximum_tokens"]),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"CONTEXT\n{context_text}\n\nQUESTION\n{question}"},
            ],
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=int(self.config["timeout_seconds"]),
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


def _term_is_reported(text: str, term: str) -> bool:
    """Return whether at least one occurrence is affirmed rather than negated.

    Intake answers commonly negate a comma-separated list with one trailing
    phrase (for example, ``마비, 출혈, 호흡곤란은 없어요``).  Keyword-only
    matching turns that safe answer into a false emergency.  The first status
    expression after each occurrence applies to that occurrence; an affirmative
    occurrence anywhere still wins.
    """
    status_pattern = re.compile(r"없지\s*않|없|아니|않|있")
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        suffix = text[index + len(term):index + len(term) + 120]
        suffix = re.split(r"[.!?\n]", suffix, maxsplit=1)[0]
        status = status_pattern.search(suffix)
        if status is None or status.group(0).startswith("없지") or status.group(0) == "있":
            return True
        start = index + len(term)


def _match_is_reported(text: str, match: re.Match[str]) -> bool:
    return _term_is_reported(text, match.group(0))


def classify_risk(question: str, rules: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    normalized = question.casefold()
    # Quoted document questions can contain phrases such as "의식이 없다"
    # without describing the user's present condition.
    if (
        "공식 문서" in normalized
        and any(term in normalized for term in ("핵심 내용", "문서에서", "무엇인가요"))
    ):
        return "none", None
    informational_terms = (
        "전화번호", "연락처", "주소", "위치", "운영시간", "이용시간", "홈페이지",
        "기관", "센터", "어디", "찾아", "목록", "안내해", "예방교육", "예방접종",
        "사용법", "정보", "상담", "상담시간", "교육", "알려", "대처법", "대응법",
    )
    information_request = any(term in normalized for term in informational_terms)

    # Composite safety decisions are maintained as evidence-linked data, not
    # embedded symptom phrases. Every group must contribute at least one term.
    for composite in rules.get("composite_rules", []):
        groups = composite.get("all_of", [])
        if groups and all(
            any(_term_is_reported(normalized, term.casefold()) for term in group)
            for group in groups
        ):
            category = composite.get("category", "")
            if category in rules:
                return category, rules[category]

    suicide_intent = (
        "죽고 싶", "살기 싫", "극단적 선택", "목숨을 끊", "자해할", "자해했",
        "자살할", "자살하려", "자살 방법", "끝내고 싶", "사라지고 싶",
        "살고 싶지 않", "사라져야", "내가 없어져", "다들 편할 것 같",
        "살 이유가 없", "사는 이유가 없", "살 가치가 없",
        "자살 생각", "자해 생각", "자살 충동", "자해 충동", "자살 계획", "자해 계획",
    )
    if any(_term_is_reported(normalized, term) for term in suicide_intent):
        return "suicide", rules["suicide"]

    emergency_signs = (
        "의식이 없", "의식을 잃", "숨을 못", "숨이 너무 차",
        "숨이 가빠", "숨쉬기 힘", "한 문장 못", "호흡곤란", "가슴 통증", "가슴이 아프",
        "가슴을 쥐어짜", "가슴이 조이", "흉통", "심한 출혈", "쓰러졌", "쓰러질",
        "경련", "마비", "입이 돌아", "말이 어눌", "한쪽 팔", "한쪽 다리", "얼굴이 한쪽",
    )
    chest_pain_reported = any(
        _match_is_reported(normalized, match)
        for match in re.finditer(
            r"(?:가슴|흉부)[^.!?\n]{0,12}(?:아프|통증|조이|쥐어짜)",
            normalized,
        )
    )
    breathing_distress_reported = any(
        _match_is_reported(normalized, match)
        for match in re.finditer(
            r"(?:숨|호흡)[^.!?\n]{0,14}(?:안\s*쉬|못\s*쉬|힘들|곤란|가빠|차)",
            normalized,
        )
    )
    if (
        any(_term_is_reported(normalized, term) for term in emergency_signs)
        or chest_pain_reported
        or breathing_distress_reported
    ):
        return "emergency", rules["emergency"]

    addiction_crisis = (
        "과다복용", "과다 복용", "약을 너무 많이", "마약을 했", "금단 증상",
        "금단으로", "의식이 흐", "호흡 이상", "한번에 여러 알", "한꺼번에 여러 알",
        "여러 알 먹었", "몸이 떨리고 환각", "떨림과 환각", "술 끊었더니",
        "술 끊은 지",
    )
    alcohol_stopped = bool(re.search(
        r"(?:술|알코올)(?:을)?\s*(?:끊|중단)",
        normalized,
    ))
    withdrawal_crisis = (
        (
            alcohol_stopped
            or any(term in normalized for term in ("알코올 금단", "금단 중", "금단으로"))
        )
        and any(term in normalized for term in ("손떨림", "손이 떨", "환각", "헛것", "환청"))
    )
    ingestion_reported = any(term in normalized for term in ("먹었", "복용했", "삼켰", "마셨"))
    numeric_pill_amounts = [
        int(value)
        for value in re.findall(
            r"(?:타이레놀|해열제|진통제|수면제|감기약|약)[^.!?\n]{0,16}?(\d+)\s*(?:알|정|개)",
            normalized,
        )
    ]
    whole_container = bool(
        re.search(r"(?:수면제|진통제|감기약|약)[^.!?\n]{0,12}(?:한\s*통|한\s*병|한\s*봉지)", normalized)
    )
    medication_amount_crisis = ingestion_reported and (
        any(amount >= 5 for amount in numeric_pill_amounts) or whole_container
    )
    if (
        any(_term_is_reported(normalized, term) for term in addiction_crisis)
        or withdrawal_crisis
        or medication_amount_crisis
    ):
        return "addiction", rules["addiction"]

    medical_high_risk = (
        "진단해", "진단하고", "처방해", "복용량", "약을 얼마나", "용량을 정",
        "임신 중 약", "임산부 약", "영아 약", "아기 약", "고열이 계속",
        "혈압이 높은데 약", "혈당이 높은데 약", "해열제 얼마나", "몇 알 먹",
    )
    medication_name = (
        r"(?:타이레놀|아세트아미노펜|이부프로펜|해열제|진통제|"
        r"수면제|감기약|상비약|이\s*약|약)"
    )
    medication_mentioned = bool(re.search(medication_name, normalized))
    administration_terms = r"(?:먹|복용|먹이|사용|투여|써)"
    permission_question = bool(re.search(
        rf"{medication_name}[^.!?\n]{{0,28}}{administration_terms}[^.!?\n]{{0,8}}(?:돼|되|괜찮)",
        normalized,
    ))
    dose_schedule_question = bool(
        medication_mentioned
        and re.search(
            rf"(?:얼마나|몇\s*(?:알|정|개|ml|㎖|cc|mg|㎎|번)|"
            rf"하루[^.!?\n]{{0,8}}몇\s*번|몇\s*시간[^.!?\n]{{0,8}}간격|간격)[^.!?\n]{{0,18}}{administration_terms}",
            normalized,
        )
    )
    personal_dose_question = permission_question or dose_schedule_question
    if any(term in normalized for term in medical_high_risk) or personal_dose_question or (
        "해열제" in normalized
        and ("얼마나" in normalized or re.search(r"몇\s*(?:ml|㎖|cc|알)", normalized))
    ):
        return "medical_high_risk", rules["medical_high_risk"]

    # Bare domain words in informational requests describe a service, not the
    # user's current state (for example "자살예방센터 전화번호").
    if not information_request:
        for category in ("suicide", "addiction", "emergency"):
            rule = rules[category]
            if any(
                _term_is_reported(normalized, keyword.casefold())
                for keyword in rule["keywords"]
            ):
                return category, rule
    return "none", None


def citations(contexts: list[dict[str, Any]], limit: int = 3) -> list[dict[str, str]]:
    values = []
    seen: set[str] = set()
    for row in contexts:
        if row["chunk_id"] in seen:
            continue
        seen.add(row["chunk_id"])
        values.append({
            "url": row["url"],
            "document": row["title"],
            "doc_id": row["doc_id"],
            "chunk_id": row["chunk_id"],
        })
        if len(values) >= limit:
            break
    return values


def augment_safety_evidence(
    question: str,
    retrieved: list[dict[str, Any]],
    evidence_pool: list[dict[str, Any]],
    safety_rules: dict[str, Any],
) -> list[dict[str, Any]]:
    category, rule = classify_risk(question, safety_rules)
    if not rule:
        return retrieved
    output = list(retrieved)
    for term in rule.get("evidence_terms", rule["required_terms"]):
        if any(term in row["text"] for row in output):
            continue
        candidates = [row for row in evidence_pool if term in row["text"]]
        def evidence_score(row: dict[str, Any]) -> tuple[int, int]:
            title = row.get("title", "")
            text = row.get("text", "")
            if term == "109":
                preferred = int("자살" in text or "정신건강" in title)
            elif "중독관리" in term:
                preferred = int("중독관리" in text or "중독" in title)
            elif term == "119":
                preferred = int(
                    any(value in title for value in ("심폐소생술", "응급처치", "기도폐쇄"))
                    and any(value in text for value in ("119에 신고", "119로 신고", "119에 연락", "119로 도움"))
                )
            else:
                preferred = 0
            # Short, focused evidence is preferred over navigation-heavy pages.
            return preferred, -len(text)
        match = max(candidates, key=evidence_score) if candidates else None
        if match:
            output.append(dict(match, embedding_score=0.0, reranker_score=0.0))
    return output


def normalize_symptom_headings(answer_text: str) -> str:
    """Make the resident-facing sequence stable when a generator drops numbering."""
    headings = (
        "먼저 마음부터",
        "생각해볼 수 있는 원인",
        "지금 할 수 있는 대처",
        "상비의약품 안내",
    )
    output = answer_text
    for number, title in enumerate(headings, start=1):
        pattern = re.compile(
            rf"^[ \t]*(?:#{{1,6}}[ \t]*)?(?:{number}[.)][ \t]*)?{re.escape(title)}[ \t]*$",
            re.MULTILINE,
        )
        output = pattern.sub(f"### {number}. {title}", output, count=1)
    return output


def answer(
    question: str,
    contexts: list[dict[str, Any]],
    generator: OpenAICompatibleGenerator,
    safety_rules: dict[str, Any],
) -> dict[str, Any]:
    risk_category, rule = classify_risk(question, safety_rules)
    if rule:
        used_contexts = []
        for term in rule.get("evidence_terms", rule["required_terms"]):
            match = next((row for row in contexts if term in row["text"]), None)
            if match and match["chunk_id"] not in {row["chunk_id"] for row in used_contexts}:
                used_contexts.append(match)
        if not used_contexts:
            used_contexts = contexts[:1]
        answer_text = rule["message"]
        safety_applied = True
    elif not contexts:
        used_contexts = []
        answer_text = no_evidence_answer(question)
        safety_applied = False
    else:
        used_contexts = contexts[:3]
        answer_text = generator.generate(question, used_contexts)
        safety_applied = False
    if not safety_applied and is_symptom_question(question):
        answer_text = normalize_symptom_headings(answer_text)
    return {
        "question": question,
        "answer": answer_text,
        "risk_category": risk_category,
        "safety_rule_applied": safety_applied,
        "citations": citations(used_contexts),
        "retrieved_chunk_ids": [row["chunk_id"] for row in contexts],
        "generator_model": generator.model_name,
        "temperature": generator.config["temperature"],
        "generation_policy_version": "resident_friendly_five_step_v3",
    }


def is_symptom_question(question: str) -> bool:
    """Recognize ordinary symptom wording without mistaking institution lookups for symptoms."""
    normalized = " ".join(question.casefold().split())
    symptom_terms = (
        "아파", "아프", "통증", "쑤셔", "쑤시", "지끈", "욱신", "찌릿", "뻐근",
        "두통", "편두통", "치통", "근육통", "몸살", "결려", "결림", "저려", "저림",
        "메스꺼", "메슥", "울렁", "어지", "현기증", "열이", "열나", "발열", "오한",
        "기침", "가래", "콧물", "코가 막", "코막힘", "재채기", "목이 따갑", "목이 칼칼",
        "목이 쉬", "먹먹", "충혈", "시려", "붓", "부었", "가려", "발진", "두드러기", "설사", "구토", "토했", "변비",
        "속이 쓰", "속쓰림", "소화가 안", "체했", "복통", "배가 아", "숨이 차",
        "속이 더부룩", "답답", "피곤", "기운이 없", "잠이 안", "불면", "불편", "증상", "상비약", "약을 먹",
    )
    # "마취통증의학과" is a facility specialty, not a symptom report.
    symptom_text = re.sub(r"(?:마취)?통증의학과(?:의원)?", "", normalized)
    return any(term in symptom_text for term in symptom_terms)


def no_evidence_answer(question: str) -> str:
    if is_symptom_question(question):
        return (
            "### 1. 먼저 마음부터\n"
            "말씀하신 증상 때문에 많이 불편하고 걱정되셨겠어요. 안전하게 확인할 수 있도록 차근차근 도와드릴게요.\n\n"
            "### 2. 생각해볼 수 있는 원인\n"
            "비슷한 증상도 원인이 여러 가지일 수 있어 지금 정보만으로 단정하기는 어렵습니다. 현재 수집된 "
            "공식 자료에도 이 증상의 원인을 구분할 근거가 없습니다. 언제 시작됐는지, 얼마나 심한지, 함께 "
            "나타나는 증상이 있는지 알려주시면 필요한 진료 방향을 좁히는 데 도움이 됩니다.\n\n"
            "### 3. 지금 할 수 있는 대처\n"
            "증상이 시작된 시각과 변화를 메모하고 무리하지 말고 상태를 살펴보세요. 갑자기 매우 심해졌거나 "
            "의식·호흡 이상, 마비, 심한 가슴 통증이 있으면 즉시 119에 연락하세요. 그 외에도 증상이 계속되거나 "
            "악화되면 의료기관에서 직접 확인받으세요.\n\n"
            "### 4. 상비의약품 안내\n"
            "현재 근거만으로 특정 상비약이나 복용량을 권하기 어렵습니다. 복용 중인 약이나 질환이 있다면 "
            "약사 또는 의료진에게 먼저 확인해 주세요.\n\n"
            "### 5. 가까운 의료기관 찾기\n"
            "현재 계신 원주시 읍면동을 알려주시면 보유한 공식 기관 정보에서 가까운 곳을 찾아드릴게요."
        )
    return (
        "현재 수집된 원주시 공식 자료에서는 요청하신 내용을 확인하지 못했습니다. "
        "찾으시는 기관명이나 읍면동, 필요한 정보가 전화번호·주소·운영시간 중 무엇인지 알려주시면 "
        "확인 가능한 범위를 다시 찾아보겠습니다."
    )
