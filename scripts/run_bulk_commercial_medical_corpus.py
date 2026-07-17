from __future__ import annotations

import argparse
from pathlib import Path

from medical_corpus.bulk_pipeline import run


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the capacity-bounded full commercial medical corpus pipeline.")
    parser.add_argument("--config", type=Path, default=ROOT / "config/commercial_medical_corpus_bulk.json")
    parser.add_argument(
        "--stage",
        choices=("plan", "download", "scan", "supplement", "select", "medlineplus", "korean", "materialize", "integrate", "process", "all"),
        default="all",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    return run(args.config, args.stage, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
