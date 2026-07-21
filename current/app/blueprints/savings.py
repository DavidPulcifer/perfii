from __future__ import annotations

import hashlib
import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..db import get_meta_db, unit_of_work
from ..repositories import accounts_repo, aggregates_repo, envelopes_repo, savings_repo
from ..services.savings_planner_service import (
    SavingsPlannerError,
    basis_points_label,
    calculate_preview,
    configuration_fingerprint,
    validate_configuration,
    validate_enabled_rule_total,
    validate_plan,
    validate_recommendation_freshness,
    validate_recording_recommendation,
    validate_rule,
)
from ..services.transactions_service import TransactionsService
from ..utils import parse_money_to_cents_strict


bp = Blueprint("savings", __name__)
PREVIEW_TOKEN_SALT = "pay-yourself-first-preview-v1"
PREVIEW_TOKEN_MAX_AGE_SECONDS = 2 * 60 * 60


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=PREVIEW_TOKEN_SALT)


def _lists_and_maps() -> tuple[list[dict], list[dict], dict[int, dict], dict[int, dict]]:
    all_accounts = accounts_repo.list_accounts()
    accounts = [account for account in all_accounts if account.get("account_type") == "bank"]
    envelopes = envelopes_repo.list_envelopes()
    return (
        accounts,
        envelopes,
        {int(account["id"]): account for account in all_accounts},
        {int(envelope["id"]): envelope for envelope in envelopes},
    )


def _render_payday(
    *,
    preview: dict | None = None,
    preview_token: str | None = None,
    recorded_indices: set[int] | None = None,
):
    accounts, envelopes, _accounts_by_id, _envelopes_by_id = _lists_and_maps()
    plan = savings_repo.get_plan()
    rules = savings_repo.list_rules()
    total_enabled_basis_points = sum(
        int(rule.get("contribution_basis_points") or 0)
        for rule in rules
        if int(rule.get("enabled", 1)) == 1
    )
    return render_template(
        "savings_payday.html",
        plan=plan,
        rules=rules,
        savings_accounts=accounts,
        savings_envelopes=envelopes,
        total_enabled_basis_points=total_enabled_basis_points,
        preview=preview,
        preview_token=preview_token,
        recorded_indices=recorded_indices or set(),
        default_posted_at=(preview or {}).get("posted_at") or date.today().isoformat(),
        take_home_value=_money_input_value((preview or {}).get("take_home_cents")),
    )


def _money_input_value(cents: int | None) -> str:
    if cents is None:
        return ""
    amount = int(cents)
    sign = "-" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), 100)
    return f"{sign}{whole}.{fraction:02d}"


def _parse_percentage_basis_points(raw: str | None) -> int:
    try:
        percent = Decimal(str(raw or "").strip())
    except (InvalidOperation, ValueError):
        raise SavingsPlannerError("Savings percentage must be a number.")
    if not percent.is_finite() or percent <= 0 or percent > 100:
        raise SavingsPlannerError("Savings percentage must be greater than 0% and no more than 100%.")
    basis_points = percent * Decimal(100)
    if basis_points != basis_points.to_integral_value():
        raise SavingsPlannerError("Savings percentage can have at most two decimal places.")
    return int(basis_points)


def _optional_int(raw: str | None) -> int | None:
    raw = str(raw or "").strip()
    return int(raw) if raw else None


def _rule_from_form(*, existing: dict | None = None) -> dict:
    target_cents = parse_money_to_cents_strict(
        request.form.get("accessible_target"),
        field_name="Accessible savings target",
        allow_blank=True,
    )
    if target_cents < 0:
        raise SavingsPlannerError("The accessible savings target cannot be negative.")
    rule = {
        "id": (existing or {}).get("id", 0),
        "name": (request.form.get("name") or "").strip(),
        "contribution_basis_points": _parse_percentage_basis_points(request.form.get("percentage")),
        "accessible_account_id": request.form.get("accessible_account_id", type=int),
        "accessible_envelope_id": request.form.get("accessible_envelope_id", type=int),
        "long_term_account_id": _optional_int(request.form.get("long_term_account_id")),
        "long_term_envelope_id": _optional_int(request.form.get("long_term_envelope_id")),
        "accessible_target_cents": target_cents,
        "enabled": 1 if request.form.get("enabled") == "1" else 0,
        "display_order": int((existing or {}).get("display_order") or 0),
    }
    return rule


def _validate_rule_change(candidate: dict, *, replace_rule_id: int | None = None) -> None:
    plan = savings_repo.get_plan()
    _accounts, _envelopes, accounts_by_id, envelopes_by_id = _lists_and_maps()
    validate_plan(plan, accounts_by_id=accounts_by_id, envelopes_by_id=envelopes_by_id)
    validate_rule(
        candidate,
        source_account_id=int(plan["source_account_id"]),
        accounts_by_id=accounts_by_id,
        envelopes_by_id=envelopes_by_id,
    )
    proposed_rules = [
        rule
        for rule in savings_repo.list_rules()
        if replace_rule_id is None or int(rule["id"]) != int(replace_rule_id)
    ]
    proposed_rules.append(candidate)
    validate_enabled_rule_total(proposed_rules)


def _recommendation_key(token: str, group_index: int) -> str:
    return hashlib.sha256(f"{token}:{int(group_index)}".encode("utf-8")).hexdigest()


def _current_ledger_binding() -> dict:
    """Return a non-reversible binding for the selected user and ledger path."""
    raw_user_id = session.get("user_id")
    user_id = int(raw_user_id) if raw_user_id is not None else 0
    db_path = Path(current_app.config["DB_PATH"])
    if user_id:
        row = get_meta_db().execute(
            "SELECT db_path FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if row and row["db_path"]:
            db_path = Path(row["db_path"])
    normalized_path = str(db_path.expanduser().resolve()).casefold()
    return {
        "user_id": user_id,
        "ledger_fingerprint": hashlib.sha256(normalized_path.encode("utf-8")).hexdigest(),
    }


def _recorded_indices(token: str, count: int) -> set[int]:
    keys_by_index = {
        index: _recommendation_key(token, index)
        for index in range(count)
    }
    used = savings_repo.recorded_transfer_keys(list(keys_by_index.values()))
    return {
        index
        for index, key in keys_by_index.items()
        if key in used
    }


@bp.get("/")
def payday():
    return _render_payday()


@bp.post("/plan")
def save_plan():
    name = (request.form.get("name") or "Pay Yourself First").strip() or "Pay Yourself First"
    source_account_id = request.form.get("source_account_id", type=int)
    source_envelope_id = request.form.get("source_envelope_id", type=int)
    candidate = {
        "name": name,
        "source_account_id": source_account_id,
        "source_envelope_id": source_envelope_id,
    }
    _accounts, _envelopes, accounts_by_id, envelopes_by_id = _lists_and_maps()
    try:
        validate_plan(candidate, accounts_by_id=accounts_by_id, envelopes_by_id=envelopes_by_id)
        existing_rules = savings_repo.list_rules()
        for rule in existing_rules:
            validate_rule(
                rule,
                source_account_id=int(source_account_id),
                accounts_by_id=accounts_by_id,
                envelopes_by_id=envelopes_by_id,
            )
        validate_enabled_rule_total(existing_rules)
        savings_repo.save_plan(
            name=name,
            source_account_id=int(source_account_id),
            source_envelope_id=int(source_envelope_id),
        )
        flash("Paycheck source saved.", "success")
    except (SavingsPlannerError, TypeError, ValueError) as exc:
        flash(str(exc), "warning")
    return redirect(url_for("savings.payday"))


@bp.post("/rules")
def create_rule():
    try:
        with unit_of_work(immediate=True) as db:
            candidate = _rule_from_form()
            _validate_rule_change(candidate)
            savings_repo.insert_rule(candidate, db=db)
        flash("Savings rule created.", "success")
    except (SavingsPlannerError, TypeError, ValueError) as exc:
        flash(str(exc), "warning")
    return redirect(url_for("savings.payday"))


@bp.post("/rules/<int:rule_id>")
def update_rule(rule_id: int):
    autosave = request.headers.get("X-Savings-Autosave") == "1"
    try:
        with unit_of_work(immediate=True) as db:
            existing = savings_repo.get_rule(rule_id)
            if not existing:
                if autosave:
                    return jsonify(ok=False, error="Savings rule not found."), 404
                flash("Savings rule not found.", "warning")
                return redirect(url_for("savings.payday"))
            candidate = _rule_from_form(existing=existing)
            _validate_rule_change(candidate, replace_rule_id=rule_id)
            savings_repo.update_rule(rule_id, candidate, db=db)
            updated = savings_repo.get_rule(rule_id) or candidate
            rules = savings_repo.list_rules()
            total_basis_points = sum(
                int(rule.get("contribution_basis_points") or 0)
                for rule in rules
                if int(rule.get("enabled", 1)) == 1
            )
        if autosave:
            return jsonify(
                ok=True,
                rule={
                    "id": int(rule_id),
                    "name": updated["name"],
                    "contribution_basis_points": int(updated["contribution_basis_points"]),
                    "enabled": bool(updated["enabled"]),
                },
                total_basis_points=total_basis_points,
                total_percent=basis_points_label(total_basis_points),
            )
        flash("Savings rule updated.", "success")
    except (SavingsPlannerError, TypeError, ValueError) as exc:
        if autosave:
            return jsonify(ok=False, error=str(exc)), 400
        flash(str(exc), "warning")
    return redirect(url_for("savings.payday"))


@bp.post("/rules/<int:rule_id>/delete")
def delete_rule(rule_id: int):
    savings_repo.delete_rule(rule_id)
    flash("Savings rule deleted.", "info")
    return redirect(url_for("savings.payday"))


@bp.post("/preview")
def preview():
    plan = savings_repo.get_plan()
    rules = savings_repo.list_rules()
    _accounts, _envelopes, accounts_by_id, envelopes_by_id = _lists_and_maps()
    try:
        take_home_cents = parse_money_to_cents_strict(
            request.form.get("take_home"),
            field_name="Take-home pay",
        )
        posted_at = date.fromisoformat(request.form.get("posted_at") or "").isoformat()
        preview_data = calculate_preview(
            take_home_cents=take_home_cents,
            posted_at=posted_at,
            plan=plan,
            rules=rules,
            accounts_by_id=accounts_by_id,
            envelopes_by_id=envelopes_by_id,
            account_envelope_balances=aggregates_repo.get_account_envelope_balances(),
        )
        token = _serializer().dumps(
            {
                "preview": preview_data,
                "preview_nonce": secrets.token_urlsafe(18),
                "ledger_binding": _current_ledger_binding(),
                "configuration_fingerprint": configuration_fingerprint(plan or {}, rules),
            }
        )
        return _render_payday(
            preview=preview_data,
            preview_token=token,
            recorded_indices=_recorded_indices(token, len(preview_data["recommendations"])),
        )
    except (SavingsPlannerError, ValueError) as exc:
        flash(str(exc), "warning")
        return redirect(url_for("savings.payday"))


@bp.post("/record")
def record_recommendation():
    token = request.form.get("preview_token") or ""
    group_index = request.form.get("group_index", type=int)
    preview_data = None
    recommendations: list[dict] = []
    recorded_message = None
    already_recorded = False
    try:
        payload = _serializer().loads(token, max_age=PREVIEW_TOKEN_MAX_AGE_SECONDS)
        if payload.get("ledger_binding") != _current_ledger_binding():
            raise SavingsPlannerError(
                "That savings preview belongs to a different user or ledger. Create a fresh preview."
            )
        preview_data = payload["preview"]
        recommendations = preview_data["recommendations"]
        if group_index is None or group_index < 0 or group_index >= len(recommendations):
            raise SavingsPlannerError("Choose a valid reviewed savings transfer.")

        key = _recommendation_key(token, group_index)
        with unit_of_work(immediate=True) as db:
            plan = savings_repo.get_plan()
            rules = savings_repo.list_rules()
            _accounts, _envelopes, accounts_by_id, envelopes_by_id = _lists_and_maps()
            if payload.get("configuration_fingerprint") != configuration_fingerprint(plan or {}, rules):
                raise SavingsPlannerError(
                    "Savings settings changed after this preview. Create a fresh preview before recording."
                )
            validate_configuration(
                plan,
                rules,
                accounts_by_id=accounts_by_id,
                envelopes_by_id=envelopes_by_id,
            )
            recommendation = recommendations[group_index]
            posted_at = date.fromisoformat(preview_data["posted_at"]).isoformat()
            amount_cents = int(recommendation["amount_cents"])
            if not savings_repo.reserve_transfer_record(
                db=db,
                idempotency_key=key,
                group_index=group_index,
            ):
                recorded_message = "That reviewed savings transfer was already recorded."
                already_recorded = True
            else:
                current_preview = calculate_preview(
                    take_home_cents=int(preview_data["take_home_cents"]),
                    posted_at=posted_at,
                    plan=plan,
                    rules=rules,
                    accounts_by_id=accounts_by_id,
                    envelopes_by_id=envelopes_by_id,
                    account_envelope_balances=aggregates_repo.get_account_envelope_balances(),
                )
                validate_recommendation_freshness(
                    recommendation,
                    current_preview=current_preview,
                )
                validate_recording_recommendation(
                    recommendation,
                    plan=plan or {},
                    accounts_by_id=accounts_by_id,
                    envelopes_by_id=envelopes_by_id,
                )
                tx_out_id, tx_in_id = TransactionsService.create_transfer(
                    {
                        "from_account_id": int(recommendation["source_account_id"]),
                        "to_account_id": int(recommendation["destination_account_id"]),
                        "amount_cents": amount_cents,
                        "posted_at": posted_at,
                        "memo": recommendation.get("memo") or "Pay Yourself First",
                    },
                    out_splits=[
                        {
                            "envelope_id": int(recommendation["source_envelope_id"]),
                            "amount_cents": -amount_cents,
                        }
                    ],
                    in_splits=[
                        {
                            "envelope_id": int(split["envelope_id"]),
                            "amount_cents": int(split["amount_cents"]),
                        }
                        for split in recommendation["destination_splits"]
                    ],
                    db=db,
                )
                savings_repo.complete_transfer_record(
                    db=db,
                    idempotency_key=key,
                    tx_out_id=tx_out_id,
                    tx_in_id=tx_in_id,
                )
                recorded_message = (
                    f"Recorded {recommendation['destination_account_name']} savings transfer."
                )
    except SignatureExpired:
        flash("That savings preview expired. Create a fresh preview before recording.", "warning")
    except BadSignature:
        flash("That savings preview could not be verified. Create a fresh preview.", "warning")
    except (SavingsPlannerError, KeyError, TypeError, ValueError) as exc:
        flash(str(exc), "warning")
    except Exception:
        current_app.logger.exception("Could not record reviewed savings transfer")
        flash("The savings transfer could not be recorded. No partial transfer was saved.", "danger")
    else:
        flash(recorded_message, "info" if already_recorded else "success")
        return _render_payday(
            preview=preview_data,
            preview_token=token,
            recorded_indices=_recorded_indices(token, len(recommendations)),
        )
    return redirect(url_for("savings.payday"))
