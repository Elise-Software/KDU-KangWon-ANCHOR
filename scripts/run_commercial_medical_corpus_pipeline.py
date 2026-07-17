from __future__ import annotations

import argparse
from pathlib import Path

from medical_corpus.pipeline import run_pipeline


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect and normalize a license-gated commercial medical RAG corpus."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "commercial_medical_corpus.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "commercial_medical_corpus",
    )
    parser.add_argument("--limit-per-source", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_pipeline(
        config_path=args.config,
        output_dir=args.output_dir,
        limit_per_source=args.limit_per_source,
        strict=args.strict,
    )


if __name__ == "__main__":
    raise SystemExit(main())
