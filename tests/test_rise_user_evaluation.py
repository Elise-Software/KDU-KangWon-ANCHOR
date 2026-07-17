from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts/collect_rise_user_evaluation.py"
    spec = importlib.util.spec_from_file_location("collect_rise_user_evaluation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(index: int, group: str) -> dict:
    return {
        "response_id": f"test-{index}", "consent": True,
        "participant_group": group, "scenario_id": f"actual-0{index}",
        "completed": True, "duration_seconds": 60 + index,
        "ratings": {field: 4 for field in [
            "readability", "clarity", "trust", "connection_ease", "safety_clarity"
        ]},
        "comment": "좋았습니다",
    }


def test_actual_user_summary_requires_real_count_and_target_groups():
    module = load_module()
    config = module.read_json(ROOT / "config/rise_user_evaluation.json")
    rows = [record(1, "senior"), record(2, "senior"), record(3, "caregiver"),
            record(4, "resident"), record(5, "senior")]
    report = module.summarize(rows, config)
    assert report["participant_count"] == 5
    assert report["required_groups_present"] is True
    assert report["scenario_coverage_complete"] is True
    assert report["completion_rate"] == 1.0
    assert report["average_ratings"]["readability"] == 4.0
    assert report["actual_user_evaluation_passed"] is True


def test_actual_user_summary_never_counts_nonconsenting_or_insufficient_rows():
    module = load_module()
    config = module.read_json(ROOT / "config/rise_user_evaluation.json")
    rows = [record(1, "senior"), record(2, "senior")]
    rejected = record(3, "caregiver")
    rejected["consent"] = False
    report = module.summarize([*rows, rejected], config)
    assert report["participant_count"] == 2
    assert report["required_groups_present"] is False
    assert report["scenario_coverage_complete"] is False
    assert report["actual_user_evaluation_passed"] is False


def test_synthetic_profiles_are_never_counted_as_actual_users():
    module = load_module()
    config = module.read_json(ROOT / "config/rise_user_evaluation.json")
    synthetic = module.simulated_records(config)
    actual_report = module.summarize(synthetic, config)
    simulation_report = module.summarize(synthetic, config, include_synthetic=True)
    assert actual_report["participant_count"] == 0
    assert actual_report["actual_user_evaluation_passed"] is False
    assert simulation_report["participant_count"] == 5
    assert simulation_report["required_groups_present"] is True
    assert simulation_report["scenario_coverage"] == [
        "actual-01", "actual-02", "actual-03", "actual-04", "actual-05"
    ]
    assert simulation_report["scenario_coverage_complete"] is True
    assert simulation_report["simulation_acceptance_criteria_met"] is True
    assert simulation_report["simulation_completed"] is True
    assert simulation_report["actual_user_evaluation_passed"] is False
