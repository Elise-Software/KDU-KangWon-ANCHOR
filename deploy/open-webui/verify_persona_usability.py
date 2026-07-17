"""Live multi-turn usability checks for three Wonju Health user perspectives."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any

from playwright.sync_api import Page, sync_playwright

from verify_live_browser import (
    assert_min_touch_targets,
    assert_no_horizontal_overflow,
    default_browser,
    set_message_scroll,
    sign_in_browser,
    submit_followup,
    submit_question,
    take_screenshot,
    visible_raw_metadata_count,
    wait_for_evidence_state,
)
from verify_live_stack import create_temporary_user, delete_user, sign_in


FINAL_HEADINGS = (
    "1. 먼저 마음부터",
    "2. 생각해볼 수 있는 원인",
    "3. 지금 할 수 있는 대처",
    "4. 상비의약품 안내",
    "5. 가까운 의료기관 찾기",
)

SYMPTOM_PERSONAS = (
    {
        "id": "older_adult",
        "label": "노인 사용자",
        "question": "75세인데 머리가 깨질 듯 아프고 어디로 가야 할까요?",
        "answers": (
            "오늘 아침부터 서서히 심해졌고 갑자기 최고로 아파진 것은 아니에요.",
            "한쪽 힘 빠짐이나 저림, 말 어눌함, 시야 이상, 의식 저하, 고열, 목 뻣뻣함, 반복 구토, 머리 외상은 없어요.",
            "75세이고 고혈압약을 복용 중이며 현재 행구동이에요.",
        ),
        "facts": ("75세", "행구동"),
    },
    {
        "id": "guardian_child",
        "label": "유아 보호자",
        "question": "보호자인데 6살 아이가 열이 나고 기침을 해요. 어디로 가야 하나요?",
        "answers": (
            "어젯밤부터 서서히 시작됐고 체온은 38.2도예요. 아이는 깨어 있고 물은 마셔요.",
            "의식이나 호흡 이상, 마비, 심한 출혈, 반복 구토, 최근 외상은 없어요.",
            "6살이고 중요한 질환이나 복용약은 없으며 현재 단구동이에요.",
        ),
        "facts": ("6살", "단구동"),
    },
)


def last_assistant_text(page: Page) -> str:
    messages = page.locator(".wonju-health-assistant-message")
    if not messages.count():
        raise AssertionError("assistant response was not rendered")
    return messages.last.inner_text().strip()


def wait_for_heading(page: Page, heading: str, timeout: int = 90_000) -> None:
    page.locator(".wonju-health-assistant-message").last.get_by_role(
        "heading", name=heading, exact=True
    ).wait_for(state="visible", timeout=timeout)
    page.wait_for_timeout(300)


def assert_readable_response(page: Page, *, label: str) -> dict[str, float]:
    assistant = page.locator(".wonju-health-assistant-message").last
    metrics = assistant.evaluate(
        """node => {
          const style = getComputedStyle(node);
          return {
            fontSize: parseFloat(style.fontSize),
            lineHeight: parseFloat(style.lineHeight),
          };
        }"""
    )
    if metrics["fontSize"] < 16:
        raise AssertionError(f"{label} response font is too small: {metrics}")
    if metrics["lineHeight"] < metrics["fontSize"] * 1.35:
        raise AssertionError(f"{label} response line height is too tight: {metrics}")
    return metrics


def assert_clean_korean(text: str, *, label: str) -> None:
    if "�" in text or re.search(r"\?{3,}", text):
        raise AssertionError(f"{label} contains broken Korean text")
    if len(text) < 80:
        raise AssertionError(f"{label} is too short to be actionable: {text!r}")


def run_symptom_persona(
    page: Page,
    base_url: str,
    persona: dict[str, Any],
    screenshot_dir: Path,
) -> dict[str, Any]:
    submit_question(page, base_url, persona["question"])
    stage_texts: list[str] = []
    for stage, answer in enumerate(persona["answers"], start=1):
        wait_for_heading(page, f"증상 확인 {stage}/3", timeout=60_000)
        stage_text = last_assistant_text(page)
        assert_clean_korean(stage_text, label=f"{persona['label']} stage {stage}")
        stage_texts.append(stage_text)
        submit_followup(page, answer)

    wait_for_heading(page, FINAL_HEADINGS[-1], timeout=240_000)
    wait_for_evidence_state(page)
    try:
        page.locator(".wonju-health-institution-card").first.wait_for(
            state="visible", timeout=30_000
        )
    except Exception as error:
        take_screenshot(
            page, screenshot_dir, f"persona_{persona['id']}_missing_institution.png"
        )
        diagnostic = page.evaluate(
            """() => ({
              sourceCards: document.querySelectorAll('.wonju-health-source-card').length,
              renderedHosts: [...document.querySelectorAll('.wonju-health-rendered-cards')].map(node => ({
                fingerprint: node.dataset.wonjuMetadata || '',
                text: (node.innerText || '').slice(0, 300),
              })),
              codeLabels: [...document.querySelectorAll('#messages-container span, #messages-container div')]
                .filter(node => (node.textContent || '').trim() === 'wonju-health-meta')
                .map(label => {
                  const chain = [];
                  let node = label;
                  for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                    chain.push({tag: node.tagName, classes: node.className || '', length: (node.textContent || '').length});
                  }
                  return chain;
                }),
              metadataShells: [...document.querySelectorAll('[data-wonju-metadata-shell]')]
                .map(node => ({length: (node.textContent || '').length, text: (node.textContent || '').slice(0, 80)})),
            })"""
        )
        raise AssertionError(
            f"{persona['label']} institution cards did not render: {json.dumps(diagnostic, ensure_ascii=False)}"
        ) from error
    final_text = last_assistant_text(page)
    assert_clean_korean(final_text, label=f"{persona['label']} final response")
    missing = [heading for heading in FINAL_HEADINGS if heading not in final_text]
    if missing:
        raise AssertionError(
            f"{persona['label']} is missing final sections: {missing}; "
            f"visible final text={final_text[:4000]!r}"
        )
    if visible_raw_metadata_count(page):
        raise AssertionError(f"{persona['label']} exposes raw response metadata")
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:mg|mL)\b", final_text, re.IGNORECASE):
        raise AssertionError(f"{persona['label']} received an unverified exact medicine dose")
    if "119" not in final_text:
        raise AssertionError(f"{persona['label']} final response omits emergency escalation")

    institution_cards = page.locator(".wonju-health-institution-card").count()
    source_cards = page.locator(".wonju-health-source-card").count()
    evidence_empty = page.locator(".wonju-health-evidence-empty").count()
    if institution_cards < 1:
        raise AssertionError(f"{persona['label']} final response has no nearby institution card")
    if source_cards < 1 and evidence_empty < 1:
        raise AssertionError(f"{persona['label']} final response has no source state")

    desktop_metrics = assert_readable_response(page, label=persona["label"])
    assert_no_horizontal_overflow(page, label=f"{persona['label']} desktop flow")
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, f"persona_{persona['id']}_desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    assert_no_horizontal_overflow(page, label=f"{persona['label']} mobile flow")
    assert_min_touch_targets(
        page,
        (
            ".wonju-health-header-menu",
            ".wonju-health-call-button",
            ".wonju-health-map-button",
            ".wonju-health-message-action",
        ),
        label=f"{persona['label']} mobile actions",
    )
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, f"persona_{persona['id']}_mobile.png")
    page.set_viewport_size({"width": 1440, "height": 1000})

    return {
        "label": persona["label"],
        "turn_count": 4,
        "intake_markers": [f"증상 확인 {stage}/3" for stage in range(1, 4)],
        "final_headings": list(FINAL_HEADINGS),
        "institution_cards": institution_cards,
        "source_cards": source_cards,
        "evidence_empty_states": evidence_empty,
        "font_metrics": desktop_metrics,
        "raw_metadata_visible": 0,
        "exact_dose_detected": False,
        "passed": True,
    }


def run_general_persona(page: Page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    submit_question(page, base_url, "제가 있는 동네에서 이용할 수 있는 병원이나 약국을 찾아주세요.")
    page.get_by_text(re.compile("읍면동")).last.wait_for(state="visible", timeout=60_000)
    first_text = last_assistant_text(page)
    assert_clean_korean(first_text, label="일반 사용자 위치 질문")
    if "읍면동" not in first_text:
        raise AssertionError("general flow did not ask for a usable location")

    submit_followup(page, "행구동이에요.")
    page.locator(".wonju-health-institution-card").first.wait_for(
        state="visible", timeout=90_000
    )
    page.wait_for_timeout(500)
    final_text = last_assistant_text(page)
    assert_clean_korean(final_text, label="일반 사용자 기관 결과")
    institution_cards = page.locator(".wonju-health-institution-card").count()
    source_cards = page.locator(".wonju-health-source-card").count()
    map_actions = page.locator(".wonju-health-map-button").count()
    call_actions = page.locator(".wonju-health-routine-call").count()
    if institution_cards < 2 or source_cards < 1:
        raise AssertionError(
            f"general nearby flow is incomplete: institutions={institution_cards}, sources={source_cards}"
        )
    if map_actions < institution_cards:
        raise AssertionError("not every nearby result has a map action")
    if "실제 거리" not in final_text or "방문 전에" not in final_text:
        raise AssertionError("general nearby flow does not explain distance and live-status limits")
    if visible_raw_metadata_count(page):
        raise AssertionError("general nearby flow exposes raw response metadata")

    desktop_metrics = assert_readable_response(page, label="일반 사용자")
    assert_no_horizontal_overflow(page, label="general desktop flow")
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, "persona_general_desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    assert_no_horizontal_overflow(page, label="general mobile flow")
    assert_min_touch_targets(
        page,
        (
            ".wonju-health-header-menu",
            ".wonju-health-call-button",
            ".wonju-health-map-button",
            ".wonju-health-message-action",
        ),
        label="general mobile actions",
    )
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, "persona_general_mobile.png")
    page.set_viewport_size({"width": 1440, "height": 1000})
    return {
        "label": "일반 사용자",
        "turn_count": 2,
        "location_prompted": True,
        "institution_cards": institution_cards,
        "source_cards": source_cards,
        "map_actions": map_actions,
        "call_actions": call_actions,
        "font_metrics": desktop_metrics,
        "raw_metadata_visible": 0,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost")
    parser.add_argument("--user-email")
    parser.add_argument("--user-password", default=os.getenv("WONJU_HEALTH_USER_PASSWORD"))
    parser.add_argument("--create-temporary-user", action="store_true")
    parser.add_argument("--admin-email")
    parser.add_argument("--admin-password", default=os.getenv("WONJU_HEALTH_ADMIN_PASSWORD"))
    parser.add_argument("--browser-executable", default=default_browser())
    parser.add_argument("--screenshot-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    if args.create_temporary_user and not args.admin_email:
        parser.error("--create-temporary-user requires --admin-email")
    if not args.create_temporary_user and not args.user_email:
        parser.error("--user-email is required unless --create-temporary-user is used")
    if args.create_temporary_user and not args.admin_password:
        args.admin_password = getpass.getpass("Open WebUI admin password: ")
    if not args.create_temporary_user and not args.user_password:
        args.user_password = getpass.getpass("Open WebUI user password: ")
    if not args.browser_executable:
        parser.error("no supported Edge or Chrome executable was found")

    admin_token: str | None = None
    temporary_user: dict[str, str] | None = None
    if args.create_temporary_user:
        admin_token = sign_in(base_url, args.admin_email, args.admin_password)
        user_email = args.user_email or f"wonju-persona-{secrets.token_hex(6)}@wonju.local"
        temporary_user = create_temporary_user(base_url, admin_token, user_email)
        user_password = temporary_user["password"]
    else:
        user_email = args.user_email
        user_password = args.user_password

    results: list[dict[str, Any]] = []
    try:
        with sync_playwright() as manager:
            browser = manager.chromium.launch(executable_path=args.browser_executable, headless=True)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                login_page = context.new_page()
                sign_in_browser(
                    login_page, base_url, user_email, user_password, args.screenshot_dir
                )
                login_page.close()
                for persona in SYMPTOM_PERSONAS:
                    page = context.new_page()
                    try:
                        results.append(run_symptom_persona(
                            page, base_url, persona, args.screenshot_dir
                        ))
                    finally:
                        page.close()
                page = context.new_page()
                try:
                    results.append(run_general_persona(page, base_url, args.screenshot_dir))
                finally:
                    page.close()
            finally:
                browser.close()
    finally:
        if temporary_user and admin_token:
            delete_user(base_url, admin_token, temporary_user["id"])

    report = {
        "base_url": base_url,
        "personas": results,
        "temporary_user_removed": temporary_user is not None,
        "all_checks_passed": all(result.get("passed") for result in results) and len(results) == 3,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"persona usability verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
