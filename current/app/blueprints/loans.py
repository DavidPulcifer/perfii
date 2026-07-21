from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from ..repositories import accounts_repo
from ..repositories import loans_repo
from ..repositories import envelopes_repo
from ..repositories import transactions_repo
from ..services.transactions_service import TransactionsService
from ..utils import cents_to_dollars, parse_money_to_cents_strict

bp = Blueprint('loans', __name__)


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(start.day, days_in_month[month - 1])
    return date(year, month, day)


def _estimate_payoff(*, principal_cents: int, apr_percent: float, payment_cents: int, one_time_cents: int = 0, today: date | None = None) -> dict:
    principal_cents = max(0, int(principal_cents or 0))
    one_time_cents = max(0, int(one_time_cents or 0))
    payment_cents = max(0, int(payment_cents or 0))
    apr_percent = max(0.0, float(apr_percent or 0.0))

    starting_principal = max(0, principal_cents - one_time_cents)
    result = {
        'starting_principal_cents': starting_principal,
        'total_interest_cents': 0,
        'total_paid_cents': 0,
        'months': 0,
        'final_payment_cents': 0,
        'payoff_date': None,
        'possible': True,
        'message': None,
    }
    today = today or date.today()
    if starting_principal == 0:
        result['message'] = 'Already paid off with the entered principal assumptions.'
        result['payoff_date'] = today.isoformat()
        return result
    if payment_cents <= 0:
        result['possible'] = False
        result['message'] = 'Enter a Payment greater than $0.00 to estimate payoff.'
        return result

    monthly_rate = apr_percent / 100 / 12
    first_interest = int(round(starting_principal * monthly_rate))
    if monthly_rate > 0 and payment_cents <= first_interest:
        result['possible'] = False
        result['message'] = 'Payment does not cover the first month of estimated interest, so payoff will not converge.'
        result['first_month_interest_cents'] = first_interest
        return result

    balance = starting_principal
    total_interest = 0
    total_paid = 0
    months = 0
    final_payment = 0
    while balance > 0 and months < 1200:
        interest = int(round(balance * monthly_rate))
        total_interest += interest
        balance += interest
        paid = min(payment_cents, balance)
        balance -= paid
        total_paid += paid
        final_payment = paid
        months += 1

    if balance > 0:
        result['possible'] = False
        result['message'] = 'This estimate did not reach payoff within 100 years.'
        result['total_interest_cents'] = total_interest
        result['total_paid_cents'] = total_paid
        result['months'] = months
        result['final_payment_cents'] = final_payment
        return result

    result['total_interest_cents'] = total_interest
    result['total_paid_cents'] = total_paid
    result['months'] = months
    result['final_payment_cents'] = final_payment
    result['payoff_date'] = _add_months(today, months).isoformat()
    return result


def _build_payoff_estimator(acct: dict, loan: dict | None) -> dict:
    balance_cents = accounts_repo.get_account_balance(acct['id'])
    principal_default = abs(balance_cents) if balance_cents < 0 else 0
    if principal_default == 0 and loan and loan.get('original_principal_cents'):
        principal_default = int(loan['original_principal_cents'] or 0)

    payment_default = int((loan or {}).get('normal_monthly_payment_cents') or 0)
    values = {
        'principal': cents_to_dollars(principal_default),
        'apr': '',
        'payment': cents_to_dollars(payment_default) if payment_default else '',
        'one_time': '',
    }
    estimator = {
        'current_principal_cents': principal_default,
        'normal_payment_cents': payment_default,
        'values': values,
        'result': None,
        'error': None,
    }
    if request.args.get('estimate') != '1':
        return estimator

    values = {
        'principal': (request.args.get('principal') or '').strip(),
        'apr': (request.args.get('apr') or '').strip(),
        'payment': (request.args.get('payment') or '').strip(),
        'one_time': (request.args.get('one_time') or '').strip(),
    }
    estimator['values'] = values
    try:
        principal_cents = parse_money_to_cents_strict(values['principal'], field_name='Principal')
        payment_cents = parse_money_to_cents_strict(values['payment'], field_name='Payment')
        one_time_cents = parse_money_to_cents_strict(values['one_time'], field_name='One-time principal payment', allow_blank=True)
        apr = float(values['apr']) if values['apr'] else 0.0
    except ValueError as exc:
        estimator['error'] = str(exc)
        return estimator
    if apr < 0:
        estimator['error'] = 'APR must be zero or greater.'
        return estimator
    if principal_cents < 0:
        estimator['error'] = 'Principal must be zero or greater.'
        return estimator
    if payment_cents < 0:
        estimator['error'] = 'Payment must be zero or greater.'
        return estimator
    if one_time_cents < 0:
        estimator['error'] = 'One-time principal payment must be zero or greater.'
        return estimator

    estimator['result'] = _estimate_payoff(
        principal_cents=principal_cents,
        apr_percent=apr,
        payment_cents=payment_cents,
        one_time_cents=one_time_cents,
    )
    return estimator

@bp.get('/<int:account_id>', endpoint='loan_dashboard')
def loan_dashboard(account_id: int):
    acct = accounts_repo.get_account(account_id)
    if not acct or acct['account_type'] != 'loan':
        abort(404)
    loan = loans_repo.get_loan(account_id)
    payoff_estimator = _build_payoff_estimator(acct, loan)

    # Fetch transactions for this loan account.
    tx_rows, _ = transactions_repo.list_transactions(account_id=account_id, limit=500)

    # Aggregate existing parts per payment
    parts = loans_repo.parts_by_payment_tx_ids([t['id'] for t in tx_rows])
    parts_map = {}
    for p in parts:
        d = parts_map.setdefault(p['payment_tx_id'], {'principal': 0, 'interest': 0, 'fees': 0, 'note': None})
        d[p['part_type']] = d.get(p['part_type'], 0) + (p['amount_cents'] or 0)
        if p.get('note'):
            d['note'] = p['note']

    # Shape rows to match template expectations
    payments = []
    for t in tx_rows:
        d = parts_map.get(t['id'], {})
        payments.append({
            'id': t['id'],
            'posted_at': t['posted_at'],
            'amount_cents': t['amount_cents'],
            'payee': t['payee'],
            'memo': t['memo'],
            'principal_cents': d.get('principal', 0),
            'interest_cents': d.get('interest', 0),
            'fees_cents': d.get('fees', 0),
            'parts_note': d.get('note'),
        })

    # Minimal estimates placeholder
    estimates = {'has_statement': False}

    # For the payment modal: all accounts + all envelopes
    accounts = accounts_repo.list_accounts()
    envelopes_all = envelopes_repo.list_envelopes()

    return render_template(
        'loan.html',
        acct=acct,
        loan=loan,
        payoff_estimator=payoff_estimator,
        payments=payments,
        estimates=estimates,
        account_id=account_id,
        accounts=accounts,           
        envelopes_all=envelopes_all  
    )

@bp.post('/<int:account_id>/payment', endpoint='make_payment')
def make_payment(account_id: int):
    loan_acct = accounts_repo.get_account(account_id)
    if not loan_acct or loan_acct['account_type'] != 'loan':
        abort(404)

    posted = request.form
    posted_at = posted.get('posted_at') or ''
    memo = posted.get('memo') or None
    try:
        amount_cents = parse_money_to_cents_strict(posted.get('amount'), field_name="Payment amount")
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(url_for('loans.loan_dashboard', account_id=account_id))
    if amount_cents <= 0:
        flash("Amount must be greater than 0.", "warning")
        return redirect(url_for('loans.loan_dashboard', account_id=account_id))

    from_account_id = posted.get('from_account_id', type=int)
    if from_account_id:
        src_acct = accounts_repo.get_account(from_account_id)
        if not src_acct or src_acct['id'] == loan_acct['id']:
            flash("Please choose a different source account.", "warning")
            return redirect(url_for('loans.loan_dashboard', account_id=account_id))

        # Collect source envelope splits: pay_amount_{envelope_id} (+ optional remainder)
        total = amount_cents
        parts: dict[int, int] = {}
        for k in posted.keys():
            if not k.startswith('pay_amount_'):
                continue
            try:
                eid = int(k.split('_')[-1])
            except ValueError:
                continue
            raw = (posted.get(k) or "").strip()
            if not raw:
                continue
            try:
                cents = parse_money_to_cents_strict(raw, field_name="Payment split amount")
            except ValueError as e:
                flash(str(e), "warning")
                return redirect(url_for('loans.loan_dashboard', account_id=account_id))
            if cents == 0:
                continue
            parts[eid] = parts.get(eid, 0) + cents

        explicit_signed = any(cents < 0 for cents in parts.values())
        target = -total if explicit_signed else total
        allocated = sum(parts.values())
        rem_eid = posted.get('pay_remainder', type=int)
        remainder_amount = (int(target) - int(allocated)) if rem_eid else None
        if rem_eid and allocated != target:
            parts[rem_eid] = parts.get(rem_eid, 0) + (target - allocated)
            allocated = target

        if allocated != target or not parts:
            flash("Envelope splits must sum exactly to the payment amount for the source account.", "warning")
            return redirect(url_for('loans.loan_dashboard', account_id=account_id))

        try:
            # Source leg gets splits (OUT); loan leg has no envelopes → allow unallocated IN
            TransactionsService.create_transfer(
                payload={
                    'amount_cents': amount_cents,
                    'date': posted_at,
                    'memo': memo,
                    'from_account_id': src_acct['id'],
                    'to_account_id': loan_acct['id'],
                },
                out_splits=[{'envelope_id': eid, 'amount_cents': cents} for eid, cents in parts.items()],
                in_splits=[],
                allow_unallocated_in=True,
                out_remainder_envelope_id=rem_eid or None,
                out_remainder_amount_cents=remainder_amount,
            )
            flash("Loan payment recorded (transfer).", "success")
        except ValueError as e:
            flash(str(e), "warning")

        return redirect(url_for('loans.loan_dashboard', account_id=account_id))

    # External payment into the loan (no loan envelopes)
    external_name = (posted.get('counterparty') or '').strip() or None
    try:
        TransactionsService.create_income(
            payload={
                'account_id': loan_acct['id'],
                'posted_at': posted_at,
                'payee': external_name,
                'memo': memo,
                'external_counterparty': external_name,
                'amount_cents': amount_cents,
            },
            splits=[],  # loans typically do not use envelopes
            allow_unallocated=True,
        )
        flash("Loan payment recorded.", "success")
    except ValueError as e:
        flash(str(e), "warning")

    return redirect(url_for('loans.loan_dashboard', account_id=account_id))



@bp.post('/<int:account_id>/parts/<int:payment_tx_id>/save', endpoint='loan_parts_save')
def loan_parts_save(account_id: int, payment_tx_id: int):
    # amounts in dollars string -> to cents
    def to_cents(x, field_name):
        return parse_money_to_cents_strict(x, field_name=field_name, allow_blank=True)

    try:
        principal = to_cents(request.form.get('principal'), 'Principal')
        interest  = to_cents(request.form.get('interest'), 'Interest')
        fees      = to_cents(request.form.get('fees'), 'Fees')
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('loans.loan_dashboard', account_id=account_id))
    note      = request.form.get('note')

    loans_repo.replace_parts(payment_tx_id, [
        ('principal', principal),
        ('interest', interest),
        ('fees', fees),
    ], note=note)

    flash('Payment breakdown saved', 'success')
    return redirect(url_for('loans.loan_dashboard', account_id=account_id))
