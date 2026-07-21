from __future__ import annotations

from datetime import datetime, timedelta

from flask import jsonify, request

from ..repositories.accounts_repo import get_account
from ..repositories.import_review_drafts_repo import (
    discard_import_review_draft,
    get_import_review_draft,
    save_import_review_draft,
)
from ..services.import_draft_service import draft_public_metadata


def _posted_int(value) -> int | None:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _valid_account(account_id: int | None) -> bool:
    return bool(account_id and get_account(account_id))


def register_import_draft_routes(bp) -> None:
    @bp.post('/draft/save')
    def save_import_review_draft_api():
        payload = request.get_json(silent=True) or {}
        fingerprint = str(payload.get("fingerprint") or "").strip()
        account_id = _posted_int(payload.get("account_id"))
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
        if not fingerprint or not _valid_account(account_id):
            return jsonify({"ok": False, "message": "Invalid import draft scope."}), 400
        saved = save_import_review_draft(
            fingerprint=fingerprint,
            account_id=account_id,
            source_type=str(payload.get("source_type") or "unknown"),
            source_filename=payload.get("source_filename"),
            file_sha256=payload.get("file_sha256"),
            row_count=int(payload.get("row_count") or 0),
            draft_json=draft,
            expires_at=str(payload.get("expires_at") or (datetime.utcnow() + timedelta(days=14)).isoformat(timespec="seconds")),
        )
        return jsonify({"ok": True, "draft": draft_public_metadata(saved)})

    @bp.post('/draft/discard')
    def discard_import_review_draft_api():
        payload = request.get_json(silent=True) or request.form or {}
        fingerprint = str(payload.get("fingerprint") or "").strip()
        account_id = _posted_int(payload.get("account_id"))
        if not fingerprint or not _valid_account(account_id):
            return jsonify({"ok": False, "message": "Invalid import draft scope."}), 400
        deleted = discard_import_review_draft(fingerprint, account_id)
        return jsonify({"ok": True, "deleted": deleted})

    @bp.get('/draft/<fingerprint>')
    def get_import_review_draft_api(fingerprint: str):
        account_id = _posted_int(request.args.get("account_id"))
        if not fingerprint or not _valid_account(account_id):
            return jsonify({"ok": False, "message": "Invalid import draft scope."}), 400
        return jsonify({"ok": True, "draft": draft_public_metadata(get_import_review_draft(fingerprint, account_id))})
