"""Privacy-conscious runtime audit storage for the Wonju resident service."""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt


UTC = timezone.utc
RATING_VALUES = {"helpful", "unhelpful"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def redact_question(value: str) -> str:
    """Retain operational meaning without storing obvious direct identifiers."""
    text = " ".join((value or "").split())
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[이메일]", text)
    text = re.sub(r"\b\d{6}\s*[- ]?\s*[1-4]\d{6}\b", "[주민번호]", text)
    text = re.sub(r"\b(?:01[016789]|0\d{1,2})[- .]?\d{3,4}[- .]?\d{4}\b", "[전화번호]", text)
    return text[:500]


class AuditStore:
    def __init__(self, path: Path, *, hash_salt: str, retention_days: int = 30) -> None:
        self.path = path
        self.hash_salt = hash_salt or "wonju-health-audit"
        self.retention_days = max(1, retention_days)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_env(cls) -> "AuditStore":
        configured = os.getenv("P1_AUDIT_DB_PATH")
        path = Path(configured) if configured else Path(tempfile.gettempdir()) / "wonju-health-audit.sqlite3"
        return cls(
            path,
            hash_salt=os.getenv("P1_AUDIT_HASH_SALT", "wonju-health-audit"),
            retention_days=int(os.getenv("P1_AUDIT_RETENTION_DAYS", "30")),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self.lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    user_role TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success', 'failure')),
                    risk_category TEXT NOT NULL,
                    response_kind TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    institution_count INTEGER NOT NULL,
                    citation_count INTEGER NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    feedback_rating TEXT NOT NULL DEFAULT '',
                    feedback_comment TEXT NOT NULL DEFAULT '',
                    feedback_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS audit_created_at_idx ON audit_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS audit_status_idx ON audit_events(status);
                CREATE INDEX IF NOT EXISTS audit_risk_idx ON audit_events(risk_category);
                CREATE INDEX IF NOT EXISTS audit_feedback_idx ON audit_events(feedback_rating);
                """
            )
            cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
            connection.execute("DELETE FROM audit_events WHERE created_at < ?", (cutoff,))

    def user_hash(self, identity: str | None) -> str:
        normalized = (identity or "anonymous").strip().casefold()
        return hashlib.sha256(f"{self.hash_salt}:{normalized}".encode("utf-8")).hexdigest()[:20]

    def record(
        self,
        *,
        event_id: str,
        user_identity: str | None,
        user_role: str,
        question: str,
        status: str,
        risk_category: str = "none",
        response_kind: str = "answer",
        duration_ms: int = 0,
        institution_count: int = 0,
        citation_count: int = 0,
        error_code: str = "",
    ) -> None:
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO audit_events (
                    event_id, created_at, completed_at, user_hash, user_role, question_text,
                    status, risk_category, response_kind, duration_ms, institution_count,
                    citation_count, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, now, now, self.user_hash(user_identity), user_role or "user",
                    redact_question(question), status, risk_category or "none",
                    response_kind or "answer", max(0, int(duration_ms)),
                    max(0, int(institution_count)), max(0, int(citation_count)), error_code[:80],
                ),
            )

    def set_feedback(
        self,
        event_id: str,
        *,
        actor_identity: str,
        actor_role: str,
        rating: str,
        comment: str = "",
    ) -> bool:
        if rating not in RATING_VALUES:
            raise ValueError("invalid feedback rating")
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT user_hash FROM audit_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if not row:
                return False
            belongs_to_actor = row["user_hash"] == self.user_hash(actor_identity)
            was_forwarded_anonymously = row["user_hash"] == self.user_hash("anonymous")
            if actor_role != "admin" and not (belongs_to_actor or was_forwarded_anonymously):
                raise PermissionError("feedback event does not belong to the current user")
            connection.execute(
                """UPDATE audit_events SET feedback_rating = ?, feedback_comment = ?, feedback_at = ?
                   WHERE event_id = ?""",
                (rating, redact_question(comment)[:300], utc_now(), event_id),
            )
            return True

    @staticmethod
    def _filters(status: str, risk: str, rating: str, query: str) -> tuple[str, list[str]]:
        clauses: list[str] = []
        values: list[str] = []
        for column, value in (("status", status), ("risk_category", risk), ("feedback_rating", rating)):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if query:
            clauses.append("question_text LIKE ?")
            values.append(f"%{query[:80]}%")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    def list_events(
        self,
        *,
        status: str = "",
        risk: str = "",
        rating: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where, values = self._filters(status, risk, rating, query)
        limit = min(500, max(1, limit))
        offset = max(0, offset)
        with self.lock, self._connect() as connection:
            total = connection.execute(f"SELECT count(*) FROM audit_events{where}", values).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM audit_events{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {"total": total, "rows": [dict(row) for row in rows], "limit": limit, "offset": offset}

    def summary(self) -> dict[str, int]:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                """SELECT count(*) AS total,
                    sum(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                    sum(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure,
                    sum(CASE WHEN feedback_rating='helpful' THEN 1 ELSE 0 END) AS helpful,
                    sum(CASE WHEN feedback_rating='unhelpful' THEN 1 ELSE 0 END) AS unhelpful,
                    sum(CASE WHEN risk_category!='none' THEN 1 ELSE 0 END) AS high_risk
                    FROM audit_events"""
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def export_csv(self, **filters: str) -> str:
        payload = self.list_events(limit=500, offset=0, **filters)
        fields = [
            "event_id", "created_at", "user_hash", "user_role", "question_text", "status",
            "risk_category", "response_kind", "duration_ms", "institution_count",
            "citation_count", "error_code", "feedback_rating", "feedback_comment", "feedback_at",
        ]
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["rows"])
        return output.getvalue()


def decode_webui_token(token: str, secret: str) -> dict[str, Any]:
    if not token or not secret:
        raise PermissionError("missing Open WebUI credential")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})
    except jwt.PyJWTError as exc:
        raise PermissionError("invalid Open WebUI credential") from exc
    if not payload.get("id"):
        raise PermissionError("Open WebUI user id is missing")
    return payload
