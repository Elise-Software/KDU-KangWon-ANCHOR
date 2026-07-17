from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_only_caddy_publishes_a_host_port():
    services = compose()["services"]
    assert set(services) == {"p1-api", "raw-dev-proxy", "open-webui", "permissions-bootstrap", "caddy"}
    assert "ports" in services["caddy"]
    assert all("ports" not in services[name] for name in services if name != "caddy")


def test_p0_p1_mounts_are_read_only_and_p1_api_is_internal():
    service = compose()["services"]["p1-api"]
    assert all(volume.endswith(":ro") for volume in service["volumes"] if "../../" in volume)
    assert service["expose"] == ["8010"]
    assert compose()["services"]["raw-dev-proxy"]["expose"] == ["8020"]


def test_open_webui_own_rag_and_direct_provider_access_are_disabled():
    environment = compose()["services"]["open-webui"]["environment"]
    assert environment["ENABLE_DIRECT_CONNECTIONS"] == "false"
    assert environment["ENABLE_WEB_SEARCH"] == "false"
    assert environment["ENABLE_TITLE_GENERATION"] == "false"
    assert environment["ENABLE_FOLLOW_UP_GENERATION"] == "false"
    assert environment["USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS"] == "false"
    assert environment["USER_PERMISSIONS_CHAT_FILE_UPLOAD"] == "false"
    assert environment["BYPASS_MODEL_ACCESS_CONTROL"] == "false"


def test_gateway_blocks_provider_routes():
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "/v1/*" in caddy
    assert "/raw-api/*" in caddy
    assert 'respond "not found" 404' in caddy


def test_overlay_is_wonju_specific_and_contains_accessibility_cards():
    source = "\n".join(
        (ROOT / "overlay" / filename).read_text(encoding="utf-8")
        for filename in ("wonju-health-overlay.js", "wonju-health-overlay.css")
    )
    assert "원주시 생활건강 안내 AI" in source
    assert "wonju-health-safety-card" in source
    assert "wonju-health-source-card" in source
    assert "wonju-health-institution-card" in source
    assert "CDXVI" not in source
    assert "inventory" not in source.casefold()
    assert "purchase" not in source.casefold()


def test_static_first_paint_and_pwa_manifest_are_fully_wonju_branded():
    dockerfile = (ROOT / "open-webui" / "Dockerfile").read_text(encoding="utf-8")
    installer = (ROOT / "open-webui" / "install_overlay.sh").read_text(encoding="utf-8")
    manifest = yaml.safe_load((ROOT / "overlay" / "manifest.json").read_text(encoding="utf-8"))
    mark = (ROOT / "overlay" / "wonju-health-mark.svg").read_text(encoding="utf-8")

    assert manifest["name"] == "원주시 생활건강 안내 AI"
    assert manifest["short_name"] == "생활건강 AI"
    assert "Open WebUI" not in str(manifest)
    assert manifest["theme_color"] == "#155247"
    assert manifest["icons"][0]["src"] == "/wonju-health-mark.svg"
    assert "COPY overlay/manifest.json" in dockerfile
    assert "COPY overlay/wonju-health-mark.svg" in dockerfile
    assert '<title>원주시 생활건강 안내 AI</title>' in installer
    assert '<html lang="ko">' in installer
    assert "/wonju-health-mark.svg" in installer
    assert "wonju-health-manifest.json" in installer
    assert "rewrite * /wonju-health-manifest.json" in (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "Open WebUI" not in mark


def test_resident_friendly_prompt_and_app_split_health_guidance_from_facility_lookup():
    prompt_source = (ROOT.parents[1] / "scripts" / "p1_rag" / "models.py").read_text(encoding="utf-8")
    for phrase in (
        "짧게 공감",
        "가능한 원인",
        "안전한 생활 대처",
        "상비의약품",
        "검증된 기관 마스터로 별도 생성",
        "답변은 4번 제목에서 끝낸다",
    ):
        assert phrase in prompt_source
    app_source = (ROOT / "p1-api" / "app.py").read_text(encoding="utf-8")
    assert "### 5. 가까운 의료기관 찾기" in app_source


def test_raw_metadata_is_hidden_before_card_rendering():
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    assert "code.language-wonju-health-meta" in css
    assert 'div:has(> .language-wonju-health-meta)' not in css
    assert '[class~=\'language-wonju-health-meta\']' in js
    assert "characterData: true" in js


def test_overlay_customizes_the_full_service_experience_not_only_response_cards():
    css = (ROOT / "overlay" / "wonju-health-overlay.css").read_text(encoding="utf-8")
    js = (ROOT / "overlay" / "wonju-health-overlay.js").read_text(encoding="utf-8")
    for selector in (
        "#auth-page",
        "#sidebar",
        "#messages-container",
        "#message-input-container",
        "#chat-input",
        "#send-message-button",
    ):
        assert selector in css or selector in js
    for component in (
        "wonju-health-service-header",
        "wonju-health-auth-story",
        "wonju-health-welcome",
        "wonju-health-quick-button",
        "wonju-health-step-heading",
        "wonju-health-routine-call",
        "wonju-health-emergency-call",
        "wonju-health-source-technical",
    ):
        assert component in css and component in js
    assert ":root {\n    font-size: 17px" not in css


def test_docker_build_context_excludes_runtime_secrets():
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns


def test_live_verifier_does_not_require_password_on_the_command_line():
    source = (ROOT / "verify_live_stack.py").read_text(encoding="utf-8")
    assert 'getpass.getpass("Open WebUI user password: ")' in source
    assert 'default=os.getenv("WONJU_HEALTH_USER_PASSWORD")' in source
    assert 'add_argument("--user-password", required=True)' not in source


def test_vllm_user_service_recovers_with_a_loopback_only_publication():
    script = (ROOT / "systemd" / "run-wonju-vllm.sh").read_text(encoding="utf-8")
    unit = (ROOT / "systemd" / "wonju-vllm.service").read_text(encoding="utf-8")
    assert "-p 127.0.0.1:8000:8000" in script
    assert "wonju-health-internal" in script
    assert 'docker wait "${CONTAINER_NAME}"' in script
    assert "0.0.0.0:8000:8000" not in script
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
