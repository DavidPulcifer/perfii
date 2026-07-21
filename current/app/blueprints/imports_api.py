import json

from flask import jsonify, request
from werkzeug.datastructures import MultiDict

from ..services.imports_service import (
    imported_fitids_request_response,
    manual_import_candidates_request_response,
)


IMPORT_SOURCE_REUPLOAD_MESSAGE = "This import review expired or could not be verified. Upload the statement again."


def request_payload_account_id(payload: dict) -> int | None:
    try:
        return int(payload.get("account_id") or 0) or None
    except (TypeError, ValueError):
        return None


def source_args_from_payload(payload: dict, account_id: int | None, get_import_review_source_func) -> tuple[dict, tuple[dict, int] | None]:
    token = str(payload.get("import_source_token") or "").strip()
    if not token:
        return {}, None
    if not account_id or not get_import_review_source_func:
        return {}, ({"ok": False, "error": IMPORT_SOURCE_REUPLOAD_MESSAGE}, 400)
    source = get_import_review_source_func(token, account_id)
    if not source:
        return {}, ({"ok": False, "error": IMPORT_SOURCE_REUPLOAD_MESSAGE}, 400)
    return {
        "source_bankid": source.get("source_bankid") or "",
        "source_acctid": source.get("source_acctid") or "",
    }, None


def register_import_api_routes(
    bp,
    *,
    list_transactions_func,
    get_transaction_func,
    list_imported_fitid_rows_func,
    list_import_provenance_matches_func=None,
    list_import_matched_transaction_ids_func=None,
    get_import_review_source_func=None,
) -> None:
    @bp.route('/manual-candidates', methods=['GET', 'POST'])
    def manual_candidates_api():
        """
        Return manual transactions for an account as JSON.

        The import rows used for auto-match suggestions may be large for QFX
        files, so POST accepts them in the JSON body. GET remains supported for
        older/static callers and tiny payloads.
        """
        args = request.args
        if request.method == 'POST':
            payload = request.get_json(silent=True) or {}
            account_id = request_payload_account_id(payload)
            _source, error = source_args_from_payload(payload, account_id, get_import_review_source_func)
            if error:
                body, status = error
                return jsonify(body), status
            args = MultiDict({
                "account_id": str(payload.get("account_id") or ""),
                "days": str(payload.get("days") or ""),
                "imports": json.dumps(payload.get("imports") or []),
            })
        return jsonify(manual_import_candidates_request_response(
            args,
            list_transactions_func=list_transactions_func,
            get_transaction_func=get_transaction_func,
            list_imported_fitid_rows_func=list_imported_fitid_rows_func,
            list_import_matched_transaction_ids_func=list_import_matched_transaction_ids_func,
        ))

    @bp.route('/dupes', methods=['GET', 'POST'])
    def dupes_api():
        """Return prior-import FITIDs, fuzzy transfer duplicates, and basic details."""
        args = request.args
        import_rows = None
        if request.method == 'POST':
            payload = request.get_json(silent=True) or {}
            account_id = request_payload_account_id(payload)
            source, error = source_args_from_payload(payload, account_id, get_import_review_source_func)
            if error:
                body, status = error
                return jsonify(body), status
            args = MultiDict({
                "account_id": str(payload.get("account_id") or ""),
                "source_bankid": str(source.get("source_bankid") or ""),
                "source_acctid": str(source.get("source_acctid") or ""),
            })
            import_rows = payload.get("imports") or []
        return jsonify(imported_fitids_request_response(
            args,
            list_imported_fitid_rows_func=list_imported_fitid_rows_func,
            import_rows=import_rows,
            list_transactions_func=list_transactions_func,
            get_transaction_func=get_transaction_func,
            list_import_provenance_matches_func=list_import_provenance_matches_func,
        ))
