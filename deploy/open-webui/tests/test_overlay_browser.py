from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from playwright import sync_api as playwright

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

VIEWPORT_WIDTHS = (320, 360, 390, 768, 1024, 1440)


def assert_no_horizontal_overflow(page, *, widths=VIEWPORT_WIDTHS, label: str) -> None:
    """Check responsive layout without relying on exact component dimensions."""
    original = page.viewport_size or {"width": 1440, "height": 1000}
    try:
        for width in widths:
            page.set_viewport_size({"width": width, "height": 844 if width < 768 else 1000})
            page.wait_for_timeout(50)
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
            assert widest <= geometry["viewport"] + 1, (
                f"{label} horizontally overflows at {width}px: {geometry}"
            )
            assert not geometry["offenders"], (
                f"{label} has clipped custom components at {width}px: {geometry}"
            )
    finally:
        page.set_viewport_size(original)
        page.wait_for_timeout(50)


def assert_layout_hidden(page, selector: str, *, label: str) -> None:
    locator = page.locator(selector)
    assert locator.count() >= 1, f"{label} was not marked in the DOM"
    states = locator.evaluate_all(
        """nodes => nodes.map(node => {
          const style = getComputedStyle(node);
          return {display: style.display, visibility: style.visibility};
        })"""
    )
    assert all(
        state["display"] == "none" or state["visibility"] in {"hidden", "collapse"}
        for state in states
    ), f"{label} is still layout-visible: {states}"


def assert_boxes_do_not_overlap(
    page,
    first_selector: str,
    second_selector: str,
    *,
    label: str,
    tolerance: float = 1.0,
) -> None:
    first = page.locator(first_selector).first
    second = page.locator(second_selector).first
    assert first.is_visible(), f"{label}: {first_selector} is not visible"
    assert second.is_visible(), f"{label}: {second_selector} is not visible"
    first_box = first.bounding_box()
    second_box = second.bounding_box()
    assert first_box and second_box, f"{label}: a bounding box is unavailable"
    overlap_width = min(
        first_box["x"] + first_box["width"], second_box["x"] + second_box["width"]
    ) - max(first_box["x"], second_box["x"])
    overlap_height = min(
        first_box["y"] + first_box["height"], second_box["y"] + second_box["height"]
    ) - max(first_box["y"], second_box["y"])
    assert overlap_width <= tolerance or overlap_height <= tolerance, (
        f"{label} overlaps: {first_selector}={first_box}, {second_selector}={second_box}"
    )


def browser_executable() -> str | None:
    for candidate in (CHROME, EDGE):
        if candidate.is_file():
            return str(candidate)
    return None


def launch_browser(manager):
    """Use a system browser when available, otherwise require Playwright Chromium."""
    executable = browser_executable()
    kwargs = {"headless": True}
    if executable:
        kwargs["executable_path"] = executable
    return manager.chromium.launch(**kwargs)


def encoded_metadata() -> str:
    payload = {
        "schema_version": "wonju-health-card-v1",
        "risk_category": "none",
        "safety_rule_applied": False,
        "safety_contacts": [],
        "institutions": [{
            "name": "원주시보건소",
            "address": "원주시 원일로 139",
            "map_url": "https://map.kakao.com/link/search/%EC%9B%90%EC%A3%BC%EC%8B%9C%EB%B3%B4%EA%B1%B4%EC%86%8C",
            "phones": [{"label": "대표전화", "value": "033-737-4011"}],
            "operation_hours": [],
        }],
        "citations": [{
            "url": "https://www.wonju.go.kr/health/contents.do?key=1624",
            "document": "원주시보건소 공식 안내",
            "doc_id": "structured:phc:wonju",
            "chunk_id": "profile:phc:wonju",
        }],
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()


def encoded_empty_metadata() -> str:
    payload = {
        "schema_version": "wonju-health-card-v1",
        "risk_category": "none",
        "safety_rule_applied": False,
        "safety_contacts": [],
        "institutions": [],
        "citations": [],
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()


def encoded_institution_only_metadata() -> str:
    payload = {
        "schema_version": "wonju-health-card-v1",
        "risk_category": "none",
        "safety_rule_applied": False,
        "safety_contacts": [],
        "institutions": [{
            "institution_id": "wonju:test",
            "name": "테스트의원",
            "address": "원주시 테스트로 1",
            "phones": [],
            "operation_hours": [],
        }],
        "citations": [],
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()


def fixture_html(encoded: str, editable: bool, nested_shell: bool = False) -> str:
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    if editable:
        block = f"""
        <div class="relative flex flex-col code-shell">
          <div class="toolbar">wonju-health-meta <button>저장</button><button>복사</button></div>
          <div class="language-wonju-health-meta"><div class="cm-editor"><div class="cm-content">{encoded}</div></div></div>
        </div>"""
    else:
        block = f'<pre><code class="language-wonju-health-meta">{encoded}</code></pre>'
    if nested_shell:
        # Open WebUI currently wraps the rendered code shell in one extra
        # Svelte-owned div. The overlay must still remove the markdown
        # institution/source fallback without duplicating the cards.
        block = f'<div class="open-webui-code-wrapper">{block}</div>'
    return f"""<!doctype html><html><head><style>{css}</style></head><body>
      <main id="message"><p>근거에 따라 친절하게 안내합니다.</p>
      <h3>기관 정보</h3><ul><li>원주시보건소</li></ul>
      <h3>출처</h3><ul><li>원주시보건소 공식 안내</li></ul>
      {block}</main><script>{js}</script></body></html>"""


@pytest.mark.parametrize("editable", [True, False])
@pytest.mark.parametrize("nested_shell", [True, False])
def test_metadata_is_replaced_by_single_card_set_in_real_browser(editable: bool, nested_shell: bool):
    encoded = encoded_metadata()
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page()
        page.set_content(fixture_html(encoded, editable, nested_shell))
        page.wait_for_selector(".wonju-health-source-card")
        assert page.locator(".language-wonju-health-meta").count() == 0
        assert page.get_by_text(encoded, exact=True).count() == 0
        assert page.locator(".wonju-health-institution-card").count() == 1
        assert page.locator(".wonju-health-source-card").count() == 1
        assert page.locator(".wonju-health-source-title").count() == 1
        assert page.locator(".wonju-health-routine-call").count() == 1
        assert page.locator(".wonju-health-map-button").count() == 1
        assert page.locator(".wonju-health-map-button").get_attribute("target") == "_blank"
        assert page.locator(".wonju-health-source-technical").count() == 1
        assert not page.locator(".wonju-health-source-meta").is_visible()
        assert page.get_by_role("heading", name="기관 정보", exact=True).count() == 0
        assert page.get_by_role("heading", name="출처", exact=True).count() == 0

        # A Svelte rerender of the same metadata must be removed without
        # duplicating the already rendered cards.
        page.evaluate(
            """value => {
              const shell = document.createElement('div');
              shell.className = 'code-shell';
              shell.innerHTML = `<div class="language-wonju-health-meta">${value}</div>`;
              document.querySelector('#message').append(shell);
            }""",
            encoded,
        )
        page.wait_for_timeout(100)
        assert page.locator(".wonju-health-institution-card").count() == 1
        assert page.locator(".wonju-health-source-card").count() == 1
        assert page.locator(".language-wonju-health-meta").count() == 0
        browser.close()


def test_no_evidence_notice_remains_visible_when_there_are_no_replacement_cards():
    encoded = encoded_empty_metadata()
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <main id="message"><h3>출처</h3><ul><li>제공된 근거에서 확인할 수 없습니다.</li></ul>
      <div><pre><code class="language-wonju-health-meta">{encoded}</code></pre></div>
      </main><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page()
        page.set_content(html)
        page.wait_for_function("document.querySelector('.language-wonju-health-meta') === null")
        assert page.get_by_role("heading", name="출처", exact=True).count() == 1
        assert page.get_by_text("제공된 근거에서 확인할 수 없습니다.", exact=True).count() == 1
        assert page.get_by_text("확인된 공식자료가 없습니다", exact=True).count() == 1
        assert page.locator(".wonju-health-evidence-empty").count() == 1
        assert page.locator(".wonju-health-evidence-empty-row .wonju-health-icon").count() == 1
        assert page.locator(".wonju-health-rendered-cards").count() == 0
        browser.close()


def test_source_fallback_removal_stops_at_an_earlier_metadata_block():
    payload = json.loads(base64.urlsafe_b64decode(encoded_metadata()).decode())
    payload["institutions"] = []
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <main id="message"><h3>출처</h3><ul><li>공식 안내</li></ul>
      <div id="earlier-metadata" data-wonju-metadata-shell="true">wonju-health-meta placeholder</div>
      <pre><code class="language-wonju-health-meta">{encoded}</code></pre>
      </main><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page()
        page.set_content(html)
        page.wait_for_selector(".wonju-health-source-card")
        assert page.locator("#earlier-metadata").count() == 1
        browser.close()


def test_no_evidence_notice_remains_when_only_institution_cards_replace_fallback():
    encoded = encoded_institution_only_metadata()
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <main id="message"><h3>기관 정보</h3><ul><li>테스트의원</li></ul>
      <h3>출처</h3><ul><li>제공된 근거에서 확인할 수 없습니다.</li></ul>
      <div><pre><code class="language-wonju-health-meta">{encoded}</code></pre></div>
      </main><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page()
        page.set_content(html)
        page.wait_for_selector(".wonju-health-institution-card")
        assert page.locator(".wonju-health-map-button").count() == 1
        assert page.locator(".wonju-health-map-button").get_attribute("href").startswith(
            "https://map.kakao.com/link/search/"
        )
        assert page.get_by_role("heading", name="기관 정보", exact=True).count() == 0
        assert page.get_by_role("heading", name="출처", exact=True).count() == 1
        assert page.get_by_text("제공된 근거에서 확인할 수 없습니다.", exact=True).count() == 1
        assert page.locator(".wonju-health-evidence-empty").count() == 1
        browser.close()


def test_live_codemirror_metadata_shell_is_replaced_with_cards():
    encoded = encoded_metadata()
    wrapped = "\n".join(encoded[index:index + 120] for index in range(0, len(encoded), 120))
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <section id="messages-container"><article class="chat-assistant">
        <h3>기관 정보</h3><ul><li>원주시보건소</li></ul>
        <h3>출처</h3><ul><li>원주시보건소 공식 안내</li></ul>
        <div class="live-code-shell">
          <header><span>wonju-health-meta</span><button aria-label="접기"></button><button aria-label="저장"></button></header>
          <div class="cm-editor"><div class="cm-content">{wrapped}\n{wrapped}</div></div>
        </div>
      </article></section><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page()
        page.set_content(html)
        page.wait_for_selector(".wonju-health-source-card")
        assert page.locator(".live-code-shell").count() == 0
        assert page.locator(".wonju-health-institution-card").count() == 1
        assert page.locator(".wonju-health-source-card").count() == 1
        assert page.get_by_role("button", name="접기", exact=True).count() == 0
        assert page.get_by_role("button", name="저장", exact=True).count() == 0
        browser.close()


@pytest.mark.parametrize("has_messages_container", [True, False])
def test_service_shell_has_branded_home_quick_questions_and_accessible_composer(
    has_messages_container: bool,
):
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    native_actions = """<div id="stock-message-actions">
      <button aria-label="편집"><svg></svg></button>
      <button class="copy-response-button" aria-label="복사"><svg></svg></button>
      <button aria-label="읽어주기"><svg></svg></button>
      <button aria-label="좋은 응답"><svg></svg></button>
      <button aria-label="잘못된 응답"><svg></svg></button>
      <button id="continue-response-button" aria-label="답변 이어서 받기"><svg></svg></button>
      <div aria-label="재생성"><svg></svg></div>
      <button aria-label="접기"><svg></svg></button>
      <button aria-label="저장"><svg></svg></button>
    </div>"""
    assistant_header = """<div class="stock-assistant-header"><div class="response-header">
      <span id="response-message-model-name">wonju-health-rag</span><span class="stock-time">오늘 11:14 PM</span>
    </div></div>"""
    message_host = f'<section id="messages-container">{native_actions}{assistant_header}</section>' if has_messages_container else ''
    stock_toolbar = """<nav id="stock-model-toolbar" class="sticky top-0">
      <button id="model-selector-0-button">wonju-health-rag</button>
      <img alt="stock profile" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
    </nav><nav id="stock-model-toolbar-secondary"><button id="model-selector-1-button">wonju-health-rag</button></nav>"""
    stock_empty_home = """<div class="flex items-center h-full stock-empty-home">
      <div class="m-auto w-full max-w-6xl">기본 시작 화면</div>
    </div>"""
    stock_scroll_latest = """<div class="flex justify-center">
      <button class="bg-white border border-gray-100 p-1.5 rounded-full pointer-events-auto">
        <svg aria-hidden="true" width="20" height="20"></svg>
      </button>
    </div>"""
    composer = """<form><div id="message-input-container"><div id="chat-input-container">
          <button id="input-menu-button">추가</button>
          <textarea id="chat-input"></textarea>
          <button id="voice-input-button">음성</button>
          <button id="send-message-button">전송</button>
        </div></div></form>"""
    composer_shell = f'<div id="stock-composer-home" class="stock-composer-shell">{composer}</div>'
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <aside id="sidebar"><button id="sidebar-new-chat-button">새 대화</button>
        <button id="sidebar-search-button">검색</button>
        <button id="stock-profile-button" aria-label="프로필"><img alt="사용자"></button>
      </aside>
      <main id="chat-container">{stock_toolbar}<div id="chat-pane">
        {message_host}
        {stock_empty_home}
        {stock_scroll_latest}
        {composer_shell}
        <span id="stock-assistant-avatar" class="assistant-message-profile-image" style="display:inline-flex;width:28px;height:28px">이</span>
      </div></main><script>
        window.wonjuTestClicks = {{history: 0, account: 0}};
        document.querySelector('#sidebar-search-button').addEventListener('click', () => window.wonjuTestClicks.history++);
        document.querySelector('#stock-profile-button').addEventListener('click', () => window.wonjuTestClicks.account++);
      </script><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_content(html)
        page.locator("#wonju-health-welcome").wait_for(state="visible")
        assert page.locator("#wonju-health-service-header").count() == 1
        assert not page.locator(".wonju-health-header-menu").is_visible()
        assert page.locator(".wonju-health-quick-button").count() == 4
        assert page.locator("#wonju-health-composer-note").count() == 1
        home_composer_height = page.locator("#message-input-container").bounding_box()["height"]
        composer_state = page.locator("#message-input-container").evaluate(
            """node => {
              const form = node.closest('form');
              return {
                parentId: form?.parentElement?.id || '',
                position: form ? getComputedStyle(form).position : '',
              };
            }"""
        )
        assert composer_state["parentId"] == "stock-composer-home"
        assert composer_state["position"] != "fixed"
        assert page.locator(".wonju-health-native-toolbar").count() == 2
        assert_layout_hidden(page, ".wonju-health-native-toolbar", label="native model toolbars")
        assert_layout_hidden(page, ".wonju-health-stock-suggestions", label="stock empty home")
        assert page.locator(".wonju-health-scroll-latest").count() == 1
        assert page.locator(".wonju-health-scroll-latest-wrap").count() == 1
        assert page.locator(".wonju-health-scroll-latest").get_attribute("aria-label") == "최신 답변으로 이동"
        assert_layout_hidden(page, ".wonju-health-scroll-latest", label="native latest-answer control")
        assert page.locator("body > :first-child").get_attribute("id") == "wonju-health-service-header"
        page.get_by_role("button", name="지난 건강 질문 찾기").click()
        page.get_by_role("button", name="내 정보 열기").click()
        assert page.evaluate("window.wonjuTestClicks") == {"history": 1, "account": 1}
        page.evaluate("document.querySelector('#stock-profile-button').remove()")
        page.get_by_role("button", name="내 정보 열기").click()
        page.locator("#wonju-health-service-notice.is-visible").wait_for(state="visible")
        assert "새로고침" in page.locator("#wonju-health-service-notice").inner_text()
        if has_messages_container:
            assert_layout_hidden(page, ".wonju-health-native-action-hidden", label="non-service message actions")
            assert page.locator(".wonju-health-native-action-hidden").count() == 5
            assert page.locator(".wonju-health-message-action").count() == 4
            assert page.locator(".wonju-health-action-label").all_inner_texts() == ["복사", "소리로 듣기"]
            assert page.get_by_text("오늘 오후 11:14", exact=True).count() == 1
            assert page.get_by_text("오늘 11:14 PM", exact=True).count() == 0
            for box in page.locator(".wonju-health-message-action").evaluate_all(
                "nodes => nodes.map(node => node.getBoundingClientRect()).map(rect => ({width: rect.width, height: rect.height}))"
            ):
                assert box["width"] >= 44 and box["height"] >= 44
        page.locator("#stock-assistant-avatar.wonju-health-stock-avatar").wait_for(
            state="attached"
        )
        assert_layout_hidden(page, ".wonju-health-stock-avatar", label="stock assistant avatar")
        assert page.locator("#chat-input").get_attribute("placeholder") == "증상, 동네, 찾는 기관을 편하게 적어주세요"
        assert page.locator("#input-menu-button").evaluate("node => getComputedStyle(node).display") == "none"
        page.get_by_role("button", name="가까운 병원·약국 찾기", exact=False).click()
        assert "동네" in page.locator("#chat-input").input_value()
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-service-header",
            "#wonju-health-welcome",
            label="desktop header and welcome",
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-service-header",
            "#wonju-health-welcome",
            label="mobile header and welcome",
        )
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-welcome",
            "#message-input-container",
            label="mobile welcome and composer",
        )
        assert page.locator("#message-input-container").evaluate(
            "node => getComputedStyle(node.closest('form')).position"
        ) != "fixed"
        assert float(page.locator("#chat-input").evaluate("node => parseFloat(getComputedStyle(node).fontSize)")) >= 18
        for width in (320, 360):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_timeout(100)
            menu = page.locator(".wonju-health-header-menu")
            assert menu.is_visible()
            assert not page.locator(".wonju-health-header-account").is_visible()
            menu_box = menu.bounding_box()
            assert menu_box["width"] >= 44 and menu_box["height"] >= 44
            menu.click()
            assert page.locator(".wonju-health-mobile-menu-item").count() == 3
            page.get_by_role("button", name="지난 질문", exact=True).click()
        assert page.evaluate("window.wonjuTestClicks.history") == 3
        for link in page.locator(".wonju-health-welcome-safety a").all():
            box = link.bounding_box()
            assert box["height"] >= 44
        assert_no_horizontal_overflow(page, label="service home")
        page.evaluate(
            """hasMessages => {
              const message = document.createElement('div');
              message.className = 'user-message';
              (hasMessages ? document.querySelector('#messages-container') : document.querySelector('#chat-pane')).append(message);
            }""",
            has_messages_container,
        )
        page.wait_for_function("document.querySelector('#wonju-health-welcome') === null")
        page.wait_for_function("document.body.classList.contains('wonju-health-conversation')")
        assert page.locator("#message-input-container").count() == 1
        assert page.locator("#wonju-health-composer-note").evaluate(
            "node => getComputedStyle(node).display"
        ) == "none"
        assert page.locator("#message-input-container").bounding_box()["height"] < home_composer_height
        assert page.locator("#message-input-container").evaluate(
            "node => node.closest('form')?.parentElement?.id"
        ) == "stock-composer-home"
        assert page.locator("#message-input-container").evaluate(
            "node => getComputedStyle(node.closest('form')).position"
        ) != "fixed"
        browser.close()


def test_admin_routes_use_branded_console_without_being_misclassified_as_chat():
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <div id="application-root" class="h-screen">
        <aside id="sidebar"><span id="stock-admin-logo">OI</span></aside>
        <div id="admin-settings-tabs-container"><a id="general">일반</a><a id="models">모델</a></div>
        <main><h2>일반 설정</h2><p>Open WebUI 사용 방법</p><textarea>관리 문구</textarea>
          <button class="bg-black">저장</button>
        </main>
        <div id="stock-update" style="position:fixed">새로운 버전을 사용할 수 있습니다.</div>
      </div><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route(
            "http://wonju-admin.test/admin/settings/general",
            lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"),
        )
        page.goto("http://wonju-admin.test/admin/settings/general")
        page.wait_for_function("document.body.classList.contains('wonju-health-admin')")
        assert not page.locator("body").evaluate(
            "node => node.classList.contains('wonju-health-chat')"
        )
        assert page.locator("#wonju-health-admin-header").is_visible()
        admin_context = page.locator(".wonju-health-admin-context")
        assert admin_context.is_visible(), admin_context.evaluate(
            "node => ({text: node.innerText, display: getComputedStyle(node).display, rect: node.getBoundingClientRect().toJSON()})"
        )
        assert admin_context.locator("strong").inner_text() == "관리자 센터"
        assert page.locator("#application-root").evaluate(
            "node => getComputedStyle(node).marginTop"
        ) == "68px"
        assert page.locator("#general").evaluate(
            "node => node.classList.contains('wonju-health-admin-current')"
        )
        assert page.locator("#stock-admin-logo").inner_text() == "원주"
        assert page.get_by_text("원주시 생활건강 관리 서비스 사용 방법", exact=True).count() == 1
        assert_layout_hidden(page, "#stock-update", label="stock admin update notice")
        assert_no_horizontal_overflow(page, label="admin console")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.get_by_role("link", name="챗봇으로", exact=True).is_visible()
        assert_no_horizontal_overflow(page, widths=(390,), label="mobile admin console")
        browser.close()


def test_streamed_first_answer_renders_without_a_page_reload():
    encoded = encoded_metadata()
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <main id="chat-container"><div id="chat-pane"><section id="messages-container"></section>
        <form><div id="message-input-container"><textarea id="chat-input"></textarea></div></form>
      </div></main><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_content(html)
        page.wait_for_selector("#wonju-health-welcome")
        page.evaluate(
            """value => {
              const host = document.querySelector('#messages-container');
              host.insertAdjacentHTML('beforeend', '<div class="user-message">보건소 알려주세요</div>');
              const answer = document.createElement('article');
              answer.className = 'chat-assistant';
              answer.innerHTML = '<p>공식 정보를 확인했습니다.</p><pre><code class="language-wonju-health-meta"></code></pre>';
              host.append(answer);
              const code = answer.querySelector('code');
              code.textContent = value.slice(0, Math.floor(value.length / 2));
              setTimeout(() => { code.textContent = value; }, 25);
            }""",
            encoded,
        )
        page.wait_for_selector(".wonju-health-institution-card")
        assert page.locator("#wonju-health-welcome").count() == 0
        assert page.locator(".wonju-health-institution-card").count() == 1
        assert page.locator(".wonju-health-source-card").count() == 1
        assert page.evaluate("performance.getEntriesByType('navigation').length") <= 1
        browser.close()


def test_developer_with_raw_model_keeps_a_model_selection_path():
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <main id="chat-container"><nav id="developer-model-toolbar">
        <button id="model-selector-0-button">wonju-health-rag</button>
      </nav><div id="chat-pane"><textarea id="chat-input"></textarea>
        <form><div id="message-input-container"><div id="chat-input-container"></div></div></form>
      </div></main>
      <script>
        localStorage.setItem('token', 'developer-test-token');
        window.fetch = async () => ({{
          ok: true,
          json: async () => ({{data: [{{id: 'wonju-health-rag'}}, {{id: 'gemma-4-31b-nvfp4'}}]}})
        }});
      </script><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("http://wonju-developer.test/", lambda route: route.fulfill(body=html, content_type="text/html"))
        page.goto("http://wonju-developer.test/")
        page.wait_for_function("document.body.classList.contains('wonju-health-developer')")
        assert page.locator("#developer-model-toolbar").is_visible()
        assert page.locator("#model-selector-0-button").is_visible()
        assert page.locator("#developer-model-toolbar").evaluate(
            "node => getComputedStyle(node).position"
        ) == "sticky"
        toolbar_box = page.locator("#developer-model-toolbar").bounding_box()
        chat_pane_box = page.locator("#chat-pane").bounding_box()
        assert toolbar_box and chat_pane_box
        assert toolbar_box["y"] + toolbar_box["height"] <= chat_pane_box["y"] + 68
        browser.close()


def test_login_is_a_two_panel_wonju_service_experience_without_open_webui_copy():
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    html = f"""<!doctype html><html><head><style>{css}</style></head><body>
      <div id="logo">OUI</div><div id="auth-page"><div id="auth-container"><div><div>
        <form><h1>원주시 생활건강 안내 AI (Open WebUI) 로그인</h1>
          <label>이메일<input type="email"></label>
          <label>비밀번호<div id="password-shell"><input name="password" type="password"><button type="button" aria-label="비밀번호 보이기">보기</button></div></label>
          <button type="submit">로그인</button>
        </form>
      </div></div></div></div><script>{js}</script></body></html>"""
    with playwright.sync_playwright() as manager:
        browser = launch_browser(manager)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_content(html)
        page.locator("#wonju-health-auth-story").wait_for(state="visible")
        assert page.locator(".wonju-health-auth-card").count() == 1
        assert page.get_by_role("heading", name="로그인", exact=True).count() == 1
        assert "건강" in page.locator("#wonju-health-auth-story h1").inner_text()
        assert page.locator(".wonju-health-auth-urgent a[href='tel:119']").count() == 1
        assert page.locator(".wonju-health-auth-urgent a[href='tel:109']").count() == 1
        assert page.get_by_text("Open WebUI", exact=False).count() == 0
        assert_layout_hidden(page, "#logo", label="stock auth logo")
        assert page.locator("#password-shell.wonju-health-password-shell").count() == 1
        password_geometry = page.evaluate(
            """() => {
              const box = selector => {
                const rect = document.querySelector(selector).getBoundingClientRect();
                return {x: rect.x, right: rect.right, width: rect.width, height: rect.height};
              };
              const shell = box('#password-shell');
              const input = box('#password-shell input');
              const button = box('#password-shell button');
              return {shell, input, button};
            }"""
        )
        assert password_geometry["button"]["width"] >= 44 and password_geometry["button"]["height"] >= 44
        assert password_geometry["input"]["x"] >= password_geometry["shell"]["x"]
        assert password_geometry["button"]["right"] <= password_geometry["shell"]["right"] + 1
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-auth-story",
            ".wonju-health-auth-card",
            label="desktop auth story and card",
        )
        assert_no_horizontal_overflow(page, label="authentication page")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        for link in page.locator(".wonju-health-auth-urgent a").all():
            assert link.is_visible()
            box = link.bounding_box()
            assert box["height"] >= 44
        assert_boxes_do_not_overlap(
            page,
            "#wonju-health-auth-story",
            ".wonju-health-auth-card",
            label="mobile auth story and card",
        )
        browser.close()
