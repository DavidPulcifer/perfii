from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from ..repositories import envelopes_repo
from ..utils import parse_money_to_cents_strict

bp = Blueprint('envelopes', __name__)

def _parse_default_amount(form) -> int:
    if form.get('default_amount_cents', '').strip():
        try:
            return int(form.get('default_amount_cents'))
        except ValueError:
            raise ValueError('Default amount cents must be a whole number of cents.')
    return parse_money_to_cents_strict(
        form.get('default_amount', '0'),
        field_name='Default amount',
        allow_blank=True,
    )

@bp.get('/')
def list_():
    envs = envelopes_repo.list_envelopes()
    archived_envs = envelopes_repo.list_archived_envelopes()
    return render_template('envelopes.html', envelopes=envs, archived_envelopes=archived_envs)

def _envelope_activity_summary(activity):
    current_total_cents = sum(int(row.get("split_amount_cents") or 0) for row in activity)
    amounts_in_cents = sum(
        int(row.get("split_amount_cents") or 0)
        for row in activity
        if int(row.get("split_amount_cents") or 0) > 0
    )
    amounts_out_cents = abs(sum(
        int(row.get("split_amount_cents") or 0)
        for row in activity
        if int(row.get("split_amount_cents") or 0) < 0
    ))
    return current_total_cents, amounts_in_cents, amounts_out_cents


@bp.get('/by-name/<path:envelope_name>/', endpoint='detail_by_name')
def detail_by_name(envelope_name: str):
    envelope_group = envelopes_repo.list_active_envelopes_by_name(envelope_name)
    if not envelope_group:
        abort(404)

    activity = envelopes_repo.get_envelope_activity_for_ids(e["id"] for e in envelope_group)
    current_total_cents, amounts_in_cents, amounts_out_cents = _envelope_activity_summary(activity)

    return render_template(
        'envelope_detail.html',
        envelope={"name": envelope_name, "archived_at": None, "locked_account_id": None},
        envelope_group=envelope_group,
        activity=activity,
        current_total_cents=current_total_cents,
        amounts_in_cents=amounts_in_cents,
        amounts_out_cents=amounts_out_cents,
    )


@bp.get('/<int:envelope_id>/', endpoint='detail')
def detail(envelope_id: int):
    envelope = envelopes_repo.get_envelope(envelope_id)
    if not envelope:
        abort(404)

    activity = envelopes_repo.get_envelope_activity(envelope_id)
    current_total_cents, amounts_in_cents, amounts_out_cents = _envelope_activity_summary(activity)

    return render_template(
        'envelope_detail.html',
        envelope=envelope,
        envelope_group=None,
        activity=activity,
        current_total_cents=current_total_cents,
        amounts_in_cents=amounts_in_cents,
        amounts_out_cents=amounts_out_cents,
    )

@bp.post('/new', endpoint='envelope_new')
def envelope_new():
    form = request.form
    name = form.get('name','').strip()
    locked_account_id = form.get('locked_account_id', type=int)

    if not name:
        flash('Name required', 'warning')
        return redirect(url_for('envelopes.list_'))

    try:
        default_amount_cents = _parse_default_amount(form)
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('envelopes.list_'))

    envelopes_repo.insert_envelope({
        'name': name,
        'locked_account_id': locked_account_id,
        'default_amount_cents': default_amount_cents,
    })
    flash('Envelope created', 'success')
    return redirect(url_for('envelopes.list_'))

@bp.route('/<int:envelope_id>/edit', methods=['GET', 'POST'], endpoint='envelope_edit')
def envelope_edit(envelope_id: int):
    if request.method == 'GET':
        env = envelopes_repo.get_envelope(envelope_id)
        if not env:
            abort(404)
        return render_template('envelope_edit.html', env=env)

    # POST
    form = request.form
    name = form.get('name', '').strip()
    locked_account_id = form.get('locked_account_id', type=int)

    try:
        default_amount_cents = _parse_default_amount(form)
    except ValueError as e:
        flash(str(e), 'warning')
        return redirect(url_for('envelopes.envelope_edit', envelope_id=envelope_id))

    envelopes_repo.update_envelope(envelope_id, {
        'name': name,
        'locked_account_id': locked_account_id,
        'default_amount_cents': default_amount_cents,
    })
    flash('Envelope updated', 'success')
    return redirect(url_for('envelopes.list_'))

@bp.post('/<int:envelope_id>/delete', endpoint='envelope_delete')
def envelope_delete(envelope_id: int):
    envelopes_repo.archive_envelope(envelope_id)
    flash('Envelope archived. Existing transaction splits were preserved.', 'info')
    return redirect(url_for('envelopes.list_'))

@bp.post('/<int:envelope_id>/restore', endpoint='envelope_restore')
def envelope_restore(envelope_id: int):
    envelopes_repo.restore_envelope(envelope_id)
    flash('Envelope restored', 'success')
    return redirect(url_for('envelopes.list_'))
