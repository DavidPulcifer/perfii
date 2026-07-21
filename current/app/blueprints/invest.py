# app/blueprints/invest.py
import re
from datetime import date

from flask import Blueprint, request, redirect, url_for, flash, render_template, abort
from ..repositories import invest_repo, accounts_repo
from ..db import get_db
from ..utils import parse_money_to_cents_strict

bp = Blueprint('invest', __name__)


def _parse_valuation_date(raw: str | None) -> str:
    value = (raw or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Valuation date must be a valid date.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Valuation date must be a valid date.")


def _parse_note_date(raw: str | None) -> str:
    value = (raw or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Note date must be a valid date.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Note date must be a valid date.")


def _parse_note_body(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("Note text is required.")
    return value


def _parse_valuation_cents(raw: str | None) -> int:
    value_cents = parse_money_to_cents_strict(raw, field_name="Valuation")
    if value_cents < 0:
        raise ValueError("Valuation cannot be negative.")
    return value_cents


def _get_investment_account_or_404(account_id: int):
    acct = accounts_repo.get_account(account_id)
    if not acct or acct['account_type'] != 'investment':
        abort(404)
    return acct


def _investment_graph_range(*series: list[dict]) -> dict[str, str | None]:
    dates = []
    for points in series:
        for point in points:
            value = point.get("x")
            if not value:
                continue
            try:
                dates.append(date.fromisoformat(value))
            except ValueError:
                continue

    if not dates:
        return {
            "fullMin": None,
            "fullMax": None,
            "defaultMin": None,
            "defaultMax": None,
        }

    full_min = min(dates)
    full_max = max(dates)
    if (full_max - full_min).days < 365:
        default_min = full_min
    else:
        default_min = date(full_max.year, 1, 1)

    return {
        "fullMin": full_min.isoformat(),
        "fullMax": full_max.isoformat(),
        "defaultMin": default_min.isoformat(),
        "defaultMax": full_max.isoformat(),
    }


def _investment_note_markers(notes: list[dict], valuation_xy: list[dict]) -> list[dict]:
    if valuation_xy:
        ordered_values = sorted(valuation_xy, key=lambda point: point["x"])
    else:
        ordered_values = []

    markers = []
    for note in notes:
        marker_y = 0
        for point in ordered_values:
            if point["x"] > note["note_date"]:
                break
            marker_y = point["y"]
        if marker_y == 0 and ordered_values:
            marker_y = ordered_values[0]["y"]
        markers.append(
            {
                "id": note["id"],
                "x": note["note_date"],
                "y": marker_y,
                "body": note["body"],
            }
        )
    return markers


@bp.get('/<int:account_id>', endpoint='invest_dashboard')
def invest_dashboard(account_id: int):
    acct = _get_investment_account_or_404(account_id)

    vals = invest_repo.list_valuations(account_id)
    chart_vals = sorted(vals, key=lambda v: (v.get('asof_date') or '', v.get('id') or 0))
    display_vals = sorted(vals, key=lambda v: (v.get('asof_date') or '', v.get('id') or 0), reverse=True)
    val_xy = [{"x": v["asof_date"], "y": (v["value_cents"] or 0) / 100} for v in chart_vals]

    # --- Cumulative contributions (independent XY; continuous & extended) ---
    db = get_db()
    tx_rows = db.execute("""
        SELECT posted_at, ttype, amount_cents
        FROM transactions
        WHERE account_id = ?
        ORDER BY posted_at ASC, id ASC
    """, (account_id,)).fetchall()

    def signed(ttype: str, cents: int) -> int:
        if ttype in ('income', 'transfer_in'):  return abs(cents)
        if ttype in ('expense', 'transfer_out'): return -abs(cents)
        return 0

    contrib_xy = []
    running = 0
    for r in tx_rows:
        running += signed(r['ttype'], r['amount_cents'])
        day = r['posted_at']

        # If we already have a point for this day, update its y to the
        # latest running total instead of adding a new point.
        if contrib_xy and contrib_xy[-1]["x"] == day:
            contrib_xy[-1]["y"] = running / 100  # dollars
        else:
            contrib_xy.append({"x": day, "y": running / 100})  # dollars

    # Extend to the rightmost date among valuations or txs, so the line reaches the end
    def last_date(seq): return seq[-1]["x"] if seq else None
    last_val_date = last_date(val_xy)
    last_tx_date  = last_date(contrib_xy)
    right_edge    = max([d for d in (last_val_date, last_tx_date) if d is not None], default=None)
    if right_edge and (not contrib_xy or contrib_xy[-1]["x"] < right_edge):
        # append a final flat point at right edge
        y = contrib_xy[-1]["y"] if contrib_xy else 0
        contrib_xy.append({"x": right_edge, "y": y})

    latest_balance_cents = chart_vals[-1]['value_cents'] if chart_vals else 0
    notes = invest_repo.list_notes(account_id)
    note_markers = _investment_note_markers(notes, val_xy)
    graph_range = _investment_graph_range(val_xy, contrib_xy, note_markers)

    return render_template(
        'invest.html',
        acct=acct,
        valuations=display_vals,
        notes=notes,
        val_xy=val_xy,
        contrib_xy=contrib_xy,
        note_markers=note_markers,
        graph_range=graph_range,
        latest_balance_cents=latest_balance_cents,
        account_id=account_id,
        new_valuation_date=date.today().isoformat(),
    )

@bp.post('/valuation/new', endpoint='invest_valuation_new')
def invest_valuation_new():
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        abort(400)
    _get_investment_account_or_404(account_id)
    note = request.form.get('note')
    try:
        asof_date = _parse_valuation_date(request.form.get('asof_date'))
        value_cents = _parse_valuation_cents(request.form.get('value'))
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('invest.invest_dashboard', account_id=account_id))
    invest_repo.insert_valuation({
        'account_id': account_id,
        'asof_date': asof_date,
        'value_cents': value_cents,
        'note': note,
    })
    flash('Valuation added', 'success')
    return redirect(url_for('invest.invest_dashboard', account_id=account_id))

@bp.post('/valuation/<int:valuation_id>/edit', endpoint='invest_valuation_edit')
def invest_valuation_edit(valuation_id: int):
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        abort(400)
    _get_investment_account_or_404(account_id)
    valuation = invest_repo.get_valuation(valuation_id, account_id=account_id)
    if not valuation:
        abort(404)

    try:
        asof_date = _parse_valuation_date(request.form.get('asof_date'))
        value_cents = _parse_valuation_cents(request.form.get('value'))
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('invest.invest_dashboard', account_id=account_id))

    invest_repo.update_valuation(valuation_id, {
        'account_id': account_id,
        'asof_date': asof_date,
        'value_cents': value_cents,
        'note': request.form.get('note'),
    })
    flash('Valuation updated', 'success')
    return redirect(url_for('invest.invest_dashboard', account_id=account_id))

@bp.post('/valuation/<int:valuation_id>/delete', endpoint='invest_valuation_delete')
def invest_valuation_delete(valuation_id: int):
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        abort(400)
    invest_repo.delete_valuation(valuation_id)
    flash('Valuation deleted', 'info')
    return redirect(url_for('invest.invest_dashboard', account_id=account_id))


@bp.post('/note/new', endpoint='invest_note_new')
def invest_note_new():
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        abort(400)
    _get_investment_account_or_404(account_id)

    try:
        note_date = _parse_note_date(request.form.get('note_date'))
        body = _parse_note_body(request.form.get('body'))
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('invest.invest_dashboard', account_id=account_id))

    invest_repo.insert_note({
        'account_id': account_id,
        'note_date': note_date,
        'body': body,
    })
    flash('Investment note added', 'success')
    return redirect(url_for('invest.invest_dashboard', account_id=account_id))


@bp.post('/note/<int:note_id>/edit', endpoint='invest_note_edit')
def invest_note_edit(note_id: int):
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        abort(400)
    _get_investment_account_or_404(account_id)
    note = invest_repo.get_note(note_id, account_id=account_id)
    if not note:
        abort(404)

    try:
        note_date = _parse_note_date(request.form.get('note_date'))
        body = _parse_note_body(request.form.get('body'))
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('invest.invest_dashboard', account_id=account_id))

    invest_repo.update_note(note_id, {
        'account_id': account_id,
        'note_date': note_date,
        'body': body,
    })
    flash('Investment note updated', 'success')
    return redirect(url_for('invest.invest_dashboard', account_id=account_id))


@bp.post('/note/<int:note_id>/delete', endpoint='invest_note_delete')
def invest_note_delete(note_id: int):
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        abort(400)
    _get_investment_account_or_404(account_id)
    note = invest_repo.get_note(note_id, account_id=account_id)
    if not note:
        abort(404)

    invest_repo.delete_note(note_id, account_id=account_id)
    flash('Investment note deleted', 'info')
    return redirect(url_for('invest.invest_dashboard', account_id=account_id))
