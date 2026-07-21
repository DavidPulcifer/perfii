from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from urllib.parse import urlencode
from ..repositories import credit_repo, accounts_repo, envelopes_repo, aggregates_repo, transactions_repo
from ..services.credit_availability_service import get_credit_budget_metrics
from ..services.transactions_service import TransactionsService
from ..utils import parse_money_to_cents_strict
from datetime import datetime, timedelta
import math

bp = Blueprint('credit', __name__)


@bp.get('/<int:account_id>', endpoint='credit_card_dashboard')
def credit_card_dashboard(account_id: int):
    acct = accounts_repo.get_account(account_id)
    if not acct or acct['account_type'] != 'credit_card':
        abort(404)

    credit_limit = credit_repo.get_credit_limit(account_id) or 0

    # === Use the SAME totals source as Index ===
    totals = aggregates_repo.get_account_totals()
    acct_total = int(totals.get(account_id, 0))

    # Payments/credits ("income" to the card): use ALL positive transactions, not only transfer_in
    # for the payoff estimate. We keep signs.
    tx_rows, _ = transactions_repo.list_transactions(
        limit=100000, offset=0, account_id=account_id, use_abs=False
    )
    cutoff = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
    paid_last_30 = sum(
        int(t['amount_cents']) for t in tx_rows
        if int(t['amount_cents']) > 0 and t.get('posted_at') and str(t['posted_at']) >= cutoff
    )
    # === Envelopes locked to this card ===
    all_envs = envelopes_repo.list_envelopes()
    cc_envs = [e for e in all_envs if e.get('locked_account_id') == account_id]

    # Compute envelope balances for just this card using the same aggregate the Index uses
    acct_env_balances = aggregates_repo.get_account_envelope_balances()  # {(account_id, envelope_id): total_cents}
    env_metrics = []
    sum_env_balances = 0
    for e in cc_envs:
        bal = int(acct_env_balances.get((account_id, e['id']), 0))
        env_metrics.append({
            'envelope_id': e['id'],
            'envelope_name': e['name'],
            'balance_cents': bal,
        })
        # For "Available to Allocate" we include the envelope balances on this card.
        # If you prefer to include only positive balances, change to: max(0, bal)
        sum_env_balances += bal

    credit_metrics = get_credit_budget_metrics(
        credit_limit_cents=credit_limit,
        account_total_cents=acct_total,
        envelope_balance_cents=sum_env_balances,
    )
    owed_total_cents = credit_metrics["owed_total_cents"]
    credit_balance_cents = credit_metrics["credit_balance_cents"]
    available_cents = credit_metrics["available_to_allocate_cents"]
    months_to_zero = math.ceil(owed_total_cents / paid_last_30) if paid_last_30 > 0 else None
    alloc_envelopes = [e for e in all_envs if e.get('locked_account_id') in (None, account_id)]
    from_envelopes = []
    for envelope in cc_envs:
        item = dict(envelope)
        item['account_name'] = acct.get('name') or str(account_id)
        item['balance_cents'] = int(acct_env_balances.get((int(account_id), envelope['id']), 0))
        from_envelopes.append(item)

    envelope_balances_json = {}
    for (balance_account_id, envelope_id), cents in acct_env_balances.items():
        envelope_balances_json.setdefault(str(balance_account_id), {})[str(envelope_id)] = int(cents or 0)

    per_page_options = [25, 50, 100]
    tx_per_page = request.args.get('per_page', type=int) or 25
    if tx_per_page not in per_page_options:
        tx_per_page = 25
    tx_page = request.args.get('page', type=int) or 1
    if tx_page < 1:
        tx_page = 1
    tx_offset = (tx_page - 1) * tx_per_page
    running_txs, tx_total = transactions_repo.list_account_transactions_with_running_balance(
        account_id=account_id,
        limit=tx_per_page,
        offset=tx_offset,
    )

    def tx_page_url(page_number: int) -> str:
        q = []
        for key, values in request.args.lists():
            if key == 'page':
                continue
            q.extend((key, value) for value in values)
        q.append(('page', str(page_number)))
        return url_for('credit.credit_card_dashboard', account_id=account_id) + '?' + urlencode(q)

    tx_prev_url = tx_page_url(tx_page - 1) if tx_page > 1 else None
    tx_next_url = tx_page_url(tx_page + 1) if (tx_offset + len(running_txs)) < tx_total else None

    return render_template(
        'credit_card.html',
        acct=acct,
        account_total_cents=acct_total,
        owed_total_cents=owed_total_cents,
        credit_balance_cents=credit_balance_cents,
        credit_limit=credit_limit,
        available_cents=available_cents,
        months_to_zero=months_to_zero,
        usage_pct=(round(min(100, max(0, (owed_total_cents / credit_limit) * 100))) if credit_limit > 0 else 0),
        # Data for template sections:
        allocations=credit_repo.list_allocations_for_account(account_id),
        cc_envs=cc_envs,         # <-- for the Allocate modal include
        env_metrics=env_metrics, # <-- for the "Envelopes on this Card" table
        account_id=account_id,
        alloc_envelopes=alloc_envelopes,
        from_envelopes=from_envelopes,
        envelope_balances_json=envelope_balances_json,
        balances_json=envelope_balances_json,
        txs=running_txs,
        tx_total=tx_total,
        tx_page=tx_page,
        tx_per_page=tx_per_page,
        tx_per_page_options=per_page_options,
        tx_prev_url=tx_prev_url,
        tx_next_url=tx_next_url,
    )

@bp.post('/<int:account_id>/allocate', endpoint='credit_allocate')
def credit_allocate(account_id: int):
    """Move budget capacity between envelopes on this credit card."""
    posted = request.form
    card_account = accounts_repo.get_account(account_id)
    if not card_account or card_account.get('account_type') != 'credit_card':
        abort(404)

    from_envelope_id = posted.get('from_envelope_id', type=int)
    from_envelope = envelopes_repo.get_envelope(from_envelope_id) if from_envelope_id else None
    if not from_envelope or from_envelope.get('archived_at'):
        flash('Choose a From envelope.', 'warning')
        return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))

    from_account_id = from_envelope.get('locked_account_id')
    if not from_account_id:
        flash('Choose an account-specific From envelope.', 'warning')
        return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))
    from_account_id = int(from_account_id)
    if from_account_id != int(account_id):
        flash('Choose a From envelope from this credit card account.', 'warning')
        return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))

    from_account = accounts_repo.get_account(from_account_id)
    if not from_account or from_account.get('account_type') not in envelopes_repo.TRANSFER_CAPABLE_ACCOUNT_TYPES:
        flash('Choose a valid From envelope account.', 'warning')
        return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))

    balances = aggregates_repo.get_account_envelope_balances()
    in_splits = []
    total_cents = 0
    for key in posted.keys():
        if not key.startswith('alloc_amt_'):
            continue
        try:
            envelope_id = int(key.split('_')[-1])
        except ValueError:
            continue
        raw = (posted.get(key) or '').strip()
        if not raw:
            continue
        destination_envelope = envelopes_repo.get_envelope(envelope_id)
        if (
            not destination_envelope
            or destination_envelope.get('archived_at')
            or (
                destination_envelope.get('locked_account_id') is not None
                and int(destination_envelope['locked_account_id']) != int(account_id)
            )
        ):
            flash('Choose destination envelopes available to this credit card account.', 'warning')
            return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))
        try:
            entered_cents = parse_money_to_cents_strict(raw, field_name='Transfer envelope amount')
        except ValueError as e:
            flash(str(e), 'warning')
            return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))
        mode = (posted.get(f'alloc_mode_{envelope_id}') or 'add').strip().lower()
        if mode == 'set':
            current_cents = int(balances.get((int(account_id), envelope_id), 0) or 0)
            split_cents = int(entered_cents) - current_cents
        else:
            split_cents = int(entered_cents)
        if split_cents != 0:
            in_splits.append({'envelope_id': envelope_id, 'amount_cents': split_cents})
            total_cents += split_cents

    if not in_splits:
        flash('Enter at least one destination envelope amount.', 'warning')
        return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))
    if total_cents <= 0:
        flash('Calculated transfer amount must be greater than $0.00.', 'warning')
        return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))

    allocation_splits = [
        {'envelope_id': int(from_envelope_id), 'amount_cents': -int(total_cents)},
        *in_splits,
    ]
    try:
        TransactionsService.create_allocation(
            {
                'account_id': int(account_id),
                'posted_at': posted.get('posted_at') or datetime.utcnow().strftime('%Y-%m-%d'),
                'memo': posted.get('note') or '',
            },
            allocation_splits,
            total_cents=0,
        )
        flash('Transfer recorded.', 'success')
    except ValueError as e:
        flash(str(e), 'warning')
    except Exception as e:
        flash(str(e), 'danger')

    return redirect(url_for('credit.credit_card_dashboard', account_id=account_id))
