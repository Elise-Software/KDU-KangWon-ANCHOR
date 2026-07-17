"""Continuously reconcile Open WebUI model ACLs and developer membership."""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import requests


BASE_URL = os.getenv("OPEN_WEBUI_URL", "http://open-webui:8080").rstrip("/")


class Api:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def authenticate(self) -> None:
        response = self.session.post(
            f"{BASE_URL}/api/v1/auths/signin",
            json={
                "email": os.environ["WEBUI_ADMIN_EMAIL"],
                "password": os.environ["WEBUI_ADMIN_PASSWORD"],
            },
            timeout=15,
        )
        response.raise_for_status()
        self.session.headers["Authorization"] = f"Bearer {response.json()['token']}"

    def get(self, path: str) -> Any:
        response = self.session.get(f"{BASE_URL}{path}", timeout=15)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        response = self.session.post(f"{BASE_URL}{path}", json=payload or {}, timeout=15)
        response.raise_for_status()
        return response.json()


def configured_emails() -> set[str]:
    return {
        value.strip().casefold()
        for value in os.getenv("DEVELOPER_EMAILS", "").split(",")
        if value.strip()
    }


def get_or_create_group(api: Api) -> dict[str, Any]:
    name = os.getenv("DEVELOPER_GROUP_NAME", "개발자")
    groups = api.get("/api/v1/groups/")
    existing = next((group for group in groups if group.get("name") == name), None)
    if existing:
        return existing
    return api.post(
        "/api/v1/groups/create",
        {
            "name": name,
            "description": "원본 생성 모델 접근이 허용된 개발자 전용 그룹",
            "permissions": {},
            "data": {"config": {"share": False}},
        },
    )


def reconcile_members(api: Api, group: dict[str, Any]) -> tuple[int, int]:
    desired_emails = configured_emails()
    users_payload = api.get("/api/v1/users/all")
    users = users_payload.get("users", []) if isinstance(users_payload, dict) else users_payload
    desired_ids = {row["id"] for row in users if row.get("email", "").casefold() in desired_emails}
    current = api.post(f"/api/v1/groups/id/{group['id']}/users")
    current_ids = {row["id"] for row in current}

    add = sorted(desired_ids - current_ids)
    remove = sorted(current_ids - desired_ids)
    if add:
        api.post(f"/api/v1/groups/id/{group['id']}/users/add", {"user_ids": add})
    if remove:
        api.post(f"/api/v1/groups/id/{group['id']}/users/remove", {"user_ids": remove})
    return len(add), len(remove)


def reconcile_model_acl(api: Api, group: dict[str, Any]) -> None:
    general_model = os.getenv("GENERAL_MODEL_ID", "wonju-health-rag")
    raw_model = os.getenv("RAW_MODEL_ID", "gemma-4-31b-nvfp4")
    api.post(
        "/api/v1/models/model/access/update",
        {
            "id": general_model,
            "name": "원주시 생활건강 안내 AI",
            "access_grants": [
                {"principal_type": "user", "principal_id": "*", "permission": "read"}
            ],
        },
    )
    api.post(
        "/api/v1/models/model/access/update",
        {
            "id": raw_model,
            "name": f"{raw_model} (개발자 전용)",
            "access_grants": [
                {"principal_type": "group", "principal_id": group["id"], "permission": "read"}
            ],
        },
    )


def reconcile() -> None:
    api = Api()
    api.authenticate()
    group = get_or_create_group(api)
    added, removed = reconcile_members(api, group)
    reconcile_model_acl(api, group)
    print(
        f"Open WebUI ACL synchronized: group={group['name']} added={added} removed={removed}",
        flush=True,
    )


def main() -> int:
    interval = max(30, int(os.getenv("SYNC_INTERVAL_SECONDS", "60")))
    while True:
        try:
            reconcile()
        except Exception as exc:
            print(f"ACL synchronization failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
