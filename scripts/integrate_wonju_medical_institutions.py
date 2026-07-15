"""Integrate Wonju medical master data with three public datasets.

The program deliberately uses the standard library so it can be run in the
collection environment without adding a data-science runtime dependency.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr")
INSTITUTION_FIELDS = ["institution_id", "source_id", "category", "normalized_category", "name", "normalized_name", "address", "normalized_address", "phone", "bed_count", "managing_authority", "managing_authority_phone", "mental_health_type", "active_status", "source_status", "primary_source", "primary_source_reference_date", "created_at", "updated_at"]
SOURCE_FIELDS = ["institution_id", "source_dataset", "source_filename", "source_reference_date", "source_row_number", "source_name", "source_address", "source_phone", "source_category", "matched_by", "match_confidence", "imported_at"]
ALIAS_FIELDS = ["institution_id", "alias_name", "normalized_alias_name", "source_dataset", "source_reference_date", "alias_reason", "verified"]
CONFLICT_FIELDS = ["institution_id", "field_name", "canonical_value", "incoming_value", "canonical_source", "incoming_source", "resolution", "review_required"]
REVIEW_FIELDS = ["source_dataset", "source_row_number", "source_name", "source_address", "source_phone", "candidate_institution_id", "candidate_name", "match_reason", "name_similarity", "address_similarity", "phone_match", "review_type"]
COORD_FIELDS = ["institution_id", "latitude", "longitude", "coordinate_source", "coordinate_reference_date", "coordinate_status", "address_match", "collected_from_file"]

def text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").replace("\xa0", " ")).strip()

def load_csv_with_encoding(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    last: Exception | None = None
    for encoding in ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                return [{text(k): text(v) for k, v in row.items()} for row in csv.DictReader(f)]
        except UnicodeDecodeError as exc:
            last = exc
    raise RuntimeError(f"Cannot decode {path}") from last

def load_review_decisions(path: str | Path) -> dict[tuple[str, str], dict[str, str]]:
    return {(r["source_dataset"], r["source_row_number"]): r for r in load_csv_with_encoding(path)}

def normalize_name(value: Any) -> str:
    s = text(value).casefold()
    s = re.sub(r"[()\[\]{}<>·ㆍ,.'\"’`~!@#$%^&*_+=|\\/:;?-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # comparison form only; the original is always retained separately.
    return re.sub(r"[\s()-]", "", s)

def normalize_address(value: Any) -> str:
    s = text(value).replace("강원도", "강원특별자치도")
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r",\s*(?:\d+\s*층|\d+호|\d+동|지하\s*\d+층).*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def normalize_phone(value: Any) -> str:
    raw = text(value)
    digits = re.sub(r"\D", "", raw)
    if not digits or len(digits) not in (9, 10, 11):
        return raw
    if digits.startswith("02"):
        split = (2, 5) if len(digits) == 9 else (2, 6)
    elif len(digits) == 10:
        split = (3, 6)
    else:
        split = (3, 7)
    return f"{digits[:split[0]]}-{digits[split[0]:split[1]]}-{digits[split[1]:]}"

def normalize_category(value: Any) -> str:
    s = text(value)
    mapping = {"정신건강복지센터": "mental_health_welfare_center", "중독관리통합지원센터": "addiction_management_center", "정신재활시설": "mental_rehabilitation_facility", "정신병원": "psychiatric_hospital", "정신건강의학과의원": "psychiatric_clinic", "요양병원": "long_term_care_hospital"}
    return mapping.get(s, re.sub(r"\s+", "_", s.casefold()))

def get(row: dict[str, str], *names: str) -> str:
    for name in names:
        if text(row.get(name)):
            return text(row[name])
    return ""

def base_address(value: str) -> str:
    return re.sub(r"\s+", "", normalize_address(value)).replace("-0", "-")

def similarity(a: str, b: str) -> float:
    return round(SequenceMatcher(None, a, b).ratio(), 4) if a and b else 0.0

def match_institution(incoming: dict[str, str], institutions: list[dict[str, str]], aliases: list[dict[str, str]]) -> tuple[dict[str, str] | None, str, float, list[dict[str, str]]]:
    name, address, phone = normalize_name(incoming["name"]), base_address(incoming["address"]), normalize_phone(incoming["phone"])
    sid = incoming.get("source_id", "")
    checks = [("source_id", lambda r: sid and r["source_id"] == sid), ("phone_exact", lambda r: phone and normalize_phone(r["phone"]) == phone), ("name_address_exact", lambda r: r["normalized_name"] == name and base_address(r["address"]) == address), ("name_phone_exact", lambda r: r["normalized_name"] == name and phone and normalize_phone(r["phone"]) == phone), ("address_phone_exact", lambda r: base_address(r["address"]) == address and phone and normalize_phone(r["phone"]) == phone)]
    for method, predicate in checks:
        found = [r for r in institutions if predicate(r)]
        if len(found) == 1:
            return found[0], method, 1.0, []
    alias_map = {normalize_name(a["alias_name"]): a for a in aliases}
    alias = alias_map.get(name)
    if alias:
        found = [r for r in institutions if r["normalized_name"] == normalize_name(alias["canonical_name"])]
        if len(found) == 1 and ((not alias.get("expected_phone") or normalize_phone(alias["expected_phone"]) == phone) or (not alias.get("expected_address") or base_address(alias["expected_address"]) == address)):
            return found[0], "manual_alias", 1.0, []
    candidates = []
    for r in institutions:
        ns, ads = similarity(name, r["normalized_name"]), similarity(address, base_address(r["address"]))
        if ns >= .72 and ads >= .55:
            candidates.append((ns + ads, r))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return None, "similarity_candidate" if candidates else "no_match", candidates[0][0] / 2 if candidates else 0.0, [x[1] for x in candidates[:3]]

def resolve_field_conflicts(inst: dict[str, str], incoming: dict[str, str], dataset: str, conflicts: list[dict[str, str]]) -> None:
    for field in ("name", "address", "phone", "category", "bed_count", "mental_health_type"):
        new, old = text(incoming.get(field)), text(inst.get(field))
        if not new or new == old: continue
        if not old:
            inst[field] = new
            conflicts.append({"institution_id": inst["institution_id"], "field_name": field, "canonical_value": old, "incoming_value": new, "canonical_source": inst["primary_source"], "incoming_source": dataset, "resolution": "enrich_missing_field", "review_required": "false"})
        else:
            conflicts.append({"institution_id": inst["institution_id"], "field_name": field, "canonical_value": old, "incoming_value": new, "canonical_source": inst["primary_source"], "incoming_source": dataset, "resolution": "keep_current_master", "review_required": "true"})

def create_alias_record(inst: dict[str, str], incoming: dict[str, str], dataset: str, date: str, reason: str) -> dict[str, str] | None:
    if normalize_name(inst["name"]) == normalize_name(incoming["name"]): return None
    return {"institution_id": inst["institution_id"], "alias_name": incoming["name"], "normalized_alias_name": normalize_name(incoming["name"]), "source_dataset": dataset, "source_reference_date": date, "alias_reason": reason, "verified": "false"}

def create_source_record(iid: str, dataset: str, filename: str, date: str, number: int, row: dict[str, str], method: str, confidence: float) -> dict[str, str]:
    return {"institution_id": iid, "source_dataset": dataset, "source_filename": filename, "source_reference_date": date, "source_row_number": str(number), "source_name": row["name"], "source_address": row["address"], "source_phone": row["phone"], "source_category": row["category"], "matched_by": method, "match_confidence": f"{confidence:.4f}", "imported_at": now()}

def create_coordinate_record(inst: dict[str, str], row: dict[str, str], filename: str, date: str) -> dict[str, str] | None:
    try: lat, lon = float(row["latitude"]), float(row["longitude"])
    except (TypeError, ValueError): return None
    same = base_address(inst["address"]) == base_address(row["address"])
    status = "source_provided" if same and 37 <= lat <= 38 and 127 <= lon <= 128.5 else "manual_review_required"
    return {"institution_id": inst["institution_id"], "latitude": str(lat), "longitude": str(lon), "coordinate_source": "mental_health_20220813", "coordinate_reference_date": date, "coordinate_status": status, "address_match": str(same).lower(), "collected_from_file": filename}

def now() -> str: return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)

def build_integration_report(**kwargs: Any) -> dict[str, Any]: return kwargs

def validate_outputs(inst: list[dict[str, str]], source: list[dict[str, str]], aliases: list[dict[str, str]], coords: list[dict[str, str]], master_count: int) -> dict[str, bool]:
    ids = {r["institution_id"] for r in inst}
    return {"institution_id_unique": len(ids) == len(inst), "master_preserved": len([r for r in inst if r["source_id"]]) >= master_count, "source_id_unique": len([r["source_id"] for r in inst if r["source_id"]]) == len(set(r["source_id"] for r in inst if r["source_id"])), "required_fields_complete": all(r["name"] and r["category"] and r["address"] for r in inst), "aliases_reference_institutions": all(r["institution_id"] in ids for r in aliases), "coordinates_reference_institutions": all(r["institution_id"] in ids for r in coords), "coordinates_in_range": all(37 <= float(r["latitude"]) <= 38 and 127 <= float(r["longitude"]) <= 128.5 for r in coords if r["coordinate_status"] == "source_provided"), "source_records_traceable": all(r["institution_id"] in ids for r in source)}

def parse_master(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    else: rows = load_csv_with_encoding(path)
    out = []
    for row in rows:
        sid, name, address = get(row, "source_id"), get(row, "name"), get(row, "address")
        iid = f"wonju:{sid}" if sid else "public:" + hashlib.sha256((normalize_name(name)+base_address(address)).encode()).hexdigest()[:16]
        out.append({"institution_id": iid, "source_id": sid, "category": get(row, "category"), "normalized_category": normalize_category(get(row, "category")), "name": name, "normalized_name": normalize_name(name), "address": address, "normalized_address": normalize_address(address), "phone": normalize_phone(get(row, "phone")), "bed_count": "", "managing_authority": "", "managing_authority_phone": "", "mental_health_type": "", "active_status": "active", "source_status": "current_master", "primary_source": "wonju_health_medical_2026", "primary_source_reference_date": "2026", "created_at": now(), "updated_at": now()})
    return out

def incoming_rows(path: Path, kind: str) -> Iterable[dict[str, str]]:
    for n, r in enumerate(load_csv_with_encoding(path), 2):
        if kind == "clinic": yield {"name": get(r, "의료기관명"), "address": get(r, "의료기관주소(도로명)"), "phone": get(r, "의료기관전화번호"), "category": "의원", "bed_count": get(r, "병상"), "managing_authority": get(r, "관리기관"), "managing_authority_phone": get(r, "관리기관 전화번호"), "source_id": "", "row_number": str(n), "date": get(r, "데이터 기준일자")}
        elif kind == "mental": yield {"name": get(r, "기관명"), "address": get(r, "주소"), "phone": get(r, "전화번호"), "category": get(r, "구분"), "mental_health_type": normalize_category(get(r, "구분")), "latitude": get(r, "위도"), "longitude": get(r, "경도"), "source_id": "", "row_number": str(n), "date": "2022-08-13"}
        else: yield {"name": get(r, "기공소명"), "address": get(r, "주소"), "phone": get(r, "연락처"), "category": "치과기공소", "source_id": "", "row_number": str(n), "date": "2023-06-12"}

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(); p.add_argument("--master", default="data/processed/medical_institutions/wonju_medical_institutions.csv"); p.add_argument("--clinic", required=True); p.add_argument("--mental", required=True); p.add_argument("--dental-lab", required=True); p.add_argument("--aliases", default="config/wonju_institution_aliases.csv"); p.add_argument("--review-decisions", default="config/wonju_manual_review_decisions.csv"); p.add_argument("--output-dir", default="data/integrated/wonju"); p.add_argument("--strict", action="store_true"); a = p.parse_args(argv)
    master = Path(a.master)
    if not master.exists(): master = Path("data/processed/medical_institutions/wonju_medical_institutions.csv")
    inst, aliases_cfg, decisions = parse_master(master), load_csv_with_encoding(a.aliases), load_review_decisions(a.review_decisions)
    source, aliases, conflicts, reviews, coords = [], [], [], [], []
    datasets = [(Path(a.clinic), "clinic"), (Path(a.mental), "mental"), (Path(a.dental_lab), "dental_lab")]
    counts = {}
    for path, kind in datasets:
        for row in incoming_rows(path, kind):
            found, method, confidence, candidates = match_institution(row, inst, aliases_cfg)
            dataset = f"{kind}_{row['date']}"; counts.setdefault(dataset, Counter())[method] += 1
            if found:
                resolve_field_conflicts(found, row, dataset, conflicts); found["updated_at"] = now()
                source.append(create_source_record(found["institution_id"], dataset, path.name, row["date"], int(row["row_number"]), row, method, confidence))
                alias = create_alias_record(found, row, dataset, row["date"], method)
                if alias: aliases.append(alias)
                if kind == "mental":
                    coordinate = create_coordinate_record(found, row, path.name, row["date"])
                    if coordinate: coords.append(coordinate)
            else:
                decision = decisions.get((dataset, row["row_number"]))
                if decision and decision["decision"] == "merge_existing":
                    found = next((x for x in inst if x["institution_id"] == decision["institution_id"]), None)
                    if found:
                        resolve_field_conflicts(found, row, dataset, conflicts); source.append(create_source_record(found["institution_id"], dataset, path.name, row["date"], int(row["row_number"]), row, "review_decision_merge", 1.0))
                        if kind == "mental":
                            coordinate = create_coordinate_record(found, row, path.name, row["date"])
                            if coordinate: coords.append(coordinate)
                        continue
                if decision and decision["decision"] in ("add_new", "historical_only"):
                    if decision["decision"] == "add_new":
                        iid = "public:" + hashlib.sha256((normalize_name(row["name"])+base_address(row["address"])).encode()).hexdigest()[:16]
                        new = {k: "" for k in INSTITUTION_FIELDS}; new.update({"institution_id": iid, "category": row["category"], "normalized_category": normalize_category(row["category"]), "name": row["name"], "normalized_name": normalize_name(row["name"]), "address": row["address"], "normalized_address": normalize_address(row["address"]), "phone": normalize_phone(row["phone"]), "mental_health_type": row.get("mental_health_type", ""), "active_status": "pending_current_verification", "source_status": "source_provided", "primary_source": dataset, "primary_source_reference_date": row["date"], "created_at": now(), "updated_at": now()}); inst.append(new); source.append(create_source_record(iid, dataset, path.name, row["date"], int(row["row_number"]), row, "review_decision_new", 1.0))
                    continue
                # Clinic public records may be legitimate new health institutions; mental/dental stay review-only.
                if kind == "clinic" and method == "no_match" and row["name"] and row["address"]:
                    iid = "public:" + hashlib.sha256((normalize_name(row["name"])+base_address(row["address"])).encode()).hexdigest()[:16]
                    new = {k: "" for k in INSTITUTION_FIELDS}; new.update({"institution_id": iid, "category": row["category"], "normalized_category": normalize_category(row["category"]), "name": row["name"], "normalized_name": normalize_name(row["name"]), "address": row["address"], "normalized_address": normalize_address(row["address"]), "phone": normalize_phone(row["phone"]), "bed_count": row.get("bed_count", ""), "managing_authority": row.get("managing_authority", ""), "managing_authority_phone": normalize_phone(row.get("managing_authority_phone", "")), "active_status": "pending_current_verification", "source_status": "source_provided", "primary_source": dataset, "primary_source_reference_date": row["date"], "created_at": now(), "updated_at": now()}); inst.append(new); source.append(create_source_record(iid, dataset, path.name, row["date"], int(row["row_number"]), row, "new_source_provided", 1.0))
                else:
                    c = candidates[0] if candidates else {}; reviews.append({"source_dataset": dataset, "source_row_number": row["row_number"], "source_name": row["name"], "source_address": row["address"], "source_phone": row["phone"], "candidate_institution_id": c.get("institution_id", ""), "candidate_name": c.get("name", ""), "match_reason": method, "name_similarity": str(similarity(normalize_name(row["name"]), c.get("normalized_name", ""))), "address_similarity": str(similarity(base_address(row["address"]), base_address(c.get("address", "")))), "phone_match": str(bool(row["phone"] and normalize_phone(row["phone"]) == normalize_phone(c.get("phone", "")))).lower(), "review_type": "historical_only" if kind == "dental_lab" else ("ambiguous_match" if candidates else "no_match")})
    out = Path(a.output_dir); write_csv(out / "institutions.csv", INSTITUTION_FIELDS, inst); (out / "institutions.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in inst) + "\n", encoding="utf-8"); write_csv(out / "institution_aliases.csv", ALIAS_FIELDS, aliases); write_csv(out / "institution_source_records.csv", SOURCE_FIELDS, source); write_csv(out / "institution_coordinates.csv", COORD_FIELDS, coords); write_csv(out / "merge_conflicts.csv", CONFLICT_FIELDS, conflicts); write_csv(out / "manual_review.csv", REVIEW_FIELDS, reviews); write_csv(out / "review_decisions.csv", ["source_dataset", "source_row_number", "source_name", "decision", "institution_id", "reason"], list(decisions.values()))
    checks = validate_outputs(inst, source, aliases, coords, len(parse_master(master))); report = build_integration_report(input_files={str(x[0]): sum(1 for _ in incoming_rows(x[0], x[1])) for x in datasets}, existing_master_count=len(parse_master(master)), final_institution_count=len(inst), new_institution_count=len([r for r in inst if r["source_status"] == "source_provided"]), clinic_new_candidate_count=sum(1 for r in source if r["source_dataset"].startswith("clinic_") and r["matched_by"] == "new_source_provided"), review_decision_new_count=sum(1 for r in source if r["matched_by"] == "review_decision_new"), review_decision_count=len(decisions), match_counts={k: dict(v) for k,v in counts.items()}, automatic_merge_count=len(source), aliases_created=len(aliases), conflicts=len(conflicts), coordinates_linked=len([r for r in coords if r["coordinate_status"] == "source_provided"]), manual_review_count=len(reviews), final_category_counts=dict(Counter(r["category"] for r in inst)), output_files=sorted({str(x) for x in out.iterdir()} | {str(out / "integration_report.json")}), checks=checks, all_checks_passed=all(checks.values()), executed_at=now()); (out / "integration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)); return 0 if not a.strict or report["all_checks_passed"] else 1

if __name__ == "__main__": sys.exit(main())
