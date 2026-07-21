# app/blueprints/transactions.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from werkzeug.exceptions import NotFound
from datetime import datetime
from app.services.transactions_service import TransactionsService
from app.services.import_rule_proposal_service import safe_refresh_import_rule_proposals
from app.repositories import transactions_repo as txrepo, accounts_repo, envelopes_repo, splits_repo, remainder_intents_repo, aggregates_repo
from ..utils import parse_money_to_cents, parse_money_to_cents_strict
import re
from urllib.parse import urlencode, urlsplit
from flask import current_app

bp = Blueprint('transactions', __name__, url_prefix='/tx')


def _safe_return_to(raw: str | None, fallback: str | None = None) -> str:
    """Return a local path/query target, never an external redirect."""
    fallback = fallback or url_for('transactions.list_')
    if not raw:
        return fallback
    raw = str(raw).strip()
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not raw.startswith('/') or raw.startswith('//'):
        return fallback
    return raw


def _edit_url_with_return(endpoint: str, *, tx_id: int, return_to: str | None = None) -> str:
    values = {'tx_id': tx_id}
    safe_return_to = _safe_return_to(return_to, fallback='') if return_to else ''
    if safe_return_to:
        values['return_to'] = safe_return_to
    return url_for(endpoint, **values)

@bp.get('/', endpoint='list_')
def list_():
    return _render_transaction_list(
        account_id=None,
        endpoint='transactions.list_',
    )


@bp.get('/<int:account_id>', endpoint='list_by_account')
def list_by_account(account_id: int):
    return _render_transaction_list(
        account_id=account_id,
        endpoint='transactions.list_by_account',
        endpoint_values={'account_id': account_id},
    )


def _render_transaction_list(*, account_id: int | None, endpoint: str, endpoint_values: dict | None = None):
    args = request.args
    endpoint_values = endpoint_values or {}

    # ---- filters from querystring (match template names) ----
    f_date_from   = args.get('date_from') or None
    f_date_to     = args.get('date_to') or None
    f_account_ids = _query_int_list(args, 'account_id')
    if account_id:
        f_account_ids = [account_id]
    f_ttypes      = _query_str_list(args, 'ttype')
    f_amt_exact   = args.get('amount_exact') or None
    f_amt_min     = args.get('amount_min') or None
    f_amt_max     = args.get('amount_max') or None
    f_envelope_ids = _query_int_list(args, 'envelope_id')
    f_q_payee     = args.get('q_payee') or None
    f_q_memo      = args.get('q_memo') or None
    f_abs         = args.get('abs', '1')  # default to absolute amounts
    f_reconciliation = args.get('reconciliation') or 'all'
    if f_reconciliation not in {'all', 'unreconciled', 'reconciled'}:
        f_reconciliation = 'all'

    per_page_options = [25, 50, 100, 200]
    per_page = args.get('per_page', type=int) or 200
    if per_page not in per_page_options:
        per_page = 200
    page = args.get('page', type=int) or 1
    if page < 1:
        page = 1
    offset = (page - 1) * per_page

    use_abs = (f_abs != '0')
    amount_exact_cents = parse_money_to_cents(f_amt_exact) if f_amt_exact else None
    amount_min_cents = parse_money_to_cents(f_amt_min) if f_amt_min and amount_exact_cents is None else None
    amount_max_cents = parse_money_to_cents(f_amt_max) if f_amt_max and amount_exact_cents is None else None

    rows, total = txrepo.list_transactions(
        limit=per_page,
        offset=offset,
        account_id=account_id,
        account_ids=None if account_id else f_account_ids,
        date_from=f_date_from,
        date_to=f_date_to,
        ttypes=f_ttypes,
        amount_exact_cents=amount_exact_cents,
        amount_min_cents=amount_min_cents,
        amount_max_cents=amount_max_cents,
        envelope_ids=f_envelope_ids,
        q_payee=f_q_payee,
        q_memo=f_q_memo,
        use_abs=use_abs,
        reconciliation_status=None if f_reconciliation == 'all' else f_reconciliation,
    )

    accounts, envelopes, accounts_map, env_map = _base_lists()

    txs = []
    for t in rows or []:
        t = dict(t)
        t["account_name"] = accounts_map.get(t.get("account_id"), "")
        txs.append(t)

    splits = _split_map_for_tx_ids([t["id"] for t in txs], env_map)

    def with_page(n: int) -> str:
        q = []
        for key, values in args.lists():
            if key == 'page':
                continue
            q.extend((key, value) for value in values)
        q.append(('page', str(n)))
        base_url = url_for(endpoint, **endpoint_values)
        return base_url + ('?' + urlencode(q) if q else '')

    has_prev = page > 1
    has_next = (offset + len(txs)) < total
    prev_url = with_page(page - 1) if has_prev else None
    next_url = with_page(page + 1) if has_next else None

    return render_template(
        'transactions.html',
        txs=txs,
        splits=splits,
        envelopes=envelopes,
        accounts=accounts,
        accounts_map=accounts_map,

        # current selection
        acc_id=account_id,

        # filter echo-back for the form
        f_account_ids=f_account_ids,
        f_date_from=f_date_from,
        f_date_to=f_date_to,
        f_ttypes=f_ttypes,
        f_amt_exact=f_amt_exact,
        f_amt_min=f_amt_min,
        f_amt_max=f_amt_max,
        f_envelope_ids=f_envelope_ids,
        f_q_payee=f_q_payee,
        f_q_memo=f_q_memo,
        f_abs=f_abs,
        f_reconciliation=f_reconciliation,

        # paging
        per_page_options=per_page_options,
        per_page=per_page,
        page=page,
        total=total,
        prev_url=prev_url,
        next_url=next_url,

        # JSON blobs referenced by page scripts
        envelopes_json=envelopes,
        balances_json=aggregates_repo.account_envelope_balances_json(),
    )


def _query_int_list(args, name: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for raw in args.getlist(name):
        for part in str(raw).split(','):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value and value not in seen:
                values.append(value)
                seen.add(value)
    return values


def _query_str_list(args, name: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in args.getlist(name):
        for part in str(raw).split(','):
            value = part.strip()
            if not value or value in seen:
                continue
            values.append(value)
            seen.add(value)
    return values

# ----- Create transactions --------------------

@bp.post('/new/expense', endpoint='new_expense')
def new_expense():
    form = request.form
    account_id = form.get('account_id', type=int)
    if not account_id or not accounts_repo.get_account(account_id):
        flash("Choose a valid account.", "warning")
        return redirect(url_for('core.index'))

    # Build raw splits (amount may be '12.34', service will parse and sign)
    splits = _collect_raw_amount_splits(form, prefix='expense')

    payload = {
        'account_id': account_id,
        'posted_at': form.get('posted_at') or datetime.utcnow().strftime('%Y-%m-%d'),
        'payee': form.get('payee') or '',
        'memo': form.get('memo') or '',
        'amount': form.get('amount') or form.get('amount_cents'),  # either is fine
        'fitid': form.get('fitid') or None,
        'external_counterparty': form.get('external_counterparty') or None,
    }
    remainder_id = form.get('remainder_envelope_id', type=int)

    try:
        TransactionsService.create_expense(payload, splits, remainder_envelope_id=remainder_id)
        flash("Expense added.", "success")
    except ValueError as e:
        flash(str(e), "danger")

    return redirect(form.get("return_to") or url_for('transactions.list_by_account', account_id=account_id))


@bp.post('/new/income', endpoint='new_income')
def new_income():
    form = request.form
    account_id = form.get('account_id', type=int)
    if not account_id or not accounts_repo.get_account(account_id):
        flash("Choose a valid account.", "warning")
        return redirect(url_for('core.index'))

    splits = _collect_raw_amount_splits(form, prefix='income')

    payload = {
        'account_id': account_id,
        'posted_at': form.get('posted_at') or datetime.utcnow().strftime('%Y-%m-%d'),
        'payee': form.get('payee') or '',
        'memo': form.get('memo') or '',
        'amount': form.get('amount') or form.get('amount_cents'),
        'fitid': form.get('fitid') or None,
        'external_counterparty': form.get('external_counterparty') or None,
    }
    remainder_id = form.get('remainder_envelope_id', type=int)

    try:
        TransactionsService.create_income(payload, splits, remainder_envelope_id=remainder_id)
        flash("Income added.", "success")
    except ValueError as e:
        flash(str(e), "danger")

    return redirect(form.get("return_to") or url_for('transactions.list_by_account', account_id=account_id))


@bp.post('/new/transfer', endpoint='new_transfer')
def new_transfer():
    form = request.form

    from_account_id = form.get('from_account_id', type=int)
    to_account_id   = form.get('to_account_id', type=int)
    if not from_account_id or not to_account_id:
        flash("Choose valid accounts for transfer.", "warning")
        return redirect(url_for('core.index'))

    # Amount (absolute, in cents)
    try:
        if form.get('amount_cents', '').strip():
            amount_cents = abs(int(form.get('amount_cents')))
        else:
            amount_cents = abs(parse_money_to_cents_strict(form.get('amount'), field_name="Transfer amount"))
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(url_for('transactions.list_by_account', account_id=from_account_id))

    payload = {
        'from_account_id': from_account_id,
        'to_account_id': to_account_id,
        'amount_cents': amount_cents,
        'posted_at': form.get('posted_at') or datetime.utcnow().strftime('%Y-%m-%d'),
        'memo': form.get('memo', ''),
        'payee': form.get('payee', ''),
    }
    balances = aggregates_repo.get_account_envelope_balances()

    # Source (OUT) side — uses: transfer_from_amt_*, remainder: from_remainder
    try:
        out_pairs, out_total, out_remainder_id, out_remainder_amount = _collect_signed_split_pairs(
            form,
            pattern=r'^transfer_from_(\d+)$',
            mode_prefix='transfer_from',
            account_id=from_account_id,
            balances=balances,
            remainder_field='from_remainder',
            target_cents=-amount_cents,
            field_name="Transfer split amount",
        )
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(url_for('transactions.list_by_account', account_id=from_account_id))
    if out_total != -amount_cents:
        flash("Source envelope amounts must net to the transfer amount.", "warning")
        return redirect(url_for('transactions.list_by_account', account_id=from_account_id))
    out_splits = out_pairs

    # Destination (IN) side — uses: transfer_to_amt_*, remainder: to_remainder
    try:
        in_pairs, in_total, in_remainder_id, in_remainder_amount = _collect_signed_split_pairs(
            form,
            pattern=r'^transfer_to_(\d+)$',
            mode_prefix='transfer_to',
            account_id=to_account_id,
            balances=balances,
            remainder_field='to_remainder',
            target_cents=amount_cents,
            field_name="Transfer split amount",
        )
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(url_for('transactions.list_by_account', account_id=to_account_id))
    if in_total != amount_cents:
        flash("Destination envelope amounts must net to the transfer amount.", "warning")
        return redirect(url_for('transactions.list_by_account', account_id=to_account_id))
    in_splits = in_pairs

    # Commit via service (writes two transfer legs + both sets of splits)
    try:
        tx_out_id, tx_in_id = TransactionsService.create_transfer(
            payload,
            out_splits,
            in_splits,
            out_remainder_envelope_id=out_remainder_id,
            in_remainder_envelope_id=in_remainder_id,
            out_remainder_amount_cents=out_remainder_amount,
            in_remainder_amount_cents=in_remainder_amount,
        )
        flash("Transfer recorded.", "success")
    except Exception as e:
        flash(str(e), "danger")

    return_to = form.get("return_to")
    if return_to:
        return redirect(return_to)
    return redirect(url_for('transactions.list_by_account', account_id=to_account_id))

@bp.get('/<int:tx_id>/edit')
def transaction_edit(tx_id: int):
    tx = txrepo.get_transaction(tx_id)
    if not tx:
        raise NotFound()
    return_to = _safe_return_to(request.args.get('return_to'), fallback=url_for('transactions.list_'))
    
    # Fetch splits + lists the template needs
    splits = splits_repo.get_splits_for_transaction(tx_id) or []
    remainder_intent = remainder_intents_repo.get_remainder_intent(tx_id)
    existing_envelope_ids = [s.get("envelope_id") for s in splits]
    if remainder_intent and remainder_intent.get("envelope_id"):
        existing_envelope_ids.append(remainder_intent.get("envelope_id"))
    accounts = accounts_repo.list_accounts() or []
    envelopes = envelopes_repo.list_envelopes_for_selector(existing_envelope_ids) or []
    env_map = {e["id"]: e["name"] for e in envelopes}

    # Ensure each split has an envelope_name for display
    for s in splits:
        if not s.get("envelope_name"):
            s["envelope_name"] = env_map.get(s.get("envelope_id"))
    
    return render_template(
        'transaction_edit.html',
        transaction=tx,
        splits=splits,
        remainder_intent=remainder_intent,
        accounts=accounts,
        envelopes=envelopes,
        envelope_balances={},
        return_to=return_to,
    )


@bp.post('/<int:tx_id>/convert-transfer')
def transaction_convert_transfer(tx_id: int):
    form = request.form
    old = txrepo.get_transaction(tx_id)
    if not old:
        raise NotFound()

    ttype = (old.get('ttype') or '').lower()
    if ttype not in {'expense', 'income'} or old.get('xfer_pair_id'):
        flash('Only standard expense and income transactions can be converted to transfers.', 'warning')
        return redirect(_edit_url_with_return('transactions.transaction_edit', tx_id=tx_id, return_to=form.get('return_to')))

    amount_cents = abs(int(old.get('amount_cents') or 0))
    current_is_out = ttype == 'expense'
    current_target = -amount_cents if current_is_out else amount_cents
    other_target = amount_cents if current_is_out else -amount_cents

    try:
        current_splits, current_total, current_remainder_id, current_remainder_amount = _collect_signed_split_pairs(
            form,
            pattern=r'^convert_current_amount_(\d+)$',
            remainder_field='convert_current_remainder',
            target_cents=current_target,
            field_name='Transfer split amount',
        )
        other_splits, other_total, other_remainder_id, other_remainder_amount = _collect_signed_split_pairs(
            form,
            pattern=r'^convert_other_amount_(\d+)$',
            remainder_field='convert_other_remainder',
            target_cents=other_target,
            field_name='Transfer split amount',
        )
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(_edit_url_with_return('transactions.transaction_edit', tx_id=tx_id, return_to=form.get('return_to')))

    if current_total != current_target:
        flash('Current-account envelope amounts must net to the transfer amount.', 'warning')
        return redirect(_edit_url_with_return('transactions.transaction_edit', tx_id=tx_id, return_to=form.get('return_to')))
    if other_total != other_target:
        flash('Other-account envelope amounts must net to the transfer amount.', 'warning')
        return redirect(_edit_url_with_return('transactions.transaction_edit', tx_id=tx_id, return_to=form.get('return_to')))

    try:
        tx_out_id, tx_in_id = TransactionsService.convert_standard_transaction_to_transfer(
            tx_id,
            other_account_id=int(form.get('other_account_id') or 0),
            current_splits=current_splits,
            other_splits=other_splits,
            current_remainder_envelope_id=current_remainder_id,
            other_remainder_envelope_id=other_remainder_id,
            current_remainder_amount_cents=current_remainder_amount,
            other_remainder_amount_cents=other_remainder_amount,
        )
        flash('Transaction converted to transfer.', 'success')
        edit_id = tx_out_id if current_is_out else tx_in_id
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=edit_id, return_to=form.get('return_to')))
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(_edit_url_with_return('transactions.transaction_edit', tx_id=tx_id, return_to=form.get('return_to')))

@bp.post('/<int:tx_id>/edit')
def transaction_edit_post(tx_id: int):
    form = request.form

    # we need the old tx to know ttype (for sign) and default amount if not edited
    old = txrepo.get_transaction(tx_id)
    if not old:
        raise NotFound()

    ttype = (old.get('ttype') or '').lower()
    is_allocation = ttype == 'allocation'

    # total amount in ABS dollars (prefer the edited amount if provided)
    try:
        if 'amount' in form and (form.get('amount') or '').strip():
            total_abs = abs(parse_money_to_cents_strict(form.get('amount'), field_name="Transaction amount"))
        else:
            total_abs = abs(int(old.get('amount_cents') or 0))
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(_edit_url_with_return('transactions.transaction_edit', tx_id=tx_id, return_to=form.get('return_to')))

    outflow = ttype in ('expense', 'transfer_out')

    # collect splits from the edit form (edit_amt_<envelope_id> + optional remainder)
    splits, remainder_id, remainder_amount = _collect_splits_from_form(
        form,
        outflow=outflow,
        total_abs=None if is_allocation else total_abs,
    )
    if is_allocation:
        remainder_id = None
        remainder_amount = None

    try:
        payload = form.to_dict()
        payload['ignore_match'] = 1 if form.get('ignore_match') else 0
        TransactionsService.edit_transaction(
            tx_id,
            payload,
            splits=splits,
            remainder_envelope_id=remainder_id,
            remainder_amount_cents=remainder_amount,
        )
        flash('Transaction updated', 'success')
        if ttype in ('expense', 'income') and old.get('account_id'):
            safe_refresh_import_rule_proposals(
                account_id=old.get('account_id'),
                reason="transaction_edit",
                logger=current_app.logger,
            )
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(_safe_return_to(form.get('return_to')))

@bp.post('/<int:tx_id>/delete')
def transaction_delete(tx_id: int):
    try:
        TransactionsService.delete_transaction(tx_id)
        flash('Transaction deleted', 'info')
    except ValueError as e:
        flash(str(e), 'warning')
    return redirect(_safe_return_to(request.form.get('return_to')))

@bp.post('/bulk')
def transactions_bulk():
    form = request.form

    # Safe parse ('' from "All accounts" becomes None)
    raw_acc = (form.get('account_id') or '').strip()
    account_id = int(raw_acc) if raw_acc else None

    action = form.get('action', '').strip().lower()
    tx_ids = [int(x) for x in form.getlist('tx_id') if (x or '').strip()]

    return_to = form.get('return_to') or url_for('transactions.list_', account_id=account_id)

    if not tx_ids:
        flash('Select at least one transaction.', 'warning')
        return redirect(return_to)

    if action == 'delete':
        deleted = 0
        failed = 0
        for tid in tx_ids:
            try:
                TransactionsService.delete_transaction(tid)
                deleted += 1
            except Exception:
                failed += 1
                current_app.logger.exception("Bulk delete failed for tx_id=%s", tid)
        if failed:
            flash(f'Deleted {deleted} transaction{"s" if deleted != 1 else ""}. Failed to delete {failed}.', 'warning')
        else:
            flash(f'Deleted {deleted} transaction{"s" if deleted != 1 else ""}', 'success')
        return redirect(return_to)

    # (Future bulk actions can be added here; current UI only has Delete)
    flash('Unknown or unsupported bulk action.', 'warning')
    return redirect(return_to)

@bp.route('/transfer/<int:tx_id>/edit', methods=['GET', 'POST'])
def transfer_edit(tx_id: int):
    tx = txrepo.get_transaction(tx_id)
    # Consistent field name: use xfer_pair_id (schema column)
    if not tx or not tx.get('xfer_pair_id'):
        raise NotFound()
    return_to = _safe_return_to(request.values.get('return_to'), fallback=url_for('transactions.list_'))

    pair_id = tx['xfer_pair_id']

    if request.method == 'GET':
        other = txrepo.get_transaction(pair_id)
        # Identify legs
        tx_out = tx if int(tx['amount_cents']) < 0 else other
        tx_in  = other if int(other['amount_cents']) > 0 else tx

        # Build envelope value maps expected by the template (_envelope_selector)
        out_splits = splits_repo.get_splits_for_transaction(tx_out['id']) or []
        in_splits  = splits_repo.get_splits_for_transaction(tx_in['id']) or []

        # Preserve signed cents so edit round-trips mixed positive/negative splits.
        out_map = { int(s['envelope_id']): int(s['amount_cents']) for s in out_splits }
        in_map  = { int(s['envelope_id']): int(s['amount_cents']) for s in in_splits }

        accounts = accounts_repo.list_accounts()
        out_remainder_intent = remainder_intents_repo.get_remainder_intent(tx_out['id'])
        in_remainder_intent = remainder_intents_repo.get_remainder_intent(tx_in['id'])
        existing_envelope_ids = {
            int(s['envelope_id'])
            for s in (out_splits + in_splits)
            if s.get('envelope_id') is not None
        }
        for intent in (out_remainder_intent, in_remainder_intent):
            if intent and intent.get('envelope_id') is not None:
                existing_envelope_ids.add(int(intent['envelope_id']))
        envelopes = envelopes_repo.list_envelopes_for_selector(existing_envelope_ids)
        envelope_balances = {}

        return render_template(
            'transfer_edit.html',
            tx_out=tx_out,
            tx_in=tx_in,
            accounts=accounts,
            envelopes=envelopes,
            envelope_balances=envelope_balances,
            out_map=out_map,
            in_map=in_map,
            out_remainder_intent=out_remainder_intent,
            in_remainder_intent=in_remainder_intent,
            return_to=return_to,
        )

    # POST (update)
    form = request.form
    other = txrepo.get_transaction(pair_id)
    if not other:
        raise NotFound()
    tx_out = tx if int(tx['amount_cents']) < 0 else other
    tx_in = other if int(tx['amount_cents']) < 0 else tx

    try:
        if form.get('amount_cents', '').strip():
            amount_cents = abs(int(form.get('amount_cents')))
        else:
            amount_cents = abs(parse_money_to_cents_strict(form.get('amount'), field_name="Transfer amount"))
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=tx_id, return_to=form.get('return_to')))

    payload = {
        'from_account_id': int(form['from_account_id']),
        'to_account_id': int(form['to_account_id']),
        'amount_cents': amount_cents,
        'posted_at': form.get('posted_at') or datetime.utcnow().strftime('%Y-%m-%d'),
        'memo': form.get('memo', ''),
        'out_fitid': tx_out.get('fitid'),
        'in_fitid': tx_in.get('fitid'),
    }
    
    # From side (OUT leg)
    try:
        out_pairs, out_total, out_remainder_id, out_remainder_amount = _collect_signed_split_pairs(
            form,
            pattern=r'^from_amount_(\d+)$',
            remainder_field='from_remainder',
            target_cents=-amount_cents,
            field_name="Transfer split amount",
        )
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=tx_id, return_to=form.get('return_to')))
    if out_total != -amount_cents:
        flash("Source envelope amounts must net to the transfer amount.", "warning")
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=tx_id, return_to=form.get('return_to')))

    # To side (IN leg)
    try:
        in_pairs, in_total, in_remainder_id, in_remainder_amount = _collect_signed_split_pairs(
            form,
            pattern=r'^to_amount_(\d+)$',
            remainder_field='to_remainder',
            target_cents=amount_cents,
            field_name="Transfer split amount",
        )
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=tx_id, return_to=form.get('return_to')))
    if in_total != amount_cents:
        flash("Destination envelope amounts must net to the transfer amount.", "warning")
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=tx_id, return_to=form.get('return_to')))

    out_splits = out_pairs
    in_splits  = in_pairs

    try:
        TransactionsService.edit_transfer(
            tx_id,
            payload,
            out_splits,
            in_splits,
            out_remainder_envelope_id=out_remainder_id,
            in_remainder_envelope_id=in_remainder_id,
            out_remainder_amount_cents=out_remainder_amount,
            in_remainder_amount_cents=in_remainder_amount,
        )
        flash('Transfer updated', 'success')
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(_edit_url_with_return('transactions.transfer_edit', tx_id=tx_id, return_to=form.get('return_to')))
    return redirect(return_to)


def _collect_raw_amount_splits(form, *, prefix: str) -> list[dict]:
    """Collect raw split inputs for create expense/income flows.

    The service layer owns sign normalization and strict money parsing for these
    flows, so this helper intentionally preserves the posted amount string.
    """
    pattern = re.compile(rf'^{re.escape(prefix)}_(\d+)$')
    splits = []
    for key, raw in form.items():
        match = pattern.match(key)
        if match and raw is not None and str(raw).strip():
            splits.append({'envelope_id': int(match.group(1)), 'amount': raw})
    return splits


def _collect_signed_split_pairs(
    form,
    *,
    pattern: str,
    mode_prefix: str | None = None,
    account_id: int | None = None,
    balances: dict[tuple[int, int], int] | None = None,
    remainder_field: str,
    target_cents: int,
    field_name: str,
) -> tuple[list[dict], int, int | None, int | None]:
    """Collect one side of a transfer split form as signed split dicts.

    Backward compatibility: when the target leg is negative and every entered
    amount is non-negative, treat those entries as magnitudes to move out and
    convert them to negative split amounts. If the user enters any negative
    value, preserve all entered signs and validate the signed net.
    """
    compiled = re.compile(pattern)
    parsed: list[tuple[int, int]] = []

    for key, raw in form.items():
        match = compiled.match(key)
        if not match or raw is None or not str(raw).strip():
            continue
        cents = parse_money_to_cents_strict(raw, field_name=field_name)
        if cents == 0:
            continue
        if mode_prefix:
            mode = (form.get(f'{mode_prefix}_mode_{match.group(1)}') or 'add').strip().lower()
            if mode == 'set':
                current_cents = int((balances or {}).get((int(account_id or 0), int(match.group(1))), 0) or 0)
                cents = int(cents) - current_cents
                if cents == 0:
                    continue
        parsed.append((int(match.group(1)), int(cents)))

    explicit_signed = any(cents < 0 for _, cents in parsed)
    if int(target_cents) < 0 and not explicit_signed:
        pairs = [(eid, -abs(cents)) for eid, cents in parsed]
    else:
        pairs = parsed

    total = sum(cents for _, cents in pairs)
    rem_id = form.get(remainder_field, type=int)
    remainder_delta: int | None = None
    if rem_id:
        remainder_delta = int(target_cents) - total
        if total != int(target_cents):
            delta = remainder_delta
            if delta != 0:
                pairs.append((int(rem_id), delta))
                total += delta

    return ([{'envelope_id': eid, 'amount_cents': cents} for (eid, cents) in pairs], total, rem_id, remainder_delta)

def _collect_splits_from_form(form, *, outflow: bool, total_abs: int | None = None) -> tuple[list[dict], int | None, int | None]:
    """
    Parse envelope amounts from the edit UI.

    Accepts signed inputs named:  edit_amt_<envelope_id>
      - Positive values ADD to that envelope
      - Negative values SUBTRACT from that envelope (allowed; envelope may go negative)

    If total_abs is provided, the target signed total is:
        target = (+/-) total_abs, using outflow to set the sign
      and if a remainder_envelope_id is present, we will fill the exact difference
      so that SUM(split amounts) == target.

    Returns a list of dicts:
      [{ 'envelope_id': int, 'amount_cents': int (signed) }, ...]
    """
    entries: list[tuple[int, int]] = []
    signed_sum = 0

    # Collect signed amounts per envelope
    for key, raw in form.items():
        m = re.match(r'^edit_amt_(\d+)$', key)
        if not m:
            continue
        if raw is None or not str(raw).strip():
            continue

        eid = int(m.group(1))
        cents = int(parse_money_to_cents_strict(raw, field_name="Split amount"))  # PRESERVE SIGN (no abs)
        if cents == 0:
            continue

        entries.append((eid, cents))
        signed_sum += cents

    # If a total target is provided and we have a remainder envelope, fill the gap
    remainder_id = None
    remainder_delta = None
    if total_abs is not None:
        target = (-total_abs) if outflow else (total_abs)  # signed parent amount
        remainder_id = form.get('remainder_envelope_id', type=int)
        if remainder_id:
            remainder_delta = int(target - signed_sum)  # may be positive or negative
        if signed_sum != target and remainder_id:
            delta = int(target - signed_sum)  # may be positive or negative
            if delta != 0:
                entries.append((int(remainder_id), delta))
                signed_sum += delta

    # Return as list of dicts, preserving entered signs
    return [{'envelope_id': eid, 'amount_cents': cents} for (eid, cents) in entries], remainder_id, remainder_delta


def _base_lists():
    """Fetch accounts/envelopes and build helper maps used by the template."""
    accounts = accounts_repo.list_accounts() or []
    envelopes = envelopes_repo.list_envelopes() or []
    accounts_map = {a["id"]: a["name"] for a in accounts}
    env_map = {e["id"]: e["name"] for e in envelopes}
    return accounts, envelopes, accounts_map, env_map


def _split_map_for_tx_ids(tx_ids, env_map):
    """
    Build { tx_id: [ {envelope_id, envelope_name, amount_cents}, ... ] }
    using the existing splits_repo.get_splits_for_transaction() helper.
    """
    splits_by_tx = {}
    for tid in tx_ids:
        try:
            rows = splits_repo.get_splits_for_transaction(tid) or []
        except Exception:
            rows = []
        norm = []
        for r in rows:
            eid = r.get("envelope_id")
            norm.append({
                "envelope_id": eid,
                "envelope_name": r.get("envelope_name") or env_map.get(eid),
                "amount_cents": r.get("amount_cents", 0),
            })
        splits_by_tx[tid] = norm
    return splits_by_tx
