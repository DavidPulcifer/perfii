from time import perf_counter
import base64

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from ..services.imports_service import (
    apply_csv_polarity,
    CsvColumnMappingRequired,
    csv_mapping_prompt_payload,
    detect_csv_credit_card_polarity,
    find_account_for_import,
    find_account_for_import_source,
    import_account_by_id,
    import_review_context,
    import_upload_context,
    normalize_csv_polarity,
    parse_statement_upload,
    parse_uploaded_statement_file,
)


def _posted_account_id(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw or raw.lower() == "auto":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _account_not_detected_message() -> str:
    return "We couldn't confidently detect which account this file belongs to. Choose the account and upload again."


def _elapsed_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)


def _csv_mapping_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.secret_key, salt="csv-import-column-mapping")


def _csv_mapping_token(data: bytes, filename: str) -> str:
    return _csv_mapping_serializer().dumps({
        "filename": filename,
        "data_b64": base64.b64encode(data).decode("ascii"),
    })


def _csv_mapping_upload_from_token(token: str) -> tuple[bytes, str]:
    try:
        payload = _csv_mapping_serializer().loads(token or "")
        return base64.b64decode(payload.get("data_b64") or "", validate=True), payload.get("filename") or "statement.csv"
    except (BadSignature, ValueError, TypeError) as ex:
        raise ValueError("The saved CSV upload expired or could not be verified. Upload the file again.") from ex


def _csv_mapping_from_form(form) -> dict[str, str | None]:
    return {
        "date": form.get("csv_date_col"),
        "amount": form.get("csv_amount_col"),
        "debit": form.get("csv_debit_col"),
        "credit": form.get("csv_credit_col"),
        "payee": form.get("csv_payee_col"),
        "memo": form.get("csv_memo_col"),
        "fitid": form.get("csv_fitid_col"),
    }


def _source_type(parsed: dict) -> str:
    return str(parsed.get("_source_type") or "").strip().lower()


def _prepare_csv_polarity_review(
    parsed: dict,
    *,
    upload_data: bytes,
    filename: str,
    form,
    account: dict | None,
) -> None:
    if _source_type(parsed) != "csv":
        return
    requested_polarity = normalize_csv_polarity(form.get("csv_polarity"))
    parsed["_csv_upload_token"] = _csv_mapping_token(upload_data, filename)
    parsed["_csv_polarity_suggestion"] = detect_csv_credit_card_polarity(parsed, account)
    apply_csv_polarity(parsed, requested_polarity)


def _render_csv_mapping(mapping_payload: dict, *, data: bytes, filename: str, selected_account_id: int | None, accounts: list[dict], error_message: str | None = None):
    return render_template(
        "import_csv_mapping.html",
        accounts=accounts,
        filename=filename,
        selected_account_id=selected_account_id,
        csv_mapping=mapping_payload,
        csv_upload_token=_csv_mapping_token(data, filename),
        error_message=error_message,
    )


def register_import_review_routes(
    bp,
    *,
    list_accounts_func,
    list_fitids_func,
    list_envelopes_func,
    account_envelope_balances_func=None,
    list_transactions_func=None,
    get_transaction_func=None,
    list_import_provenance_matches_func=None,
    get_import_review_draft_func=None,
    cleanup_import_review_drafts_func=None,
    create_import_review_source_func=None,
    cleanup_import_review_sources_func=None,
) -> None:
    @bp.get('/')
    def upload_form():
        return render_template('import.html', **import_upload_context(list_accounts_func=list_accounts_func))

    @bp.post('/detect-account')
    def detect_account():
        result = parse_uploaded_statement_file(request.files.get('statement'))
        if result.csv_mapping_required:
            return jsonify({
                "ok": True,
                "detected": False,
                "account_id": None,
                "account_name": None,
                "message": _account_not_detected_message(),
            })
        if not result.ok:
            return jsonify({
                "ok": False,
                "message": result.error_message,
            }), 400

        accounts = list_accounts_func()
        account = find_account_for_import(accounts, result.parsed)
        detection_source = find_account_for_import_source(accounts, result.parsed)
        payload = {
            "ok": True,
            "detected": account is not None,
            "account_id": account.get("id") if account else None,
            "account_name": account.get("name") if account else None,
        }
        if account:
            if detection_source == "filename":
                payload["message"] = f"Detected account from filename: {account.get('name')}."
            else:
                payload["message"] = f"Detected account: {account.get('name')}."
        else:
            payload["message"] = _account_not_detected_message()
        return jsonify(payload)

    @bp.post('/upload')
    def upload_and_review():
        timings: dict[str, int] = {}

        start = perf_counter()
        selected_account_id = _posted_account_id(request.form.get("account_id"))
        accounts = list_accounts_func()

        csv_upload_token = request.form.get("csv_upload_token")
        if csv_upload_token:
            try:
                upload_data, filename = _csv_mapping_upload_from_token(csv_upload_token)
            except Exception as ex:
                flash(str(ex), "warning")
                return redirect(url_for('imports.upload_form'))
            try:
                parsed = parse_statement_upload(upload_data, filename, csv_mapping=_csv_mapping_from_form(request.form))
                result = type("UploadResult", (), {"ok": True, "parsed": parsed, "error_message": None, "flash_category": "warning"})()
            except Exception as ex:
                from pathlib import Path
                import tempfile
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix.lower() or ".csv") as tmp:
                        tmp.write(upload_data)
                        tmp_path = Path(tmp.name)
                    payload = csv_mapping_prompt_payload(tmp_path, message="Fix the CSV column assignments before import review.")
                finally:
                    if tmp_path:
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass
                return _render_csv_mapping(
                    payload,
                    data=upload_data,
                    filename=filename,
                    selected_account_id=selected_account_id,
                    accounts=accounts,
                    error_message=str(ex),
                )
        else:
            upload = request.files.get('statement')
            filename = (getattr(upload, "filename", None) or "").strip() if upload else ""
            if not upload or not filename:
                flash("Please choose a QFX/OFX/CSV file.", "warning")
                return redirect(url_for('imports.upload_form'))
            upload_data = upload.read()
            if not upload_data:
                flash("Uploaded file is empty.", "warning")
                return redirect(url_for('imports.upload_form'))
            try:
                parsed = parse_statement_upload(upload_data, filename)
                result = type("UploadResult", (), {"ok": True, "parsed": parsed, "error_message": None, "flash_category": "warning"})()
            except CsvColumnMappingRequired as ex:
                timings["parse_ms"] = _elapsed_ms(start)
                return _render_csv_mapping(
                    ex.payload,
                    data=upload_data,
                    filename=filename,
                    selected_account_id=selected_account_id,
                    accounts=accounts,
                )
            except Exception as ex:
                flash(f"Could not parse file: {ex}", "danger")
                return redirect(url_for('imports.upload_form'))
        timings["parse_ms"] = _elapsed_ms(start)
        if selected_account_id is not None and import_account_by_id(accounts, selected_account_id) is None:
            flash("Choose a valid account for this import.", "warning")
            return render_template(
                'import.html',
                **import_upload_context(list_accounts_func=lambda: accounts),
            )
        if selected_account_id is None and find_account_for_import(accounts, result.parsed) is None:
            message = _account_not_detected_message()
            flash(message, "warning")
            return render_template(
                'import.html',
                **import_upload_context(
                    list_accounts_func=lambda: accounts,
                    account_detection_message=message,
                ),
            )
        review_account = (
            import_account_by_id(accounts, selected_account_id)
            if selected_account_id is not None
            else find_account_for_import(accounts, result.parsed)
        )
        _prepare_csv_polarity_review(
            result.parsed,
            upload_data=upload_data,
            filename=filename,
            form=request.form,
            account=review_account,
        )

        start = perf_counter()
        context = import_review_context(
            result.parsed,
            list_accounts_func=list_accounts_func,
            list_fitids_func=list_fitids_func,
            list_envelopes_func=list_envelopes_func,
            account_envelope_balances_func=account_envelope_balances_func,
            selected_account_id=selected_account_id,
            list_transactions_func=list_transactions_func,
            get_transaction_func=get_transaction_func,
            list_import_provenance_matches_func=list_import_provenance_matches_func,
            get_import_review_draft_func=get_import_review_draft_func,
            cleanup_import_review_drafts_func=cleanup_import_review_drafts_func,
            create_import_review_source_func=create_import_review_source_func,
            cleanup_import_review_sources_func=cleanup_import_review_sources_func,
            timings=timings,
        )
        timings["context_ms"] = _elapsed_ms(start)

        start = perf_counter()
        rendered = render_template("import_review.html", **context)
        timings["render_ms"] = _elapsed_ms(start)
        current_app.logger.info(
            "Import review timing: transactions=%s eligible_predictions=%s existing_fitids=%s prefills=%s "
            "parse_ms=%s accounts_ms=%s fitids_ms=%s transfer_dupes_ms=%s envelopes_ms=%s prefills_ms=%s context_ms=%s render_ms=%s",
            timings.get("transaction_count", 0),
            timings.get("prediction_eligible_count", 0),
            timings.get("existing_fitid_count", 0),
            timings.get("prefill_count", 0),
            timings.get("parse_ms", 0),
            timings.get("accounts_ms", 0),
            timings.get("fitids_ms", 0),
            timings.get("transfer_dupes_ms", 0),
            timings.get("envelopes_ms", 0),
            timings.get("prefills_ms", 0),
            timings.get("context_ms", 0),
            timings.get("render_ms", 0),
        )
        return rendered
