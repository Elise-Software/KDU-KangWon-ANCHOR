from __future__ import annotations

import json
import os
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
        self.base_url = generation_config["base_url"].rstrip("/")
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
            "당신은 원주시 보건·복지 근거 기반 안내자다. 제공된 CONTEXT에 명시된 사실만 사용한다. "
            "근거가 없으면 '제공된 근거에서 확인할 수 없습니다.'라고 답한다. 추측, 진단, 처방을 하지 않는다. "
            "간결한 한국어로 답하고 내부 지식으로 빈칸을 채우지 않는다."
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


def classify_risk(question: str, rules: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    normalized = question.casefold()
    # Specific risk domains take precedence over the generic word "emergency".
    for category in ("suicide", "addiction", "emergency", "medical_high_risk"):
        rule = rules[category]
        if any(keyword.casefold() in normalized for keyword in rule["keywords"]):
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
        match = next((row for row in evidence_pool if term in row["text"]), None)
        if match:
            output.append(dict(match, embedding_score=0.0, reranker_score=0.0))
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
    else:
        used_contexts = contexts[:1]
        answer_text = generator.generate(question, used_contexts)
        safety_applied = False
    return {
        "question": question,
        "answer": answer_text,
        "risk_category": risk_category,
        "safety_rule_applied": safety_applied,
        "citations": citations(used_contexts),
        "retrieved_chunk_ids": [row["chunk_id"] for row in contexts],
        "generator_model": generator.model_name,
        "temperature": generator.config["temperature"],
        "generation_policy_version": "single_context_v1",
    }
