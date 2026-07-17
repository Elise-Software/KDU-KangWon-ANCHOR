"""OpenAI-compatible serving adapter for the existing Wonju P1 RAG pipeline."""
from __future__ import annotations

import asyncio
import base64
import copy
import csv
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
import requests

from audit import AuditStore, decode_webui_token


MODEL_ID = "wonju-health-rag"


def repo_root() -> Path:
    configured = os.getenv("WONJU_REPO_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[3]


ROOT = repo_root()
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = MODEL_ID
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


class FeedbackRequest(BaseModel):
    rating: str
    comment: str = ""


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def truthy(value: str | None) -> bool:
    return (value or "").casefold() in {"1", "true", "yes", "y"}


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return clean(content)
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
        ]
        return clean(" ".join(parts))
    return ""


def extract_question(messages: list[dict[str, Any]]) -> str:
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    for message in messages:
        value = message_text(message)
        if value:
            if message.get("role") == "user":
                user_messages.append(value)
            elif message.get("role") == "assistant":
                assistant_messages.append(value)
    if not user_messages:
        return ""
    latest = user_messages[-1]
    if len(user_messages) < 2:
        return latest
    def is_location(value: str) -> bool:
        return bool(re.search(
            r"[가-힣]+(?:동|읍|면|리)(?:이요|요|이?에요|예요|입니다|이야)?[.!?]?\Z",
            value,
        ))

    def is_followup(value: str) -> bool:
        return bool(
            len(value) <= 40
            and (
                is_location(value)
                or re.search(r"(?:하루|이틀|사흘|나흘|\d+\s*(?:일|시간|주|개월))째", value)
                or any(term in value for term in (
                "여기", "근처", "그곳", "그거", "전화번호도", "주소도", "운영시간도",
                "어제부터", "오늘부터", "부터고", "열은 없", "열이 있", "없어요", "있어요",
                "정도예요", "정도입니다", "복용 중", "먹고 있어", "살이에요", "오늘", "내일",
            ))
            )
        )

    if is_followup(latest):
        anchor_index = len(user_messages) - 2
        while anchor_index > 0 and is_followup(user_messages[anchor_index]):
            anchor_index -= 1
        anchor = user_messages[anchor_index]
        details = user_messages[anchor_index + 1:]
        combined = f"이전 요청: {anchor}\n추가 정보: {' / '.join(details)}"
        if is_location(latest) and assistant_messages:
            last_assistant = assistant_messages[-1]
            if "약국" in last_assistant and "약국" not in anchor:
                combined += "\n이어진 요청: 가까운 약국 찾아주세요"
            elif (
                any(term in last_assistant for term in ("의료기관", "병원", "진료기관"))
                and not any(term in anchor for term in ("병원", "의료기관", "의원"))
            ):
                combined += "\n이어진 요청: 가까운 의료기관 찾아주세요"
        return combined
    return latest


INTAKE_MARKER = re.compile(r"증상 확인\s*(\d+)/(\d+)")


def prepare_symptom_intake(
    messages: list[dict[str, Any]],
    safety_rules: dict[str, Any] | None = None,
    intake_config: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Continue a three-question symptom intake using the chat history itself.

    No server-side session is required. Once all three answers are present the
    combined history is sent through the normal RAG, safety and location flow.
    """
    from p1_rag.models import classify_risk, is_symptom_question

    if intake_config is None:
        config_path = ROOT / "config" / "p1_symptom_intake.json"
        if not config_path.is_file():
            return extract_question(messages), None
        intake_config = json.loads(config_path.read_text(encoding="utf-8"))

    question = extract_question(messages)
    if not question:
        return question, None

    latest_user_index = next((
        index for index in range(len(messages) - 1, -1, -1)
        if messages[index].get("role") == "user" and message_text(messages[index])
    ), -1)
    previous_assistant_index = next((
        index for index in range(latest_user_index - 1, -1, -1)
        if messages[index].get("role") == "assistant" and message_text(messages[index])
    ), -1)
    previous_assistant = message_text(messages[previous_assistant_index]) if previous_assistant_index >= 0 else ""
    marker = INTAKE_MARKER.search(previous_assistant)
    stage = int(marker.group(1)) if marker else 0

    if stage:
        first_marker_index = previous_assistant_index
        for index in range(previous_assistant_index, -1, -1):
            if messages[index].get("role") != "assistant":
                continue
            found = INTAKE_MARKER.search(message_text(messages[index]))
            if found and found.group(1) == "1":
                first_marker_index = index
                break
        anchor_index = next((
            index for index in range(first_marker_index - 1, -1, -1)
            if messages[index].get("role") == "user" and message_text(messages[index])
        ), latest_user_index)
        user_details = [
            message_text(message)
            for message in messages[anchor_index:latest_user_index + 1]
            if message.get("role") == "user" and message_text(message)
        ]
        question = "처음 증상: " + user_details[0]
        if len(user_details) > 1:
            question += "\n확인 답변: " + " / ".join(user_details[1:])

    if not is_symptom_question(question):
        return question, None
    if safety_rules and classify_risk(question, safety_rules)[0] != "none":
        return question, None
    plans = list(intake_config.get("plans", []))
    plan = next((
        value for value in plans
        if value.get("match_terms")
        and any(term.casefold() in question.casefold() for term in value.get("match_terms", []))
    ), next((value for value in plans if not value.get("match_terms")), None))
    stages = list((plan or {}).get("stages", []))
    if not stages or stage >= len(stages):
        if stage and not any(term in question for term in ("의료기관", "병원", "약국")):
            question += "\n요청: 현재 위치 주변의 적절한 의료기관 찾기"
        return question, None

    current = stages[stage]
    prompt = (
        f"{clean(current.get('intro'))}\n\n"
        f"### 증상 확인 {stage + 1}/{len(stages)}\n"
        f"{clean(current.get('question'))}"
    ).strip()
    return question, {
        "question": question,
        "answer": prompt,
        "response_kind": "symptom_intake",
        "intake_plan_id": clean((plan or {}).get("plan_id")),
        "intake_stage_id": clean(current.get("stage_id")),
        "risk_category": "none",
        "safety_rule_applied": False,
        "citations": [],
        "retrieved_chunk_ids": [],
        "generator_model": "deterministic-symptom-intake",
        "temperature": 0,
        "generation_policy_version": "symptom_intake_v1",
        "institutions": [],
        "safety_contacts": [],
        "retrieval_model": "not-required-during-intake",
        "reranker_model": "not-required-during-intake",
    }


class InstitutionCatalog:
    """Read-only projection of P0 master/link artifacts for answer cards."""

    def __init__(self, root: Path) -> None:
        integrated = root / "data" / "integrated" / "wonju"
        processed = root / "data" / "p1_rag" / "processed"

        self.links = read_csv(processed / "document_institution_links.csv")
        masters = read_csv(integrated / "institutions_p0_public_health_enriched.csv")
        self.master_by_id = {row.get("institution_id", ""): row for row in masters}
        profiles = read_csv(integrated / "institution_public_health_profiles.csv")
        self.profile_by_id = {row.get("institution_id", ""): row for row in profiles}
        self.contacts_by_id = self._group(read_csv(integrated / "institution_contacts.csv"), "institution_id")
        self.schedules_by_id = self._group(
            read_csv(integrated / "institution_operation_schedules.csv"), "institution_id"
        )
        self.pharmacy_by_id = self._group(
            read_csv(integrated / "institution_pharmacy_operations.csv"), "institution_id"
        )
        self.pharmacy_sources_by_id = self._group(
            read_csv(integrated / "institution_pharmacy_operation_sources.csv"), "institution_id"
        )
        self.organizational_units = read_csv(integrated / "institution_organizational_units.csv")
        decisions = read_csv(integrated / "pharmacy_operation_manual_review.csv")
        self.pharmacy_review_by_id = {row.get("institution_id", ""): row for row in decisions}
        self.location_names = sorted({
            value
            for master in self.master_by_id.values()
            for value in re.findall(
                r"(?<![가-힣])([가-힣]{2,}(?:동|읍|면))(?![가-힣])",
                clean(master.get("address")),
            )
        }, key=len, reverse=True)

    @staticmethod
    def _group(rows: list[dict[str, str]], key: str) -> dict[str, list[dict[str, str]]]:
        output: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            value = row.get(key, "")
            if value:
                output.setdefault(value, []).append(row)
        return output

    def for_citations(self, citations: list[dict[str, str]], limit: int = 3) -> list[dict[str, Any]]:
        chunk_ids = {row.get("chunk_id", "") for row in citations}
        doc_ids = {row.get("doc_id", "") for row in citations}
        exact = [row for row in self.links if row.get("chunk_id") in chunk_ids]
        candidates = exact or [row for row in self.links if row.get("doc_id") in doc_ids]
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in candidates:
            institution_id = link.get("institution_id", "")
            if not institution_id or institution_id in seen:
                continue
            seen.add(institution_id)
            output.append(self._card(institution_id, link))
            if len(output) >= limit:
                break
        return output

    def _card(
        self, institution_id: str, link: dict[str, str], day_type_filter: str | None = None
    ) -> dict[str, Any]:
        master = self.master_by_id.get(institution_id, {})
        profile = self.profile_by_id.get(institution_id, {})
        pharmacy_review = self.pharmacy_review_by_id.get(institution_id, {})
        phones: list[dict[str, str]] = []
        phone_seen: set[str] = set()
        for row in self.contacts_by_id.get(institution_id, []):
            value = clean(row.get("contact_value"))
            if not value or value in phone_seen:
                continue
            phone_seen.add(value)
            phones.append({
                "label": clean(row.get("contact_label")) or "전화",
                "value": value,
            })
        profile_phone = clean(profile.get("representative_phone"))
        if profile_phone and profile_phone not in phone_seen:
            phone_seen.add(profile_phone)
            phones.append({"label": "대표전화", "value": profile_phone})
        master_phone = clean(master.get("phone"))
        review_notice = ""
        if pharmacy_review.get("phone_status") == "unresolved_official_conflict":
            phones = []
            phone_seen = set()
            for label, value in (
                ("운영정보 출처", clean(pharmacy_review.get("source_phone"))),
                ("관내의료기관 출처", clean(pharmacy_review.get("master_phone"))),
            ):
                if value and value not in phone_seen:
                    phone_seen.add(value)
                    phones.append({"label": label, "value": value})
            review_notice = (
                "원주시 공식 자료에서 전화번호가 서로 다르게 확인됩니다. "
                "방문 전 번호를 재확인하세요."
            )
        elif master_phone and master_phone not in phone_seen and not profile_phone:
            phones.append({"label": "대표전화", "value": master_phone})

        if pharmacy_review.get("review_scope") == "source_schedule":
            review_notice = (
                "공식 출처별 운영시간이 다릅니다. 방문 전 약국에 전화하거나 "
                "E-GEN·119를 통해 확인하세요."
            )

        current_status = clean(profile.get("current_status"))
        status_labels = {
            "source_confirmed": "공식 출처에서 운영 확인",
            "operating_confirmed": "운영 중으로 확인",
            "unverified": "현재 운영 상태 미확인",
            "current_status_unknown": "현재 운영 상태 미확인",
        }
        current_status_label = status_labels.get(current_status, "")
        current_status_basis = ""
        if current_status == "operating_confirmed":
            current_status_basis = "기록된 사용자 확인 결정"
        elif current_status == "source_confirmed":
            current_status_basis = "수집된 공식 출처"

        hours: list[dict[str, str]] = []
        hour_seen: set[tuple[str, str]] = set()
        day_labels = {"weekday": "평일", "saturday": "토요일", "sunday": "일요일·공휴일"}
        for row in self.schedules_by_id.get(institution_id, []):
            if not self._day_matches(row.get("day_type"), day_type_filter):
                continue
            day = day_labels.get(row.get("day_type", ""), clean(row.get("day_type")) or "운영시간")
            value = clean(row.get("hours_normalized")) or clean(row.get("hours_source_raw"))
            if value and (day, value) not in hour_seen:
                hour_seen.add((day, value))
                hours.append({"label": day, "value": value})
        preserve_source_variants = pharmacy_review.get("review_scope") == "source_schedule"
        pharmacy_rows = [
            row for row in self.pharmacy_by_id.get(institution_id, [])
            if self._day_matches(row.get("day_type"), day_type_filter)
            and (
                (
                    row.get("schedule_status") == "parsed"
                    and clean(row.get("hours_normalized")) not in {"", "-", "not_provided"}
                )
                or (preserve_source_variants and row.get("schedule_status") == "not_provided")
            )
        ]

        def pharmacy_value(row: dict[str, str]) -> str:
            value = clean(row.get("hours_normalized")) or clean(row.get("hours_source_raw"))
            return "미제공" if value in {"", "-", "not_provided"} else value

        pharmacy_values_by_day: dict[str, set[str]] = {}
        for row in pharmacy_rows:
            pharmacy_values_by_day.setdefault(row.get("day_type", ""), set()).add(
                pharmacy_value(row)
            )
        source_labels = {"late_night": "심야약국", "year_round": "연중무휴약국"}
        for row in pharmacy_rows:
            day = day_labels.get(row.get("day_type", ""), clean(row.get("day_type")) or "운영시간")
            value = pharmacy_value(row)
            label = day
            if preserve_source_variants or len(pharmacy_values_by_day.get(row.get("day_type", ""), set())) > 1:
                label = f"{day} ({source_labels.get(row.get('source_type', ''), '공식 출처')})"
            if value and (label, value) not in hour_seen:
                hour_seen.add((label, value))
                hours.append({"label": label, "value": value})

        return {
            "institution_id": institution_id,
            "name": clean(link.get("institution_name")) or clean(profile.get("canonical_name")) or clean(master.get("name")) or institution_id,
            "category": clean(master.get("category")) or clean(master.get("normalized_category")),
            "address": clean(profile.get("address")) or clean(master.get("address")),
            "map_url": self._map_url(
                clean(link.get("institution_name")) or clean(profile.get("canonical_name")) or clean(master.get("name")),
                clean(profile.get("address")) or clean(master.get("address")),
            ),
            "phones": phones,
            "operation_hours": hours[:8],
            "current_status": current_status,
            "current_status_label": current_status_label,
            "current_status_basis": current_status_basis,
            "review_notice": review_notice,
            "matched_by": clean(link.get("link_method")),
            "match_confidence": clean(link.get("confidence")),
        }

    @staticmethod
    def _map_url(name: str, address: str) -> str:
        """Build Kakao's documented, key-free map search URL from official fields."""
        query = clean(f"{name} {address}")
        return f"https://map.kakao.com/link/search/{quote(query, safe='')}" if query else ""

    @staticmethod
    def _is_active(master: dict[str, str]) -> bool:
        return clean(master.get("active_status")) in {"", "active"}

    def _area_ids(self, location: str, categories: set[str]) -> list[str]:
        """Return active area matches, removing duplicate name/address/phone records."""
        matches: list[tuple[str, str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for institution_id, master in self.master_by_id.items():
            if clean(master.get("normalized_category")) not in categories:
                continue
            if location not in clean(master.get("address")) or not self._is_active(master):
                continue
            duplicate_key = (
                self._key(master.get("name")),
                self._key(master.get("address")),
                self._key(master.get("phone")),
            )
            if duplicate_key in seen:
                continue
            seen.add(duplicate_key)
            matches.append((
                clean(master.get("normalized_category")),
                clean(master.get("name")),
                institution_id,
                clean(master.get("address")),
            ))
        return [row[2] for row in sorted(matches)]

    @staticmethod
    def _key(value: str | None) -> str:
        return re.sub(r"[^0-9a-z가-힣]", "", (value or "").casefold())

    @staticmethod
    def _result(answer_text: str, institutions: list[dict[str, Any]], citations: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "question": "",
            "answer": answer_text,
            "risk_category": "none",
            "safety_rule_applied": False,
            "citations": citations,
            "retrieved_chunk_ids": [row["chunk_id"] for row in citations],
            "generator_model": "deterministic-structured-institution-lookup",
            "temperature": 0,
            "generation_policy_version": "structured_lookup_v1",
            "institutions": institutions,
            "safety_contacts": [],
            "retrieval_model": "p0-structured-master",
            "reranker_model": "not-required-for-structured-lookup",
        }

    @staticmethod
    def _medical_master_citation(institution_id: str = "wonju-medical-master") -> dict[str, str]:
        return {
            "url": "https://www.wonju.go.kr/health/selectResrceListPA3.do?key=1787",
            "document": "원주시 관내의료기관 공식 목록",
            "doc_id": "structured:wonju-medical-master",
            "chunk_id": f"institution:{institution_id}",
        }

    def _profile_citation(self, institution_id: str) -> dict[str, str] | None:
        profile = self.profile_by_id.get(institution_id, {})
        url = clean(profile.get("source_url")) or clean(profile.get("match_source_url"))
        if not profile:
            return None
        target_id = clean(profile.get("target_id")) or institution_id
        document = f"{clean(profile.get('canonical_name')) or '공공보건기관'} 공식 안내"
        if not url:
            document = f"{clean(profile.get('canonical_name')) or '공공보건기관'} P0 검토 결정"
        return {
            "url": url,
            "document": document,
            "doc_id": f"structured:{target_id}",
            "chunk_id": f"profile:{target_id}",
        }

    def _pharmacy_citations(self, institution_ids: set[str] | None = None) -> list[dict[str, str]]:
        rows = []
        if institution_ids:
            for institution_id in institution_ids:
                rows.extend(self.pharmacy_sources_by_id.get(institution_id, []))
        else:
            rows = [row for values in self.pharmacy_sources_by_id.values() for row in values]
        output: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            url = clean(row.get("source_url"))
            if not url or url in seen:
                continue
            seen.add(url)
            output.append({
                "url": url,
                "document": f"원주시 {clean(row.get('source_label')) or '약국 운영정보'}",
                "doc_id": f"structured:pharmacy:{clean(row.get('source_type')) or len(output)}",
                "chunk_id": clean(row.get("source_record_id")) or f"pharmacy-source:{len(output) + 1}",
            })
        return output[:3]

    def _location_in_question(self, question: str) -> str:
        candidates = re.findall(r"[가-힣]{2,}(?:동|읍|면)", question)
        for candidate in reversed(candidates):
            if candidate in {"읍면", "읍면동"}:
                continue
            if any(candidate in clean(row.get("address")) for row in self.master_by_id.values()):
                return candidate
        for location in self.location_names:
            if location in question:
                return location
            stem = location[:-1]
            if len(stem) >= 2 and re.search(
                rf"{re.escape(stem)}(?:에서|근처|주변|쪽|에|입니다|이요|요|\s|,)",
                question,
            ):
                return location
        return ""

    @staticmethod
    def _requested_day_type(question: str) -> str | None:
        now = datetime.now(timezone(timedelta(hours=9)))
        if "오늘" in question:
            weekday = now.weekday()
            return "weekday" if weekday < 5 else "saturday" if weekday == 5 else "sunday"
        if "내일" in question:
            weekday = (now + timedelta(days=1)).weekday()
            return "weekday" if weekday < 5 else "saturday" if weekday == 5 else "sunday"
        if "주말" in question:
            return "weekend"
        if any(term in question for term in ("토요일", "토욜")):
            return "saturday"
        if any(term in question for term in ("일요일", "공휴일", "일욜")):
            return "sunday"
        if any(term in question for term in ("평일", "월요일", "화요일", "수요일", "목요일", "금요일")):
            return "weekday"
        return None

    @staticmethod
    def _day_matches(actual: str | None, requested: str | None) -> bool:
        if not requested:
            return True
        if requested == "weekend":
            return actual in {"saturday", "sunday"}
        return actual == requested

    def _name_aliases(self, name: str | None) -> set[str]:
        full = self._key(name)
        aliases = {full} if full else set()
        if full.startswith("원주시") and len(full) > 3:
            aliases.add(full[3:])
        return {value for value in aliases if value not in {"병원", "의원", "약국", "센터", "보건"}}

    def structured_query(self, question: str) -> dict[str, Any] | None:
        question_key = self._key(question)

        # Organizational units such as the suicide-prevention center are not
        # independent institutions but still need a direct, non-crisis lookup.
        for unit in sorted(self.organizational_units, key=lambda row: len(self._key(row.get("unit_name"))), reverse=True):
            name = clean(unit.get("unit_name"))
            aliases = self._name_aliases(name)
            if not any(len(alias) >= 3 and alias in question_key for alias in aliases):
                continue
            parent_id = clean(unit.get("parent_institution_id"))
            parent = self.master_by_id.get(parent_id, {})
            phone = clean(unit.get("representative_phone"))
            card = {
                "institution_id": clean(unit.get("organizational_unit_id")),
                "name": name,
                "address": clean(parent.get("address")),
                "phones": [{"label": "대표전화", "value": phone}] if phone else [],
                "operation_hours": [],
                "review_notice": "원주시정신건강복지센터 소속 조직단위입니다.",
                "matched_by": "official_organizational_unit_name",
                "match_confidence": "1.0000",
            }
            citation = {
                "url": clean(unit.get("source_url")),
                "document": f"{name} 공식 안내",
                "doc_id": f"structured:{clean(unit.get('target_id'))}",
                "chunk_id": f"organizational-unit:{clean(unit.get('target_id'))}",
            }
            return self._result("요청하신 조직의 공식 연락 정보를 확인했습니다.", [card], [citation])

        # Exact institution names take precedence over semantic document
        # retrieval, which can otherwise attach a page that merely mentions it.
        named: list[tuple[int, str, dict[str, str]]] = []
        for institution_id, master in self.master_by_id.items():
            matches = [
                alias for alias in self._name_aliases(master.get("name"))
                if len(alias) >= 3 and alias in question_key
            ]
            if matches:
                named.append((max(map(len, matches)), institution_id, master))
        document_detail_terms = (
            "준비물", "신청", "대상", "자격", "프로그램", "서비스", "지원 내용",
            "검사 방법", "접종", "예약 방법", "이용 방법", "상담 내용", "교육 내용", "비용",
        )
        structured_field_terms = (
            "주소", "전화", "연락처", "위치", "찾아오", "홈페이지", "운영시간",
            "이용시간", "몇 시", "영업시간", "휴무", "운영 중", "운영중", "상태", "폐업",
        )
        if (
            named
            and any(term in question for term in document_detail_terms)
            and not any(term in question for term in structured_field_terms)
        ):
            named = [
                item for item in named
                if item[1] in self.pharmacy_by_id or clean(item[2].get("normalized_category")) == "약국"
            ]
        if named:
            _, institution_id, master = max(named, key=lambda item: item[0])
            is_pharmacy = institution_id in self.pharmacy_by_id or clean(master.get("normalized_category")) == "약국"
            day_type = self._requested_day_type(question) if is_pharmacy else None
            card = self._card(institution_id, {
                "institution_name": clean(master.get("name")),
                "link_method": "official_name_exact",
                "confidence": "1.0000",
            }, day_type_filter=day_type)
            citation = self._profile_citation(institution_id)
            if citation is None and self.pharmacy_sources_by_id.get(institution_id):
                citations = self._pharmacy_citations({institution_id})
            else:
                citations = [citation or self._medical_master_citation(institution_id)]
            if is_pharmacy and day_type:
                if any(row.get("value") != "미제공" for row in card["operation_hours"]):
                    answer_text = (
                        "요청하신 약국의 해당 요일 공식 운영시간을 확인했습니다. "
                        "실시간 영업 여부는 확인할 수 없으므로 방문 전에 전화로 확인해 주세요."
                    )
                else:
                    answer_text = (
                        "요청하신 약국의 해당 요일 운영시간은 공식 자료에 제공되지 않았습니다. "
                        "방문 전에 약국 또는 E-GEN·119를 통해 확인해 주세요."
                    )
            elif any(term in question for term in ("운영 중", "운영중", "상태", "폐업")) and card.get("current_status"):
                if card["current_status"] == "operating_confirmed":
                    answer_text = (
                        f"{card['name']}은 P0 검토 결정에 운영 중으로 확인되어 있습니다. "
                        "다만 최신 공식 운영시간 근거는 별도로 확인되지 않았으므로 방문 전 전화 확인이 필요합니다."
                    )
                else:
                    answer_text = f"{card['name']}의 현재 상태는 '{card['current_status_label']}'으로 기록되어 있습니다."
            else:
                answer_text = "요청하신 기관의 공식 정보를 확인했습니다."
            return self._result(answer_text, [card], citations)

        location = self._location_in_question(question)
        medical_terms = (
            "병원", "의원", "의료기관", "진료기관", "정형외과", "내과", "소아과",
            "소아청소년과", "안과", "이비인후과", "피부과", "산부인과", "치과", "한의원",
        )
        wants_pharmacy = "약국" in question
        wants_medical = any(term in question for term in medical_terms)

        # A combined nearby request must not be narrowed to the small special-
        # operation pharmacy dataset. The official medical master already has
        # ordinary pharmacies and medical facilities for every address area.
        if wants_pharmacy and wants_medical:
            if not location:
                return self._result(
                    "가까운 병원과 약국을 찾으려면 현재 계신 원주시 읍면동을 알려주세요. "
                    "위치를 알려주시면 원주시 공식 관내의료기관 목록에서 해당 지역의 병·의원과 일반 약국을 함께 찾아드릴게요.",
                    [],
                    [self._medical_master_citation()],
                )
            medical_categories = {"종합병원", "병원", "의원", "한방병원", "한의원", "치과병원", "치과"}
            # Keep the first response compact enough for the accessibility-focused UI
            # and for Open WebUI's virtualized message DOM. Users can continue with a
            # narrower location or category when they need more choices.
            medical_ids = self._area_ids(location, medical_categories)[:2]
            pharmacy_ids = self._area_ids(location, {"약국"})[:1]
            combined_ids = medical_ids + pharmacy_ids
            cards = [
                self._card(institution_id, {
                    "institution_name": clean(self.master_by_id[institution_id].get("name")),
                    "link_method": "official_address_area_match",
                    "confidence": "1.0000",
                })
                for institution_id in combined_ids
            ]
            return self._result(
                (
                    f"원주시 공식 관내의료기관 목록에서 주소에 {location}이 확인되는 "
                    f"의료기관 {len(medical_ids)}곳과 약국 {len(pharmacy_ids)}곳을 찾았습니다. "
                    "현재 위치와의 실제 거리 순서는 아니며, 지도에서 위치를 확인하고 방문 전에 전화로 진료·영업 여부를 확인해 주세요."
                    if cards else f"원주시 공식 관내의료기관 목록에서 주소가 {location}인 병원이나 약국을 찾지 못했습니다."
                ),
                cards,
                [self._medical_master_citation()],
            )

        if "약국" in question:
            day_type = self._requested_day_type(question)
            if not location:
                if "오늘" in question:
                    day_hint = "오늘 이용할 수 있는 "
                elif "내일" in question:
                    day_hint = "내일 이용할 수 있는 "
                elif "주말" in question:
                    day_hint = "주말에 이용할 수 있는 "
                else:
                    day_hint = ""
                return self._result(
                    f"{day_hint}약국을 정확히 좁히려면 현재 계신 원주시 읍면동을 알려주세요. "
                    "위치를 알려주시면 공식 심야·연중무휴 약국 운영정보에서 주소, 전화번호와 "
                    + ("해당 요일 운영시간을 확인해 드릴게요." if day_type else "등록된 운영시간을 확인해 드릴게요."),
                    [],
                    self._pharmacy_citations(),
                )
            candidate_ids: list[str] = []
            for institution_id, rows in self.pharmacy_by_id.items():
                master = self.master_by_id.get(institution_id, {})
                if location not in clean(master.get("address")):
                    continue
                if any(
                    self._day_matches(row.get("day_type"), day_type)
                    and row.get("schedule_status") == "parsed"
                    and clean(row.get("hours_normalized")) not in {"", "-", "not_provided"}
                    for row in rows
                ):
                    candidate_ids.append(institution_id)
            candidate_ids = sorted(set(candidate_ids), key=lambda value: clean(self.master_by_id[value].get("name")))[:3]
            cards = [
                self._card(institution_id, {
                    "institution_name": clean(self.master_by_id[institution_id].get("name")),
                    "link_method": (
                        "official_address_area_and_requested_day_schedule"
                        if day_type else "official_address_area_and_schedule"
                    ),
                    "confidence": "1.0000",
                }, day_type_filter=day_type)
                for institution_id in candidate_ids
            ]
            if not cards:
                general_ids = self._area_ids(location, {"약국"})[:3]
                general_cards = [
                    self._card(institution_id, {
                        "institution_name": clean(self.master_by_id[institution_id].get("name")),
                        "link_method": "official_address_area_match_no_schedule",
                        "confidence": "1.0000",
                    })
                    for institution_id in general_ids
                ]
                if general_cards:
                    requested_period = "요청하신 날짜의 " if day_type else ""
                    return self._result(
                        f"{location} 주소로 등록된 일반 약국 {len(general_cards)}곳을 찾았습니다. "
                        f"다만 원주시 공식 자료에서 {requested_period}운영시간은 확인되지 않아 실시간 영업 여부를 단정할 수 없습니다. "
                        "지도에서 위치를 확인하고 방문 전에 전화하거나 E-GEN에서 확인해 주세요.",
                        general_cards,
                        [self._medical_master_citation()],
                    )
                requested_period = "주말에 " if day_type == "weekend" else "해당 요일에 " if day_type else ""
                return self._result(
                    f"공식 심야·연중무휴 운영정보에서 {requested_period}{location} 주소로 운영시간이 명시된 약국을 찾지 못했습니다. "
                    "일반 약국 운영 여부는 E-GEN 또는 119에서 추가로 확인해 주세요.",
                    [],
                    self._pharmacy_citations(),
                )
            schedule_description = (
                "주말 운영시간이 확인되는" if day_type == "weekend"
                else "해당 요일 운영시간이 확인되는" if day_type
                else "등록된 운영시간이 확인되는"
            )
            return self._result(
                f"공식 운영정보에서 {schedule_description} {location} 약국을 안내합니다. "
                "실시간 영업 여부와 공휴일 변동은 확인할 수 없으니 방문 전에 반드시 전화로 확인해 주세요.",
                cards,
                self._pharmacy_citations(set(candidate_ids)),
            )

        nearby_terms = ("가까운 병원", "주변 병원", "근처 병원", "병원 찾아", "의료기관 찾아")
        facility_terms = medical_terms
        find_terms = ("어디", "찾", "근처", "주변", "가까", "추천")
        nearby_request = (
            any(term in question for term in nearby_terms)
            or (any(term in question for term in facility_terms) and any(term in question for term in find_terms))
            or (bool(location) and any(term in question for term in facility_terms))
        )
        if nearby_request:
            if not location:
                return self._result(
                    "집 주변 의료기관을 찾으려면 현재 계신 원주시 읍면동을 알려주세요. "
                    "정확한 거리를 계산하는 대신, 공식 관내의료기관 목록에서 해당 지역 주소가 확인되는 곳을 안내합니다.",
                    [],
                    [self._medical_master_citation()],
                )
            categories = {"종합병원", "병원", "의원", "한방병원", "한의원", "치과병원", "치과"}
            priorities = {"종합병원": 0, "병원": 1, "의원": 2, "한방병원": 3, "한의원": 4, "치과병원": 5, "치과": 6}
            requested_specialty = next((
                term for term in (
                    "정형외과", "내과", "소아청소년과", "소아과", "안과", "이비인후과",
                    "피부과", "산부인과", "치과", "한의원",
                ) if term in question
            ), "")
            matches = [
                (priorities.get(clean(master.get("normalized_category")), 99), clean(master.get("name")), institution_id, master)
                for institution_id, master in self.master_by_id.items()
                if clean(master.get("normalized_category")) in categories
                and location in clean(master.get("address"))
                and clean(master.get("active_status")) in {"", "active"}
                and (
                    not requested_specialty
                    or requested_specialty in clean(master.get("name"))
                    or requested_specialty in clean(master.get("normalized_category"))
                    or (requested_specialty == "소아과" and "소아청소년과" in clean(master.get("name")))
                )
            ]
            cards = [
                self._card(institution_id, {
                    "institution_name": clean(master.get("name")),
                    "link_method": "official_address_area_match",
                    "confidence": "1.0000",
                })
                for _, _, institution_id, master in sorted(matches)[:3]
            ]
            return self._result(
                (
                    f"원주시 공식 관내의료기관 목록에서 주소에 {location}이 확인되는 "
                    f"{requested_specialty or '의료기관'}을 안내합니다. "
                    "직선거리 순위는 아니므로 방문 전에 진료과목과 접수 가능 여부를 전화로 확인해 주세요."
                    if cards else f"원주시 공식 관내의료기관 목록에서 주소가 {location}인 기관을 찾지 못했습니다."
                ),
                cards,
                [self._medical_master_citation()],
            )
        return None


class WonjuRagService:
    """Loads and calls the existing P1 index, reranker, safety rules, and generator."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.index = None
        self.reranker = None
        self.generator = None
        self.last_error = ""
        self.catalog = InstitutionCatalog(root)

        from p1_rag.common import read_json, read_jsonl

        self.config = read_json(root / "config" / "p1_rag_config.json")
        self.safety = read_json(root / "config" / "p1_rag_safety_rules.json")
        self.intake = read_json(root / "config" / "p1_symptom_intake.json")
        self.evidence_pool = read_jsonl(root / "data" / "p1_rag" / "index" / "chunk_metadata.jsonl")
        generation_url = os.getenv("VLLM_BASE_URL", "").strip()
        if generation_url:
            self.config = copy.deepcopy(self.config)
            self.config["generation"]["base_url"] = generation_url.rstrip("/")

    def required_files(self) -> list[Path]:
        return [
            self.root / "config" / "p1_rag_config.json",
            self.root / "config" / "p1_rag_safety_rules.json",
            self.root / "config" / "p1_symptom_intake.json",
            self.root / "data" / "p1_rag" / "index" / "bge_m3.faiss",
            self.root / "data" / "p1_rag" / "index" / "chunk_metadata.jsonl",
            self.root / "data" / "p1_rag" / "processed" / "document_institution_links.csv",
            self.root / "data" / "integrated" / "wonju" / "institutions_p0_public_health_enriched.csv",
        ]

    def status(self) -> dict[str, Any]:
        missing = [path.relative_to(self.root).as_posix() for path in self.required_files() if not path.is_file()]
        return {
            "status": "ok" if not missing else "degraded",
            "model": MODEL_ID,
            "required_files_present": not missing,
            "missing_files": missing,
            "retrieval_loaded": self.index is not None and self.reranker is not None,
            "generation_loaded": self.generator is not None,
            "last_error": self.last_error,
        }

    def ensure_loaded(self, include_generator: bool = True) -> None:
        if self.index is not None and self.reranker is not None and (self.generator is not None or not include_generator):
            return
        with self.lock:
            try:
                from p1_rag.models import EmbeddingIndex, OpenAICompatibleGenerator, Reranker

                if self.index is None:
                    self.index = EmbeddingIndex(self.config["embedding"])
                    self.index.load()
                if self.reranker is None:
                    self.reranker = Reranker(self.config["reranker"])
                if include_generator and self.generator is None:
                    self.generator = OpenAICompatibleGenerator(self.config["generation"])
                self.last_error = ""
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise

    def _intake_evidence(self, question: str) -> list[dict[str, Any]]:
        """Resolve evidence declared by the selected intake plan."""
        plans = list(self.intake.get("plans", []))
        plan = next((
            value for value in plans
            if value.get("match_terms")
            and any(term.casefold() in question.casefold() for term in value.get("match_terms", []))
        ), None)
        if not plan:
            return []
        sources = {row.get("source_id"): row for row in self.intake.get("sources", [])}
        output: list[dict[str, Any]] = []
        for source_id in plan.get("final_evidence_source_ids", []):
            source = sources.get(source_id, {})
            url = clean(source.get("url"))
            candidates = [row for row in self.evidence_pool if clean(row.get("url")) == url]
            if not candidates:
                continue
            terms = [term.casefold() for term in source.get("retrieval_terms", [])]
            selected = max(candidates, key=lambda row: (
                sum(term in f"{row.get('section_title', '')} {row.get('text', '')}".casefold() for term in terms),
                -len(str(row.get("text", ""))),
            ))
            output.append(dict(selected, embedding_score=1.0, reranker_score=1.0))
        return output

    def query(self, question: str) -> dict[str, Any]:
        from p1_rag.models import answer, augment_safety_evidence, classify_risk, is_symptom_question

        risk_category, _ = classify_risk(question, self.safety)
        if risk_category != "none":
            contexts = augment_safety_evidence(question, [], self.evidence_pool, self.safety)
            result = answer(question, contexts, SafetyOnlyGenerator(self.config["generation"]), self.safety)
            result["institutions"] = self.catalog.for_citations(result["citations"])
            result["safety_contacts"] = safety_contacts(result["risk_category"], result["institutions"])
            result["retrieval_model"] = "deterministic-safety-evidence"
            result["reranker_model"] = "not-required-for-safety"
            return result

        structured = self.catalog.structured_query(question)
        symptom_question = is_symptom_question(question)
        if structured is not None and not symptom_question:
            structured["question"] = question
            return structured

        self.ensure_loaded(include_generator=True)
        assert self.index is not None and self.reranker is not None

        candidates = self.index.search([question], int(self.config["reranker"]["candidate_count"]))[0]
        reranked = self.reranker.rerank(question, candidates)
        minimum_score = float(self.config["reranker"].get("minimum_relevance_score", -4.0))
        relevant = [
            row for row in reranked[: int(self.config["evaluation"]["retrieval_top_k"])]
            if float(row.get("reranker_score", float("-inf"))) >= minimum_score
            and context_is_relevant(question, row)
        ]
        declared_evidence = self._intake_evidence(question) if symptom_question else []
        combined_relevant: list[dict[str, Any]] = []
        seen_chunks: set[str] = set()
        for row in [*declared_evidence, *relevant]:
            chunk_id = clean(row.get("chunk_id"))
            if not chunk_id or chunk_id in seen_chunks:
                continue
            seen_chunks.add(chunk_id)
            combined_relevant.append(row)
        contexts = augment_safety_evidence(
            question,
            combined_relevant,
            self.index.chunks,
            self.safety,
        )

        generator = self.generator
        if generator is None:
            generator = SafetyOnlyGenerator(self.config["generation"])
        result = answer(question, contexts, generator, self.safety)
        if symptom_question:
            result["answer"] = apply_response_consistency(
                str(result.get("answer", "")), question, self.intake
            )
        if structured is not None and symptom_question:
            result = merge_structured_symptom_result(result, structured)
            institutions = result["institutions"]
        else:
            institutions = self.catalog.for_citations(result["citations"])
            if result["risk_category"] == "none":
                institutions = institutions_mentioned_in_answer(institutions, question, result["answer"])
        result["institutions"] = institutions
        result["safety_contacts"] = safety_contacts(result["risk_category"], result["institutions"])
        result["retrieval_model"] = self.config["embedding"]["model"]
        result["reranker_model"] = self.config["reranker"]["model"]
        return result


def merge_structured_symptom_result(
    result: dict[str, Any], structured: dict[str, Any]
) -> dict[str, Any]:
    """Keep symptom guidance while attaching the explicitly requested local lookup."""
    output = dict(result)
    lookup_answer = clean(structured.get("answer"))
    answer_text = str(result.get("answer", "")).strip()
    final_section = f"### 5. 가까운 의료기관 찾기\n{lookup_answer}"
    fifth_heading = re.compile(
        r"^#{1,6}\s*5\.\s*가까운 의료기관 찾기\s*\n.*\Z",
        re.MULTILINE | re.DOTALL,
    )
    if fifth_heading.search(answer_text):
        output["answer"] = fifth_heading.sub(final_section, answer_text)
    else:
        output["answer"] = f"{answer_text}\n\n{final_section}".strip()

    merged_citations: list[dict[str, str]] = []
    seen_chunks: set[str] = set()
    for citation in [*result.get("citations", []), *structured.get("citations", [])]:
        chunk_id = clean(citation.get("chunk_id"))
        if not chunk_id or chunk_id in seen_chunks:
            continue
        seen_chunks.add(chunk_id)
        merged_citations.append(citation)
    output["citations"] = merged_citations
    output["retrieved_chunk_ids"] = list(dict.fromkeys([
        *result.get("retrieved_chunk_ids", []),
        *(row["chunk_id"] for row in merged_citations),
    ]))
    output["institutions"] = list(structured.get("institutions", []))
    return output


def apply_response_consistency(answer_text: str, question: str, intake_config: dict[str, Any]) -> str:
    """Apply declarative rewrites when generated prose contradicts confirmed intake facts."""
    output = answer_text
    normalized_question = question.casefold()
    for rule in intake_config.get("response_consistency_rules", []):
        terms = [clean(value).casefold() for value in rule.get("when_question_contains_any", [])]
        if terms and not any(term in normalized_question for term in terms):
            continue
        for original, replacement in rule.get("literal_rewrites", {}).items():
            output = output.replace(str(original), str(replacement))
    return output


def institutions_mentioned_in_answer(
    institutions: list[dict[str, Any]], question: str, answer_text: str
) -> list[dict[str, Any]]:
    """Do not turn a merely retrieved but irrelevant institution into a recommendation."""
    haystack = re.sub(r"\s+", "", f"{question}\n{answer_text}").casefold()
    return [
        institution
        for institution in institutions
        if re.sub(r"\s+", "", clean(institution.get("name"))).casefold() in haystack
    ]


def context_is_relevant(question: str, row: dict[str, Any]) -> bool:
    """Reject a topically incompatible chunk even when its dense score is high."""
    query = clean(question).casefold()
    evidence = f"{row.get('title', '')} {row.get('section_title', '')} {row.get('text', '')}".casefold()

    qualifiers = (
        (("금연", "흡연", "담배"), ("금연", "흡연", "담배")),
        (("어린이", "소아", "영아", "아동"), ("아이", "아기", "어린이", "소아", "영아", "아동", "자녀")),
        (("임신", "임산부"), ("임신", "임산부")),
        (("예방접종", "백신"), ("예방접종", "백신", "접종")),
        (("마약", "약물 오남", "중독"), ("마약", "오남용", "중독", "과다복용", "금단")),
        (("심폐소생술", "응급상식", "응급처치", "기도폐쇄"),
         ("응급", "119", "심폐소생술", "기도", "의식", "호흡", "출혈", "경련", "마비",
          "다쳤", "화상", "골절", "이물질", "물렸", "쏘였", "머리 부딪", "열", "발열")),
    )
    for evidence_terms, required_query_terms in qualifiers:
        if any(term in evidence for term in evidence_terms) and not any(term in query for term in required_query_terms):
            return False

    # A symptom word that happens to occur on a disease-specific page must not
    # be treated as evidence that the user has that disease.  This prevents a
    # generic headache/cough question from producing alarming HIV, syphilis or
    # tuberculosis explanations simply because dense retrieval found a shared
    # symptom token.
    restricted_domains = (
        (("성매개감염", "매독", "에이즈", "hiv"),
         ("성매개", "성병", "매독", "에이즈", "hiv")),
        (("결핵",), ("결핵",)),
        (("심뇌혈관질환",), ("심뇌혈관", "뇌졸중", "심근경색", "고혈압", "혈압")),
        (("뇌졸중",), ("뇌졸중", "마비", "말이 어눌", "시야 이상", "의식", "갑자기", "갑작스럽")),
        (("비만",), ("비만", "체중", "체질량", "bmi")),
    )
    for evidence_terms, required_query_terms in restricted_domains:
        if any(term in evidence for term in evidence_terms) and not any(term in query for term in required_query_terms):
            return False

    topic_groups = (
        (("약국",), ("약국",)),
        (("예방접종", "백신", "접종"), ("예방접종", "백신", "접종")),
        (("두통", "머리가", "머리 아"), ("두통", "머리가 아", "머리 통증")),
        (("메스꺼", "구토", "설사", "복통", "소화", "배가 아", "속이 아"),
         ("메스꺼", "구토", "설사", "복통", "소화불량", "배가 아", "속이 아", "위장관")),
        (("감기", "기침", "콧물", "코막힘", "코가 막", "코 막", "목이 아", "목이 따", "목 따"),
         ("감기", "기침", "콧물", "코막힘", "인후", "목이 아", "목 통증", "목 따")),
        (("우울", "불안", "정신건강", "마음", "심리"), ("우울", "불안", "정신건강", "마음", "심리", "상담")),
        (("치매", "기억력"), ("치매", "기억력")),
    )
    matched = [evidence_terms for query_terms, evidence_terms in topic_groups if any(term in query for term in query_terms)]
    symptom_intent = any(term in query for term in (
        "아파", "아프", "통증", "메스꺼", "어지", "열이", "발열", "기침", "콧물",
        "막혀", "막혔", "따갑", "설사", "구토", "뻐근", "부었", "붓", "떨", "가려",
        "발진", "상처", "불편",
    ))
    # Each evidence chunk may support one of several symptoms in a compound
    # question. Requiring one chunk to contain every symptom causes false
    # abstention; accepting short substrings such as "목" or "위장" causes
    # navigation words and "차상위장애인" to look clinical. The phrases above
    # are deliberately specific and at least one matched topic must be present.
    if not matched:
        return not symptom_intent
    return any(any(term in evidence for term in terms) for terms in matched)


class SafetyOnlyGenerator:
    """Carries generator metadata when deterministic safety rules bypass generation."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.model_name = "safety-rule-no-generation"

    def generate(self, question: str, contexts: list[dict[str, Any]]) -> str:  # pragma: no cover
        raise RuntimeError("Safety-only generator must not be used for normal generation")


def safety_contacts(category: str, institutions: list[dict[str, Any]]) -> list[dict[str, str]]:
    values: dict[str, list[dict[str, str]]] = {
        "emergency": [{"label": "119 전화", "phone": "119"}],
        "suicide": [
            {"label": "자살예방상담 109", "phone": "109"},
            {"label": "응급 119", "phone": "119"},
            {"label": "긴급신고 112", "phone": "112"},
        ],
        "addiction": [{"label": "응급 119", "phone": "119"}],
        "medical_high_risk": [],
    }
    contacts = list(values.get(category, []))
    if category == "addiction":
        for institution in institutions:
            if "중독" not in institution.get("name", ""):
                continue
            for phone in institution.get("phones", []):
                contacts.append({"label": phone.get("label", "전문기관 전화"), "phone": phone.get("value", "")})
            break
    return [row for row in contacts if row.get("phone")]


def escape_markdown(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def encode_card_metadata(metadata: dict[str, Any]) -> tuple[str, str]:
    encoded = base64.urlsafe_b64encode(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    wrapped = "\n".join(encoded[index:index + 120] for index in range(0, len(encoded), 120))
    return encoded, f"```wonju-health-meta\n{wrapped}\n```"


def card_institution_metadata(institution: dict[str, Any]) -> dict[str, Any]:
    """Project the full data record onto fields that the browser card renders."""
    keys = (
        "institution_id",
        "name",
        "address",
        "phones",
        "operation_hours",
        "current_status_label",
        "current_status_basis",
        "review_notice",
    )
    return {
        key: institution[key]
        for key in keys
        if institution.get(key) not in (None, "", [], {})
    }


def card_citation_metadata(citation: dict[str, Any]) -> dict[str, Any]:
    """Retain the resident-visible source title, URL, and required chunk ID."""
    keys = ("url", "document", "chunk_id")
    return {
        key: citation[key]
        for key in keys
        if citation.get(key) not in (None, "")
    }


def card_metadata_blocks(result: dict[str, Any], max_encoded_length: int = 3600) -> list[str]:
    """Keep every UI fact while staying below Open WebUI's virtualized DOM limit."""
    base = {
        "schema_version": "wonju-health-card-v1",
        "risk_category": result.get("risk_category", "none"),
        "safety_rule_applied": bool(result.get("safety_rule_applied")),
        "safety_contacts": result.get("safety_contacts", []),
        "audit_event_id": result.get("audit_event_id", ""),
    }
    institutions = [
        card_institution_metadata(row) for row in result.get("institutions", [])
    ]
    citations = [card_citation_metadata(row) for row in result.get("citations", [])]
    complete = {**base, "institutions": institutions, "citations": citations}
    encoded, block = encode_card_metadata(complete)
    if len(encoded) <= max_encoded_length:
        return [block]

    blocks: list[str] = []
    if base["safety_rule_applied"] or base["safety_contacts"]:
        _, safety_block = encode_card_metadata({**base, "institutions": [], "citations": []})
        blocks.append(safety_block)

    neutral = {
        "schema_version": base["schema_version"],
        "risk_category": "none",
        "safety_rule_applied": False,
        "safety_contacts": [],
        "audit_event_id": base["audit_event_id"],
    }
    for institution in institutions:
        institution_metadata = {**neutral, "institutions": [institution], "citations": []}
        institution_encoded, institution_block = encode_card_metadata(institution_metadata)
        if len(institution_encoded) > max_encoded_length:
            raise ValueError("A single institution card exceeds the Open WebUI metadata limit")
        blocks.append(institution_block)

    citation_group: list[dict[str, Any]] = []
    for citation in citations:
        candidate = citation_group + [citation]
        citation_metadata = {**neutral, "institutions": [], "citations": candidate}
        citation_encoded, citation_block = encode_card_metadata(citation_metadata)
        if citation_group and len(citation_encoded) > max_encoded_length:
            _, previous_block = encode_card_metadata(
                {**neutral, "institutions": [], "citations": citation_group}
            )
            blocks.append(previous_block)
            citation_group = [citation]
        else:
            citation_group = candidate
        single_encoded, _ = encode_card_metadata(
            {**neutral, "institutions": [], "citations": citation_group}
        )
        if len(single_encoded) > max_encoded_length:
            raise ValueError("A single source card exceeds the Open WebUI metadata limit")
    if citation_group:
        _, citation_block = encode_card_metadata(
            {**neutral, "institutions": [], "citations": citation_group}
        )
        blocks.append(citation_block)

    return blocks


def render_content(result: dict[str, Any]) -> str:
    answer_text = str(result.get("answer", "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    if result.get("response_kind") == "symptom_intake":
        return answer_text
    blocks = [answer_text]
    institutions = result.get("institutions", [])
    if institutions:
        blocks.append("### 기관 정보")
        for institution in institutions:
            fields = [f"- **{escape_markdown(institution['name'])}**"]
            if institution.get("current_status_label"):
                basis = f" ({institution['current_status_basis']})" if institution.get("current_status_basis") else ""
                fields.append(f"  - 현재 상태: {institution['current_status_label']}{basis}")
            if institution.get("address"):
                fields.append(f"  - 주소: {institution['address']}")
            for phone in institution.get("phones", []):
                fields.append(f"  - {phone['label']}: {phone['value']}")
            for hours in institution.get("operation_hours", []):
                fields.append(f"  - {hours['label']}: {hours['value']}")
            if institution.get("review_notice"):
                fields.append(f"  - 확인 안내: {institution['review_notice']}")
            blocks.append("\n".join(fields))

    blocks.append("### 출처")
    citations = result.get("citations", [])
    if citations:
        for citation in citations:
            title = escape_markdown(clean(citation.get("document")) or citation.get("doc_id", "문서"))
            url = citation.get("url", "")
            chunk_id = citation.get("chunk_id", "")
            if url:
                blocks.append(f"- [{title}]({url}) · `{chunk_id}`")
            else:
                blocks.append(f"- {title} · `{chunk_id}`")
    else:
        blocks.append("- 제공된 근거에서 확인할 수 없습니다.")

    blocks.extend(card_metadata_blocks(result))
    return "\n\n".join(block for block in blocks if block)


def completion_payload(content: str, completion_id: str) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": max(1, len(content) // 4),
            "total_tokens": max(1, len(content) // 4),
        },
    }


def sse_response(content: str, completion_id: str) -> StreamingResponse:
    async def events():
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
        # Open WebUI paints streamed deltas immediately. Sending a complete,
        # potentially large answer as one synchronous event can leave the
        # first conversation visually empty until its persisted copy is loaded
        # on refresh. Small progressive deltas also keep reverse proxies from
        # coalescing the whole response into one terminal frame.
        for index in range(0, len(content), 384):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "delta": {"content": content[index:index + 384]},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.005)
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def authorize(authorization: str | None) -> None:
    expected = os.getenv("P1_INTERNAL_API_KEY", "")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="invalid internal API credential")


def bearer_token(authorization: str | None) -> str:
    return authorization.removeprefix("Bearer ").strip() if authorization else ""


def webui_user(authorization: str | None, *, require_admin: bool = False) -> dict[str, Any]:
    token = bearer_token(authorization)
    try:
        user = decode_webui_token(token, os.getenv("WEBUI_SECRET_KEY", ""))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not user.get("role"):
        # Open WebUI browser JWTs intentionally contain only a user ID in
        # some versions. Resolve the current role through Open WebUI itself,
        # using that same signed token, instead of trusting a client header.
        try:
            response = requests.get(
                f"{os.getenv('OPEN_WEBUI_INTERNAL_URL', 'http://open-webui:8080').rstrip('/')}/api/v1/auths/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            response.raise_for_status()
            profile = response.json()
            if str(profile.get("id", "")) != str(user["id"]):
                raise PermissionError("Open WebUI user identity does not match")
            user = {**user, "role": profile.get("role", "user"), "email": profile.get("email", "")}
        except (requests.RequestException, ValueError, PermissionError) as exc:
            raise HTTPException(status_code=401, detail="Open WebUI user verification failed") from exc
    if require_admin and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="administrator access is required")
    return user


def forwarded_user(request: Request) -> tuple[str, str]:
    identity = (
        request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-open-webui-user-id")
        or request.headers.get("x-openwebui-user-email")
        or request.headers.get("x-open-webui-user-email")
        or "anonymous"
    )
    role = (
        request.headers.get("x-openwebui-user-role")
        or request.headers.get("x-open-webui-user-role")
        or "user"
    )
    return identity, role


def create_app(service: Any | None = None, audit_store: AuditStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if service is None and truthy(os.getenv("P1_PRELOAD_MODELS", "true")):
            try:
                await asyncio.to_thread(application.state.service.ensure_loaded, True)
            except Exception:
                # Liveness remains available; /ready reports the exact failure.
                pass
        yield

    application = FastAPI(title="Wonju Health P1 RAG API", version="1.0.0", lifespan=lifespan)
    application.state.service = service or WonjuRagService()
    application.state.audit = audit_store or AuditStore.from_env()

    @application.get("/health")
    async def health() -> dict[str, Any]:
        status = application.state.service.status()
        return {
            **status,
            "audit_storage": True,
            "audit_retention_days": application.state.audit.retention_days,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    @application.get("/ready")
    async def ready() -> JSONResponse:
        try:
            await asyncio.to_thread(application.state.service.ensure_loaded, True)
        except Exception:
            return JSONResponse(status_code=503, content=application.state.service.status())
        status = application.state.service.status()
        code = 200 if status.get("required_files_present") and status.get("generation_loaded") else 503
        return JSONResponse(status_code=code, content=status)

    @application.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        return {
            "object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "wonju-health-p1"}],
        }

    @application.post("/v1/chat/completions")
    async def chat(
        payload: ChatRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authorize(authorization)
        if payload.model != MODEL_ID:
            raise HTTPException(status_code=404, detail=f"model '{payload.model}' is not available")
        question, intake_result = prepare_symptom_intake(
            payload.messages,
            getattr(application.state.service, "safety", None),
            getattr(application.state.service, "intake", None),
        )
        if not question:
            raise HTTPException(status_code=400, detail="a non-empty user message is required")
        audit_event_id = f"audit_{uuid.uuid4().hex}"
        started = time.perf_counter()
        user_identity, user_role = forwarded_user(request)
        try:
            result = intake_result or await asyncio.to_thread(application.state.service.query, question)
        except Exception as exc:
            request_id = uuid.uuid4().hex[:12]
            application.state.audit.record(
                event_id=audit_event_id,
                user_identity=user_identity,
                user_role=user_role,
                question=question,
                status="failure",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_code=f"{type(exc).__name__}:{request_id}",
            )
            raise HTTPException(status_code=503, detail=f"P1 RAG unavailable ({request_id}): {type(exc).__name__}") from exc
        result["audit_event_id"] = audit_event_id
        application.state.audit.record(
            event_id=audit_event_id,
            user_identity=user_identity,
            user_role=user_role,
            question=question,
            status="success",
            risk_category=str(result.get("risk_category", "none")),
            response_kind=str(result.get("response_kind", "answer")),
            duration_ms=round((time.perf_counter() - started) * 1000),
            institution_count=len(result.get("institutions", [])),
            citation_count=len(result.get("citations", [])),
        )
        content = render_content(result)
        completion_id = f"chatcmpl-wonju-{uuid.uuid4().hex}"
        if payload.stream:
            return sse_response(content, completion_id)
        return completion_payload(content, completion_id)

    @application.post("/audit/events/{event_id}/feedback")
    async def feedback(
        event_id: str,
        payload: FeedbackRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        user = webui_user(authorization)
        try:
            updated = application.state.audit.set_feedback(
                event_id,
                actor_identity=str(user["id"]),
                actor_role=str(user.get("role", "user")),
                rating=payload.rating,
                comment=payload.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="audit event was not found")
        return {"updated": True, "event_id": event_id, "rating": payload.rating}

    @application.get("/audit/summary")
    async def audit_summary(authorization: str | None = Header(default=None)) -> dict[str, int]:
        webui_user(authorization, require_admin=True)
        return application.state.audit.summary()

    @application.get("/audit/events")
    async def audit_events(
        status: str = "",
        risk: str = "",
        rating: str = "",
        q: str = "",
        limit: int = 100,
        offset: int = 0,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        webui_user(authorization, require_admin=True)
        return application.state.audit.list_events(
            status=status,
            risk=risk,
            rating=rating,
            query=q,
            limit=limit,
            offset=offset,
        )

    @application.get("/audit/export.csv")
    async def audit_export(
        status: str = "",
        risk: str = "",
        rating: str = "",
        q: str = "",
        authorization: str | None = Header(default=None),
    ) -> Response:
        webui_user(authorization, require_admin=True)
        content = application.state.audit.export_csv(status=status, risk=risk, rating=rating, query=q)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=wonju-health-audit.csv"},
        )

    return application


app = create_app()
