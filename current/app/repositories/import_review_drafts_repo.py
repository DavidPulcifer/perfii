from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..db import get_db


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cleanup_expired_import_review_drafts(now: str | None = None) -> int:
    db = get_db()
    cur = db.execute(
        "DELETE FROM import_review_drafts WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now or _now(),),
    )
    db.commit()
    return int(cur.rowcount or 0)


def get_import_review_draft(fingerprint: str, account_id: int) -> dict | None:
    if not fingerprint or not account_id:
        return None
    db = get_db()
    row = db.execute(
        """
        SELECT id, fingerprint, account_id, source_type, source_filename, file_sha256,
               row_count, draft_json, created_at, updated_at, expires_at
        FROM import_review_drafts
        WHERE fingerprint=? AND account_id=?
          AND (expires_at IS NULL OR expires_at >= ?)
        """,
        (str(fingerprint), int(account_id), _now()),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["draft_json"] = _loads(data.get("draft_json"))
    return data


def save_import_review_draft(*, fingerprint: str, account_id: int, source_type: str, source_filename: str | None, file_sha256: str | None, row_count: int, draft_json: dict, expires_at: str) -> dict:
    now = _now()
    db = get_db()
    db.execute(
        """
        INSERT INTO import_review_drafts(
            fingerprint, account_id, source_type, source_filename, file_sha256,
            row_count, draft_json, created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            account_id=excluded.account_id,
            source_type=excluded.source_type,
            source_filename=excluded.source_filename,
            file_sha256=excluded.file_sha256,
            row_count=excluded.row_count,
            draft_json=excluded.draft_json,
            updated_at=excluded.updated_at,
            expires_at=excluded.expires_at
        """,
        (
            str(fingerprint),
            int(account_id),
            str(source_type or "unknown"),
            source_filename,
            file_sha256,
            int(row_count or 0),
            _json(draft_json),
            now,
            now,
            expires_at,
        ),
    )
    db.commit()
    return get_import_review_draft(fingerprint, account_id) or {}


def discard_import_review_draft(fingerprint: str, account_id: int | None = None) -> int:
    if not fingerprint:
        return 0
    db = get_db()
    if account_id:
        cur = db.execute(
            "DELETE FROM import_review_drafts WHERE fingerprint=? AND account_id=?",
            (str(fingerprint), int(account_id)),
        )
    else:
        cur = db.execute("DELETE FROM import_review_drafts WHERE fingerprint=?", (str(fingerprint),))
    db.commit()
    return int(cur.rowcount or 0)
