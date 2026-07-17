"""Verify the rendered Wonju Health experience in a real browser.

This complements ``verify_live_stack.py``: the API verifier checks access
control and payload metadata, while this script checks that Open WebUI's
overlay actually turns that metadata into accessible cards.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from playwright.sync_api import Page, sync_playwright

from verify_live_stack import create_temporary_user, delete_user, sign_in


RESPONSIVE_WIDTHS = (320, 360, 390, 768, 1024, 1440)


def assert_no_horizontal_overflow(
    page: Page,
    *,
    label: str,
    widths: tuple[int, ...] = RESPONSIVE_WIDTHS,
) -> None:
    """Exercise supported breakpoints and report useful layout diagnostics."""
    original = page.viewport_size or {"width": 1440, "height": 1000}
    try:
        for width in widths:
            page.set_viewport_size({"width": width, "height": 844 if width < 768 else 1000})
            # Open WebUI ships a 200ms width transition on the chat shell.
            # Measure after it settles so an in-flight transform is not
            # mistaken for persistent clipping.
            page.wait_for_timeout(300)
            geometry = page.evaluate(
                """() => {
                  const viewport = document.documentElement.clientWidth;
                  const offenders = [...document.querySelectorAll('body *')]
                    .map(node => {
                      const rect = node.getBoundingClientRect();
                      const style = getComputedStyle(node);
                       return {
                         node: `${node.tagName.toLowerCase()}#${node.id}.${node.className || ''}`,
                         custom: node.id.startsWith('wonju-health-')
                           || [...node.classList].some(value => value.startsWith('wonju-health-')),
                        left: Math.round(rect.left * 10) / 10,
                        right: Math.round(rect.right * 10) / 10,
                        width: Math.round(rect.width * 10) / 10,
                        display: style.display,
                        visibility: style.visibility,
                      };
                    })
                    .filter(item => item.custom && item.display !== 'none' && item.visibility !== 'hidden'
                      && (item.left < -1 || item.right > viewport + 1))
                    .slice(0, 6);
                  return {
                    viewport,
                    documentWidth: document.documentElement.scrollWidth,
                    bodyWidth: document.body.scrollWidth,
                    offenders,
                  };
                }"""
            )
            widest = max(geometry["documentWidth"], geometry["bodyWidth"])
            if widest > geometry["viewport"] + 1:
                raise AssertionError(
                    f"{label} horizontally overflows at {width}px: {geometry}"
                )
            if geometry["offenders"]:
                raise AssertionError(
                    f"{label} has clipped custom components at {width}px: {geometry}"
                )
    finally:
        page.set_viewport_size(original)
        page.wait_for_timeout(300)


def assert_layout_hidden(
    page: Page,
    selector: str,
    *,
    label: str,
    required: bool = True,
) -> int:
    locator = page.locator(selector)
    count = locator.count()
    if not count:
        if required:
            raise AssertionError(f"{label} was not marked in the DOM")
        return 0
    states = locator.evaluate_all(
        """nodes => nodes.map(node => {
          const style = getComputedStyle(node);
          return {display: style.display, visibility: style.visibility};
        })"""
    )
    visible = [
        state
        for state in states
        if state["display"] != "none" and state["visibility"] not in {"hidden", "collapse"}
    ]
    if visible:
        raise AssertionError(f"{label} is still layout-visible: {visible}")
    return count


def assert_boxes_do_not_overlap(
    page: Page,
    first_selector: str,
    second_selector: str,
    *,
    label: str,
    tolerance: float = 1.0,
) -> None:
    first = page.locator(first_selector).first
    second = page.locator(second_selector).first
    if not first.is_visible() or not second.is_visible():
        raise AssertionError(
            f"{label} cannot be checked because a component is not visible"
        )
    first_box = first.bounding_box()
    second_box = second.bounding_box()
    if not first_box or not second_box:
        raise AssertionError(f"{label} cannot be checked because a bounding box is unavailable")
    overlap_width = min(
        first_box["x"] + first_box["width"], second_box["x"] + second_box["width"]
    ) - max(first_box["x"], second_box["x"])
    overlap_height = min(
        first_box["y"] + first_box["height"], second_box["y"] + second_box["height"]
    ) - max(first_box["y"], second_box["y"])
    if overlap_width > tolerance and overlap_height > tolerance:
        raise AssertionError(
            f"{label} overlaps: {first_selector}={first_box}, {second_selector}={second_box}"
        )


def default_browser() -> str | None:
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    )
    return next((str(path) for path in candidates if path.exists()), None)


def chat_input(page: Page):
    selectors = (
        "#chat-input",
        "textarea#chat-input",
        "textarea[aria-label]",
        "textarea",
        "[contenteditable='true'][role='textbox']",
        "[contenteditable='true']",
        "[role='textbox']",
    )
    page.wait_for_timeout(1_000)
    for _ in range(30):
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(locator.count() - 1, -1, -1):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        page.wait_for_timeout(1_000)
    diagnostics = {
        "url": page.url,
        "body": page.locator("body").inner_text()[:4_000],
        "buttons": [value.strip() for value in page.locator("button").all_inner_texts() if value.strip()],
    }
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2), file=sys.stderr)
    raise AssertionError("visible chat input was not found")


def select_only_model_if_needed(page: Page) -> None:
    """Choose wonju-health-rag when the UI has not selected its only model."""
    selected = page.locator("text=wonju-health-rag")
    if any(selected.nth(index).is_visible() for index in range(selected.count())):
        return
    for label in ("모델을 선택하세요", "모델 선택", "Select a model"):
        trigger = page.get_by_text(label, exact=False)
        if not trigger.count():
            continue
        visible_trigger = next(
            (trigger.nth(index) for index in range(trigger.count()) if trigger.nth(index).is_visible()),
            None,
        )
        if visible_trigger is None:
            continue
        visible_trigger.click()
        option = page.get_by_text("wonju-health-rag", exact=True)
        option.first.wait_for(state="visible", timeout=15_000)
        visible_option = next(
            (option.nth(index) for index in range(option.count()) if option.nth(index).is_visible()),
            None,
        )
        if visible_option is None:
            raise AssertionError("wonju-health-rag model option is not visible")
        visible_option.click()
        return
    # The curated single-model layout may hide the selector entirely and use
    # DEFAULT_MODELS. In that state submitting the chat input is the intended
    # path; the response metadata check below still verifies the served model.
    return


def sign_in_browser(
    page: Page,
    base_url: str,
    email: str,
    password: str,
    screenshot_dir: Path | None = None,
) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1_000)
    email_input = page.locator("input[type='email']")
    if email_input.count() and email_input.is_visible():
        page.locator("#wonju-health-auth-story").wait_for(state="visible", timeout=15_000)
        if page.locator("#auth-container").get_by_text("Open WebUI", exact=False).filter(visible=True).count():
            raise AssertionError("stock Open WebUI login copy is still visible")
        assert_layout_hidden(page, "#logo", label="stock authentication logo", required=False)
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-auth-story",
            ".wonju-health-auth-card",
            label="desktop authentication story and card",
        )
        assert_no_horizontal_overflow(page, label="authentication page")
        password_shell = page.locator(".wonju-health-password-shell")
        password_toggle = page.locator(".wonju-health-password-toggle")
        if password_shell.count() != 1 or password_toggle.count() != 1:
            raise AssertionError("password input and visibility control are not grouped as one field")
        password_geometry = page.evaluate(
            """() => {
              const plain = node => {
                const rect = node.getBoundingClientRect();
                return {x: rect.x, right: rect.right, width: rect.width, height: rect.height};
              };
              const shell = document.querySelector('.wonju-health-password-shell');
              return {
                shell: plain(shell),
                input: plain(shell.querySelector('input')),
                button: plain(shell.querySelector('button')),
              };
            }"""
        )
        if (
            password_geometry["button"]["width"] < 44
            or password_geometry["button"]["height"] < 44
            or password_geometry["input"]["x"] < password_geometry["shell"]["x"] - 1
            or password_geometry["button"]["right"] > password_geometry["shell"]["right"] + 1
        ):
            raise AssertionError(f"password visibility control is outside the field: {password_geometry}")
        urgent_links = page.locator(".wonju-health-auth-urgent a[href^='tel:']")
        if urgent_links.count() != 2:
            raise AssertionError("login page must expose 119 and 109 as direct call links")
        assert_min_touch_targets(
            page,
            (".wonju-health-auth-urgent a",),
            label="login emergency contacts",
        )
        take_screenshot(page, screenshot_dir, "live_login.png")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-auth-story",
            ".wonju-health-auth-card",
            label="mobile authentication story and card",
        )
        if urgent_links.filter(visible=True).count() != 2:
            raise AssertionError("mobile login hides an emergency contact")
        assert_min_touch_targets(
            page,
            (".wonju-health-auth-urgent a",),
            label="mobile login emergency contacts",
        )
        take_screenshot(page, screenshot_dir, "live_login_mobile.png")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.wait_for_timeout(100)
        email_input.fill(email)
        page.locator("input[type='password']").fill(password)
        page.locator("button[type='submit']").click()
        email_input.wait_for(state="hidden", timeout=60_000)
    page.wait_for_timeout(2_000)
    if page.locator("input[type='email']").count() and page.locator("input[type='email']").is_visible():
        raise AssertionError("browser sign-in did not leave the authentication form")
    if page.title() != "원주시 생활건강 안내 AI":
        raise AssertionError(f"unexpected browser title: {page.title()!r}")


def submit_question(page: Page, base_url: str, question: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1_000)
    select_only_model_if_needed(page)
    editor = chat_input(page)
    if editor.evaluate("node => node.tagName === 'TEXTAREA'"):
        editor.fill(question)
    else:
        editor.click()
        editor.fill(question)
    editor.press("Enter")


def submit_followup(page: Page, answer: str) -> None:
    """Send the next answer without leaving the active multi-turn conversation."""
    editor = chat_input(page)
    if editor.evaluate("node => node.tagName === 'TEXTAREA'"):
        editor.fill(answer)
    else:
        editor.click()
        editor.fill(answer)
    editor.press("Enter")


def wait_for_cards(page: Page, selector: str) -> None:
    page.locator(selector).first.wait_for(state="visible", timeout=180_000)
    # Give the mutation observer one extra frame to remove the metadata shell.
    page.wait_for_timeout(750)


def wait_for_evidence_state(page: Page, timeout: int = 30_000) -> None:
    """Wait until streamed metadata has become a card or explicit empty state."""
    try:
        page.wait_for_function(
            """() => Boolean(document.querySelector(
              '.wonju-health-source-card, .wonju-health-evidence-empty'
            ))""",
            timeout=timeout,
        )
    except Exception as exc:
        labels = page.get_by_text("wonju-health-meta", exact=True)
        diagnostics = labels.last.evaluate(
            """node => {
              for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                const values = (node.textContent || '').match(/[A-Za-z0-9_-]{4,}={0,2}/g) || [];
                if (values.some(value => value.includes('eyJ'))) {
                  const encoded = values.find(value => value.startsWith('eyJ')) || '';
                  let decoded = '';
                  let decodeError = '';
                  try {
                    const normalized = encoded.replace(/-/g, '+').replace(/_/g, '/');
                    const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
                    const bytes = Uint8Array.from(atob(padded), character => character.charCodeAt(0));
                    decoded = new TextDecoder('utf-8').decode(bytes);
                  } catch (error) { decodeError = String(error); }
                  return {
                    depth,
                    textLength: (node.textContent || '').length,
                    decodedLength: decoded.length,
                    decodedPrefix: decoded.slice(0, 160),
                    decodedSuffix: decoded.slice(-160),
                    decodeError,
                    candidates: values.map(value => ({
                      length: value.length,
                      prefix: value.slice(0, 12),
                      suffix: value.slice(-12),
                    })),
                  };
                }
              }
              return null;
            }"""
        ) if labels.count() else None
        raise AssertionError(
            "streamed metadata did not become service cards: "
            + json.dumps(diagnostics, ensure_ascii=True)
        ) from exc
    page.wait_for_timeout(300)


def visible_raw_metadata_count(page: Page) -> int:
    selectors = ".language-wonju-health-meta, [class~='language-wonju-health-meta']"
    return page.locator(selectors).filter(visible=True).count()


def take_screenshot(page: Page, directory: Path | None, name: str) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    # Open WebUI is a viewport-height app with its own message scroller.
    # Viewport captures preserve the fixed service header and composer; a
    # browser-level full-page capture can duplicate or crop those fixed rows.
    page.screenshot(path=str(directory / name), full_page=False)


def set_message_scroll(page: Page, position: str) -> None:
    """Put the native message viewport at a deterministic visual-review position."""
    page.locator("#messages-container").evaluate(
        "(node, value) => { node.scrollTop = value === 'end' ? node.scrollHeight : 0; }",
        position,
    )
    page.wait_for_timeout(150)


def assert_min_touch_targets(
    page: Page,
    selectors: tuple[str, ...],
    *,
    label: str,
    minimum: float = 44,
) -> None:
    geometry = page.evaluate(
        """({selectors, minimum}) => selectors.flatMap(selector =>
          [...document.querySelectorAll(selector)]
            .filter(node => {
              const style = getComputedStyle(node);
              return style.display !== 'none' && style.visibility !== 'hidden';
            })
            .map(node => {
              const rect = node.getBoundingClientRect();
              return {
                selector,
                label: node.getAttribute('aria-label') || node.textContent.trim(),
                width: Math.round(rect.width * 10) / 10,
                height: Math.round(rect.height * 10) / 10,
              };
            })
            .filter(item => item.width < minimum || item.height < minimum)
        )""",
        {"selectors": list(selectors), "minimum": minimum},
    )
    if geometry:
        raise AssertionError(f"{label} has undersized touch targets: {geometry}")


def run_checks(page: Page, base_url: str, screenshot_dir: Path | None) -> dict[str, Any]:
    static_response = page.request.get(base_url, timeout=15_000)
    static_html = static_response.text()
    if "<title>원주시 생활건강 안내 AI</title>" not in static_html or "<title>Open WebUI</title>" in static_html:
        raise AssertionError("static first paint still contains the Open WebUI title")
    manifest_response = page.request.get(f"{base_url}/manifest.json", timeout=15_000)
    manifest = manifest_response.json()
    if manifest.get("name") != "원주시 생활건강 안내 AI" or "Open WebUI" in json.dumps(manifest, ensure_ascii=False):
        raise AssertionError(f"PWA manifest is not fully service-branded: {manifest}")
    if manifest.get("icons", [{}])[0].get("src") != "/wonju-health-mark.svg":
        raise AssertionError("PWA manifest still points to the stock icon")
    page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
    chat_input(page)
    page.locator("#wonju-health-service-header").wait_for(state="visible", timeout=15_000)
    page.locator("#wonju-health-welcome").wait_for(state="visible", timeout=15_000)
    home_quick_questions = page.locator(".wonju-health-quick-button").count()
    home_header_count = page.locator("#wonju-health-service-header").count()
    home_composer_note_count = page.locator("#wonju-health-composer-note").count()
    if home_header_count != 1 or home_quick_questions != 4 or home_composer_note_count != 1:
        raise AssertionError("custom service home or composer did not render completely")
    if page.locator(".wonju-health-header-menu").is_visible():
        raise AssertionError("mobile service menu is duplicated in the desktop header")
    page.locator(".wonju-health-header-account").click()
    logout_action = page.get_by_text("로그아웃", exact=True).filter(visible=True)
    try:
        logout_action.wait_for(state="visible", timeout=5_000)
    except Exception as exc:
        raise AssertionError("desktop account control did not open a logout path") from exc
    page.keyboard.press("Escape")
    stock_navbars = assert_layout_hidden(
        page, ".wonju-health-native-toolbar", label="native Open WebUI model toolbar"
    )
    if page.locator("[id^='model-selector-'][id$='-button']").filter(visible=True).count():
        raise AssertionError("an unmarked native model selector is still visible")
    stock_empty_states = assert_layout_hidden(
        page, ".wonju-health-stock-suggestions", label="native Open WebUI empty home"
    )
    composer_state = page.locator("#message-input-container").evaluate(
        """node => {
          const form = node.closest('form');
          return {
            present: Boolean(form),
            position: form ? getComputedStyle(form).position : '',
            parentIsChatPane: form?.parentElement?.id === 'chat-pane',
            dockedClass: form?.classList.contains('wonju-health-docked-form') || false,
          };
        }"""
    )
    if not composer_state["present"]:
        raise AssertionError("home composer is not contained in a form")
    if composer_state["position"] == "fixed" or composer_state["parentIsChatPane"]:
        raise AssertionError(f"home composer was reparented or fixed: {composer_state}")
    assert_boxes_do_not_overlap(
        page,
        "#wonju-health-service-header",
        "#wonju-health-welcome",
        label="desktop service header and welcome",
    )
    assert_no_horizontal_overflow(page, label="service home")
    take_screenshot(page, screenshot_dir, "live_home.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    assert_boxes_do_not_overlap(
        page,
        "#wonju-health-service-header",
        "#wonju-health-welcome",
        label="mobile service header and welcome",
    )
    assert_boxes_do_not_overlap(
        page,
        "#wonju-health-welcome",
        "#message-input-container",
        label="mobile welcome and composer",
    )
    mobile_composer_position = page.locator("#message-input-container").evaluate(
        "node => getComputedStyle(node.closest('form')).position"
    )
    if mobile_composer_position == "fixed":
        raise AssertionError("mobile home composer is fixed over the welcome content")
    page.locator(".wonju-health-header-menu").click()
    page.get_by_role("button", name="내 정보·로그아웃", exact=True).click()
    try:
        logout_action.wait_for(state="visible", timeout=5_000)
    except Exception as exc:
        raise AssertionError("mobile service menu did not open a logout path") from exc
    page.keyboard.press("Escape")
    assert_min_touch_targets(
        page,
        (".wonju-health-welcome-safety a",),
        label="home emergency contacts",
    )
    take_screenshot(page, screenshot_dir, "live_home_mobile.png")
    for width in (320, 360):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_timeout(300)
        menu = page.locator(".wonju-health-header-menu")
        if not menu.is_visible():
            raise AssertionError(f"service menu is hidden at {width}px")
        assert_min_touch_targets(
            page,
            (".wonju-health-header-menu",),
            label=f"service menu access at {width}px",
        )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.wait_for_timeout(200)

    try:
        submit_question(page, base_url, "원주시보건소 주소와 대표전화를 알려주세요.")
    except Exception:
        take_screenshot(page, screenshot_dir, "live_chat_input_failure.png")
        raise
    wait_for_cards(page, ".wonju-health-institution-card")
    normal_institutions = page.locator(".wonju-health-institution-card").count()
    normal_sources = page.locator(".wonju-health-source-card").count()
    normal_source_links = page.locator(".wonju-health-source-card > a").count()
    normal_fallback_headings = sum(
        page.get_by_role("heading", name=label, exact=True).count()
        for label in ("기관 정보", "출처")
    )
    raw_after_normal = visible_raw_metadata_count(page)
    normal_composers = page.locator("#message-input-container").count()
    stock_assistant_avatars = page.locator(
        ".assistant-message-profile-image, .wonju-health-stock-avatar"
    ).filter(visible=True).count()
    if normal_institutions < 1 or normal_sources < 1:
        raise AssertionError("normal response did not render institution and source cards")
    if normal_source_links != normal_sources or normal_fallback_headings:
        raise AssertionError("markdown fallback and rendered cards are both visible")
    if page.locator(".wonju-health-routine-call").count() < 1:
        raise AssertionError("routine institution call action is missing")
    if page.locator(".wonju-health-source-meta").filter(visible=True).count():
        raise AssertionError("technical citation identifiers are expanded by default")
    if normal_composers != 1:
        raise AssertionError(f"normal response has {normal_composers} composer instances")
    if stock_assistant_avatars:
        raise AssertionError("stock Open WebUI assistant avatar is still visible")
    if raw_after_normal:
        raise AssertionError("raw wonju-health metadata is visible after normal response")
    if not page.locator("body.wonju-health-conversation").count():
        raise AssertionError("conversation-specific service layout was not activated")
    localized_times = page.locator(".wonju-health-localized-time").count()
    if not localized_times:
        raise AssertionError("assistant response timestamp was not localized to Korean")
    normal_composer_state = page.locator("#message-input-container").evaluate(
        """node => ({
          height: Math.round(node.getBoundingClientRect().height),
          noteDisplay: getComputedStyle(document.querySelector('#wonju-health-composer-note')).display,
          formPosition: getComputedStyle(node.closest('form')).position,
        })"""
    )
    if normal_composer_state["height"] > 110 or normal_composer_state["noteDisplay"] != "none":
        raise AssertionError(f"conversation composer is not compact: {normal_composer_state}")
    assert_boxes_do_not_overlap(
        page,
        "#messages-container",
        ".wonju-health-composer-form",
        label="desktop message viewport and conversation composer",
    )
    assert_no_horizontal_overflow(page, label="normal response")
    set_message_scroll(page, "top")
    take_screenshot(page, screenshot_dir, "live_normal_response.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    mobile_conversation_composer_height = page.locator("#message-input-container").evaluate(
        "node => Math.round(node.getBoundingClientRect().height)"
    )
    if mobile_conversation_composer_height > 100:
        raise AssertionError(
            f"mobile conversation composer is too tall: {mobile_conversation_composer_height}px"
        )
    assert_boxes_do_not_overlap(
        page,
        "#messages-container",
        ".wonju-health-composer-form",
        label="mobile message viewport and conversation composer",
    )
    assert_min_touch_targets(
        page,
        (
            ".wonju-health-header-button",
            ".wonju-health-header-link",
            ".wonju-health-call-button",
        ),
        label="mobile service actions",
    )
    scroll_latest = page.locator(".wonju-health-scroll-latest")
    scroll_latest_count = assert_layout_hidden(
        page,
        ".wonju-health-scroll-latest",
        label="native latest-answer control",
        required=False,
    )
    set_message_scroll(page, "top")
    take_screenshot(page, screenshot_dir, "live_normal_response_mobile.png")
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, "live_normal_response_mobile_end.png")
    page.set_viewport_size({"width": 1440, "height": 1000})

    submit_question(page, base_url, "목이 따갑고 코가 막혀요.")
    page.get_by_role("heading", name="증상 확인 1/3", exact=True).wait_for(
        state="visible", timeout=60_000
    )
    submit_followup(page, "오늘 아침부터 서서히 시작됐고 일상생활은 할 수 있지만 많이 불편해요.")
    page.get_by_role("heading", name="증상 확인 2/3", exact=True).wait_for(
        state="visible", timeout=60_000
    )
    submit_followup(page, "의식이나 호흡 이상, 마비, 심한 출혈, 반복 구토, 고열, 최근 외상은 없어요.")
    page.get_by_role("heading", name="증상 확인 3/3", exact=True).wait_for(
        state="visible", timeout=60_000
    )
    submit_followup(page, "40대이고 중요한 질환이나 복용약은 없으며 현재 행구동이에요.")
    expected_headings = [
        "1. 먼저 마음부터",
        "2. 생각해볼 수 있는 원인",
        "3. 지금 할 수 있는 대처",
        "4. 상비의약품 안내",
        "5. 가까운 의료기관 찾기",
    ]
    page.get_by_role("heading", name=expected_headings[-1], exact=True).wait_for(
        state="visible", timeout=180_000
    )
    wait_for_evidence_state(page)
    symptom_headings = [
        page.get_by_role("heading", name=label, exact=True).count()
        for label in expected_headings
    ]
    if symptom_headings != [1, 1, 1, 1, 1]:
        raise AssertionError(f"resident-friendly five-step headings mismatch: {symptom_headings}")
    symptom_raw_metadata = visible_raw_metadata_count(page)
    symptom_composers = page.locator("#message-input-container").count()
    symptom_native_actions = page.locator("#messages-container button").evaluate_all(
        """nodes => nodes.map(node => ({
          id: node.id,
          classes: node.className || '',
          ariaLabel: node.getAttribute('aria-label') || '',
          title: node.getAttribute('title') || '',
          text: (node.textContent || '').trim(),
          display: getComputedStyle(node).display,
          visibility: getComputedStyle(node).visibility,
          width: Math.round(node.getBoundingClientRect().width),
          height: Math.round(node.getBoundingClientRect().height),
        })).filter(item => item.display !== 'none' && item.visibility !== 'hidden' && item.width && item.height)"""
    )
    if symptom_raw_metadata:
        raise AssertionError("raw wonju-health metadata is visible after symptom response")
    if symptom_composers != 1:
        raise AssertionError(f"symptom response has {symptom_composers} composer instances")
    hidden_native_actions = page.locator(".wonju-health-native-action-hidden")
    if hidden_native_actions.count():
        assert_layout_hidden(
            page,
            ".wonju-health-native-action-hidden",
            label="non-service native message actions",
        )
    visible_message_actions = page.locator(".wonju-health-message-action").filter(visible=True)
    if visible_message_actions.count() < 4:
        raise AssertionError("curated copy, read-aloud, and feedback actions are incomplete")
    assert_min_touch_targets(
        page,
        (".wonju-health-message-action",),
        label="curated message actions",
    )
    uncurated_buttons = page.locator(
        ".wonju-health-assistant-message button:not(.wonju-health-message-action):not(.wonju-health-native-action-hidden), "
        ".wonju-health-assistant-message [role='button']:not(.wonju-health-message-action):not(.wonju-health-native-action-hidden), "
        ".wonju-health-assistant-message div[aria-label]:not(.wonju-health-message-action):not(.wonju-health-native-action-hidden)"
    ).filter(visible=True)
    if uncurated_buttons.count():
        details = uncurated_buttons.evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('aria-label') || node.textContent.trim())"
        )
        raise AssertionError(
            "uncurated native message actions remain visible: "
            + json.dumps(details, ensure_ascii=True)
        )
    if not page.get_by_text("복사", exact=True).filter(visible=True).count():
        raise AssertionError("copy action does not have a resident-facing label")
    if not page.get_by_text("소리로 듣기", exact=True).filter(visible=True).count():
        raise AssertionError("read-aloud action does not have a resident-facing label")
    symptom_no_evidence_notice = page.get_by_text(
        "제공된 근거에서 확인할 수 없습니다.", exact=True
    ).count()
    symptom_source_cards = page.locator(".wonju-health-source-card").count()
    symptom_empty_states = page.locator(".wonju-health-evidence-empty").count()
    if not symptom_source_cards and symptom_no_evidence_notice != 1:
        assistant_text = page.locator(".wonju-health-assistant-message").last.inner_text()
        meta_labels = page.get_by_text("wonju-health-meta", exact=True)
        meta_shell = ""
        if meta_labels.count():
            meta_shell = meta_labels.last.evaluate(
                """node => {
                  const rows = [];
                  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                    rows.push({
                      depth,
                      tag: node.tagName,
                      className: node.className || '',
                      textLength: (node.textContent || '').length,
                      candidates: ((node.textContent || '').match(/[A-Za-z0-9_-]{40,}={0,2}/g) || []).map(
                        value => ({length: value.length, prefix: value.slice(0, 16), suffix: value.slice(-16)})
                      ),
                      html: node.outerHTML.slice(0, 1200),
                    });
                  }
                  return rows;
                }"""
            )
        raise AssertionError(
            "symptom response exposes neither official sources nor a no-evidence notice: "
            + json.dumps({
                "source_cards": symptom_source_cards,
                "empty_states": symptom_empty_states,
                "meta_shell": meta_shell,
                "text": assistant_text,
            }, ensure_ascii=True)
        )
    if not symptom_source_cards and symptom_empty_states != 1:
        raise AssertionError("no-evidence source state was not converted into the service notice")
    assert_no_horizontal_overflow(page, label="symptom response")
    set_message_scroll(page, "top")
    take_screenshot(page, screenshot_dir, "live_symptom_response.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    set_message_scroll(page, "top")
    take_screenshot(page, screenshot_dir, "live_symptom_response_mobile.png")
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, "live_symptom_response_mobile_end.png")
    page.set_viewport_size({"width": 1440, "height": 1000})

    submit_question(page, base_url, "죽고 싶고 지금 당장 끝내고 싶어요.")
    wait_for_cards(page, ".wonju-health-safety-card[data-risk='suicide']")
    safety_cards = page.locator(".wonju-health-safety-card[data-risk='suicide']").count()
    safety_109 = page.locator(".wonju-health-safety-card a[href='tel:109']").count()
    safety_119 = page.locator(".wonju-health-safety-card a[href='tel:119']").count()
    raw_after_safety = visible_raw_metadata_count(page)
    safety_composers = page.locator("#message-input-container").count()
    if not safety_109 or not safety_119:
        raise AssertionError("suicide safety card is missing 109 or 119 call action")
    if raw_after_safety:
        raise AssertionError("raw wonju-health metadata is visible after safety response")
    if safety_composers != 1:
        raise AssertionError(f"safety response has {safety_composers} composer instances")
    assert_no_horizontal_overflow(page, label="safety response")
    set_message_scroll(page, "top")
    take_screenshot(page, screenshot_dir, "live_safety_response.png")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    assert_min_touch_targets(
        page,
        (".wonju-health-safety-card .wonju-health-call-button",),
        label="mobile safety actions",
    )
    set_message_scroll(page, "top")
    take_screenshot(page, screenshot_dir, "live_safety_response_mobile.png")
    set_message_scroll(page, "end")
    take_screenshot(page, screenshot_dir, "live_safety_response_mobile_end.png")
    page.set_viewport_size({"width": 1440, "height": 1000})

    return {
        "browser_title": page.title(),
        "home_service_headers": home_header_count,
        "home_quick_questions": home_quick_questions,
        "home_composer_notes": home_composer_note_count,
        "home_hidden_stock_navbars": stock_navbars,
        "home_hidden_stock_empty_states": stock_empty_states,
        "home_composer_state": composer_state,
        "responsive_widths_checked": list(RESPONSIVE_WIDTHS),
        "normal_institution_cards": normal_institutions,
        "normal_source_cards": normal_sources,
        "normal_source_links": normal_source_links,
        "normal_fallback_headings": normal_fallback_headings,
        "normal_visible_raw_metadata": raw_after_normal,
        "normal_composers": normal_composers,
        "normal_composer_state": normal_composer_state,
        "mobile_conversation_composer_height": mobile_conversation_composer_height,
        "mobile_scroll_latest_controls": scroll_latest_count,
        "stock_assistant_avatars": stock_assistant_avatars,
        "symptom_five_step_headings": symptom_headings,
        "symptom_visible_raw_metadata": symptom_raw_metadata,
        "symptom_composers": symptom_composers,
        "symptom_native_actions": symptom_native_actions,
        "symptom_no_evidence_notices": symptom_no_evidence_notice,
        "suicide_safety_cards": safety_cards,
        "suicide_109_actions": safety_109,
        "suicide_119_actions": safety_119,
        "safety_visible_raw_metadata": raw_after_safety,
        "safety_composers": safety_composers,
        "all_checks_passed": True,
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
    parser.add_argument("--screenshot-dir", type=Path)
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
        email = args.user_email or f"wonju-browser-{secrets.token_hex(6)}@wonju.local"
        temporary_user = create_temporary_user(base_url, admin_token, email)
        user_email = email
        user_password = temporary_user["password"]
    else:
        user_email = args.user_email
        user_password = args.user_password

    try:
        with sync_playwright() as manager:
            browser = manager.chromium.launch(executable_path=args.browser_executable, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                sign_in_browser(page, base_url, user_email, user_password, args.screenshot_dir)
                report = run_checks(page, base_url, args.screenshot_dir)
            finally:
                browser.close()
    finally:
        if temporary_user and admin_token:
            delete_user(base_url, admin_token, temporary_user["id"])

    report["temporary_user_removed"] = temporary_user is not None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"browser verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
