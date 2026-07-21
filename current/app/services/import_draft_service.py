from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

DRAFT_TTL_DAYS = 14


def _fingerprint_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _source_type(parsed: dict) -> str:
    return str(parsed.get("_source_type") or "unknown").strip().lower() or "unknown"


def _source_filename(parsed: dict) -> str:
    return str(parsed.get("_source_filename") or "").strip()


def _file_sha256(parsed: dict) -> str:
    return str(parsed.get("file_hash") or "").strip().lower()


def _import_amount_cents(row: dict) -> int:
    from .imports_service import import_transaction_amount_cents

    return import_transaction_amount_cents(row)


def import_draft_row_fingerprint(row: dict, row_index: int) -> str:
    payload = {
        "v": 1,
        "row_index": int(row_index),
        "posted_at": str(row.get("posted_at") or "").strip(),
        "amount_cents": _import_amount_cents(row),
        "fitid": str(row.get("fitid") or row.get("refnum") or row.get("checknum") or "").strip(),
        "payee": _fingerprint_text(row.get("payee") or row.get("name")),
        "memo": _fingerprint_text(row.get("memo")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def import_draft_row_fingerprints(transactions: list[dict]) -> list[dict]:
    return [
        {"row_index": idx, "fingerprint": import_draft_row_fingerprint(row, idx)}
        for idx, row in enumerate(transactions or [])
    ]


def build_import_draft_identity(parsed: dict, account_id: int | None) -> dict:
    rows = import_draft_row_fingerprints(parsed.get("transactions") or [])
    row_fingerprints = [row["fingerprint"] for row in rows]
    payload = {
        "v": 1,
        "source_type": _source_type(parsed),
        "file_sha256": _file_sha256(parsed),
        "account_id": int(account_id) if account_id is not None else None,
        "rows": row_fingerprints,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    now = datetime.utcnow()
    return {
        "fingerprint": fingerprint,
        "account_id": int(account_id) if account_id is not None else None,
        "source_type": payload["source_type"],
        "source_filename": _source_filename(parsed),
        "file_sha256": payload["file_sha256"],
        "row_count": len(rows),
        "row_fingerprints": rows,
        "expires_at": (now + timedelta(days=DRAFT_TTL_DAYS)).isoformat(timespec="seconds"),
    }


def draft_public_metadata(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "fingerprint": row.get("fingerprint"),
        "account_id": row.get("account_id"),
        "source_type": row.get("source_type"),
        "source_filename": row.get("source_filename"),
        "row_count": row.get("row_count"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "draft": row.get("draft_json") or {},
    }
