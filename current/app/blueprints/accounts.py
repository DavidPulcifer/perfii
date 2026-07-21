# app/blueprints/accounts.py
from flask import Blueprint, render_template, abort, request, redirect, url_for, flash
from ..utils import parse_money_to_cents, parse_money_to_cents_strict
from app.repositories import accounts_repo, credit_repo, transactions_repo, invest_repo, envelopes_repo, loans_repo, savings_repo
from app.db import unit_of_work

bp = Blueprint('accounts', __name__, url_prefix='/accounts')

@bp.get('/')
def list_accounts():
    """Endpoint: accounts.list_accounts"""
    accounts = accounts_repo.list_accounts()   
    return render_template('accounts.html', accounts=accounts)

@bp.get('/<int:account_id>')
def account_dashboard(account_id):
    acct = accounts_repo.get_account(account_id)
    if not acct:
        abort(404)
    
    current_balance_cents = accounts_repo.get_account_balance(account_id)

    return render_template('account.html', acct=acct, current_balance_cents=current_balance_cents)

@bp.post('/new', endpoint='account_new')
def account_new():
    name = request.form.get('name','').strip()
    acct_type = request.form.get('account_type','bank')
    if not name:
        flash('Name required', 'warning')
        return redirect(url_for('accounts.list_accounts'))    

    try:
        opening_balance_cents = parse_money_to_cents_strict(
            request.form.get("opening_balance"),
            field_name="Opening balance",
            allow_blank=True,
        )
        opening_date = (request.form.get("opening_date") or None)

        credit_limit = None
        if acct_type == 'credit_card':
            credit_limit = parse_money_to_cents_strict(
                request.form.get("credit_limit"),
                field_name="Credit limit",
                allow_blank=True,
            )
            if credit_limit < 0:
                raise ValueError("Credit limit must be zero or greater.")

        initial_value_cents = None
        if acct_type == 'investment':
            raw_initial_value = request.form.get('initial_value')
            if raw_initial_value is None or raw_initial_value.strip() == "":
                initial_value_cents = opening_balance_cents or 0
            else:
                initial_value_cents = parse_money_to_cents_strict(
                    raw_initial_value,
                    field_name="Initial value",
                )

        original_principal_cents = None
        normal_monthly_payment_cents = None
        if acct_type == 'loan':
            original_principal_cents = parse_money_to_cents_strict(
                request.form.get("original_principal"),
                field_name="Original principal",
                allow_blank=True,
            )
            if original_principal_cents < 0:
                raise ValueError("Original principal must be zero or greater.")
            normal_monthly_payment_cents = parse_money_to_cents_strict(
                request.form.get("normal_monthly_payment"),
                field_name="Normal monthly payment",
                allow_blank=True,
            )
            if normal_monthly_payment_cents < 0:
                raise ValueError("Normal monthly payment must be zero or greater.")
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('accounts.list_accounts'))

    payload = {
        'name': name,
        'account_type': acct_type,
        'opening_balance_cents': opening_balance_cents,
        'opening_date': opening_date,
    }

    with unit_of_work() as db:
        new_id = accounts_repo.insert_account(payload, db=db)
        envelopes_repo.ensure_locked_unallocated_envelope(
            new_id,
            account_type=acct_type,
            db=db,
        )

        if opening_balance_cents:
            # For asset accounts (bank/invest) opening is positive funds => positive amount
            # For liability accounts (credit_card/loan) opening is debt => negative amount
            if acct_type in ('credit_card', 'loan'):
                signed_cents = -abs(opening_balance_cents)
                ttype = 'expense'
            else:
                signed_cents = abs(opening_balance_cents)
                ttype = 'income'

            transactions_repo.insert_transaction(
                db=db,
                account_id=new_id,
                ttype=ttype,
                amount_cents=signed_cents,
                posted_at=opening_date,
                payee="Opening balance",
                memo="Initial balance at account creation",
                ignore_match=1,
            )

        # save credit limit for credit cards
        if acct_type == 'credit_card':
            credit_repo.set_credit_limit(new_id, credit_limit or 0, db=db)

        if acct_type == 'loan':
            loans_repo.upsert_loan_details(
                new_id,
                original_principal_cents=original_principal_cents or None,
                normal_monthly_payment_cents=normal_monthly_payment_cents or None,
                db=db,
            )

        # Investment: create initial valuation (defaults to opening balance if empty)
        if acct_type == 'investment':
            invest_repo.insert_valuation({
                'account_id': new_id,
                'asof_date': opening_date,
                'value_cents': initial_value_cents or 0,
                'note': 'Initial valuation',
            }, db=db)

    flash('Account added', 'success')
    return redirect(url_for('accounts.account_dashboard', account_id=new_id))

@bp.route('/<int:account_id>/edit', methods=['GET','POST'], endpoint='account_edit')
def account_edit(account_id: int):
    a = accounts_repo.get_account(account_id)
    if not a:
        abort(404)

    editable_type_options = []
    if a.get('account_type') in ('bank', 'credit_card'):
        editable_type_options = [
            ('bank', 'Bank'),
            ('credit_card', 'Credit Card'),
        ]

    if request.method == 'GET':
        credit_limit_cents = credit_repo.get_credit_limit(account_id) if a.get('account_type') == 'credit_card' else None
        loan = loans_repo.get_loan(account_id) if a.get('account_type') == 'loan' else None
        return render_template(
            'account_edit.html',
            a=a,
            editable_type_options=editable_type_options,
            credit_limit_cents=credit_limit_cents,
            loan=loan,
        )

    # POST
    name = request.form.get('name', '').strip()
    current_type = a.get('account_type') or 'bank'
    requested_type = request.form.get('account_type', current_type)

    if requested_type != current_type and (current_type not in ('bank', 'credit_card') or requested_type not in ('bank', 'credit_card')):
        flash('This edit flow currently supports only Bank ↔ Credit Card conversions.', 'warning')
        return redirect(url_for('accounts.account_edit', account_id=account_id))

    if request.form.get('clear_opening'):
        opening_balance_cents = None
        opening_date = None
    else:
        opening_balance_cents = parse_money_to_cents(request.form.get('opening_balance'))
        opening_date = request.form.get('opening_date') or None

    bankid = request.form.get('bankid') or None
    acctid = request.form.get('acctid') or None

    credit_limit_cents = None
    if requested_type == 'credit_card':
        try:
            credit_limit_cents = parse_money_to_cents_strict(
                request.form.get('credit_limit'),
                field_name='Credit limit',
                allow_blank=True,
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('accounts.account_edit', account_id=account_id))
        if credit_limit_cents < 0:
            flash('Credit limit must be zero or greater.', 'warning')
            return redirect(url_for('accounts.account_edit', account_id=account_id))

    original_principal_cents = None
    normal_monthly_payment_cents = None
    if current_type == 'loan' and requested_type == 'loan':
        try:
            original_principal_cents = parse_money_to_cents_strict(
                request.form.get('original_principal'),
                field_name='Original principal',
                allow_blank=True,
            )
            normal_monthly_payment_cents = parse_money_to_cents_strict(
                request.form.get('normal_monthly_payment'),
                field_name='Normal monthly payment',
                allow_blank=True,
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('accounts.account_edit', account_id=account_id))
        if original_principal_cents < 0:
            flash('Original principal must be zero or greater.', 'warning')
            return redirect(url_for('accounts.account_edit', account_id=account_id))
        if normal_monthly_payment_cents < 0:
            flash('Normal monthly payment must be zero or greater.', 'warning')
            return redirect(url_for('accounts.account_edit', account_id=account_id))

    accounts_repo.update_account(account_id, {
        'name': name,
        'account_type': requested_type,
        'bankid': bankid,
        'acctid': acctid,
        'opening_balance_cents': opening_balance_cents,
        'opening_date': opening_date,
    })

    if requested_type == 'credit_card':
        credit_repo.set_credit_limit(account_id, credit_limit_cents or 0)
    elif current_type == 'credit_card' and requested_type == 'bank':
        credit_repo.delete_credit_limit(account_id)

    if current_type == 'loan' and requested_type == 'loan':
        loans_repo.upsert_loan_details(
            account_id,
            original_principal_cents=original_principal_cents or None,
            normal_monthly_payment_cents=normal_monthly_payment_cents or None,
        )

    flash('Account updated', 'success')
    return redirect(url_for('accounts.account_dashboard', account_id=account_id))


@bp.post('/<int:account_id>/delete', endpoint='account_delete')
def account_delete(account_id: int):
    savings_dependencies = savings_repo.account_dependencies(account_id)
    if savings_dependencies:
        dependency_text = ", ".join(savings_dependencies)
        flash(
            f"This account is used as {dependency_text}. Reassign or remove those savings settings before deleting it.",
            "warning",
        )
        return redirect(url_for('accounts.list_accounts'))
    accounts_repo.delete_account(account_id)
    flash('Account deleted', 'info')
    return redirect(url_for('accounts.list_accounts'))
