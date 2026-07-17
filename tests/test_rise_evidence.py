from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_generator():
    path = ROOT / "scripts/generate_rise_evidence.py"
    spec = importlib.util.spec_from_file_location("generate_rise_evidence", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rise_quantitative_evidence_is_complete_and_unique():
    module = load_generator()
    scenarios = module.senior_scenario_rows(ROOT)
    safety = module.safety_rule_rows(ROOT)

    assert len(scenarios) >= 10
    assert len({row["scenario_id"] for row in scenarios}) == len(scenarios)
    assert all(row["passed"] == "true" for row in scenarios)

    assert len(safety) >= 20
    assert len(safety) == 31
    assert len({row["rule_id"] for row in safety}) == len(safety)
    assert {row["category"] for row in safety} >= {
        "emergency", "suicide", "addiction", "medical_high_risk"
    }


def test_rise_report_references_generated_artifacts():
    module = load_generator()
    report = module.generate(ROOT)

    assert report["integrity_checks_passed"] is True
    assert report["senior_scenario_count"] == 10
    assert report["safety_rule_pattern_count"] == 31
    for relative_path in report["artifacts"].values():
        assert (ROOT / relative_path).is_file()
