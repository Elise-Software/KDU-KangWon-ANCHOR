"""Verify the live privacy-aware administrator audit and feedback flow."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

import requests


META_PATTERN = re.compile(r"```wonju-health-meta\s*([A-Za-z0-9_=-]+)\s*```", re.MULTILINE)


def sign_in(base_url: str, email: str, password: str) -> str:
    response = requests.post(
        f"{base_url}/api/v1/auths/signin",
        json={"email": email, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["token"]


def create_user(base_url: str, admin_token: str) -> dict[str, str]:
    email = f"wonju-audit-verifier-{secrets.token_hex(6)}@wonju.local"
    password = secrets.token_urlsafe(24)
    response = requests.post(
        f"{base_url}/api/v1/auths/add",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "감사기능 검증 사용자", "email": email, "password": password, "role": "user"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return {"id": payload["id"], "token": payload["token"]}


def delete_user(base_url: str, admin_token: str, user_id: str) -> None:
    response = requests.delete(
        f"{base_url}/api/v1/users/{user_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    response.raise_for_status()


def metadata(content: str) -> dict[str, Any]:
    parts = META_PATTERN.findall(content)
    if not parts:
        raise AssertionError("audit event metadata is missing from the answer")
    encoded = "".join(parts)
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))


def dotenv(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path:
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.100.58")
    parser.add_argument("--admin-email", default="")
    parser.add_argument("--admin-password", default=os.getenv("WONJU_HEALTH_ADMIN_PASSWORD", ""))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    environment = dotenv(args.env_file)
    admin_email = args.admin_email or environment.get("WEBUI_ADMIN_EMAIL", "")
    admin_password = args.admin_password or environment.get("WEBUI_ADMIN_PASSWORD", "")
    if not admin_email or not admin_password:
        parser.error("administrator email/password arguments, environment, or --env-file are required")

    base_url = args.base_url.rstrip("/")
    admin_token = sign_in(base_url, admin_email, admin_password)
    temporary = create_user(base_url, admin_token)
    question = f"원주시보건소 전화번호를 알려주세요 audit-{secrets.token_hex(4)}"
    headers = {"Authorization": f"Bearer {temporary['token']}"}
    try:
        response = requests.post(
            f"{base_url}/api/chat/completions",
            headers=headers,
            json={
                "model": "wonju-health-rag",
                "messages": [{"role": "user", "content": question}],
                "stream": False,
            },
            timeout=180,
        )
        response.raise_for_status()
        try:
            details = metadata(response.json()["choices"][0]["message"]["content"])
            event_id = details["audit_event_id"]
        except AssertionError:
            # Open WebUI versions may remove the private metadata fence before
            # returning the API response. The unique audit suffix still gives
            # an exact administrator-side lookup without weakening access.
            lookup = requests.get(
                f"{base_url}/wonju-admin-api/audit/events",
                headers={"Authorization": f"Bearer {admin_token}"},
                params={"q": question.rsplit(" ", 1)[-1]},
                timeout=30,
            )
            lookup.raise_for_status()
            rows = lookup.json()["rows"]
            if len(rows) != 1:
                raise AssertionError(f"expected one matching live audit event, got {len(rows)}")
            event_id = rows[0]["event_id"]

        forbidden = requests.get(f"{base_url}/wonju-admin-api/audit/events", headers=headers, timeout=30)
        if forbidden.status_code != 403:
            raise AssertionError(f"resident audit listing must be forbidden, got {forbidden.status_code}")

        feedback = requests.post(
            f"{base_url}/wonju-admin-api/audit/events/{event_id}/feedback",
            headers=headers,
            json={"rating": "helpful", "comment": "실서비스 자동 검증"},
            timeout=30,
        )
        feedback.raise_for_status()

        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        summary_response = requests.get(
            f"{base_url}/wonju-admin-api/audit/summary", headers=admin_headers, timeout=30
        )
        summary_response.raise_for_status()
        events_response = requests.get(
            f"{base_url}/wonju-admin-api/audit/events",
            headers=admin_headers,
            params={"rating": "helpful", "q": "원주시보건소"},
            timeout=30,
        )
        events_response.raise_for_status()
        rows = events_response.json()["rows"]
        row = next((item for item in rows if item["event_id"] == event_id), None)
        if not row or row["feedback_rating"] != "helpful":
            raise AssertionError("the live feedback was not connected to its audit event")
        exported = requests.get(
            f"{base_url}/wonju-admin-api/audit/export.csv",
            headers=admin_headers,
            params={"rating": "helpful", "q": "원주시보건소"},
            timeout=30,
        )
        exported.raise_for_status()
        if event_id not in exported.text:
            raise AssertionError("the filtered CSV does not contain the verified event")

        report = {
            "base_url": base_url,
            "event_id": event_id,
            "resident_list_status": forbidden.status_code,
            "feedback_status": feedback.status_code,
            "admin_summary_status": summary_response.status_code,
            "admin_events_status": events_response.status_code,
            "csv_status": exported.status_code,
            "question_masked": row["question_text"],
            "feedback_rating": row["feedback_rating"],
            "audit_summary": summary_response.json(),
            "passed": True,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        delete_user(base_url, admin_token, temporary["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
