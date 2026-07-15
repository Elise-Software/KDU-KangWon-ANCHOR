"""Query an existing P1 RAG index and print answer plus auditable citations."""
from __future__ import annotations

import argparse
import json

from p1_rag.common import ROOT, read_json
from p1_rag.models import EmbeddingIndex, OpenAICompatibleGenerator, Reranker, answer, augment_safety_evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    args = parser.parse_args()
    config = read_json(ROOT / "config/p1_rag_config.json")
    safety = read_json(ROOT / "config/p1_rag_safety_rules.json")
    index = EmbeddingIndex(config["embedding"])
    candidates = index.search([args.question], int(config["reranker"]["candidate_count"]))[0]
    reranked = Reranker(config["reranker"]).rerank(args.question, candidates)
    contexts = augment_safety_evidence(
        args.question,
        reranked[: int(config["evaluation"]["retrieval_top_k"])],
        index.chunks,
        safety,
    )
    result = answer(
        args.question,
        contexts,
        OpenAICompatibleGenerator(config["generation"]),
        safety,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
