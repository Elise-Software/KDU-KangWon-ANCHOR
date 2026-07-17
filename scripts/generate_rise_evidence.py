"""Generate repeatable evidence for the quantitative RISE requirements."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def senior_scenario_rows(root: Path) -> list[dict[str, Any]]:
    config = read_json(root / "config/rise_senior_scenarios.json")
    evaluation = {row["eval_id"]: row for row in read_csv(root / "data/p1_rag/evaluation/evaluation_set.csv")}
    results = {
        row["eval_id"]: row
        for row in read_jsonl(root / "data/p1_rag/evaluation/evaluation_results.jsonl")
    }
    persona = read_json(root / "data/p1_rag/reports/persona_usability_report.json")
    persona_by_label = {row["label"]: row for row in persona.get("personas", [])}
    rows: list[dict[str, Any]] = []
    for scenario in config["scenarios"]:
        source_kind = scenario["source_kind"]
        reference_id = scenario["reference_id"]
        if source_kind == "evaluation_case":
            source = evaluation.get(reference_id)
            result = results.get(reference_id)
            passed = bool(
                source
                and result
                and source.get("case_type") == "factual"
                and source.get("expected_chunk_id")
                and result.get("recall_at_5_hit") is True
                and float(result.get("groundedness_score", 0)) >= 0.55
                and float(result.get("citation_accuracy", 0)) == 1.0
            )
            question = source.get("question", "") if source else ""
            evidence = source.get("expected_url", "") if source else ""
            retrieval_rank = result.get("retrieval_rank", "") if result else ""
            groundedness = result.get("groundedness_score", "") if result else ""
            citation_accuracy = result.get("citation_accuracy", "") if result else ""
        elif source_kind == "live_persona":
            source = persona_by_label.get(reference_id)
            markers = scenario.get("required_markers", [])
            actual = [*source.get("intake_markers", []), *source.get("final_headings", [])] if source else []
            passed = bool(source and source.get("passed") and all(marker in actual for marker in markers))
            question = "고령자 다중 턴 증상 확인 및 의료기관 연결"
            evidence = "data/p1_rag/reports/persona_usability_report.json"
            retrieval_rank = ""
            groundedness = ""
            citation_accuracy = ""
        else:
            raise ValueError(f"Unsupported scenario source: {source_kind}")
        rows.append({
            "scenario_id": scenario["scenario_id"],
            "title": scenario["title"],
            "source_kind": source_kind,
            "reference_id": reference_id,
            "question_or_flow": question,
            "evidence": evidence,
            "retrieval_rank": retrieval_rank,
            "groundedness_score": groundedness,
            "citation_accuracy": citation_accuracy,
            "passed": str(passed).lower(),
        })
    if len(rows) < int(config["minimum_required"]) or not all(row["passed"] == "true" for row in rows):
        raise RuntimeError("RISE senior scenario evidence is incomplete")
    return rows


def safety_rule_rows(root: Path) -> list[dict[str, Any]]:
    config = read_json(root / "config/p1_rag_safety_rules.json")
    rows: list[dict[str, Any]] = []
    for composite in config.get("composite_rules", []):
        rows.append({
            "rule_id": f"composite.{composite['rule_id']}",
            "category": composite["category"],
            "rule_type": "composite_all_of",
            "pattern": json.dumps(composite["all_of"], ensure_ascii=False),
            "required_terms": "",
        })
    for category in ("emergency", "suicide", "addiction", "medical_high_risk"):
        rule = config[category]
        for index, keyword in enumerate(rule["keywords"], 1):
            rows.append({
                "rule_id": f"{category}.keyword.{index:02d}",
                "category": category,
                "rule_type": "keyword",
                "pattern": keyword,
                "required_terms": " | ".join(rule.get("required_terms", [])),
            })
    if len(rows) < 20 or len({row["rule_id"] for row in rows}) != len(rows):
        raise RuntimeError("RISE safety rule evidence does not meet the 20-rule requirement")
    return rows


def generate(root: Path = ROOT) -> dict[str, Any]:
    scenarios = senior_scenario_rows(root)
    safety = safety_rule_rows(root)
    factual = [row for row in scenarios if row["source_kind"] == "evaluation_case"]
    live_personas = [row for row in scenarios if row["source_kind"] == "live_persona"]
    scenario_path = root / "data/p1_rag/evaluation/rise_senior_scenarios.csv"
    safety_path = root / "data/p1_rag/reports/rise_safety_rule_inventory.csv"
    write_csv(scenario_path, scenarios, list(scenarios[0]))
    write_csv(safety_path, safety, list(safety[0]))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "senior_scenario_count": len(scenarios),
        "senior_scenario_minimum": 10,
        "senior_scenarios_passed": len(scenarios) >= 10,
        "senior_scenario_domains": [row["title"] for row in scenarios],
        "factual_scenario_count": len(factual),
        "live_persona_scenario_count": len(live_personas),
        "factual_acceptance_criteria": {
            "recall_at_5_hit": True,
            "groundedness_score_minimum": 0.55,
            "citation_accuracy": 1.0,
        },
        "factual_observed": {
            "recall_at_5_passed_count": sum(bool(row["retrieval_rank"]) for row in factual),
            "minimum_groundedness_score": min(float(row["groundedness_score"]) for row in factual),
            "minimum_citation_accuracy": min(float(row["citation_accuracy"]) for row in factual),
        },
        "safety_rule_pattern_count": len(safety),
        "safety_rule_minimum": 20,
        "safety_rules_passed": len(safety) >= 20,
        "artifacts": {
            "senior_scenarios": scenario_path.relative_to(root).as_posix(),
            "safety_rules": safety_path.relative_to(root).as_posix(),
        },
        "integrity_checks_passed": True,
    }
    report_path = root / "data/p1_rag/reports/rise_quantitative_evidence_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    report = generate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and not report["integrity_checks_passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
