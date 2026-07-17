"""Collect and aggregate consented, de-identified RISE actual-user feedback."""
from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESPONSES = ROOT / "data/p1_rag/evaluation/rise_actual_user_responses.jsonl"
RATING_LABELS = {
    "readability": "글자와 화면의 가독성",
    "clarity": "질문과 답변의 명확성",
    "trust": "답변과 출처의 신뢰도",
    "connection_ease": "전화·지도·기관 연결 편의성",
    "safety_clarity": "응급·안전 안내의 명확성",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def redact(value: str) -> str:
    text = " ".join(value.split())
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[이메일]", text)
    text = re.sub(r"\b\d{6}\s*[- ]?\s*[1-4]\d{6}\b", "[주민번호]", text)
    text = re.sub(r"\b(?:01[016789]|0\d{1,2})[- .]?\d{3,4}[- .]?\d{4}\b", "[전화번호]", text)
    return text[:500]


def ask_choice(prompt: str, choices: list[str]) -> str:
    while True:
        value = input(f"{prompt} ({'/'.join(choices)}): ").strip()
        if value in choices:
            return value


def ask_int(prompt: str, minimum: int, maximum: int) -> int:
    while True:
        try:
            value = int(input(f"{prompt} ({minimum}~{maximum}): ").strip())
        except ValueError:
            continue
        if minimum <= value <= maximum:
            return value


def collect(config: dict[str, Any], assigned_scenario_id: str | None = None) -> dict[str, Any]:
    if ask_choice("익명 평가 참여 및 결과 집계에 동의합니까", ["yes", "no"]) != "yes":
        raise RuntimeError("consent was not provided; no record was stored")
    scenario_rows = {row["scenario_id"]: row for row in config["scenarios"]}
    scenarios = {scenario_id: row["title"] for scenario_id, row in scenario_rows.items()}
    print("평가 시나리오:")
    for scenario_id, title in scenarios.items():
        print(f"  {scenario_id}: {title}\n    {scenario_rows[scenario_id].get('task', '')}")
    if assigned_scenario_id:
        scenario_id = assigned_scenario_id
        print(f"배정 과업: {scenario_id} - {scenarios[scenario_id]}")
    else:
        scenario_id = ask_choice("수행한 시나리오", list(scenarios))
    ratings = {
        field: ask_int(
            f"{RATING_LABELS.get(field, field)} (1 매우 낮음, 5 매우 높음)",
            config["rating_minimum"],
            config["rating_maximum"],
        )
        for field in config["ratings"]
    }
    return {
        "response_id": f"rise-user-{uuid.uuid4().hex}",
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "consent": True,
        "participant_group": ask_choice("참여자 유형", ["senior", "caregiver", "resident"]),
        "age_band": ask_choice("연령대", ["under_65", "65_74", "75_plus", "not_disclosed"]),
        "scenario_id": scenario_id,
        "scenario_title": scenarios[scenario_id],
        "scenario_task": scenario_rows[scenario_id].get("task", ""),
        "completed": ask_choice("과업을 완료했습니까", ["yes", "no"]) == "yes",
        "duration_seconds": ask_int("과업 소요시간(초)", 1, 3600),
        "ratings": ratings,
        "comment": redact(input("개선 의견(개인정보를 적지 마세요, 선택): ")),
    }


def simulated_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = {row["scenario_id"]: row for row in config["scenarios"]}
    profiles = [
        ("SIM-001", "20s", "male", "resident", "actual-02", [4, 4, 4, 5, 4], 92,
         "지도와 전화 연결 버튼을 한 화면에서 확인하는 사전점검"),
        ("SIM-002", "20s", "male", "resident", "actual-03", [4, 5, 4, 4, 4], 78,
         "기관 연락처와 공식 출처 구분 표시를 확인하는 사전점검"),
        ("SIM-003", "70s", "female", "senior", "actual-01", [4, 4, 4, 4, 5], 184,
         "큰 글자와 단계별 증상 안내의 가독성을 확인하는 사전점검"),
        ("SIM-004", "40s", "female", "caregiver", "actual-04", [5, 4, 4, 5, 5], 106,
         "보호자 관점에서 긴급 연락 버튼과 경고 우선순위를 확인하는 사전점검"),
        ("SIM-005", "60s", "male", "resident", "actual-05", [4, 4, 5, 4, 4], 131,
         "출처 카드와 근거 식별정보 접근성을 확인하는 사전점검"),
    ]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for profile_id, age_band, gender, group, scenario_id, values, duration, observation in profiles:
        scenario = scenarios[scenario_id]
        rows.append({
            "response_id": profile_id,
            "collected_at": now,
            "consent": False,
            "synthetic": True,
            "participant_group": group,
            "age_band": age_band,
            "gender": gender,
            "scenario_id": scenario_id,
            "scenario_title": scenario["title"],
            "scenario_task": scenario.get("task", ""),
            "completed": True,
            "duration_seconds": duration,
            "ratings": dict(zip(config["ratings"], values, strict=True)),
            "comment": f"{observation}. 실제 사용자 의견이 아닌 합성 시나리오 결과임",
        })
    return rows


def summarize(
    records: list[dict[str, Any]], config: dict[str, Any], *, include_synthetic: bool = False
) -> dict[str, Any]:
    if include_synthetic:
        valid = [row for row in records if row.get("synthetic") is True]
    else:
        valid = [row for row in records if row.get("consent") is True and row.get("synthetic") is not True]
    groups = {group: sum(row.get("participant_group") == group for row in valid)
              for group in ("senior", "caregiver", "resident")}
    averages = {}
    for field in config["ratings"]:
        values = [float(row.get("ratings", {}).get(field, 0)) for row in valid
                  if row.get("ratings", {}).get(field)]
        averages[field] = round(sum(values) / len(values), 3) if values else None
    required_groups_present = all(groups.get(group, 0) > 0 for group in config["required_participant_groups"])
    scenario_coverage = sorted({row.get("scenario_id", "") for row in valid if row.get("scenario_id")})
    minimum_scenario_coverage = int(config.get("minimum_scenario_coverage", 1))
    scenario_coverage_complete = len(scenario_coverage) >= minimum_scenario_coverage
    sufficient = (
        len(valid) >= config["minimum_participants"]
        and required_groups_present
        and scenario_coverage_complete
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "participant_count": len(valid),
        "minimum_participants": config["minimum_participants"],
        "participant_groups": groups,
        "required_groups_present": required_groups_present,
        "scenario_coverage": scenario_coverage,
        "minimum_scenario_coverage": minimum_scenario_coverage,
        "scenario_coverage_complete": scenario_coverage_complete,
        "completion_rate": round(sum(bool(row.get("completed")) for row in valid) / len(valid), 3) if valid else None,
        "average_duration_seconds": round(sum(int(row.get("duration_seconds", 0)) for row in valid) / len(valid), 1) if valid else None,
        "average_ratings": averages,
        "comments": [row.get("comment", "") for row in valid if row.get("comment")],
        "evaluation_kind": "synthetic_pretest" if include_synthetic else "actual_user",
        "actual_user_evaluation_passed": sufficient if not include_synthetic else False,
        "simulation_acceptance_criteria_met": sufficient if include_synthetic else None,
        "simulation_completed": bool(valid) if include_synthetic else None,
        "status": (
            "simulation_acceptance_criteria_met_not_actual_users" if include_synthetic and sufficient
            else "simulation_completed_insufficient_coverage" if include_synthetic and valid
            else "completed" if sufficient
            else "pending_actual_participants"
        ),
    }


def write_reports(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    synthetic = report.get("evaluation_kind") == "synthetic_pretest"
    lines = [
        "# 모의 사용자 사전점검 결과" if synthetic else "# 실제 사용자 평가 결과", "",
        f"- 상태: `{report['status']}`",
        f"- 참여자: {report['participant_count']}명 / 최소 {report['minimum_participants']}명",
        f"- 참여 유형: {json.dumps(report['participant_groups'], ensure_ascii=False)}",
        f"- 과업 완료율: {report['completion_rate'] if report['completion_rate'] is not None else '미측정'}",
        f"- 평균 소요시간: {report['average_duration_seconds'] if report['average_duration_seconds'] is not None else '미측정'}",
        "", "## 평균 평가", "",
    ]
    lines.extend(f"- {key}: {value if value is not None else '미측정'}" for key, value in report["average_ratings"].items())
    lines.extend([
        "", "## 과업 범위", "",
        f"- 수행 과업: {', '.join(report['scenario_coverage']) or '없음'}",
        f"- 필수 참여 유형 충족: {report['required_groups_present']}",
        f"- 과업 범위 충족: {report['scenario_coverage_complete']}",
        f"- 합성 모집단 내부 기준 충족: {report.get('simulation_acceptance_criteria_met')}" if synthetic
        else f"- 실제 사용자 완료 기준 충족: {report['actual_user_evaluation_passed']}",
        "", "## 개선 의견", "",
        *([f"- {comment}" for comment in report["comments"]] or ["- 없음"]),
        "",
        "모든 값은 모의 프로필이며 실제 사용자 평가 인원·의견으로 사용하지 않는다."
        if synthetic else "실제 참여자가 최소 조건을 충족하기 전에는 완료로 판정하지 않는다.",
        "",
    ])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config/rise_user_evaluation.json")
    parser.add_argument("--responses", type=Path, default=DEFAULT_RESPONSES)
    parser.add_argument("--report-json", type=Path, default=ROOT / "data/p1_rag/reports/rise_actual_user_evaluation.json")
    parser.add_argument("--report-md", type=Path, default=ROOT / "data/p1_rag/reports/rise_actual_user_evaluation.md")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--collect-count", type=int, default=0,
                        help="지정한 인원만큼 연속 수집하고 각 응답을 즉시 저장")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    config = read_json(args.config)
    collecting = args.collect or args.collect_count > 0
    if args.collect_count < 0:
        parser.error("--collect-count must be zero or greater")
    if collecting and args.simulate:
        parser.error("collection options and --simulate cannot be used together")
    if args.simulate:
        records = simulated_records(config)
        args.responses = ROOT / "data/p1_rag/evaluation/rise_simulated_user_responses.jsonl"
        args.report_json = ROOT / "data/p1_rag/reports/rise_simulated_user_evaluation.json"
        args.report_md = ROOT / "data/p1_rag/reports/rise_simulated_user_evaluation.md"
        args.responses.parent.mkdir(parents=True, exist_ok=True)
        args.responses.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
            encoding="utf-8",
        )
    elif collecting:
        args.responses.parent.mkdir(parents=True, exist_ok=True)
        records = read_jsonl(args.responses)
        collection_count = args.collect_count or 1
        scenario_order = [row["scenario_id"] for row in config["scenarios"]]
        covered = {row.get("scenario_id") for row in records if row.get("consent") is True}
        for index in range(collection_count):
            remaining = [scenario_id for scenario_id in scenario_order if scenario_id not in covered]
            assigned_scenario_id = remaining[0] if remaining else scenario_order[index % len(scenario_order)]
            print(f"\n=== 실제 사용자 {index + 1}/{collection_count} ===")
            record = collect(config, assigned_scenario_id=assigned_scenario_id)
            with args.responses.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            records.append(record)
            covered.add(record["scenario_id"])
            interim = summarize(records, config)
            write_reports(interim, args.report_json, args.report_md)
            print(f"저장 완료: 실제 응답 {interim['participant_count']}건, "
                  f"과업 {len(interim['scenario_coverage'])}/{interim['minimum_scenario_coverage']}")
    else:
        records = read_jsonl(args.responses)
    report = summarize(records, config, include_synthetic=args.simulate)
    write_reports(report, args.report_json, args.report_md)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and args.simulate:
        return 0 if report["simulation_acceptance_criteria_met"] else 1
    return 0 if not args.strict or report["actual_user_evaluation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
