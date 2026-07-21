# app/blueprints/core.py
from flask import Blueprint, render_template
from ..repositories.accounts_repo import list_accounts
from ..repositories.envelopes_repo import list_envelopes
from ..repositories.aggregates_repo import (
    get_account_totals,
    get_envelope_totals,
    get_account_envelope_balances,
    account_envelope_balances_json,
)
from ..repositories import credit_repo, invest_repo
from ..services.credit_availability_service import get_credit_budget_metrics

bp = Blueprint('core', __name__)


def _build_dashboard_unallocated(accounts, account_totals, balances):
    credit_limits = credit_repo.get_credit_limits()
    by_account = {}
    cash_unallocated_cents = 0
    credit_available_to_allocate_cents = 0

    for account in accounts:
        account_id = int(account["id"])
        account_type = account.get("account_type")
        account_total = int(account_totals.get(account_id, 0))
        envelope_total = sum(
            int(total or 0)
            for (aid, _eid), total in balances.items()
            if int(aid) == account_id
        )

        if account_type == "bank":
            amount = account_total - envelope_total
            cash_unallocated_cents += amount
            by_account[account_id] = {
                "label": "Cash unallocated",
                "amount_cents": amount,
                "kind": "cash",
            }
        elif account_type == "credit_card":
            credit_metrics = get_credit_budget_metrics(
                credit_limit_cents=credit_limits.get(account_id, 0),
                account_total_cents=account_total,
                envelope_balance_cents=envelope_total,
            )
            available = credit_metrics["available_to_allocate_cents"]
            credit_available_to_allocate_cents += available
            by_account[account_id] = {
                "label": "Credit capacity to allocate (not cash)",
                "amount_cents": available,
                "kind": "credit",
            }

    return {
        "cash_unallocated_cents": cash_unallocated_cents,
        "credit_available_to_allocate_cents": credit_available_to_allocate_cents,
        "by_account": by_account,
    }


@bp.get('/')
def index():
    accounts = list_accounts()     
    envelopes = list_envelopes()    

    account_totals = get_account_totals()
    balances = get_account_envelope_balances()

    for a in accounts:
        if a.get("account_type") == "investment":
            latest = invest_repo.get_latest_valuation_cents(a["id"])
            account_totals[a["id"]] = int(latest or 0)
            
    return render_template(
        "index.html",
        accounts=accounts,
        envelopes=envelopes,
        account_totals=account_totals,
        balances_json=account_envelope_balances_json(balances),
        envelopes_json=envelopes,
    )


def _build_dashboard_balance_panel_model(accounts, envelopes, envelope_totals, balances, dashboard_unallocated):
    accounts_sorted = sorted(accounts, key=lambda a: (a.get("name") or ""))
    envelopes_sorted = sorted(envelopes, key=lambda e: (e.get("name") or "", e.get("id") or 0))

    master_by_name = {}
    envelopes_by_name = {}
    for envelope in envelopes_sorted:
        name = envelope.get("name")
        if not name:
            continue
        master_by_name[name] = master_by_name.get(name, 0) + int(envelope_totals.get(envelope["id"], 0) or 0)
        envelopes_by_name.setdefault(name, []).append(envelope)

    master_rows = []
    for name in sorted(master_by_name):
        matching_envelopes = envelopes_by_name[name]
        account_rows = []
        for account in accounts_sorted:
            total = 0
            has_envelope = False
            account_id = account["id"]
            for envelope in matching_envelopes:
                locked_account_id = envelope.get("locked_account_id")
                if not locked_account_id or locked_account_id == account_id:
                    has_envelope = True
                    total += int(balances.get((account_id, envelope["id"]), 0) or 0)
            if has_envelope:
                account_rows.append({"account": account, "total": total})
        master_rows.append({"name": name, "total": master_by_name[name], "accounts": account_rows})

    dashboard_unallocated_by_account = dashboard_unallocated.get("by_account", {}) if dashboard_unallocated else {}
    account_rows = []
    for account in accounts_sorted:
        account_id = account["id"]
        envelope_rows = []
        for envelope in envelopes_sorted:
            locked_account_id = envelope.get("locked_account_id")
            if not locked_account_id or locked_account_id == account_id:
                envelope_rows.append({
                    "envelope": envelope,
                    "balance": int(balances.get((account_id, envelope["id"]), 0) or 0),
                })
        account_rows.append({
            "account": account,
            "unallocated": dashboard_unallocated_by_account.get(account_id),
            "envelopes": envelope_rows,
        })

    return {"master_rows": master_rows, "account_rows": account_rows}


@bp.get('/dashboard/balances')
def dashboard_balance_panels():
    accounts = list_accounts()
    envelopes = list_envelopes()

    account_totals = get_account_totals()
    for a in accounts:
        if a.get("account_type") == "investment":
            latest = invest_repo.get_latest_valuation_cents(a["id"])
            account_totals[a["id"]] = int(latest or 0)

    envelope_totals = get_envelope_totals()
    balances = get_account_envelope_balances()
    balances_list = [
        {"account_id": aid, "envelope_id": eid, "total": total}
        for (aid, eid), total in balances.items()
    ]
    dashboard_unallocated = _build_dashboard_unallocated(accounts, account_totals, balances)

    panel_model = _build_dashboard_balance_panel_model(
        accounts, envelopes, envelope_totals, balances, dashboard_unallocated
    )

    return render_template(
        "_dashboard_balance_panels.html",
        accounts=accounts,
        dashboard_unallocated=dashboard_unallocated,
        balances_list=balances_list,
        master_rows=panel_model["master_rows"],
        account_rows=panel_model["account_rows"],
    )
