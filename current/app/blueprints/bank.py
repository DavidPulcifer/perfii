# app/blueprints/bank.py
from flask import Blueprint, render_template, abort, request, url_for
from urllib.parse import urlencode
from ..repositories import accounts_repo, aggregates_repo, envelopes_repo, transactions_repo

bp = Blueprint('bank', __name__)

@bp.get('/<int:account_id>', endpoint='bank_dashboard')
def bank_dashboard(account_id: int):
    acct = accounts_repo.get_account(account_id)
    if not acct or acct['account_type'] != 'bank':
        abort(404)

    # Use same source as Index so balances match
    totals = aggregates_repo.get_account_totals()           # { account_id: cents }
    current_balance_cents = int(totals.get(account_id, 0))

    # (Optional, light data many dashboards show)
    # Envelope balances on this account (kept minimal; template can ignore)
    acct_env_totals = aggregates_repo.get_account_envelope_balances()  # {(aid,eid): cents}
    all_envs = [
        envelope for envelope in envelopes_repo.list_envelopes()
        if envelope.get("locked_account_id") in (None, account_id)
    ]
    env_metrics = [
        {
            "envelope_id": e["id"],
            "envelope_name": e["name"],
            "balance_cents": int(acct_env_totals.get((account_id, e["id"]), 0)),
            "locked_account_id": e.get("locked_account_id"),
        }
        for e in all_envs
    ]

    per_page_options = [25, 50, 100]
    tx_per_page = request.args.get("per_page", type=int) or 25
    if tx_per_page not in per_page_options:
        tx_per_page = 25
    tx_page = request.args.get("page", type=int) or 1
    if tx_page < 1:
        tx_page = 1
    tx_offset = (tx_page - 1) * tx_per_page
    tx_rows, tx_total = transactions_repo.list_account_transactions_with_running_balance(
        account_id=account_id,
        limit=tx_per_page,
        offset=tx_offset,
    )

    def tx_page_url(page_number: int) -> str:
        q = []
        for key, values in request.args.lists():
            if key == "page":
                continue
            q.extend((key, value) for value in values)
        q.append(("page", str(page_number)))
        return url_for("bank.bank_dashboard", account_id=account_id) + "?" + urlencode(q)

    tx_prev_url = tx_page_url(tx_page - 1) if tx_page > 1 else None
    tx_next_url = tx_page_url(tx_page + 1) if (tx_offset + len(tx_rows)) < tx_total else None

    return render_template(
        'bank.html',
        acct=acct,
        current_balance_cents=current_balance_cents,
        env_metrics=env_metrics,
        recent_txs=tx_rows,
        txs=tx_rows,
        tx_total=tx_total,
        tx_page=tx_page,
        tx_per_page=tx_per_page,
        tx_per_page_options=per_page_options,
        tx_prev_url=tx_prev_url,
        tx_next_url=tx_next_url,
        account_id=account_id,
    )
