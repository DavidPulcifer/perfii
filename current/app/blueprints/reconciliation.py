from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.repositories import accounts_repo, reconciliation_repo
from app.services.reconciliation_service import ReconciliationService
from app.utils import parse_money_to_cents_strict

bp = Blueprint("reconciliation", __name__, url_prefix="/reconcile")

SUPPORTED_ACCOUNT_TYPES = {"bank", "credit_card"}


@bp.get("/accounts/<int:account_id>")
def account_reconcile(account_id: int):
    account = _require_account(account_id)
    if not _is_supported(account):
        flash("Reconciliation is available for bank and credit card accounts.", "warning")
        return redirect(url_for("accounts.account_dashboard", account_id=account_id))

    session = reconciliation_repo.latest_editable_session_for_account(account_id)
    if not session:
        return render_template("reconciliation_start.html", account=account)
    return redirect(url_for("reconciliation.session_detail", session_id=session["id"]))


@bp.post("/accounts/<int:account_id>/start")
def start_session(account_id: int):
    account = _require_account(account_id)
    if not _is_supported(account):
        flash("Reconciliation is available for bank and credit card accounts.", "warning")
        return redirect(url_for("accounts.account_dashboard", account_id=account_id))

    statement_date = (request.form.get("statement_date") or "").strip()
    try:
        if not statement_date:
            raise ValueError("Statement date is required.")
        statement_balance_cents = parse_money_to_cents_strict(
            request.form.get("statement_balance"),
            field_name="Statement balance",
        )
        session_id = ReconciliationService.create_session(
            account_id=account_id,
            statement_date=statement_date,
            statement_balance_cents=statement_balance_cents,
            label=(request.form.get("label") or "").strip() or None,
            note=(request.form.get("note") or "").strip() or None,
        )
    except ValueError as e:
        flash(str(e), "warning")
        return render_template("reconciliation_start.html", account=account), 400

    flash("Reconciliation started.", "success")
    return redirect(url_for("reconciliation.session_detail", session_id=session_id))


@bp.get("/sessions/<int:session_id>")
def session_detail(session_id: int):
    session = _require_session(session_id)
    account = _require_account(int(session["account_id"]))
    summary = ReconciliationService.compute(session_id)
    transactions = reconciliation_repo.list_candidate_transactions(
        int(session["account_id"]),
        session_id=session_id,
    )
    selected_ids = {int(row["id"]) for row in transactions if row.get("is_selected")}
    items = reconciliation_repo.list_items(session_id)
    return render_template(
        "reconciliation_session.html",
        account=account,
        session=session,
        summary=summary,
        transactions=transactions,
        selected_ids=selected_ids,
        items=items,
    )


@bp.post("/sessions/<int:session_id>/save")
def save_session(session_id: int):
    _require_session(session_id)
    try:
        ReconciliationService.update_session_metadata(
            session_id,
            statement_date=(request.form.get("statement_date") or "").strip() or None,
            statement_balance_cents=parse_money_to_cents_strict(
                request.form.get("statement_balance"),
                field_name="Statement balance",
            ),
            label=(request.form.get("label") or "").strip() or None,
            note=(request.form.get("note") or "").strip() or None,
        )
        tx_ids = [int(raw) for raw in request.form.getlist("transaction_id") if str(raw).strip()]
        ReconciliationService.set_cleared_transactions(session_id, tx_ids)
        flash("Reconciliation progress saved.", "success")
    except ValueError as e:
        flash(str(e), "warning")
    return redirect(url_for("reconciliation.session_detail", session_id=session_id))


@bp.post("/sessions/<int:session_id>/close")
def close_session(session_id: int):
    try:
        _save_posted_session_fields(session_id)
        ReconciliationService.close_session(session_id)
        flash("Reconciliation closed.", "success")
        session = _require_session(session_id)
        return redirect(url_for("reconciliation.history", account_id=session["account_id"]))
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(url_for("reconciliation.session_detail", session_id=session_id))


@bp.get("/accounts/<int:account_id>/history")
def history(account_id: int):
    account = _require_account(account_id)
    sessions = []
    for row in reconciliation_repo.list_sessions_for_account(account_id):
        row = dict(row)
        row["calculated_balance_cents"] = int(row["starting_balance_cents"] or 0) + int(row["selected_total_cents"] or 0)
        row["difference_cents"] = int(row["statement_balance_cents"] or 0) - row["calculated_balance_cents"]
        sessions.append(row)
    return render_template("reconciliation_history.html", account=account, sessions=sessions)


@bp.get("/sessions/<int:session_id>/history")
def history_detail(session_id: int):
    session = _require_session(session_id)
    account = _require_account(int(session["account_id"]))
    summary = ReconciliationService.compute(session_id)
    items = reconciliation_repo.list_items(session_id)
    return render_template(
        "reconciliation_detail.html",
        account=account,
        session=session,
        summary=summary,
        items=items,
    )


@bp.post("/sessions/<int:session_id>/reopen")
def reopen_session(session_id: int):
    try:
        ReconciliationService.reopen_session(session_id)
        flash("Reconciliation reopened.", "success")
    except ValueError as e:
        flash(str(e), "warning")
    return redirect(url_for("reconciliation.session_detail", session_id=session_id))


@bp.post("/sessions/<int:session_id>/void")
def void_session(session_id: int):
    session = _require_session(session_id)
    try:
        ReconciliationService.void_session(session_id)
        flash("Reconciliation voided.", "info")
    except ValueError as e:
        flash(str(e), "warning")
    return redirect(url_for("reconciliation.history", account_id=session["account_id"]))


def _save_posted_session_fields(session_id: int) -> None:
    if "statement_balance" not in request.form:
        return
    ReconciliationService.update_session_metadata(
        session_id,
        statement_date=(request.form.get("statement_date") or "").strip() or None,
        statement_balance_cents=parse_money_to_cents_strict(
            request.form.get("statement_balance"),
            field_name="Statement balance",
        ),
        label=(request.form.get("label") or "").strip() or None,
        note=(request.form.get("note") or "").strip() or None,
    )
    tx_ids = [int(raw) for raw in request.form.getlist("transaction_id") if str(raw).strip()]
    ReconciliationService.set_cleared_transactions(session_id, tx_ids)


def _require_account(account_id: int) -> dict:
    account = accounts_repo.get_account(account_id)
    if not account:
        abort(404)
    return account


def _require_session(session_id: int) -> dict:
    session = reconciliation_repo.get_session(session_id)
    if not session:
        abort(404)
    return session


def _is_supported(account: dict) -> bool:
    return (account.get("account_type") or "") in SUPPORTED_ACCOUNT_TYPES
