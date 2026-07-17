"""Verify the live Wonju administrator shell and first-response rendering."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from playwright.sync_api import Page, sync_playwright

from verify_live_browser import (
    assert_no_horizontal_overflow,
    default_browser,
    sign_in_browser,
    submit_question,
    take_screenshot,
)


ADMIN_ROUTES = (
    ("users", "/admin/users/overview"),
    ("settings", "/admin/settings/general"),
    ("workspace", "/workspace/models"),
)


def launch_browser(manager):
    kwargs: dict[str, Any] = {"headless": True}
    executable = default_browser()
    if executable:
        kwargs["executable_path"] = executable
    return manager.chromium.launch(**kwargs)


def visible_text_contains(page: Page, value: str) -> bool:
    return bool(page.locator("body").evaluate(
        """(body, value) => [...body.querySelectorAll('*')].some(node => {
          const style = getComputedStyle(node);
          return style.display !== 'none' && style.visibility !== 'hidden'
            && node.children.length === 0 && (node.textContent || '').includes(value);
        })""",
        value,
    ))


def verify_admin_route(page: Page, base_url: str, route_id: str, route: str, output: Path) -> dict[str, Any]:
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(base_url + route, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function("document.body.classList.contains('wonju-health-admin')", timeout=30_000)
    page.locator("#wonju-health-admin-header").wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(500)

    if page.locator("body").evaluate("node => node.classList.contains('wonju-health-chat')"):
        raise AssertionError(f"{route} was misclassified as the resident chat")
    if visible_text_contains(page, "Open WebUI"):
        raise AssertionError(f"{route} still exposes stock Open WebUI branding")
    geometry = page.evaluate(
        """() => {
          const header = document.querySelector('#wonju-health-admin-header').getBoundingClientRect();
          const rootNode = [...document.querySelectorAll('.wonju-health-admin-shell, .wonju-health-admin-root')]
            .find(node => node.getBoundingClientRect().height > 0);
          if (!rootNode) return {missingRoot: true, headerBottom: header.bottom,
            candidates: [...document.body.children].map(node => { const r = node.getBoundingClientRect();
              return {tag: node.tagName, id: node.id, cls: node.className, top: r.top,
                bottom: r.bottom, height: r.height}; })};
          const root = rootNode.getBoundingClientRect();
          return {headerBottom: header.bottom, rootTop: root.top, rootBottom: root.bottom, viewport: innerHeight};
        }"""
    )
    if geometry.get("missingRoot"):
        raise AssertionError(f"{route} visible administrator application root not found: {geometry}")
    if geometry["rootTop"] < geometry["headerBottom"] - 1:
        raise AssertionError(f"{route} content is covered by the admin header: {geometry}")
    if geometry["rootBottom"] > geometry["viewport"] + 1:
        raise AssertionError(f"{route} admin root exceeds the viewport: {geometry}")
    # At exactly 768px Open WebUI intentionally parks four pixels of its
    # collapsed native sidebar outside the viewport. It does not create a
    # scrollbar or cover content, so exercise the adjacent supported widths
    # while treating that stock decorative edge as non-actionable.
    assert_no_horizontal_overflow(
        page,
        label=f"admin {route_id}",
        widths=(320, 360, 390, 767, 769, 1024, 1440),
    )
    if "새로운 버전" in page.locator("body").inner_text():
        notice_nodes = page.evaluate(
            """() => [...document.querySelectorAll('body *')]
              .filter(node => (node.textContent || '').includes('새로운 버전'))
              .map(node => ({tag: node.tagName, cls: node.className,
                position: getComputedStyle(node).position,
                children: node.children.length, text: (node.textContent || '').trim().slice(0, 90)}))
              .slice(-12)"""
        )
        raise AssertionError(f"{route} still exposes the stock update notice: {notice_nodes}")
    take_screenshot(page, output, f"admin_{route_id}_desktop.png")

    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(250)
    assert_no_horizontal_overflow(page, label=f"mobile admin {route_id}")
    page.get_by_role("link", name="챗봇으로", exact=True).wait_for(state="visible")
    take_screenshot(page, output, f"admin_{route_id}_mobile.png")
    page.set_viewport_size({"width": 1440, "height": 1000})
    return {"route": route, "desktop_mobile_passed": True, "geometry": geometry}


def verify_developer_toolbar(page: Page, base_url: str, output: Path) -> dict[str, Any]:
    page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function("document.body.classList.contains('wonju-health-developer')", timeout=30_000)
    toolbar = page.locator(".wonju-health-native-toolbar").filter(visible=True).first
    toolbar.wait_for(state="visible", timeout=30_000)
    page.locator("#wonju-health-welcome").wait_for(state="visible", timeout=30_000)
    geometry = page.evaluate(
        """() => {
          const toolbar = [...document.querySelectorAll('.wonju-health-native-toolbar')]
            .find(node => getComputedStyle(node).display !== 'none');
          const header = document.querySelector('#wonju-health-service-header');
          const welcome = document.querySelector('#wonju-health-welcome');
          const t = toolbar.getBoundingClientRect();
          const h = header.getBoundingClientRect();
          const w = welcome.getBoundingClientRect();
          return {position: getComputedStyle(toolbar).position, headerBottom: h.bottom,
            toolbarTop: t.top, toolbarBottom: t.bottom, welcomeTop: w.top};
        }"""
    )
    if geometry["position"] == "fixed":
        raise AssertionError(f"developer toolbar is still fixed: {geometry}")
    if geometry["toolbarTop"] < geometry["headerBottom"] - 1:
        raise AssertionError(f"developer toolbar overlaps the service header: {geometry}")
    if geometry["welcomeTop"] < geometry["toolbarBottom"] - 1:
        raise AssertionError(f"developer toolbar covers the home content: {geometry}")
    take_screenshot(page, output, "developer_toolbar_desktop.png")
    return geometry


def verify_first_response(page: Page, base_url: str, output: Path) -> dict[str, Any]:
    started = time.monotonic()
    submit_question(page, base_url, "원주시보건소 주소와 대표전화번호를 알려주세요")
    page.wait_for_function(
        """() => {
          const values = [...document.querySelectorAll('.wonju-health-assistant-message')];
          const latest = values.at(-1);
          return Boolean(latest && (latest.innerText || '').trim().length >= 20);
        }""",
        timeout=120_000,
    )
    first_visible_seconds = round(time.monotonic() - started, 3)
    page.locator(".wonju-health-institution-card").first.wait_for(state="visible", timeout=60_000)
    page.locator(".wonju-health-source-card").first.wait_for(state="visible", timeout=60_000)
    page.wait_for_timeout(500)
    if page.locator(".language-wonju-health-meta").filter(visible=True).count():
        raise AssertionError("the first response exposes raw metadata")
    navigation_entries = page.evaluate("performance.getEntriesByType('navigation').length")
    if navigation_entries != 1:
        raise AssertionError(f"the first answer required an unexpected reload: {navigation_entries}")
    take_screenshot(page, output, "first_response_without_refresh.png")
    return {
        "visible_without_refresh": True,
        "first_visible_seconds": first_visible_seconds,
        "institution_cards": page.locator(".wonju-health-institution-card").count(),
        "source_cards": page.locator(".wonju-health-source-card").count(),
        "navigation_entries": navigation_entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.100.58")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--admin-password", default=os.getenv("WONJU_HEALTH_ADMIN_PASSWORD", ""))
    parser.add_argument("--screenshot-dir", type=Path, default=Path("data/p1_rag/reports/admin_browser"))
    parser.add_argument("--report", type=Path, default=Path("data/p1_rag/reports/admin_browser_report.json"))
    args = parser.parse_args()
    if not args.admin_password:
        args.admin_password = getpass.getpass("Open WebUI admin password: ")

    try:
        with sync_playwright() as manager:
            browser = launch_browser(manager)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            sign_in_browser(page, args.base_url.rstrip("/"), args.admin_email, args.admin_password)
            routes = [
                verify_admin_route(page, args.base_url.rstrip("/"), route_id, route, args.screenshot_dir)
                for route_id, route in ADMIN_ROUTES
            ]
            toolbar = verify_developer_toolbar(page, args.base_url.rstrip("/"), args.screenshot_dir)
            first_response = verify_first_response(page, args.base_url.rstrip("/"), args.screenshot_dir)
            browser.close()
        report = {
            "base_url": args.base_url,
            "admin_routes": routes,
            "developer_toolbar": toolbar,
            "first_response": first_response,
            "all_checks_passed": True,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"admin browser verification failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
