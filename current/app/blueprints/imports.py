import json

from flask import Blueprint, current_app, flash, redirect, render_template_string, request, url_for
from markupsafe import Markup, escape

from ..repositories.accounts_repo import list_accounts, get_account, update_account
from ..repositories.aggregates_repo import get_account_envelope_balances
from ..repositories.envelopes_repo import list_envelopes
from ..repositories.transactions_repo import (
    list_transactions, get_transaction, list_fitids_for_account, list_imported_fitid_rows_for_account
)  # read-only helpers
from ..repositories.import_provenance_repo import (
    delete_import_session_provenance,
    get_import_session_undo_candidate,
    latest_import_session_id_for_account,
    list_import_provenance_matches,
    list_import_matched_transaction_ids,
    record_import_session_rows,
)
from .imports_api import register_import_api_routes
from .imports_commit import register_import_commit_routes
from .imports_drafts import register_import_draft_routes
from .imports_review import register_import_review_routes
from ..repositories.import_review_drafts_repo import (
    cleanup_expired_import_review_drafts,
    get_import_review_draft,
)
from ..repositories.import_review_sources_repo import (
    cleanup_expired_import_review_sources,
    create_import_review_source,
    get_import_review_source,
)
from ..repositories import import_matching_rules_repo
from ..repositories import import_rule_proposals_repo
from ..services.import_matching_rule_service import parse_rule_form
from ..services.import_rule_proposal_service import (
    approve_import_rule_proposal,
    ignore_import_rule_proposal,
    refresh_import_rule_proposals,
    reject_import_rule_proposal,
)
from ..services.transactions_service import TransactionsService

bp = Blueprint('imports', __name__)


register_import_review_routes(
    bp,
    list_accounts_func=list_accounts,
    list_fitids_func=list_fitids_for_account,
    list_envelopes_func=list_envelopes,
    account_envelope_balances_func=get_account_envelope_balances,
    list_transactions_func=list_transactions,
    get_transaction_func=get_transaction,
    list_import_provenance_matches_func=list_import_provenance_matches,
    get_import_review_draft_func=get_import_review_draft,
    cleanup_import_review_drafts_func=cleanup_expired_import_review_drafts,
    create_import_review_source_func=create_import_review_source,
    cleanup_import_review_sources_func=cleanup_expired_import_review_sources,
)

register_import_commit_routes(
    bp,
    get_account_func=get_account,
    update_account_func=update_account,
    list_fitids_func=list_fitids_for_account,
    get_transaction_func=get_transaction,
    edit_transaction_func=TransactionsService.edit_transaction,
    create_transfer_func=TransactionsService.create_transfer,
    create_expense_func=TransactionsService.create_expense,
    create_income_func=TransactionsService.create_income,
    record_import_provenance_func=record_import_session_rows,
    get_import_review_source_func=get_import_review_source,
    get_import_session_undo_candidate_func=get_import_session_undo_candidate,
    latest_import_session_id_for_account_func=latest_import_session_id_for_account,
    delete_import_session_provenance_func=delete_import_session_provenance,
    delete_transaction_func=TransactionsService.delete_transaction,
)


register_import_draft_routes(bp)


RULES_TEMPLATE = """
{% extends "layout.html" %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0">Import Rules</h4>
  <div class="d-flex gap-2">
    <a class="btn btn-outline-secondary" href="{{ url_for('imports.rule_proposals_list') }}">Review Proposals</a>
    <button type="button" class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#createImportRuleModal">Create Rule</button>
  </div>
</div>

{% if rule is not none %}
  <div class="border rounded p-3 mb-4">
    <h5 class="mb-3">{{ 'Edit Rule' if rule.get('id') else 'New Rule' }}</h5>
    <form method="post" action="{{ url_for('imports.rules_update', rule_id=rule.id) if rule.get('id') else url_for('imports.rules_create') }}">
      <input type="hidden" name="return_to" value="{{ url_for('imports.rules_list') }}">
      {{ rule_form_fields(rule, rule_accounts, rule_envelopes, 'rulePage') | safe }}
      <div class="d-flex gap-2 mt-3">
        <button class="btn btn-primary">{{ 'Save Rule' if rule.get('id') else 'Create Rule' }}</button>
        <a class="btn btn-outline-secondary" href="{{ url_for('imports.rules_list') }}">Cancel</a>
      </div>
    </form>
  </div>
{% endif %}

<div class="table-responsive">
  <table class="table table-sm align-middle">
    <thead>
      <tr>
        <th>Name</th>
        <th>Scope</th>
        <th>Priority</th>
        <th>Enabled</th>
        <th>Uses</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for item in rules %}
        <tr>
          <td>{{ item.name }}</td>
          <td>{{ accounts_map.get(item.account_id, 'All accounts') if item.account_id else 'All accounts' }}</td>
          <td>{{ item.priority }}</td>
          <td>{{ 'Yes' if item.enabled else 'No' }}</td>
          <td>{{ item.use_count or 0 }}</td>
          <td class="text-end">
            <a class="btn btn-sm btn-outline-primary" href="{{ url_for('imports.rules_edit', rule_id=item.id) }}">Edit</a>
            <form method="post" action="{{ url_for('imports.rules_delete', rule_id=item.id) }}" class="d-inline" onsubmit="return confirm('Delete this import rule?')">
              <button class="btn btn-sm btn-outline-danger">Delete</button>
            </form>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="6" class="text-muted">No import rules yet.</td></tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
"""


RULE_PROPOSALS_TEMPLATE = """
{% extends "layout.html" %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0">Import Rule Proposals</h4>
  <a class="btn btn-outline-secondary" href="{{ url_for('imports.rules_list') }}">Back to Rules</a>
</div>

<form class="border rounded p-3 mb-4" method="post" action="{{ url_for('imports.rule_proposals_refresh') }}">
  <div class="row g-3 align-items-end">
    <div class="col-12 col-md-8">
      <label class="form-label" for="proposalRefreshAccount">Account</label>
      <select class="form-select" id="proposalRefreshAccount" name="account_id" required>
        <option value="">Choose account...</option>
        {% for account in rule_accounts %}
          <option value="{{ account.id }}"{{ ' selected' if selected_account_id and selected_account_id == account.id else '' }}>{{ account.name }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 col-md-4 text-md-end">
      <button class="btn btn-primary">Refresh Proposals</button>
    </div>
  </div>
</form>

<div class="d-flex gap-2 mb-3">
  <a class="btn btn-sm {{ 'btn-secondary' if status_filter == 'pending' else 'btn-outline-secondary' }}" href="{{ url_for('imports.rule_proposals_list', status='pending', account_id=selected_account_id) }}">Pending</a>
  <a class="btn btn-sm {{ 'btn-secondary' if status_filter == 'all' else 'btn-outline-secondary' }}" href="{{ url_for('imports.rule_proposals_list', status='all', account_id=selected_account_id) }}">All</a>
</div>

{% for proposal in proposals %}
  {% set evidence = proposal.evidence_json or {} %}
  {% set is_stale = evidence.refresh_status == 'stale_source_changed' %}
  <div class="border rounded p-3 mb-3">
    <div class="d-flex flex-column flex-md-row justify-content-between gap-2">
      <div>
        <h5 class="mb-1">{{ proposal.suggested_rule_json.name or 'Suggested import rule' }}</h5>
        <div class="text-muted small">
          {{ accounts_map.get(proposal.account_id, 'Unknown account') }} · {{ proposal.status }}{% if is_stale %} · stale source evidence{% endif %} · last seen {{ proposal.last_seen_at }}
        </div>
        {% if proposal.reviewed_at %}
          <div class="text-muted small">Reviewed {{ proposal.reviewed_at }}{% if proposal.reviewer_decision %} · {{ proposal.reviewer_decision }}{% endif %}</div>
        {% endif %}
      </div>
      {% if proposal.status == 'pending' %}
        <div class="d-flex flex-wrap gap-2">
          {% if not is_stale %}
            <form method="post" action="{{ url_for('imports.rule_proposals_approve', proposal_id=proposal.id) }}">
              <input type="hidden" name="enabled" value="0">
              <button class="btn btn-sm btn-primary">Approve Disabled</button>
            </form>
            <form method="post" action="{{ url_for('imports.rule_proposals_approve', proposal_id=proposal.id) }}">
              <input type="hidden" name="enabled" value="1">
              <button class="btn btn-sm btn-outline-primary">Approve Enabled</button>
            </form>
          {% endif %}
          <form method="post" action="{{ url_for('imports.rule_proposals_reject', proposal_id=proposal.id) }}">
            <button class="btn btn-sm btn-outline-danger">Reject</button>
          </form>
          <form method="post" action="{{ url_for('imports.rule_proposals_ignore', proposal_id=proposal.id) }}">
            <button class="btn btn-sm btn-outline-secondary">Ignore</button>
          </form>
        </div>
      {% endif %}
    </div>

    <div class="row g-3 mt-1">
      <div class="col-12 col-lg-6">
        <div class="small text-muted mb-1">Predicate</div>
        <pre class="small bg-body-tertiary border rounded p-2 mb-0">{{ proposal.condition_json | tojson(indent=2) }}</pre>
      </div>
      <div class="col-12 col-lg-6">
        <div class="small text-muted mb-1">Action</div>
        <pre class="small bg-body-tertiary border rounded p-2 mb-0">{{ proposal.action_json | tojson(indent=2) }}</pre>
      </div>
      <div class="col-12">
        <div class="small text-muted mb-1">Evidence</div>
        <div class="small">
          Support examples: {{ evidence.support_examples or 0 }};
          distinct raw identities: {{ evidence.distinct_raw_identities or 0 }};
          distinct transactions: {{ evidence.distinct_transactions or 0 }};
          feedback accepted: {{ evidence.feedback_accepted or 0 }};
          modified: {{ evidence.feedback_modified or 0 }};
          rejected: {{ evidence.feedback_rejected or 0 }}.
        </div>
        {% if proposal.reason_codes_json %}
          <div class="small mt-2">
            Reasons: {{ proposal.reason_codes_json | join(', ') }}
          </div>
        {% endif %}
        {% if evidence.sources %}
          <div class="small mt-2">
            Sources:
            {% for source, count in evidence.sources.items() %}
              {{ source }} {{ count }}{% if not loop.last %};{% endif %}
            {% endfor %}
          </div>
        {% endif %}
        {% if evidence.existing_rule_outcomes %}
          <div class="small mt-2">
            Existing rule overlap:
            {% for rule in evidence.existing_rule_outcomes %}
              #{{ rule.id }} {{ rule.name }}{% if rule.use_count %} used {{ rule.use_count }}x{% endif %}{% if not loop.last %};{% endif %}
            {% endfor %}
          </div>
        {% endif %}
        {% if evidence.raw_samples %}
          <ul class="small mb-0 mt-2">
            {% for sample in evidence.raw_samples %}
              <li>{{ sample.payee }}{% if sample.memo %} · {{ sample.memo }}{% endif %}</li>
            {% endfor %}
          </ul>
        {% endif %}
      </div>
      {% if is_stale %}
        <div class="col-12">
          <div class="alert alert-warning py-2 mb-0">
            Source evidence did not appear in the latest successful proposal refresh. Last known support examples: {{ evidence.last_known_support_examples or 0 }}.
          </div>
        </div>
      {% endif %}
      {% if proposal.validation_errors_json %}
        <div class="col-12">
          <div class="alert alert-warning py-2 mb-0">
            {{ proposal.validation_errors_json | join(' ') }}
          </div>
        </div>
      {% endif %}
      {% if proposal.approved_rule_id %}
        <div class="col-12 small">
          Approved rule:
          <a href="{{ url_for('imports.rules_edit', rule_id=proposal.approved_rule_id) }}">#{{ proposal.approved_rule_id }}</a>
        </div>
      {% endif %}
    </div>
  </div>
{% else %}
  <div class="text-muted">No import rule proposals to review.</div>
{% endfor %}
{% endblock %}
"""


def _rule_template_context(rule=None, selected_rule_account_id=None):
    rule_accounts = list_accounts()
    rule_envelopes = list_envelopes()
    return {
        "rules": import_matching_rules_repo.list_import_matching_rules(
            account_id=selected_rule_account_id,
            include_disabled=True,
        ),
        "rule": rule,
        "selected_rule_account_id": selected_rule_account_id,
        "rule_accounts": rule_accounts,
        "rule_envelopes": rule_envelopes,
        "rule_form_fields": _rule_form_fields,
    }


def _proposal_template_context(selected_account_id=None, status_filter="pending"):
    rule_accounts = list_accounts()
    include_decided = status_filter == "all"
    status = None if include_decided else "pending"
    return {
        "proposals": import_rule_proposals_repo.list_import_rule_proposals(
            account_id=selected_account_id,
            status=status,
            include_decided=include_decided,
        ),
        "rule_accounts": rule_accounts,
        "selected_account_id": selected_account_id,
        "status_filter": status_filter,
    }


def _selected(value, selected_value) -> str:
    return " selected" if str(value) == str(selected_value) else ""


def _checked(value) -> str:
    return " checked" if value else ""


def _rule_form_fields(rule, accounts, envelopes, prefix: str) -> Markup:
    rule = rule or {}
    condition = rule.get("condition_json") or {}
    action = rule.get("action_json") or {}
    split_remainder_json = json.dumps(action.get("split_remainder") or {}, sort_keys=True, separators=(",", ":"))
    transfer_json = json.dumps(action.get("transfer") or {}, sort_keys=True, separators=(",", ":"))
    manual_match_json = json.dumps(action.get("manual_match") or {}, sort_keys=True, separators=(",", ":"))
    account_id = rule.get("account_id")
    scope = "global" if account_id is None and rule.get("id") else "account"
    if not rule.get("id") and account_id is None:
        scope = "account"
    enabled = int(rule.get("enabled", 1)) != 0
    options_accounts = ['<option value="">Choose account...</option>']
    for account in accounts or []:
        options_accounts.append(
            f'<option value="{escape(account.get("id"))}"{_selected(account.get("id"), account_id)}>'
            f'{escape(account.get("name") or "")}</option>'
        )
    options_envelopes = ['<option value="">No envelope action</option>']
    for envelope in envelopes or []:
        if envelope.get("archived"):
            continue
        options_envelopes.append(
            f'<option value="{escape(envelope.get("id"))}"{_selected(envelope.get("id"), action.get("single_envelope_id"))}>'
            f'{escape(envelope.get("name") or "")}</option>'
        )
    html = f"""
    <div class="row g-3">
      <input type="hidden" name="action_split_remainder_json" value="{escape(split_remainder_json if action.get('split_remainder') else '')}">
      <input type="hidden" name="action_transfer_json" value="{escape(transfer_json if action.get('transfer') else '')}">
      <input type="hidden" name="action_manual_match_json" value="{escape(manual_match_json if action.get('manual_match') else '')}">
      <div class="col-12 col-md-6">
        <label class="form-label" for="{escape(prefix)}RuleName">Name</label>
        <input class="form-control" id="{escape(prefix)}RuleName" name="name" value="{escape(rule.get('name') or '')}" required>
      </div>
      <div class="col-6 col-md-3">
        <label class="form-label" for="{escape(prefix)}RulePriority">Priority</label>
        <input class="form-control" id="{escape(prefix)}RulePriority" name="priority" type="number" value="{escape(rule.get('priority') or 100)}">
      </div>
      <div class="col-6 col-md-3 d-flex align-items-end">
        <div class="form-check">
          <input class="form-check-input" type="checkbox" value="1" id="{escape(prefix)}RuleEnabled" name="enabled"{_checked(enabled)}>
          <label class="form-check-label" for="{escape(prefix)}RuleEnabled">Enabled</label>
        </div>
      </div>
      <div class="col-12 col-md-4">
        <label class="form-label" for="{escape(prefix)}RuleScope">Scope</label>
        <select class="form-select" id="{escape(prefix)}RuleScope" name="account_scope" data-rule-account-scope>
          <option value="account"{_selected('account', scope)}>Selected account</option>
          <option value="global"{_selected('global', scope)}>All accounts</option>
        </select>
      </div>
      <div class="col-12 col-md-8">
        <label class="form-label" for="{escape(prefix)}RuleAccount">Account</label>
        <select class="form-select" id="{escape(prefix)}RuleAccount" name="account_id" data-rule-account-select>
          {''.join(options_accounts)}
        </select>
      </div>
      <div class="col-12"><hr class="my-1"></div>
      <div class="col-12 col-md-3">
        <label class="form-label" for="{escape(prefix)}RuleDirection">Direction</label>
        <select class="form-select" id="{escape(prefix)}RuleDirection" name="direction">
          <option value="any"{_selected('any', condition.get('direction') or 'any')}>Any</option>
          <option value="expense"{_selected('expense', condition.get('direction'))}>Expense</option>
          <option value="income"{_selected('income', condition.get('direction'))}>Income</option>
        </select>
      </div>
      <div class="col-12 col-md-3">
        <label class="form-label" for="{escape(prefix)}RuleMatchField">Match Field</label>
        <select class="form-select" id="{escape(prefix)}RuleMatchField" name="match_field">
          <option value="text"{_selected('text', condition.get('field') or 'text')}>Payee + memo</option>
          <option value="payee"{_selected('payee', condition.get('field'))}>Payee/source</option>
          <option value="memo"{_selected('memo', condition.get('field'))}>Memo</option>
        </select>
      </div>
      <div class="col-12 col-md-3">
        <label class="form-label" for="{escape(prefix)}RuleOperator">Operator</label>
        <select class="form-select" id="{escape(prefix)}RuleOperator" name="match_operator">
          <option value="contains"{_selected('contains', condition.get('operator') or 'contains')}>Contains</option>
          <option value="equals"{_selected('equals', condition.get('operator'))}>Equals</option>
          <option value="starts_with"{_selected('starts_with', condition.get('operator'))}>Starts with</option>
        </select>
      </div>
      <div class="col-12 col-md-3">
        <label class="form-label" for="{escape(prefix)}RuleMatchValue">Match Text</label>
        <input class="form-control" id="{escape(prefix)}RuleMatchValue" name="match_value" value="{escape(condition.get('value') or '')}">
      </div>
      <div class="col-6 col-md-3">
        <label class="form-label" for="{escape(prefix)}RuleAmountMin">Min Amount</label>
        <input class="form-control text-end" id="{escape(prefix)}RuleAmountMin" name="amount_min" type="number" step="0.01" value="{escape((condition.get('amount_min_cents') or '') and (int(condition.get('amount_min_cents')) / 100))}">
      </div>
      <div class="col-6 col-md-3">
        <label class="form-label" for="{escape(prefix)}RuleAmountMax">Max Amount</label>
        <input class="form-control text-end" id="{escape(prefix)}RuleAmountMax" name="amount_max" type="number" step="0.01" value="{escape((condition.get('amount_max_cents') or '') and (int(condition.get('amount_max_cents')) / 100))}">
      </div>
      <div class="col-12"><hr class="my-1"></div>
      <div class="col-12 col-md-4">
        <label class="form-label" for="{escape(prefix)}RuleActionPayee">Set Payee/Source</label>
        <input class="form-control" id="{escape(prefix)}RuleActionPayee" name="action_payee" value="{escape(action.get('payee') or '')}">
      </div>
      <div class="col-12 col-md-4">
        <label class="form-label" for="{escape(prefix)}RuleActionMemo">Set Memo</label>
        <input class="form-control" id="{escape(prefix)}RuleActionMemo" name="action_memo" value="{escape(action.get('memo') or '')}">
      </div>
      <div class="col-12 col-md-4">
        <label class="form-label" for="{escape(prefix)}RuleActionType">Set Type</label>
        <select class="form-select" id="{escape(prefix)}RuleActionType" name="action_transaction_type">
          <option value="">No type action</option>
          <option value="expense"{_selected('expense', action.get('transaction_type'))}>Expense</option>
          <option value="income"{_selected('income', action.get('transaction_type'))}>Income</option>
        </select>
      </div>
      <div class="col-12 col-md-6">
        <label class="form-label" for="{escape(prefix)}RuleActionEnvelope">Set Single Envelope</label>
        <select class="form-select" id="{escape(prefix)}RuleActionEnvelope" name="action_envelope_id">
          {''.join(options_envelopes)}
        </select>
      </div>
    </div>
    """
    return Markup(html)


@bp.get("/rules")
def rules_list():
    account_id = request.args.get("account_id", type=int)
    return render_template_string(RULES_TEMPLATE, **_rule_template_context(selected_rule_account_id=account_id))


@bp.get("/rules/new")
def rules_new():
    return render_template_string(RULES_TEMPLATE, **_rule_template_context(rule={}))


@bp.post("/rules", endpoint="rules_create")
def rules_create():
    data, errors = parse_rule_form(request.form)
    if errors:
        for error in errors:
            flash(error, "warning")
        return redirect(request.form.get("return_to") or url_for("imports.rules_new"))
    rule_id = import_matching_rules_repo.create_import_matching_rule(data or {})
    flash("Import rule created.", "success")
    return redirect(request.form.get("return_to") or url_for("imports.rules_list", created=rule_id))


@bp.get("/rules/<int:rule_id>/edit")
def rules_edit(rule_id: int):
    rule = import_matching_rules_repo.get_import_matching_rule(rule_id)
    if not rule:
        flash("Import rule not found.", "warning")
        return redirect(url_for("imports.rules_list"))
    return render_template_string(
        RULES_TEMPLATE,
        **_rule_template_context(rule=rule, selected_rule_account_id=rule.get("account_id")),
    )


@bp.post("/rules/<int:rule_id>/edit")
def rules_update(rule_id: int):
    data, errors = parse_rule_form(request.form)
    if errors:
        for error in errors:
            flash(error, "warning")
        return redirect(url_for("imports.rules_edit", rule_id=rule_id))
    if import_matching_rules_repo.update_import_matching_rule(rule_id, data or {}):
        flash("Import rule updated.", "success")
    else:
        flash("Import rule not found.", "warning")
    return redirect(url_for("imports.rules_list"))


@bp.post("/rules/<int:rule_id>/delete")
def rules_delete(rule_id: int):
    if import_matching_rules_repo.delete_import_matching_rule(rule_id):
        flash("Import rule deleted.", "success")
    else:
        flash("Import rule not found.", "warning")
    return redirect(url_for("imports.rules_list"))


@bp.get("/rule-proposals")
def rule_proposals_list():
    account_id = request.args.get("account_id", type=int)
    status_filter = request.args.get("status") or "pending"
    if status_filter not in {"pending", "all"}:
        status_filter = "pending"
    return render_template_string(
        RULE_PROPOSALS_TEMPLATE,
        **_proposal_template_context(selected_account_id=account_id, status_filter=status_filter),
    )


@bp.post("/rule-proposals/refresh")
def rule_proposals_refresh():
    account_id = request.form.get("account_id", type=int)
    if not account_id:
        flash("Choose an account to refresh proposals.", "warning")
        return redirect(url_for("imports.rule_proposals_list"))
    try:
        result = refresh_import_rule_proposals(account_id=account_id)
    except Exception as ex:
        current_app.logger.exception("IMPORT RULE PROPOSALS: manual refresh failed for account %s: %s", account_id, ex)
        flash("Proposal refresh failed. Import review and transactions were not changed.", "warning")
        return redirect(url_for("imports.rule_proposals_list", account_id=account_id))
    flash(
        (
            f"Proposal refresh complete: {result['created']} new, {result['deduped']} existing, "
            f"{result.get('stale', 0)} stale, {len(result.get('withheld') or [])} withheld."
        ),
        "success",
    )
    return redirect(url_for("imports.rule_proposals_list", account_id=account_id))


@bp.post("/rule-proposals/<int:proposal_id>/approve")
def rule_proposals_approve(proposal_id: int):
    enabled = str(request.form.get("enabled") or "").strip() == "1"
    result = approve_import_rule_proposal(proposal_id, enabled=enabled)
    if result.ok:
        flash(
            "Import rule approved and created enabled." if enabled else "Import rule approved and created disabled.",
            "success",
        )
    else:
        flash(result.message, "warning")
    return redirect(url_for("imports.rule_proposals_list"))


@bp.post("/rule-proposals/<int:proposal_id>/reject")
def rule_proposals_reject(proposal_id: int):
    result = reject_import_rule_proposal(proposal_id)
    flash(result.message, "success" if result.ok else "warning")
    return redirect(url_for("imports.rule_proposals_list"))


@bp.post("/rule-proposals/<int:proposal_id>/ignore")
def rule_proposals_ignore(proposal_id: int):
    result = ignore_import_rule_proposal(proposal_id)
    flash(result.message, "success" if result.ok else "warning")
    return redirect(url_for("imports.rule_proposals_list"))


register_import_api_routes(
    bp,
    list_transactions_func=list_transactions,
    get_transaction_func=get_transaction,
    list_imported_fitid_rows_func=list_imported_fitid_rows_for_account,
    list_import_provenance_matches_func=list_import_provenance_matches,
    list_import_matched_transaction_ids_func=list_import_matched_transaction_ids,
    get_import_review_source_func=get_import_review_source,
)
