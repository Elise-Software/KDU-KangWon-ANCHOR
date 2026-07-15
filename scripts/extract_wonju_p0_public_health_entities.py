"""Extract P0-DATA-03 entity evidence from the previously saved seed HTML only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
PHONE_RE = re.compile(r"(?<!\d)(?:0\d{1,2}[-. ]?)?\d{3,4}[-. ]?\d{4}(?!\d)")

ENTITY_COLUMNS = [
    "candidate_id", "target_id", "source_url", "source_role", "authority_level",
    "source_updated_at", "collected_at", "institution_name_raw", "institution_name_normalized",
    "institution_type_raw", "institution_type_normalized", "parent_organization", "address_raw",
    "address_normalized", "base_address_key", "current_status_raw", "current_status_normalized",
    "homepage_url", "evidence_text", "evidence_hash", "extraction_method", "extraction_confidence",
    "review_required",
]
CONTACT_COLUMNS = [
    "candidate_id", "target_id", "source_url", "source_updated_at", "contact_type", "contact_label",
    "contact_value_raw", "contact_value_normalized", "extension", "department", "purpose",
    "availability_note", "evidence_text", "evidence_hash", "extraction_confidence", "review_required",
]
SCHEDULE_COLUMNS = [
    "candidate_id", "target_id", "source_url", "source_updated_at", "schedule_type", "day_type",
    "hours_raw", "hours_normalized", "open_time", "close_time", "closes_next_day", "break_start",
    "break_end", "break_note", "holiday_status", "reservation_required", "schedule_note", "parse_status",
    "evidence_text", "evidence_hash", "review_required",
]
SERVICE_COLUMNS = [
    "candidate_id", "target_id", "source_url", "source_updated_at", "service_name_raw",
    "service_name_normalized", "service_category", "target_population", "eligibility",
    "reservation_required", "application_method", "cost", "required_documents", "service_description",
    "jurisdiction", "evidence_text", "evidence_hash", "extraction_confidence", "review_required",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_name(value: str) -> str:
    return re.sub(r"[\s()·ㆍ.,\-]", "", value or "").casefold()


def normalize_address(value: str) -> str:
    text = clean(value)
    if text and not text.startswith(("원주시", "강원도", "강원특별자치도")):
        text = f"원주시 {text}"
    return text


def base_address_key(value: str) -> str:
    text = re.sub(r"\([^)]*\)", "", value or "")
    match = re.findall(r"([\w]+(?:로|길))\s*(\d+(?:-\d+)?)", text)
    return "".join(match[-1]).casefold() if match else ""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 8 and digits.startswith(("15", "16", "18")):
        return f"{digits[:4]}-{digits[4:]}"
    if len(digits) == 7:
        digits = "033" + digits
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return value.strip()


def main_content(soup: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(soup), "html.parser")
    for node in clone.select("script,style,noscript,header,footer,nav,.header,.footer,.gnb,.lnb,.snb,.breadcrumb"):
        node.decompose()
    main = clone.select_one("main, #content, #contents, .content, .contents") or clone.body or clone
    return clean(" ".join(main.stripped_strings))


def evidence_window(text: str, needle: str, before: int = 100, after: int = 450) -> str:
    index = text.find(needle)
    return text[max(0, index - before): index + len(needle) + after] if index >= 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pages", type=Path, default=ROOT / "data/collected/public_health/processed/p0_data_03_source_pages.csv")
    parser.add_argument("--page-evidence", type=Path, default=ROOT / "data/collected/public_health/processed/p0_data_03_page_evidence.csv")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/collected/public_health/raw")
    parser.add_argument("--targets", type=Path, default=ROOT / "config/p0_data_03_target_institutions.csv")
    parser.add_argument("--page-roles", type=Path, default=ROOT / "config/p0_data_03_page_roles.csv")
    parser.add_argument("--source-validation-report", type=Path, default=ROOT / "data/processed/public_health/source_validation_report.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/processed/public_health")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    validation = json.loads(args.source_validation_report.read_text(encoding="utf-8"))
    if not validation.get("integrity_checks_passed"):
        raise RuntimeError("P0-DATA-03 source validation did not pass")

    targets = read_csv(args.targets)
    target_by_id = {row["target_id"]: row for row in targets}
    roles = {row["source_url"]: row for row in read_csv(args.page_roles)}
    source_pages = read_csv(args.source_pages)
    if set(roles) != {row["source_url"] for row in source_pages}:
        raise RuntimeError("Every collected seed must have exactly one page-role entry")

    pages: dict[str, dict[str, Any]] = {}
    for source in source_pages:
        raw_path = ROOT / source["raw_html"]
        soup = BeautifulSoup(raw_path.read_text(encoding="utf-8"), "html.parser")
        pages[source["source_url"]] = {"source": source, "soup": soup, "text": main_content(soup)}

    entities: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    evidence_links: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def add_entity(target_id: str, url: str, raw_name: str, address: str, evidence: str,
                   method: str, confidence: str = "high", homepage: str = "") -> str:
        target = target_by_id[target_id]
        address = normalize_address(address)
        evidence = clean(evidence)
        evidence_hash = digest(evidence)
        candidate_id = digest("|".join([url, target_id, raw_name, address, evidence]))[:20]
        source = pages[url]["source"]
        role = roles[url]
        row = {
            "candidate_id": candidate_id, "target_id": target_id, "source_url": url,
            "source_role": role["source_role"], "authority_level": role["authority_level"],
            "source_updated_at": source.get("source_updated_at", ""), "collected_at": source.get("collected_at", ""),
            "institution_name_raw": raw_name, "institution_name_normalized": normalize_name(target["canonical_name"]),
            "institution_type_raw": target["institution_type"], "institution_type_normalized": target["institution_type"],
            "parent_organization": target["expected_parent"], "address_raw": address,
            "address_normalized": address, "base_address_key": base_address_key(address),
            "current_status_raw": "official current page", "current_status_normalized": "source_confirmed",
            "homepage_url": homepage, "evidence_text": evidence, "evidence_hash": evidence_hash,
            "extraction_method": method, "extraction_confidence": confidence, "review_required": False,
        }
        entities.append(row)
        evidence_links.append({
            "candidate_id": candidate_id, "target_id": target_id, "source_url": url,
            "evidence_type": "entity", "evidence_text": evidence, "evidence_hash": evidence_hash,
        })
        return candidate_id

    def add_contact(target_id: str, url: str, raw: str, label: str, evidence: str,
                    contact_type: str = "department_phone", department: str = "") -> None:
        value = normalize_phone(raw)
        if not value:
            return
        evidence = clean(evidence)
        evidence_hash = digest(evidence)
        contacts.append({
            "candidate_id": digest("|".join([target_id, url, contact_type, value, label]))[:20],
            "target_id": target_id, "source_url": url,
            "source_updated_at": pages[url]["source"].get("source_updated_at", ""),
            "contact_type": contact_type, "contact_label": label, "contact_value_raw": raw,
            "contact_value_normalized": value, "extension": "", "department": department,
            "purpose": label, "availability_note": "", "evidence_text": evidence,
            "evidence_hash": evidence_hash, "extraction_confidence": "high", "review_required": False,
        })

    def add_service(target_id: str, url: str, name: str, category: str,
                    evidence: str, target_population: str = "") -> None:
        evidence = clean(evidence)
        if not evidence or evidence not in pages[url]["text"]:
            errors.append({
                "target_id": target_id, "error_type": "service_evidence_not_found",
                "source_url": url, "detail": name,
            })
            return
        evidence_hash = digest(evidence)
        services.append({
            "candidate_id": digest("|".join([target_id, url, name, evidence]))[:20],
            "target_id": target_id, "source_url": url,
            "source_updated_at": pages[url]["source"].get("source_updated_at", ""),
            "service_name_raw": name, "service_name_normalized": name,
            "service_category": category, "target_population": target_population,
            "eligibility": "", "reservation_required": "", "application_method": "",
            "cost": "", "required_documents": "", "service_description": evidence,
            "jurisdiction": "원주시", "evidence_text": evidence,
            "evidence_hash": evidence_hash, "extraction_confidence": "high",
            "review_required": False,
        })

    list_url = "https://www.wonju.go.kr/health/contents.do?key=5155"
    table_target_by_name = {
        target["canonical_name"]: target["target_id"]
        for target in targets if target["institution_type"] in {"public_health_branch", "public_health_clinic"}
    }
    table_target_by_name["산현보건지료소"] = "phc:sanhyeon"  # spelling on the official page
    for table in pages[list_url]["soup"].find_all("table")[:2]:
        for tr in table.find_all("tr"):
            values = [clean(" ".join(cell.stripped_strings)) for cell in tr.find_all(["th", "td"])]
            if len(values) < 2 or values[0] not in table_target_by_name:
                continue
            target_id = table_target_by_name[values[0]]
            evidence = " | ".join(values)
            add_entity(target_id, list_url, values[0], values[1], evidence, "official_table_row")

    health_url = "https://www.wonju.go.kr/health/contents.do?key=1624"
    health_text = pages[health_url]["text"]
    address_match = re.search(r"주소\s*:\s*(원주시\s+[^()]+?\s+\d+(?:-\d+)?)", health_text)
    health_full_text = clean(" ".join(pages[health_url]["soup"].stripped_strings))
    health_footer_evidence = evidence_window(health_full_text, "원주시 보건소 [26417]", 20, 230)
    health_evidence = health_footer_evidence or (address_match.group(0) if address_match else evidence_window(health_text, "주소"))
    add_entity(
        "phc:wonju", health_url, "원주시보건소",
        address_match.group(1) if address_match else "", health_evidence,
        "official_labeled_field", homepage="https://www.wonju.go.kr/health/index.do",
    )
    if "033-737-4011" in health_evidence:
        add_contact(
            "phc:wonju", health_url, "033-737-4011", "대표전화",
            health_evidence, "representative_phone",
        )

    nam_url = "https://www.wonju.go.kr/health/contents.do?key=3745"
    nam_text = pages[nam_url]["text"]
    structured = [
        ("hls:namwonju", "남원주건강생활지원센터", r"남원주건강생활지원센터\s+주소:\s*([^※]+?)\s+혁신분소", r"남원주건강생활지원센터\s+(033-\d{3}-\d{4})"),
        ("hls:namwonju-annex", "남원주건강생활지원센터 혁신분소", r"혁신분소\s+주소:\s*([^※]+?)\s+※", r"혁신분소\s+(033-\d{3}-\d{4})"),
    ]
    for target_id, raw_name, address_pattern, phone_pattern in structured:
        match = re.search(address_pattern, nam_text)
        address = clean(match.group(1)) if match else ""
        evidence = evidence_window(nam_text, "혁신분소 주소" if "annex" in target_id else "남원주건강생활지원센터 주소", 40, 260)
        add_entity(target_id, nam_url, raw_name, address, evidence, "official_labeled_field")
        phone = re.search(phone_pattern, nam_text)
        if phone:
            add_contact(target_id, nam_url, phone.group(1), "문의전화", evidence, "inquiry_phone")

    seo_url = "https://www.wonju.go.kr/health/contents.do?key=5551"
    seo_text = pages[seo_url]["text"]
    seo_address = re.search(r"주소:\s*([^※]+?)\s+※", seo_text)
    seo_evidence = evidence_window(seo_text, "주소:", 40, 380)
    add_entity("hls:seowonju", seo_url, "서원주건강생활지원센터", clean(seo_address.group(1)) if seo_address else "", seo_evidence, "official_labeled_field")
    for number, label in [("033-737-3725", "만성질환·금연상담"), ("033-737-3726", "영양상담"), ("033-737-3724", "운동상담")]:
        if number in seo_evidence:
            add_contact("hls:seowonju", seo_url, number, label, seo_evidence, "counseling_phone")

    mental_url = "https://www.wonju.go.kr/health/contents.do?key=6230"
    mental_text = pages[mental_url]["text"]
    mental_evidence = evidence_window(mental_text, "원주시정신건강복지센터 이용시간", 160, 300)
    add_entity("mh:wonju", mental_url, "원주시정신건강복지센터", "원주시 원일로 139 4층", mental_evidence, "official_labeled_field", homepage="https://loveme.yonsei.kr/")
    suicide_direct = "https://loveme.yonsei.kr/"
    # The direct site's institution contacts are a shared header block outside
    # the page-specific main element.  Read the full saved DOM, then assign the
    # values to their explicitly labelled owner instead of copying the block to
    # every page/target.
    suicide_text = clean(" ".join(pages[suicide_direct]["soup"].stripped_strings))
    shared_contact_evidence = evidence_window(suicide_text, "정신건강복지센터 033-746-0199", 40, 220)
    for number, kind, label in [
        ("033-746-0199", "representative_phone", "센터 전화"),
        ("1577-0199", "national_hotline", "정신건강 위기상담"),
    ]:
        if number in suicide_text:
            add_contact("mh:wonju", suicide_direct, number, label, shared_contact_evidence, kind)

    suicide_match = re.search(r"자살예방센터\s+033-746-0198", suicide_text)
    suicide_evidence = suicide_match.group(0) if suicide_match else ""
    if suicide_evidence:
        add_entity(
            "mh:suicide", suicide_direct, "자살예방센터", "", suicide_evidence,
            "direct_site_organizational_unit", homepage="https://loveme.yonsei.kr/",
        )
        add_contact(
            "mh:suicide", suicide_direct, "033-746-0198", "자살예방센터 전화",
            suicide_evidence, "organizational_unit_phone",
        )

    addiction_url = "https://www.wonju.go.kr/health/contents.do?key=1671"
    addiction_text = pages[addiction_url]["text"]
    addiction_evidence = evidence_window(addiction_text, "원주시중독관리통합지원센터 이용시간", 120, 280)
    add_entity("mh:addiction", addiction_url, "원주시중독관리통합지원센터", "원주시 원일로 139 지하 1층", addiction_evidence, "official_labeled_field", homepage="http://www.alja.or.kr/")

    dementia_url = "https://wonju.nid.or.kr/center/map.aspx"
    dementia_text = pages[dementia_url]["text"]
    dementia_needle = "강원특별자치도원주시치매안심센터"
    dementia_evidence = evidence_window(dementia_text, dementia_needle, 80, 260)
    dementia_address = re.search(r"강원특별자치도\s+원주시\s+지니기길\s+11-20(?:\s*\([^)]*\))?", dementia_evidence)
    add_entity("mh:dementia", dementia_url, "원주시치매안심센터", dementia_address.group(0) if dementia_address else "", dementia_evidence, "direct_site_search_result", homepage="https://wonju.nid.or.kr/")
    if "033-737-4542" in dementia_text:
        add_contact("mh:dementia", dementia_url, "033-737-4542", "센터 전화", dementia_evidence, "representative_phone")

    service_specs = [
        ("hls:namwonju", nam_url, "통합 건강상담실", "health_consultation", "통합 건강상담실 운영", ""),
        ("hls:namwonju", nam_url, "만성질환 예방 및 관리사업", "chronic_disease_prevention", "만성질환 예방 및 관리사업", ""),
        ("hls:namwonju", nam_url, "신체활동 및 영양 프로그램", "physical_activity_nutrition", "신체활동 및 영양 프로그램", ""),
        ("hls:namwonju", nam_url, "어린이 건강체험관", "child_health_education", "어린이 건강체험관 운영", ""),
        ("hls:namwonju-annex", nam_url, "금연 클리닉", "smoking_cessation", "금연 클리닉 운영(혁신분소)", ""),
        ("hls:seowonju", seo_url, "통합 건강상담실", "health_consultation", "통합 건강상담실 운영", ""),
        ("hls:seowonju", seo_url, "신체활동 및 영양 프로그램", "physical_activity_nutrition", "신체활동 및 영양 프로그램 운영", ""),
        ("hls:seowonju", seo_url, "만성질환 예방 및 관리사업", "chronic_disease_prevention", "만성질환 예방 및 관리사업", ""),
        ("hls:seowonju", seo_url, "금연 클리닉", "smoking_cessation", "금연 클리닉 운영", ""),
        ("mh:wonju", mental_url, "정신보건사업", "adult_community_mental_health", "정신보건사업 대상 : 원주지역 정신질환자 및 가족, 원주시민", "원주지역 정신질환자 및 가족, 원주시민"),
        ("mh:wonju", mental_url, "아동·청소년 정신보건사업", "child_youth_mental_health", "아동 · 청소년 정신보건사업 대상 : 원주지역 아동 · 청소년", "원주지역 아동·청소년"),
        ("mh:wonju", mental_url, "생명사랑 및 자살예방사업", "suicide_prevention", "생명사랑 및 자살예방사업 대상 : 원주시민", "원주시민"),
        ("mh:addiction", addiction_url, "조기선별 및 단기개입 서비스", "addiction_screening_brief_intervention", "조기선별 및 단기개입 서비스", ""),
        ("mh:addiction", addiction_url, "사례관리 및 치료재활", "addiction_case_management_rehabilitation", "사례관리 및 치료재활", ""),
        ("mh:addiction", addiction_url, "예방 교육 및 홍보사업", "addiction_prevention_education_outreach", "예방 교육 및 홍보사업", ""),
    ]
    for target_id, url, name, category, evidence, target_population in service_specs:
        add_service(target_id, url, name, category, evidence, target_population)

    employee_url = "https://www.wonju.go.kr/health/selectEmployeeList.do?key=5464&searchCnd=all&searchDeptCode=1%404191050"
    employee_aliases: dict[str, list[str]] = {}
    for target in targets:
        if target["institution_type"] not in {"public_health_branch", "public_health_clinic"}:
            continue
        name = target["canonical_name"]
        aliases = [name, name.replace("읍보건지소", "보건지소"), name.replace("면보건지소", "보건지소")]
        if target["target_id"] == "phc:sanhyeon":
            aliases.append("산현보건지료소")
        employee_aliases[target["target_id"]] = list(dict.fromkeys(aliases))
    for table in pages[employee_url]["soup"].find_all("table"):
        for tr in table.find_all("tr"):
            values = [clean(" ".join(cell.stripped_strings)) for cell in tr.find_all(["th", "td"])]
            if len(values) < 5:
                continue
            evidence = " | ".join(values)
            phone = normalize_phone(values[2])
            if not re.fullmatch(r"033-\d{3,4}-\d{4}", phone):
                continue
            for target_id, aliases in employee_aliases.items():
                if any(values[4].startswith(alias) for alias in aliases):
                    add_contact(target_id, employee_url, values[2], values[4], evidence, "department_phone", values[0])

    entity_target_ids = {row["target_id"] for row in entities}
    for target in targets:
        if target["target_id"] not in entity_target_ids:
            warnings.append({
                "target_id": target["target_id"], "warning_type": "not_present_in_collected_sources",
                "source_url": "", "detail": "No institution-level evidence was found in the saved seed HTML.",
            })

    all_candidates = entities + contacts + services
    duplicate_candidate_count = len(all_candidates) - len({row["candidate_id"] for row in all_candidates})
    branch_count = sum(row["institution_type_normalized"] == "public_health_branch" for row in entities)
    clinic_count = sum(row["institution_type_normalized"] == "public_health_clinic" for row in entities)
    if duplicate_candidate_count:
        errors.append({"target_id": "", "error_type": "duplicate_candidate_id", "source_url": "", "detail": str(duplicate_candidate_count)})
    if branch_count != 9 or clinic_count != 8:
        errors.append({"target_id": "", "error_type": "official_list_count_mismatch", "source_url": list_url, "detail": f"branches={branch_count}, clinics={clinic_count}"})

    out = args.output_dir
    write_csv(out / "public_health_entity_candidates.csv", entities, ENTITY_COLUMNS)
    write_csv(out / "public_health_contact_candidates.csv", contacts, CONTACT_COLUMNS)
    write_csv(out / "public_health_schedule_candidates.csv", [], SCHEDULE_COLUMNS)
    write_csv(out / "public_health_service_candidates.csv", services, SERVICE_COLUMNS)
    write_csv(out / "public_health_evidence_links.csv", evidence_links, ["candidate_id", "target_id", "source_url", "evidence_type", "evidence_text", "evidence_hash"])
    write_csv(out / "public_health_extraction_errors.csv", errors, ["target_id", "error_type", "source_url", "detail"])
    write_csv(out / "public_health_extraction_warnings.csv", warnings, ["target_id", "warning_type", "source_url", "detail"])
    report = {
        "target_count": len(targets), "entity_candidate_count": len(entities),
        "entity_target_count": len(entity_target_ids), "contact_candidate_count": len(contacts),
        "schedule_candidate_count": 0, "service_candidate_count": len(services),
        "public_health_branch_count": branch_count, "public_health_clinic_count": clinic_count,
        "warning_count": len(warnings), "error_count": len(errors),
        "duplicate_candidate_count": duplicate_candidate_count, "offline_only": True,
        "integrity_checks": {
            "source_validation_passed": True, "only_saved_seed_html_used": True,
            "target_count_is_26": len(targets) == 26, "branch_count_is_9": branch_count == 9,
            "clinic_count_is_8": clinic_count == 8, "candidate_ids_unique": duplicate_candidate_count == 0,
            "all_candidates_traceable": all(
                row["target_id"] in target_by_id and row["source_url"] and row["evidence_hash"]
                for row in all_candidates
            ),
        },
    }
    report["integrity_checks_passed"] = all(report["integrity_checks"].values()) and not errors
    (out / "extraction_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["integrity_checks_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
