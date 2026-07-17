from __future__ import annotations

import bootstrap_permissions


class FakeApi:
    def __init__(self):
        self.calls = []
        self.groups = []
        self.users = {
            "users": [
                {"id": "dev-id", "email": "dev@example.com"},
                {"id": "old-id", "email": "old@example.com"},
            ]
        }

    def get(self, path):
        if path == "/api/v1/groups/":
            return self.groups
        if path == "/api/v1/users/all":
            return self.users
        raise AssertionError(path)

    def post(self, path, payload=None):
        self.calls.append((path, payload or {}))
        if path == "/api/v1/groups/create":
            group = {"id": "developer-group", "name": payload["name"]}
            self.groups.append(group)
            return group
        if path.endswith("/users"):
            return [{"id": "old-id", "email": "old@example.com"}]
        return {"ok": True}


def test_developer_group_membership_is_reconciled_to_environment(monkeypatch):
    monkeypatch.setenv("DEVELOPER_EMAILS", "dev@example.com")
    api = FakeApi()
    group = bootstrap_permissions.get_or_create_group(api)
    added, removed = bootstrap_permissions.reconcile_members(api, group)
    assert (added, removed) == (1, 1)
    assert (
        "/api/v1/groups/id/developer-group/users/add",
        {"user_ids": ["dev-id"]},
    ) in api.calls
    assert (
        "/api/v1/groups/id/developer-group/users/remove",
        {"user_ids": ["old-id"]},
    ) in api.calls


def test_general_and_raw_model_grants_are_disjoint(monkeypatch):
    monkeypatch.setenv("GENERAL_MODEL_ID", "wonju-health-rag")
    monkeypatch.setenv("RAW_MODEL_ID", "gemma-4-31b-nvfp4")
    api = FakeApi()
    bootstrap_permissions.reconcile_model_acl(api, {"id": "developer-group"})
    payloads = [payload for path, payload in api.calls if path.endswith("/model/access/update")]
    general = next(row for row in payloads if row["id"] == "wonju-health-rag")
    raw = next(row for row in payloads if row["id"] == "gemma-4-31b-nvfp4")
    assert general["access_grants"] == [
        {"principal_type": "user", "principal_id": "*", "permission": "read"}
    ]
    assert raw["access_grants"] == [
        {"principal_type": "group", "principal_id": "developer-group", "permission": "read"}
    ]
