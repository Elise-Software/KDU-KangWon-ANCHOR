from __future__ import annotations

import time

import jwt
from fastapi.testclient import TestClient

import raw_proxy


def signed_identity(secret: str, email: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "user-id",
            "email": email,
            "name": "개발자",
            "role": "user",
            "iss": "open-webui",
            "iat": now,
            "exp": now + 60,
        },
        secret,
        algorithm="HS256",
    )


def configure(monkeypatch):
    monkeypatch.setenv("RAW_PROXY_INTERNAL_API_KEY", "raw-internal-key")
    monkeypatch.setenv("OPEN_WEBUI_JWT_SECRET", "jwt-test-secret-with-sufficient-length")
    monkeypatch.setenv("DEVELOPER_EMAILS", "dev@example.com")
    monkeypatch.setenv("RAW_MODEL_ID", "gemma-4-31b-nvfp4")


def test_raw_model_discovery_requires_internal_key_but_not_user_identity(monkeypatch):
    configure(monkeypatch)
    client = TestClient(raw_proxy.app)
    assert client.get("/v1/models").status_code == 401
    response = client.get(
        "/v1/models",
        headers={
            "Authorization": "Bearer raw-internal-key",
            "X-OpenWebUI-User-Jwt": signed_identity("jwt-test-secret-with-sufficient-length", "user@example.com"),
        },
    )
    assert response.status_code == 200


def test_raw_chat_still_rejects_general_identity(monkeypatch):
    configure(monkeypatch)
    client = TestClient(raw_proxy.app)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer raw-internal-key",
            "X-OpenWebUI-User-Jwt": signed_identity("jwt-test-secret-with-sufficient-length", "user@example.com"),
        },
        json={"model": "gemma-4-31b-nvfp4", "messages": []},
    )
    assert response.status_code == 403


def test_raw_model_accepts_allowlisted_signed_developer(monkeypatch):
    configure(monkeypatch)
    client = TestClient(raw_proxy.app)
    response = client.get(
        "/v1/models",
        headers={
            "Authorization": "Bearer raw-internal-key",
            "X-OpenWebUI-User-Jwt": signed_identity("jwt-test-secret-with-sufficient-length", "DEV@example.com"),
        },
    )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == ["gemma-4-31b-nvfp4"]
