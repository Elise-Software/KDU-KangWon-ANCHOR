from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
import jwt

import app as p1_app
from audit import AuditStore


class FakeService:
    def ensure_loaded(self, include_generator: bool = True) -> None:
        return None

    def status(self):
        return {
            "status": "ok",
            "model": "wonju-health-rag",
            "required_files_present": True,
            "missing_files": [],
            "retrieval_loaded": True,
            "generation_loaded": True,
            "last_error": "",
        }

    def query(self, question: str):
        risk = "suicide" if "죽고" in question else "none"
        return {
            "answer": "근거에 따라 안내합니다.",
            "risk_category": risk,
            "safety_rule_applied": risk != "none",
            "safety_contacts": (
                [{"label": "자살예방상담 109", "phone": "109"}, {"label": "응급 119", "phone": "119"}]
                if risk != "none" else []
            ),
            "citations": [{
                "url": "https://www.wonju.go.kr/health/contents.do?key=1623",
                "document": "공식 보건 안내",
                "doc_id": "p1doc:test",
                "chunk_id": "p1chunk:test",
            }],
            "institutions": [{
                "institution_id": "public:test",
                "name": "원주시보건소",
                "address": "강원특별자치도 원주시 원일로 139",
                "phones": [{"label": "대표전화", "value": "033-737-4011"}],
                "operation_hours": [{"label": "평일", "value": "09:00~18:00"}],
            }],
        }


class RecordingService(FakeService):
    def __init__(self):
        self.questions = []

    def query(self, question: str):
        self.questions.append(question)
        return super().query(question)


def metadata(content: str) -> dict:
    value = "".join(
        re.search(r"```wonju-health-meta\s+([\s\S]+?)\s+```", content).group(1).split()
    )
    value += "=" * ((4 - len(value) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(value).decode("utf-8"))


def test_large_card_metadata_is_losslessly_split_below_live_dom_limit():
    institutions = [
        {
            "institution_id": f"wonju:test-{index}",
            "name": f"원주시 테스트 의료기관 {index}",
            "address": "강원특별자치도 원주시 행구로 123번길 45, 시민건강복합센터 2층",
            "map_url": "https://map.kakao.com/link/search/" + ("a" * 300),
            "phones": [{"label": "대표전화", "value": f"033-737-40{index:02d}"}],
            "operation_hours": [{"label": "평일", "value": "09:00~18:00"}],
            "current_status_label": "공식 출처에서 운영 확인",
            "current_status_basis": "수집된 공식 출처",
            "review_notice": "방문 전 전화로 운영 여부를 확인해 주세요.",
        }
        for index in range(3)
    ]
    citations = [
        {
            "url": f"https://www.wonju.go.kr/health/official-source-{index}",
            "document": f"원주시 공식 보건의료 안내 문서 {index}",
            "doc_id": f"p1doc:test-{index}",
            "chunk_id": f"p1chunk:test-{index}",
        }
        for index in range(6)
    ]
    blocks = p1_app.card_metadata_blocks({
        "risk_category": "none",
        "safety_rule_applied": False,
        "safety_contacts": [],
        "institutions": institutions,
        "citations": citations,
    }, max_encoded_length=1800)
    assert len(blocks) > 1
    decoded = [metadata(block) for block in blocks]
    assert all(len("".join(re.search(r"```wonju-health-meta\s+([\s\S]+?)\s+```", block).group(1).split())) <= 1800 for block in blocks)
    assert [row for payload in decoded for row in payload["institutions"]] == [
        p1_app.card_institution_metadata(row) for row in institutions
    ]
    assert [row for payload in decoded for row in payload["citations"]] == [
        p1_app.card_citation_metadata(row) for row in citations
    ]


def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("P1_INTERNAL_API_KEY", "test-p1-key")
    return TestClient(p1_app.create_app(FakeService()))


def post_chat(test_client: TestClient, messages: list[dict]) -> str:
    response = test_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-p1-key"},
        json={"model": "wonju-health-rag", "messages": messages, "stream": False},
    )
    assert response.status_code == 200
    return response.json()["choices"][0]["message"]["content"]


def test_symptom_intake_asks_three_short_rounds_then_calls_rag(monkeypatch):
    monkeypatch.setenv("P1_INTERNAL_API_KEY", "test-p1-key")
    service = RecordingService()
    messages = [{"role": "user", "content": "머리가 깨질 듯 아프면 어디로 가야 해요?"}]
    with TestClient(p1_app.create_app(service)) as test_client:
        first = post_chat(test_client, messages)
        assert "증상 확인 1/3" in first
        assert "### 출처" not in first
        assert "wonju-health-meta" not in first
        assert service.questions == []

        messages += [{"role": "assistant", "content": first}, {"role": "user", "content": "어제부터 서서히 심해졌어요"}]
        second = post_chat(test_client, messages)
        assert "증상 확인 2/3" in second
        assert service.questions == []

        messages += [{
            "role": "assistant",
            "content": second,
        }, {
            "role": "user",
            "content": "의식이나 호흡 이상, 마비, 심한 출혈, 반복 구토, 고열은 없어요",
        }]
        third = post_chat(test_client, messages)
        assert "증상 확인 3/3" in third
        assert service.questions == []

        messages += [{"role": "assistant", "content": third}, {"role": "user", "content": "40대, 고혈압약 복용 중, 행구동"}]
        final = post_chat(test_client, messages)
        assert "증상 확인" not in final
        assert "wonju-health-meta" in final
        assert len(service.questions) == 1
        assert "머리가 깨질 듯" in service.questions[0]
        assert "어제부터 서서히" in service.questions[0]
        assert "행구동" in service.questions[0]
        assert "적절한 의료기관 찾기" in service.questions[0]


def test_new_symptom_after_completed_intake_starts_a_new_round():
    messages = [
        {"role": "user", "content": "머리가 아파요"},
        {"role": "assistant", "content": "### 증상 확인 1/3\n언제 시작됐나요?"},
        {"role": "user", "content": "어제부터요"},
        {"role": "assistant", "content": "최종 안내입니다."},
        {"role": "user", "content": "이번에는 배가 아파요"},
    ]
    question, result = p1_app.prepare_symptom_intake(messages)
    assert question == "이번에는 배가 아파요"
    assert result is not None
    assert "증상 확인 1/3" in result["answer"]


def test_headache_intake_plan_declares_retrievable_official_evidence():
    service = p1_app.WonjuRagService(p1_app.ROOT)
    rows = service._intake_evidence("처음 증상: 머리가 깨질 듯 아파요\n확인 답변: 서서히 심해졌어요")
    assert len(rows) == 3
    hosts = {re.sub(r"^https://([^/]+)/.*$", r"\1", row["url"]) for row in rows}
    assert hosts == {"health.kdca.go.kr", "www.nhs.uk", "www.nice.org.uk"}
    assert len({row["chunk_id"] for row in rows}) == 3


def test_model_list_exposes_only_curated_model(monkeypatch):
    with client(monkeypatch) as test_client:
        response = test_client.get("/v1/models", headers={"Authorization": "Bearer test-p1-key"})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == ["wonju-health-rag"]


def test_authentication_and_model_boundary(monkeypatch):
    with client(monkeypatch) as test_client:
        assert test_client.get("/v1/models").status_code == 401
        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-p1-key"},
            json={"model": "gemma-4-31b-nvfp4", "messages": [{"role": "user", "content": "질문"}]},
        )
    assert response.status_code == 404


def test_answer_contains_auditable_cards_and_plain_text_fallback(monkeypatch):
    with client(monkeypatch) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-p1-key"},
            json={
                "model": "wonju-health-rag",
                "messages": [{"role": "user", "content": "보건소 정보를 알려주세요"}],
                "temperature": 0.9,
            },
        )
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "### 출처" in content
    assert "원주시보건소" in content
    details = metadata(content)
    assert details["schema_version"] == "wonju-health-card-v1"
    assert details["citations"][0]["chunk_id"] == "p1chunk:test"
    assert details["institutions"][0]["phones"][0]["value"] == "033-737-4011"


def test_safety_metadata_has_immediate_contacts(monkeypatch):
    with client(monkeypatch) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-p1-key"},
            json={
                "model": "wonju-health-rag",
                "messages": [{"role": "user", "content": "죽고 싶고 자해할 것 같아요"}],
            },
        )
    details = metadata(response.json()["choices"][0]["message"]["content"])
    assert details["risk_category"] == "suicide"
    assert {row["phone"] for row in details["safety_contacts"]} >= {"109", "119"}


def test_streaming_is_openai_compatible(monkeypatch):
    with client(monkeypatch) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-p1-key"},
            json={
                "model": "wonju-health-rag",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "질문"}]}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert "data: [DONE]" in response.text
    assert "wonju-health-meta" in response.text
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    content_deltas = [
        event["choices"][0]["delta"]["content"]
        for event in events
        if "content" in event["choices"][0]["delta"]
    ]
    assert len(content_deltas) >= 2
    assert "wonju-health-meta" in "".join(content_deltas)
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def audit_client(monkeypatch, tmp_path: Path) -> tuple[TestClient, AuditStore]:
    monkeypatch.setenv("P1_INTERNAL_API_KEY", "test-p1-key")
    monkeypatch.setenv("WEBUI_SECRET_KEY", "test-webui-secret-at-least-32-bytes")
    store = AuditStore(tmp_path / "audit.sqlite3", hash_salt="test-salt", retention_days=30)
    return TestClient(p1_app.create_app(FakeService(), store)), store


def webui_token(user_id: str, role: str) -> str:
    return jwt.encode(
        {"id": user_id, "role": role},
        "test-webui-secret-at-least-32-bytes",
        algorithm="HS256",
    )


def test_runtime_audit_records_masks_filters_feedback_and_exports(monkeypatch, tmp_path):
    test_client, store = audit_client(monkeypatch, tmp_path)
    with test_client:
        response = test_client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer test-p1-key",
                "X-OpenWebUI-User-Id": "resident-1",
                "X-OpenWebUI-User-Role": "user",
            },
            json={
                "model": "wonju-health-rag",
                "messages": [{"role": "user", "content": "010-1234-5678 user@example.com 보건소 알려주세요"}],
            },
        )
        assert response.status_code == 200
        details = metadata(response.json()["choices"][0]["message"]["content"])
        event_id = details["audit_event_id"]
        assert event_id.startswith("audit_")

        resident = webui_token("resident-1", "user")
        feedback = test_client.post(
            f"/audit/events/{event_id}/feedback",
            headers={"Authorization": f"Bearer {resident}"},
            json={"rating": "helpful", "comment": "010-9876-5432 좋아요"},
        )
        assert feedback.status_code == 200

        admin = webui_token("admin-1", "admin")
        summary = test_client.get(
            "/audit/summary", headers={"Authorization": f"Bearer {admin}"}
        )
        assert summary.status_code == 200
        assert summary.json() == {
            "total": 1, "success": 1, "failure": 0,
            "helpful": 1, "unhelpful": 0, "high_risk": 0,
        }
        events = test_client.get(
            "/audit/events?status=success&rating=helpful",
            headers={"Authorization": f"Bearer {admin}"},
        ).json()
        assert events["total"] == 1
        assert events["rows"][0]["user_hash"] == store.user_hash("resident-1")
        assert "010-1234-5678" not in events["rows"][0]["question_text"]
        assert "user@example.com" not in events["rows"][0]["question_text"]
        assert "010-9876-5432" not in events["rows"][0]["feedback_comment"]

        exported = test_client.get(
            "/audit/export.csv?rating=helpful",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert exported.status_code == 200
        assert exported.content.startswith(b"\xef\xbb\xbf")
        assert event_id in exported.text


def test_runtime_audit_rejects_non_admin_reads_and_cross_user_feedback(monkeypatch, tmp_path):
    test_client, _ = audit_client(monkeypatch, tmp_path)
    with test_client:
        response = test_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-p1-key", "X-OpenWebUI-User-Id": "resident-1"},
            json={"model": "wonju-health-rag", "messages": [{"role": "user", "content": "보건소"}]},
        )
        event_id = metadata(response.json()["choices"][0]["message"]["content"])["audit_event_id"]
        other = webui_token("resident-2", "user")
        assert test_client.get(
            "/audit/events", headers={"Authorization": f"Bearer {other}"}
        ).status_code == 403
        assert test_client.post(
            f"/audit/events/{event_id}/feedback",
            headers={"Authorization": f"Bearer {other}"},
            json={"rating": "unhelpful"},
        ).status_code == 403


def test_authenticated_user_can_rate_an_anonymously_forwarded_event(monkeypatch, tmp_path):
    test_client, store = audit_client(monkeypatch, tmp_path)
    store.record(
        event_id="audit_anonymous",
        user_identity="anonymous",
        user_role="user",
        question="보건소를 알려주세요",
        status="success",
    )
    with test_client:
        resident = webui_token("resident-2", "user")
        response = test_client.post(
            "/audit/events/audit_anonymous/feedback",
            headers={"Authorization": f"Bearer {resident}"},
            json={"rating": "helpful"},
        )
        assert response.status_code == 200
        assert store.list_events(rating="helpful")["total"] == 1


def test_webui_role_is_resolved_server_side_when_browser_token_omits_it(monkeypatch, tmp_path):
    test_client, _ = audit_client(monkeypatch, tmp_path)

    class ProfileResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "admin-with-minimal-token", "role": "admin", "email": "admin@example.org"}

    monkeypatch.setattr(p1_app.requests, "get", lambda *args, **kwargs: ProfileResponse())
    minimal_token = jwt.encode(
        {"id": "admin-with-minimal-token"},
        "test-webui-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    with test_client:
        response = test_client.get(
            "/audit/summary", headers={"Authorization": f"Bearer {minimal_token}"}
        )
        assert response.status_code == 200


def test_existing_pharmacy_phone_conflict_policy_is_preserved():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    card = catalog._card(
        "wonju:13193124",
        {"institution_name": "아침약국", "link_method": "exact", "confidence": "1.0000"},
    )
    assert {row["value"] for row in card["phones"]} == {"033-761-2003", "033-763-3434"}
    assert "전화번호가 서로 다르게" in card["review_notice"]


def test_unmentioned_retrieval_institution_is_not_presented_as_recommendation():
    institutions = [
        {"name": "서원주건강생활지원센터"},
        {"name": "원주시보건소"},
    ]
    selected = p1_app.institutions_mentioned_in_answer(
        institutions,
        "원주시보건소 전화번호를 알려주세요",
        "원주시보건소 정보를 확인해 드릴게요.",
    )
    assert [row["name"] for row in selected] == ["원주시보건소"]


def test_location_followup_does_not_show_unrelated_institution():
    selected = p1_app.institutions_mentioned_in_answer(
        [{"name": "서원주건강생활지원센터"}],
        "가까운 병원을 찾아주세요",
        "현재 계신 읍면동을 알려주시면 주변 의료기관을 찾아드릴게요.",
    )
    assert selected == []


def test_short_location_followup_keeps_previous_search_intent():
    question = p1_app.extract_question([
        {"role": "user", "content": "가까운 병원 찾아주세요"},
        {"role": "assistant", "content": "현재 계신 읍면동을 알려주세요."},
        {"role": "user", "content": "단계동이에요"},
    ])
    assert "가까운 병원" in question
    assert "단계동" in question


def test_new_full_question_does_not_inherit_an_old_topic():
    question = p1_app.extract_question([
        {"role": "user", "content": "가까운 병원 찾아주세요"},
        {"role": "assistant", "content": "현재 계신 읍면동을 알려주세요."},
        {"role": "user", "content": "원주시보건소 전화번호를 알려주세요"},
    ])
    assert question == "원주시보건소 전화번호를 알려주세요"


def test_symptom_detail_followup_keeps_the_original_symptom():
    question = p1_app.extract_question([
        {"role": "user", "content": "머리가 아프고 속이 메스꺼워요"},
        {"role": "assistant", "content": "언제부터 시작됐고 열이 있나요?"},
        {"role": "user", "content": "어제부터고 열은 없어요"},
    ])
    assert "머리가 아프고" in question
    assert "어제부터" in question


def test_multiple_short_followups_keep_the_original_intent():
    question = p1_app.extract_question([
        {"role": "user", "content": "머리가 아파요"},
        {"role": "assistant", "content": "언제부터 시작됐나요?"},
        {"role": "user", "content": "사흘째예요"},
        {"role": "assistant", "content": "열도 있나요?"},
        {"role": "user", "content": "열은 없어요"},
    ])
    assert "머리가 아파요" in question
    assert "사흘째예요" in question
    assert "열은 없어요" in question


def test_natural_location_followup_keeps_pharmacy_intent():
    question = p1_app.extract_question([
        {"role": "user", "content": "오늘 이용 가능한 약국을 찾아주세요"},
        {"role": "assistant", "content": "현재 계신 읍면동을 알려주세요."},
        {"role": "user", "content": "단계동이요"},
    ])
    assert "약국" in question
    assert "단계동이요" in question


def test_location_after_five_step_symptom_answer_requests_nearby_care():
    question = p1_app.extract_question([
        {"role": "user", "content": "머리가 아프고 속이 메스꺼워요"},
        {"role": "assistant", "content": "5. 가까운 의료기관 찾기\n현재 계신 읍면동을 알려주세요."},
        {"role": "user", "content": "단계동이요"},
    ])
    assert "머리가 아프고" in question
    assert "단계동이요" in question
    assert "가까운 의료기관 찾아주세요" in question


def test_symptom_and_nearby_lookup_keep_five_steps_and_attach_local_cards():
    result = {
        "answer": (
            "### 1. 먼저 마음부터\n많이 불편하셨겠어요.\n\n"
            "### 2. 생각해볼 수 있는 원인\n단정할 수 없습니다.\n\n"
            "### 3. 지금 할 수 있는 대처\n상태를 살펴보세요.\n\n"
            "### 4. 상비의약품 안내\n약사에게 확인하세요.\n\n"
            "### 5. 가까운 의료기관 찾기\n읍면동을 알려주세요."
        ),
        "citations": [],
        "retrieved_chunk_ids": [],
    }
    structured = {
        "answer": "단계동 주소가 확인되는 의료기관을 안내합니다.",
        "institutions": [{"institution_id": "wonju:test", "name": "단계동의원"}],
        "citations": [{
            "url": "https://www.wonju.go.kr/health/example",
            "document": "원주시 관내의료기관 공식 목록",
            "doc_id": "structured:master",
            "chunk_id": "institution:wonju:test",
        }],
    }
    merged = p1_app.merge_structured_symptom_result(result, structured)
    assert all(f"### {number}." in merged["answer"] for number in range(1, 6))
    assert "단계동 주소가 확인되는" in merged["answer"]
    assert "읍면동을 알려주세요" not in merged["answer"]
    assert merged["institutions"][0]["name"] == "단계동의원"
    assert merged["citations"][0]["chunk_id"] == "institution:wonju:test"


def test_intake_consistency_rules_preserve_confirmed_gradual_onset():
    config = {
        "response_consistency_rules": [{
            "when_question_contains_any": ["서서히"],
            "literal_rewrites": {"갑작스럽게 머리가 많이 아프셔서": "머리가 많이 아프셔서"},
        }]
    }
    answer = "갑작스럽게 머리가 많이 아프셔서 힘드셨겠어요. 갑자기 심해지면 119에 연락하세요."
    result = p1_app.apply_response_consistency(answer, "확인 답변: 서서히 심해졌어요", config)
    assert result.startswith("머리가 많이 아프셔서")
    assert "갑자기 심해지면 119" in result


def test_context_relevance_rejects_condition_mismatch_and_keeps_matching_topic():
    smoking = {"title": "금연클리닉", "section_title": "금단 증상", "text": "금연 중 두통과 소화불량이 나타날 수 있습니다."}
    cpr = {"title": "심폐소생술 - 응급상식", "section_title": "인공호흡", "text": "기도를 열고 인공호흡을 시행합니다."}
    mental = {"title": "정신건강복지센터", "section_title": "상담", "text": "우울과 불안에 대한 정신건강 상담을 제공합니다."}
    syphilis = {"title": "성매개감염병검사", "section_title": "임상적 특징", "text": "매독의 증상으로 열과 두통이 나타날 수 있습니다."}
    tuberculosis = {"title": "결핵검사", "section_title": "검진 대상", "text": "2주 이상 기침이 있는 사람은 결핵검진 대상입니다."}
    welfare = {"title": "장애인 등록절차", "section_title": "지원", "text": "차상위장애인 등록 대상과 제출 목록을 안내합니다."}
    assert not p1_app.context_is_relevant("머리가 아프고 메스꺼워요", smoking)
    assert not p1_app.context_is_relevant("목이 따갑고 코가 막혔어요", cpr)
    assert not p1_app.context_is_relevant("열이 나고 머리가 아파요", syphilis)
    assert not p1_app.context_is_relevant("기침이 나요", tuberculosis)
    assert not p1_app.context_is_relevant("배가 아프고 설사해요", welfare)
    assert not p1_app.context_is_relevant("목이 따갑고 코가 막혔어요", welfare)
    assert p1_app.context_is_relevant("결핵 검사는 어디서 받아요?", tuberculosis)
    assert p1_app.context_is_relevant("우울한 마음을 상담하고 싶어요", mental)


def test_exact_public_health_lookup_uses_current_verified_profile_value():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("원주시보건소 주소와 대표전화 알려주세요")
    assert result is not None
    assert [row["value"] for row in result["institutions"][0]["phones"]] == ["033-737-4011"]
    assert result["citations"][0]["url"].startswith("https://www.wonju.go.kr/health/")


def test_institution_service_questions_continue_to_document_rag():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    for question in (
        "원주시보건소 예방접종 준비물을 알려주세요",
        "원주시정신건강복지센터 상담 대상과 이용 방법을 알려주세요",
        "원주시치매안심센터 프로그램 신청 방법을 알려주세요",
    ):
        assert catalog.structured_query(question) is None


def test_confirmed_facility_status_is_exposed_with_its_evidence_basis():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("별자리 운영 중인가요?")
    assert result is not None
    card = result["institutions"][0]
    assert card["current_status"] == "operating_confirmed"
    assert card["current_status_label"] == "운영 중으로 확인"
    assert "사용자 확인" in card["current_status_basis"]
    assert "운영 중" in result["answer"]
    assert "P0 검토 결정" in result["citations"][0]["document"]


def test_pharmacy_clarification_never_cites_vaccination_or_invents_a_pharmacy():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("오늘 이용할 수 있는 원주시 약국을 알려주세요")
    assert result is not None
    assert result["institutions"] == []
    assert "읍면동" in result["answer"]
    assert result["citations"]
    assert all("약국" in row["document"] for row in result["citations"])
    assert all("1648" not in row["url"] for row in result["citations"])


def test_location_pharmacy_lookup_only_returns_that_area_with_today_schedule():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("단계동에서 오늘 이용할 수 있는 약국과 전화번호 알려주세요")
    assert result is not None
    assert result["institutions"]
    assert all("단계동" in row["address"] for row in result["institutions"])
    assert all(row["operation_hours"] for row in result["institutions"])
    requested_day = catalog._requested_day_type("오늘")
    requested_label = {"weekday": "평일", "saturday": "토요일", "sunday": "일요일"}[requested_day]
    assert all(
        requested_label in hours["label"]
        for row in result["institutions"]
        for hours in row["operation_hours"]
    )


def test_location_pharmacy_without_day_does_not_assume_weekday():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("단계동 약국과 전화번호를 알려주세요")
    assert result is not None
    assert result["institutions"]
    assert "오늘" not in result["answer"]
    assert "등록된 운영시간" in result["answer"]
    labels = {
        hours["label"]
        for row in result["institutions"]
        for hours in row["operation_hours"]
    }
    assert any("평일" in label for label in labels)
    assert any("토요일" in label or "일요일" in label for label in labels)


def test_haenggu_combined_lookup_returns_general_medical_and_pharmacy_map_cards():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    question = p1_app.extract_question([
        {"role": "user", "content": "제가 있는 동네에서 이용할 수 있는 병원이나 약국을 찾아주세요."},
        {"role": "assistant", "content": "현재 계신 읍면동을 알려주세요."},
        {"role": "user", "content": "행구동이에요"},
    ])
    result = catalog.structured_query(question)
    assert result is not None
    assert result["institutions"]
    assert 2 <= len(result["institutions"]) <= 3
    assert any("약국" in row["name"] for row in result["institutions"])
    assert any("약국" not in row["name"] for row in result["institutions"])
    assert all("행구동" in row["address"] for row in result["institutions"])
    assert all(row["map_url"].startswith("https://map.kakao.com/link/search/") for row in result["institutions"])
    assert len({(row["name"], row["address"], tuple(phone["value"] for phone in row["phones"])) for row in result["institutions"]}) == len(result["institutions"])
    assert result["citations"][0]["url"].endswith("key=1787")


def test_haenggu_pharmacy_falls_back_to_general_master_without_inventing_hours():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("행구동 약국을 찾아주세요")
    assert result is not None
    assert result["institutions"]
    assert len(result["institutions"]) <= 3
    assert all("행구동" in row["address"] for row in result["institutions"])
    assert all(not row["operation_hours"] for row in result["institutions"])
    assert "운영시간은 확인되지 않아" in result["answer"]
    assert result["citations"][0]["document"] == "원주시 관내의료기관 공식 목록"


def test_multiturn_nearby_hospital_lookup_uses_supplied_area_only():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    question = p1_app.extract_question([
        {"role": "user", "content": "가까운 병원 찾아주세요"},
        {"role": "assistant", "content": "현재 계신 읍면동을 알려주세요."},
        {"role": "user", "content": "단계동이에요"},
    ])
    result = catalog.structured_query(question)
    assert result is not None
    assert result["institutions"]
    assert len(result["institutions"]) <= 3
    assert all("단계동" in row["address"] for row in result["institutions"])
    assert result["citations"][0]["url"].endswith("key=1787")


def test_common_public_health_aliases_resolve_without_city_prefix():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    for question, expected in (
        ("보건소 전화번호", "원주시보건소"),
        ("치매안심센터 전화번호", "원주시치매안심센터"),
        ("자살예방센터 전화번호", "원주시자살예방센터"),
    ):
        result = catalog.structured_query(question)
        assert result is not None
        assert result["institutions"][0]["name"] == expected


def test_exact_pharmacy_day_query_only_shows_requested_day_and_never_dash():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("아침약국 일요일 운영시간과 전화번호를 알려주세요")
    assert result is not None
    hours = result["institutions"][0]["operation_hours"]
    assert hours
    assert all("일요일" in row["label"] for row in hours)
    assert all(row["value"] not in {"", "-", "not_provided"} for row in hours)


def test_pharmacy_source_conflict_keeps_not_provided_and_parsed_values():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    result = catalog.structured_query("구곡시장약국 토요일 출처별 운영시간 알려주세요")
    assert result is not None
    hours = result["institutions"][0]["operation_hours"]
    assert {row["value"] for row in hours} == {"미제공", "08:30~22:00"}
    assert any("심야약국" in row["label"] for row in hours)
    assert any("연중무휴약국" in row["label"] for row in hours)


def test_tomorrow_and_weekend_pharmacy_periods_are_not_treated_as_weekdays():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    tomorrow = catalog._requested_day_type("내일 운영시간")
    assert tomorrow in {"weekday", "saturday", "sunday"}
    weekend = catalog.structured_query("아침약국 주말 운영시간 알려주세요")
    labels = {row["label"] for row in weekend["institutions"][0]["operation_hours"]}
    assert labels
    assert all("평일" not in label for label in labels)
    assert any("토요일" in label for label in labels)
    assert any("일요일" in label for label in labels)


def test_location_alias_and_natural_hospital_wording_are_resolved():
    catalog = p1_app.InstitutionCatalog(p1_app.ROOT)
    assert catalog._location_in_question("문막에서 약국 찾아주세요") == "문막읍"
    assert catalog._location_in_question("흥업 근처 병원 어디 있어요?") == "흥업면"
    result = catalog.structured_query("단계동에 병원 어디 있어요?")
    assert result is not None
    assert result["institutions"]
    assert all("단계동" in row["address"] for row in result["institutions"])


def test_safety_response_does_not_depend_on_embedding_or_reranker(monkeypatch):
    service = p1_app.WonjuRagService(p1_app.ROOT)
    monkeypatch.setattr(service, "ensure_loaded", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    result = service.query("죽고 싶고 지금 자해할 것 같아요")
    assert result["risk_category"] == "suicide"
    assert result["safety_rule_applied"]
    assert {row["phone"] for row in result["safety_contacts"]} >= {"109", "119"}


def test_multiline_five_step_answer_keeps_markdown_structure():
    result = {
        "answer": "### 1. 먼저 마음부터\n걱정되셨겠어요.\n\n### 2. 생각해볼 수 있는 원인\n단정할 수 없습니다.",
        "risk_category": "none",
        "safety_rule_applied": False,
        "safety_contacts": [],
        "institutions": [],
        "citations": [],
    }
    content = p1_app.render_content(result)
    assert "### 1. 먼저 마음부터\n걱정되셨겠어요." in content
    assert "\n\n### 2. 생각해볼 수 있는 원인\n" in content
