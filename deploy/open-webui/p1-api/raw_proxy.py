"""Developer-only, allowlisted proxy for the raw vLLM chat endpoint."""
from __future__ import annotations

import json
import os
import secrets
from typing import Any

import jwt
import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="Wonju Health raw model developer proxy", version="1.0.0")


def allowed_emails() -> set[str]:
    return {
        value.strip().casefold()
        for value in os.getenv("DEVELOPER_EMAILS", "").split(",")
        if value.strip()
    }


def authorize_internal(internal_authorization: str | None) -> None:
    expected_key = os.getenv("RAW_PROXY_INTERNAL_API_KEY", "")
    supplied_key = internal_authorization.removeprefix("Bearer ").strip() if internal_authorization else ""
    if not expected_key or not supplied_key or not secrets.compare_digest(expected_key, supplied_key):
        raise HTTPException(status_code=401, detail="invalid internal API credential")


def authorize(internal_authorization: str | None, user_jwt: str | None) -> dict[str, Any]:
    authorize_internal(internal_authorization)

    jwt_secret = os.getenv("OPEN_WEBUI_JWT_SECRET", "")
    if not jwt_secret or not user_jwt:
        raise HTTPException(status_code=403, detail="signed Open WebUI user identity is required")
    try:
        claims = jwt.decode(user_jwt, jwt_secret, algorithms=["HS256"], issuer="open-webui")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=403, detail="invalid signed Open WebUI user identity") from exc

    email = str(claims.get("email", "")).casefold()
    if not email or email not in allowed_emails():
        raise HTTPException(status_code=403, detail="raw model access is restricted to configured developers")
    return claims


def upstream_headers() -> dict[str, str]:
    key = os.getenv("VLLM_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "raw_model": os.getenv("RAW_MODEL_ID", "gemma-4-31b-nvfp4"),
        "developer_allowlist_configured": bool(allowed_emails()),
    }


@app.get("/v1/models")
async def models(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    # Model discovery is authenticated as the internal Open WebUI provider,
    # while per-user visibility is enforced by Open WebUI's model ACL.  If
    # discovery itself returns 403 for a general user, Open WebUI can cache the
    # whole provider as unavailable and temporarily hide the raw model from an
    # authorized developer too.  Inference below still requires the signed,
    # allowlisted user identity.
    authorize_internal(authorization)
    model_id = os.getenv("RAW_MODEL_ID", "gemma-4-31b-nvfp4")
    return {
        "object": "list",
        "data": [{"id": model_id, "object": "model", "created": 0, "owned_by": "developer-proxy"}],
    }


@app.post("/v1/chat/completions")
async def chat(
    request: Request,
    authorization: str | None = Header(default=None),
    x_openwebui_user_jwt: str | None = Header(default=None, alias="X-OpenWebUI-User-Jwt"),
):
    authorize(authorization, x_openwebui_user_jwt)
    payload = await request.json()
    model_id = os.getenv("RAW_MODEL_ID", "gemma-4-31b-nvfp4")
    if payload.get("model") != model_id:
        raise HTTPException(status_code=404, detail="only the configured raw developer model is allowed")

    base_url = os.getenv("VLLM_BASE_URL", "http://wonju-vllm:8000/v1").rstrip("/")
    streaming = bool(payload.get("stream"))
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=upstream_headers(),
            json=payload,
            timeout=(10, 600),
            stream=streaming,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="raw model upstream is unavailable") from exc

    if not streaming:
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": "raw model returned a non-JSON response"}}
        return JSONResponse(status_code=response.status_code, content=body)

    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = {"error": {"message": "raw model upstream error"}}
        response.close()
        return JSONResponse(status_code=response.status_code, content=detail)

    def events():
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            response.close()

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
