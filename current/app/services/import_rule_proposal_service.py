from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable

from werkzeug.datastructures import MultiDict

from app.repositories import (
    import_matching_rules_repo,
    import_prefill_repo,
    import_rule_proposals_repo,
    payee_normalization_repo,
)
from app.services.import_matching_rule_service import parse_rule_form
from app.services.payee_normalization_service import (
    cleanup_part_differs_meaningfully,
    import_identity_keys,
)
from app.services.transaction_text_profile_service import (
    build_transaction_text_profile_from_row,
    merchant_cluster_signature,
)


MIN_SUPPORT_EXAMPLES = 3
MIN_DISTINCT_RAW_IDENTITIES = 2
MIN_ADVANCED_SUPPORT_EXAMPLES = 4
MIN_ADVANCED_DISTINCT_RAW_IDENTITIES = 3
SUPPORTED_TRANSACTION_TYPES = {"expense", "income"}
SUPPORTED_LEARNING_TRANSACTION_TYPES = {"expense", "income", "transfer_in", "transfer_out"}
AUTO_REFRESH_LIMIT = 500


@dataclass
class ProposalDecisionResult:
    ok: bool
    message: str
    proposal: dict[str, Any] | None = None
    rule_id: int | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class EvidenceItem:
    source: str
    account_id: int
    raw_payee: str
    raw_memo: str
    raw_identity: tuple[str, str]
    cluster_signature: str | None
    transaction_type: str | None = None
    amount_cents: int | None = None
    posted_at: str | None = None
    final_payee: str | None = None
    single_envelope_id: int | None = None
    splits: list[dict[str, Any]] = field(default_factory=list)
    remainder_intent: dict[str, Any] = field(default_factory=dict)
    paired_account_id: int | None = None
    paired_transaction: dict[str, Any] | None = None
    decision: dict[str, Any] = field(default_factory=dict)
    learning_evidence: dict[str, Any] = field(default_factory=dict)
    import_fitid: str | None = None
    row_fingerprint: str | None = None
    support_weight: int = 1
    dedupe_key: str = ""
    transaction_id: int | None = None
    learning_example_id: int | None = None
    prediction_feedback: dict[str, int] = field(default_factory=dict)


def build_import_rule_proposals(
    *,
    account_id: int,
    min_support_examples: int = MIN_SUPPORT_EXAMPLES,
    min_distinct_raw_identities: int = MIN_DISTINCT_RAW_IDENTITIES,
    limit: int = 2000,
    list_learning_examples_func: Callable[..., list[dict[str, Any]]] = import_prefill_repo.list_import_prefill_learning_examples,
    list_payee_rules_func: Callable[..., list[dict[str, Any]]] = payee_normalization_repo.list_payee_normalization_rules,
    list_existing_rules_func: Callable[..., list[dict[str, Any]]] = import_matching_rules_repo.list_import_matching_rules,
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None = None,
    list_accounts_func: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build conservative import-rule suggestions without writing or applying anything."""
    if not account_id:
        return {
            "proposals": [],
            "withheld": [],
            "source_notes": [{"source": "input", "status": "unavailable", "reason": "missing_account_id"}],
        }

    learning_examples = list_learning_examples_func(account_id=int(account_id), limit=int(limit))
    payee_rules = list_payee_rules_func(account_id=int(account_id), keys=[], min_use_count=1, limit=int(limit))
    existing_rules = list_existing_rules_func(account_id=int(account_id), include_disabled=True)

    buckets: dict[tuple[int, str], list[EvidenceItem]] = defaultdict(list)
    withheld: list[dict[str, Any]] = []
    source_counts = Counter()

    for row in learning_examples or []:
        item = _evidence_from_learning_example(row)
        if item is None:
            continue
        source_counts[item.source] += 1
        if item.cluster_signature:
            buckets[(item.account_id, item.cluster_signature)].append(item)
        else:
            withheld.append(_withheld_from_items(
                [item],
                reason_codes=["rule_proposal_predicate_too_broad"],
                detail="Raw import text does not contain a strong merchant signature.",
            ))

    for rule in payee_rules or []:
        item = _evidence_from_payee_rule(int(account_id), rule)
        if item is None:
            continue
        source_counts[item.source] += 1
        if item.cluster_signature:
            buckets[(item.account_id, item.cluster_signature)].append(item)
        else:
            withheld.append(_withheld_from_items(
                [item],
                reason_codes=["rule_proposal_predicate_too_broad"],
                detail="Payee cleanup history is tied only to weak or generic raw text.",
            ))

    proposals: list[dict[str, Any]] = []
    for (_bucket_account_id, cluster_signature), items in sorted(buckets.items(), key=lambda pair: pair[0]):
        decision = _candidate_from_bucket(
            account_id=int(account_id),
            cluster_signature=cluster_signature,
            items=items,
            existing_rules=existing_rules or [],
            min_support_examples=int(min_support_examples),
            min_distinct_raw_identities=int(min_distinct_raw_identities),
            list_envelopes_func=list_envelopes_func,
            list_accounts_func=list_accounts_func,
        )
        if decision["decision"] == "suggest":
            proposals.append(decision)
        else:
            withheld.append(decision)

    return {
        "proposals": proposals,
        "withheld": withheld,
        "source_notes": _source_notes(source_counts, learning_examples, payee_rules, existing_rules),
    }


def refresh_import_rule_proposals(
    *,
    account_id: int,
    limit: int = 2000,
    build_proposals_func: Callable[..., dict[str, Any]] = build_import_rule_proposals,
    upsert_proposal_func: Callable[..., tuple[dict[str, Any], bool]] = import_rule_proposals_repo.upsert_import_rule_proposal,
    mark_stale_func: Callable[..., int] = import_rule_proposals_repo.mark_missing_import_rule_proposals_stale,
) -> dict[str, Any]:
    """Persist suggested proposals for explicit later review without creating rules."""
    result = build_proposals_func(account_id=int(account_id), limit=int(limit))
    created = 0
    deduped = 0
    persisted: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    for proposal in result.get("proposals") or []:
        record = _proposal_record_from_candidate(proposal)
        if not record:
            continue
        seen_fingerprints.add(str(record["fingerprint"]))
        stored, was_created = upsert_proposal_func(record)
        if stored:
            persisted.append(stored)
        if was_created:
            created += 1
        else:
            deduped += 1
    stale = mark_stale_func(account_id=int(account_id), seen_fingerprints=seen_fingerprints) if mark_stale_func else 0
    return {
        "created": created,
        "deduped": deduped,
        "stale": stale,
        "persisted": persisted,
        "withheld": result.get("withheld") or [],
        "source_notes": result.get("source_notes") or [],
    }


def safe_refresh_import_rule_proposals(
    *,
    account_id: int | None,
    reason: str,
    logger=None,
    limit: int = AUTO_REFRESH_LIMIT,
    refresh_func: Callable[..., dict[str, Any]] = refresh_import_rule_proposals,
) -> dict[str, Any]:
    """Best-effort proposal refresh for commit/edit hooks; failures never block the caller."""
    if not account_id:
        return {"ok": False, "created": 0, "deduped": 0, "stale": 0, "withheld": [], "source_notes": []}
    try:
        result = refresh_func(account_id=int(account_id), limit=int(limit))
        return {"ok": True, **(result or {})}
    except Exception as ex:
        if logger:
            logger.exception("IMPORT RULE PROPOSALS: refresh failed after %s for account %s: %s", reason, account_id, ex)
        return {
            "ok": False,
            "created": 0,
            "deduped": 0,
            "stale": 0,
            "withheld": [],
            "source_notes": [],
            "error": str(ex),
        }


def approve_import_rule_proposal(
    proposal_id: int,
    *,
    enabled: bool,
    get_proposal_func: Callable[[int], dict[str, Any] | None] = import_rule_proposals_repo.get_import_rule_proposal,
    create_rule_func: Callable[[dict[str, Any]], int | None] = import_matching_rules_repo.create_import_matching_rule,
    mark_decision_func: Callable[..., bool] = import_rule_proposals_repo.mark_import_rule_proposal_decision,
    record_validation_error_func: Callable[..., bool] = import_rule_proposals_repo.record_import_rule_proposal_validation_error,
    parse_rule_form_func: Callable[..., tuple[dict[str, Any] | None, list[str]]] = parse_rule_form,
) -> ProposalDecisionResult:
    proposal = get_proposal_func(int(proposal_id))
    if not proposal:
        return ProposalDecisionResult(False, "Import rule proposal not found.")
    if proposal.get("status") != "pending":
        return ProposalDecisionResult(False, "Only pending proposals can be approved.", proposal=proposal)
    if _proposal_has_stale_source(proposal):
        return ProposalDecisionResult(
            False,
            "Proposal source evidence is stale. Refresh proposals after new matching activity before approving.",
            proposal=proposal,
        )

    rule_data, errors = _validated_rule_data_from_proposal(
        proposal,
        enabled=enabled,
        parse_rule_form_func=parse_rule_form_func,
    )
    if errors or not rule_data:
        record_validation_error_func(int(proposal_id), errors or ["Proposal payload is no longer valid."])
        return ProposalDecisionResult(
            False,
            "Proposal payload is no longer valid.",
            proposal=get_proposal_func(int(proposal_id)) or proposal,
            errors=errors,
        )

    rule_id = create_rule_func(rule_data)
    if not rule_id:
        errors = ["Import rule could not be created."]
        record_validation_error_func(int(proposal_id), errors)
        return ProposalDecisionResult(False, "Import rule could not be created.", proposal=proposal, errors=errors)

    decision = "approved_enabled" if enabled else "approved_disabled"
    mark_decision_func(
        int(proposal_id),
        status="accepted",
        reviewer_decision=decision,
        approved_rule_id=int(rule_id),
        validation_errors=[],
    )
    return ProposalDecisionResult(
        True,
        "Import rule approved.",
        proposal=get_proposal_func(int(proposal_id)) or proposal,
        rule_id=int(rule_id),
    )


def reject_import_rule_proposal(
    proposal_id: int,
    *,
    get_proposal_func: Callable[[int], dict[str, Any] | None] = import_rule_proposals_repo.get_import_rule_proposal,
    mark_decision_func: Callable[..., bool] = import_rule_proposals_repo.mark_import_rule_proposal_decision,
) -> ProposalDecisionResult:
    proposal = get_proposal_func(int(proposal_id))
    if not proposal:
        return ProposalDecisionResult(False, "Import rule proposal not found.")
    if proposal.get("status") != "pending":
        return ProposalDecisionResult(False, "Only pending proposals can be rejected.", proposal=proposal)
    mark_decision_func(int(proposal_id), status="rejected", reviewer_decision="rejected")
    return ProposalDecisionResult(True, "Import rule proposal rejected.", proposal=get_proposal_func(int(proposal_id)) or proposal)


def ignore_import_rule_proposal(
    proposal_id: int,
    *,
    get_proposal_func: Callable[[int], dict[str, Any] | None] = import_rule_proposals_repo.get_import_rule_proposal,
    mark_decision_func: Callable[..., bool] = import_rule_proposals_repo.mark_import_rule_proposal_decision,
) -> ProposalDecisionResult:
    proposal = get_proposal_func(int(proposal_id))
    if not proposal:
        return ProposalDecisionResult(False, "Import rule proposal not found.")
    if proposal.get("status") != "pending":
        return ProposalDecisionResult(False, "Only pending proposals can be ignored.", proposal=proposal)
    mark_decision_func(int(proposal_id), status="ignored", reviewer_decision="ignored")
    return ProposalDecisionResult(True, "Import rule proposal ignored.", proposal=get_proposal_func(int(proposal_id)) or proposal)


def _evidence_from_learning_example(row: dict[str, Any]) -> EvidenceItem | None:
    raw_payee = str(row.get("raw_payee") or row.get("payee") or "").strip()
    raw_memo = str(row.get("raw_memo") or row.get("memo") or "").strip()
    if not raw_payee and not raw_memo:
        return None

    transaction_type = str(row.get("ttype") or row.get("transaction_type") or "").strip().lower()
    if transaction_type not in SUPPORTED_LEARNING_TRANSACTION_TYPES:
        return None

    account_id = _int_or_none(row.get("account_id"))
    if not account_id:
        return None

    raw_identity = import_identity_keys(raw_payee, raw_memo)
    if not any(raw_identity):
        return None

    splits = [split for split in row.get("splits") or [] if isinstance(split, dict)]
    remainder = row.get("remainder_intent") if isinstance(row.get("remainder_intent"), dict) else {}
    single_envelope_id = None
    if transaction_type in SUPPORTED_TRANSACTION_TYPES and not (row.get("paired_account_id") or row.get("paired_transaction")) and len(splits) == 1 and not remainder:
        single_envelope_id = _int_or_none(splits[0].get("envelope_id"))

    final_payee = str(row.get("final_payee") or "").strip()
    feedback = row.get("prediction_feedback") if isinstance(row.get("prediction_feedback"), dict) else {}
    learning_evidence = row.get("learning_evidence") if isinstance(row.get("learning_evidence"), dict) else {}
    import_row = learning_evidence.get("import_row") if isinstance(learning_evidence.get("import_row"), dict) else {}
    validation = (
        learning_evidence.get("transaction_import_validation")
        if isinstance(learning_evidence.get("transaction_import_validation"), dict)
        else {}
    )
    return EvidenceItem(
        source=str(row.get("source") or "transaction_learning"),
        account_id=account_id,
        raw_payee=raw_payee,
        raw_memo=raw_memo,
        raw_identity=raw_identity,
        cluster_signature=_cluster_signature(raw_payee, raw_memo),
        transaction_type=transaction_type,
        amount_cents=_int_or_none(row.get("amount_cents")),
        posted_at=str(row.get("posted_at") or "").strip() or None,
        final_payee=final_payee or None,
        single_envelope_id=single_envelope_id,
        splits=splits,
        remainder_intent=remainder or {},
        paired_account_id=_int_or_none(row.get("paired_account_id")),
        paired_transaction=row.get("paired_transaction") if isinstance(row.get("paired_transaction"), dict) else None,
        decision=row.get("decision") if isinstance(row.get("decision"), dict) else {},
        learning_evidence=learning_evidence,
        import_fitid=str(validation.get("fitid") or import_row.get("fitid") or "").strip() or None,
        row_fingerprint=str(validation.get("row_fingerprint") or import_row.get("row_fingerprint") or "").strip() or None,
        support_weight=1,
        dedupe_key=_learning_dedupe_key(row, raw_identity),
        transaction_id=_int_or_none(row.get("transaction_id")),
        learning_example_id=_int_or_none(row.get("learning_example_id")),
        prediction_feedback={
            "accepted": _safe_int(feedback.get("accepted")),
            "modified": _safe_int(feedback.get("modified")),
            "rejected": _safe_int(feedback.get("rejected")),
        },
    )


def _evidence_from_payee_rule(account_id: int, rule: dict[str, Any]) -> EvidenceItem | None:
    raw_payee = str(rule.get("raw_payee_sample") or rule.get("raw_payee_key") or "").strip()
    raw_memo = str(rule.get("raw_memo_sample") or rule.get("raw_memo_key") or "").strip()
    canonical_payee = str(rule.get("canonical_payee") or "").strip()
    if not canonical_payee or not (raw_payee or raw_memo):
        return None
    if not _truthy(rule.get("payee_changed"), default=True):
        return None

    raw_identity = (
        str(rule.get("raw_payee_key") or "").strip(),
        str(rule.get("raw_memo_key") or "").strip(),
    )
    if not any(raw_identity):
        raw_identity = import_identity_keys(raw_payee, raw_memo)
    if not any(raw_identity):
        return None

    return EvidenceItem(
        source="payee_cleanup_history",
        account_id=int(account_id),
        raw_payee=raw_payee,
        raw_memo=raw_memo,
        raw_identity=raw_identity,
        cluster_signature=_cluster_signature(raw_payee, raw_memo),
        final_payee=canonical_payee,
        support_weight=max(1, _safe_int(rule.get("use_count"), default=1)),
        dedupe_key=f"payee-rule:{rule.get('id') or '|'.join(raw_identity)}",
    )


def _proposal_record_from_candidate(proposal: dict[str, Any]) -> dict[str, Any] | None:
    if proposal.get("decision") != "suggest":
        return None
    suggested_rule = proposal.get("suggested_rule") if isinstance(proposal.get("suggested_rule"), dict) else {}
    candidate_key = str(proposal.get("candidate_key") or "").strip()
    if not candidate_key or not suggested_rule:
        return None
    return {
        "fingerprint": candidate_key,
        "candidate_key": candidate_key,
        "account_id": _int_or_none(suggested_rule.get("account_id") or proposal.get("account_id")),
        "condition_json": proposal.get("condition_json") or suggested_rule.get("condition_json") or {},
        "action_json": proposal.get("action_json") or suggested_rule.get("action_json") or {},
        "suggested_rule_json": suggested_rule,
        "evidence_json": proposal.get("evidence") or {},
        "reason_codes_json": proposal.get("reason_codes") or [],
    }


def _validated_rule_data_from_proposal(
    proposal: dict[str, Any],
    *,
    enabled: bool,
    parse_rule_form_func: Callable[..., tuple[dict[str, Any] | None, list[str]]],
) -> tuple[dict[str, Any] | None, list[str]]:
    suggested_rule = proposal.get("suggested_rule_json") or {}
    condition = proposal.get("condition_json") or suggested_rule.get("condition_json") or {}
    action = proposal.get("action_json") or suggested_rule.get("action_json") or {}
    if not isinstance(suggested_rule, dict) or not isinstance(condition, dict) or not isinstance(action, dict):
        return None, ["Proposal payload is incomplete."]

    form = _proposal_rule_form(suggested_rule, condition, action, enabled=enabled)
    rule_data, errors = parse_rule_form_func(form)
    if errors or not rule_data:
        return None, errors

    expected_condition = dict(condition)
    expected_action = dict(action)
    if _canonical_json(rule_data.get("condition_json")) != _canonical_json(expected_condition):
        return None, ["Proposal condition payload is no longer valid."]
    if _canonical_json(rule_data.get("action_json")) != _canonical_json(expected_action):
        return None, ["Proposal action payload is no longer valid."]
    return rule_data, []


def _proposal_has_stale_source(proposal: dict[str, Any]) -> bool:
    evidence = proposal.get("evidence_json") if isinstance(proposal.get("evidence_json"), dict) else {}
    return (
        evidence.get("refresh_status") == "stale_source_changed"
        or proposal.get("reviewer_decision") == import_rule_proposals_repo.STALE_REVIEWER_DECISION
    )


def _proposal_rule_form(
    suggested_rule: dict[str, Any],
    condition: dict[str, Any],
    action: dict[str, Any],
    *,
    enabled: bool,
) -> MultiDict:
    form = MultiDict({
        "name": str(suggested_rule.get("name") or "Suggested import rule").strip(),
        "account_scope": "global" if suggested_rule.get("account_id") is None else "account",
        "account_id": "" if suggested_rule.get("account_id") is None else str(suggested_rule.get("account_id")),
        "priority": str(suggested_rule.get("priority") or 100),
        "direction": str(condition.get("direction") or "any"),
        "match_field": str(condition.get("field") or "text"),
        "match_operator": str(condition.get("operator") or "contains"),
        "match_value": str(condition.get("value") or ""),
        "amount_min": _cents_to_amount_text(condition.get("amount_min_cents")),
        "amount_max": _cents_to_amount_text(condition.get("amount_max_cents")),
        "action_payee": str(action.get("payee") or ""),
        "action_memo": str(action.get("memo") or ""),
        "action_transaction_type": str(action.get("transaction_type") or ""),
        "action_envelope_id": str(action.get("single_envelope_id") or ""),
        "action_split_remainder_json": json.dumps(action.get("split_remainder") or {}, sort_keys=True) if action.get("split_remainder") else "",
        "action_transfer_json": json.dumps(action.get("transfer") or {}, sort_keys=True) if action.get("transfer") else "",
        "action_manual_match_json": json.dumps(action.get("manual_match") or {}, sort_keys=True) if action.get("manual_match") else "",
    })
    if enabled:
        form["enabled"] = "1"
    return form


def _canonical_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _cents_to_amount_text(value: Any) -> str:
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{abs(cents) / 100:.2f}"


def _candidate_from_bucket(
    *,
    account_id: int,
    cluster_signature: str,
    items: list[EvidenceItem],
    existing_rules: list[dict[str, Any]],
    min_support_examples: int,
    min_distinct_raw_identities: int,
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None = None,
    list_accounts_func: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    summary = _bucket_summary(items)
    reasons: list[str] = []
    advanced_mode = _advanced_evidence_mode(items)
    support_threshold = MIN_ADVANCED_SUPPORT_EXAMPLES if advanced_mode else min_support_examples
    raw_threshold = MIN_ADVANCED_DISTINCT_RAW_IDENTITIES if advanced_mode else min_distinct_raw_identities

    if summary["support_examples"] < support_threshold:
        reasons.append("rule_proposal_insufficient_support")
    if summary["distinct_raw_identities"] < raw_threshold:
        reasons.append("rule_proposal_insufficient_distinct_raw_identities")
    if summary["feedback_rejected"] > max(0, summary["feedback_accepted"] + summary["feedback_modified"]):
        reasons.append("rule_proposal_negative_prediction_feedback")

    actions, action_reasons, condition_extra = _candidate_actions(
        items,
        min_support_examples,
        min_distinct_raw_identities,
        advanced_support_examples=MIN_ADVANCED_SUPPORT_EXAMPLES,
        advanced_distinct_raw_identities=MIN_ADVANCED_DISTINCT_RAW_IDENTITIES,
    )
    reasons.extend(action_reasons)
    condition = {
        "direction": actions.get("transaction_type") or _stable_transaction_type(items) or "any",
        "field": "text",
        "operator": "contains",
        "value": cluster_signature,
    }
    condition.update(condition_extra)

    validation_errors = _advanced_rule_validation_errors(
        account_id,
        condition,
        actions,
        list_envelopes_func=list_envelopes_func,
        list_accounts_func=list_accounts_func,
    )
    if validation_errors:
        reasons.append("rule_proposal_advanced_validation_failed")
        summary["validation_errors"] = validation_errors

    existing_overlap = _existing_rule_overlap(condition, actions, existing_rules)
    if existing_overlap:
        reasons.append("rule_proposal_existing_rule_overlap")
        summary["existing_rule_outcomes"] = existing_overlap

    base = {
        "candidate_key": _candidate_key(account_id, condition, actions),
        "decision": "withheld",
        "reason_codes": sorted(set(reasons)) if reasons else [],
        "condition_json": condition,
        "action_json": actions,
        "evidence": summary,
    }
    if reasons or not actions:
        if not actions and "rule_proposal_no_supported_simple_action" not in base["reason_codes"]:
            base["reason_codes"].append("rule_proposal_no_supported_simple_action")
        return base

    return {
        **base,
        "decision": "suggest",
        "reason_codes": ["rule_proposal_conservative_support_met"],
        "suggested_rule": {
            "account_id": account_id,
            "enabled": False,
            "priority": 100,
            "name": f"Suggested import rule: {cluster_signature}",
            "condition_json": condition,
            "action_json": actions,
        },
    }


def _candidate_actions(
    items: list[EvidenceItem],
    min_support_examples: int,
    min_distinct_raw_identities: int,
    *,
    advanced_support_examples: int,
    advanced_distinct_raw_identities: int,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    actions: dict[str, Any] = {}
    reasons: list[str] = []
    condition_extra: dict[str, Any] = {}
    advanced_mode = _advanced_evidence_mode(items)
    unique_learning = _unique_learning_items(items)

    if advanced_mode == "mixed_advanced":
        return {}, ["rule_proposal_advanced_mixed_evidence"], {}
    if advanced_mode == "partial_advanced":
        return {}, ["rule_proposal_advanced_partial_evidence"], {}

    if advanced_mode == "manual_match":
        manual_action, manual_reasons, manual_condition = _manual_match_candidate_action(
            unique_learning,
            advanced_support_examples,
            advanced_distinct_raw_identities,
        )
        actions.update(manual_action)
        reasons.extend(manual_reasons)
        condition_extra.update(manual_condition)
        return actions, sorted(set(reasons)), condition_extra

    if advanced_mode == "transfer":
        transfer_action, transfer_reasons = _transfer_candidate_action(
            unique_learning,
            advanced_support_examples,
            advanced_distinct_raw_identities,
        )
        actions.update(transfer_action)
        reasons.extend(transfer_reasons)
        return actions, sorted(set(reasons)), condition_extra

    if advanced_mode == "split_remainder":
        split_action, split_reasons = _split_remainder_candidate_action(
            unique_learning,
            advanced_support_examples,
            advanced_distinct_raw_identities,
        )
        actions.update(split_action)
        reasons.extend(split_reasons)
        return actions, sorted(set(reasons)), condition_extra

    payee_values = Counter()
    payee_raw_keys: set[tuple[str, str]] = set()
    for item in items:
        if not item.final_payee:
            continue
        if item.source == "payee_cleanup_history" or cleanup_part_differs_meaningfully(
            " ".join(part for part in item.raw_identity if part),
            item.final_payee,
        ):
            payee_values[item.final_payee] += item.support_weight
            payee_raw_keys.add(item.raw_identity)
    _apply_stable_counter_action(
        actions,
        reasons,
        "payee",
        payee_values,
        payee_raw_keys,
        min_support_examples,
        min_distinct_raw_identities,
        ambiguous_reason="rule_proposal_ambiguous_payee",
    )

    type_values = Counter(
        item.transaction_type
        for item in _unique_learning_items(items)
        if item.transaction_type in SUPPORTED_TRANSACTION_TYPES
    )
    type_raw_keys = {
        item.raw_identity
        for item in _unique_learning_items(items)
        if item.transaction_type in SUPPORTED_TRANSACTION_TYPES
    }
    _apply_stable_counter_action(
        actions,
        reasons,
        "transaction_type",
        type_values,
        type_raw_keys,
        min_support_examples,
        min_distinct_raw_identities,
        ambiguous_reason="rule_proposal_ambiguous_transaction_type",
    )

    envelope_values = Counter(
        item.single_envelope_id
        for item in _unique_learning_items(items)
        if item.single_envelope_id
    )
    envelope_raw_keys = {
        item.raw_identity
        for item in _unique_learning_items(items)
        if item.single_envelope_id
    }
    _apply_stable_counter_action(
        actions,
        reasons,
        "single_envelope_id",
        envelope_values,
        envelope_raw_keys,
        min_support_examples,
        min_distinct_raw_identities,
        ambiguous_reason="rule_proposal_ambiguous_single_envelope",
    )
    return actions, sorted(set(reasons)), condition_extra


def _advanced_evidence_mode(items: list[EvidenceItem]) -> str | None:
    unique = _unique_learning_items(items)
    if not unique:
        return None
    modes = {
        mode
        for item in unique
        for mode in [_item_advanced_mode(item)]
        if mode
    }
    if not modes:
        return None
    if len(modes) > 1:
        return "mixed_advanced"
    mode = next(iter(modes))
    if any(_item_advanced_mode(item) != mode for item in unique):
        return "partial_advanced"
    return mode


def _item_advanced_mode(item: EvidenceItem) -> str | None:
    if item.source == "manual_match":
        return "manual_match"
    if item.transaction_type in {"transfer_in", "transfer_out"} or item.paired_account_id or item.paired_transaction:
        return "transfer"
    if item.transaction_type in SUPPORTED_TRANSACTION_TYPES and (item.remainder_intent or len(item.splits or []) > 1):
        return "split_remainder"
    return None


def _split_remainder_candidate_action(
    items: list[EvidenceItem],
    min_support_examples: int,
    min_distinct_raw_identities: int,
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    if _advanced_evidence_mode(items) != "split_remainder":
        return {}, ["rule_proposal_advanced_partial_evidence"]

    signatures: Counter = Counter()
    raw_keys_by_signature: dict[str, set[tuple[str, str]]] = defaultdict(set)
    payloads: dict[str, dict[str, Any]] = {}
    for item in items:
        payload, reason = _split_remainder_payload_from_item(item)
        if reason:
            reasons.append(reason)
            continue
        key = _canonical_json(payload)
        signatures[key] += 1
        raw_keys_by_signature[key].add(item.raw_identity)
        payloads[key] = payload

    payload = _stable_advanced_payload(
        signatures,
        raw_keys_by_signature,
        payloads,
        min_support_examples,
        min_distinct_raw_identities,
        reasons,
        ambiguous_reason="rule_proposal_ambiguous_split_remainder",
    )
    return ({"split_remainder": payload} if payload else {}), reasons


def _split_remainder_payload_from_item(item: EvidenceItem) -> tuple[dict[str, Any], str | None]:
    transaction_type = item.transaction_type if item.transaction_type in SUPPORTED_TRANSACTION_TYPES else None
    amount_cents = item.amount_cents
    if not transaction_type or not amount_cents:
        return {}, "rule_proposal_advanced_incomplete_split_remainder"

    expected_negative = transaction_type == "expense"
    fixed_splits = _clean_signed_splits(item.splits, expected_negative=expected_negative)
    if fixed_splits is None:
        return {}, "rule_proposal_advanced_invalid_split_remainder"

    remainder_envelope_id = _int_or_none((item.remainder_intent or {}).get("envelope_id"))
    if remainder_envelope_id:
        fixed_splits = [split for split in fixed_splits if int(split["envelope_id"]) != remainder_envelope_id]
    if not fixed_splits and not remainder_envelope_id:
        return {}, "rule_proposal_advanced_incomplete_split_remainder"
    envelope_ids = [int(split["envelope_id"]) for split in fixed_splits]
    if remainder_envelope_id:
        envelope_ids.append(remainder_envelope_id)
    if len(envelope_ids) != len(set(envelope_ids)):
        return {}, "rule_proposal_advanced_duplicate_envelope"

    payload: dict[str, Any] = {
        "transaction_type": transaction_type,
        "splits": [
            {"envelope_id": int(split["envelope_id"]), "amount_cents": int(split["amount_cents"]), "amount_mode": "signed"}
            for split in fixed_splits
        ],
    }
    if remainder_envelope_id:
        fixed_total = sum(int(split["amount_cents"]) for split in fixed_splits)
        remainder_amount = int(amount_cents) - fixed_total
        if remainder_amount == 0 or (remainder_amount < 0) != expected_negative:
            return {}, "rule_proposal_advanced_invalid_split_remainder"
        payload["remainder_envelope_id"] = remainder_envelope_id
    else:
        if sum(int(split["amount_cents"]) for split in fixed_splits) != int(amount_cents):
            return {}, "rule_proposal_advanced_unbalanced_split_remainder"
        payload["target_amount_cents"] = int(amount_cents)
    return payload, None


def _transfer_candidate_action(
    items: list[EvidenceItem],
    min_support_examples: int,
    min_distinct_raw_identities: int,
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    if _advanced_evidence_mode(items) != "transfer":
        return {}, ["rule_proposal_advanced_partial_evidence"]

    signatures: Counter = Counter()
    raw_keys_by_signature: dict[str, set[tuple[str, str]]] = defaultdict(set)
    payloads: dict[str, dict[str, Any]] = {}
    for item in items:
        payload, reason = _transfer_payload_from_item(item)
        if reason:
            reasons.append(reason)
            continue
        key = _canonical_json(payload)
        signatures[key] += 1
        raw_keys_by_signature[key].add(item.raw_identity)
        payloads[key] = payload

    payload = _stable_advanced_payload(
        signatures,
        raw_keys_by_signature,
        payloads,
        min_support_examples,
        min_distinct_raw_identities,
        reasons,
        ambiguous_reason="rule_proposal_ambiguous_transfer",
    )
    return ({"transfer": payload} if payload else {}), reasons


def _transfer_payload_from_item(item: EvidenceItem) -> tuple[dict[str, Any], str | None]:
    amount_cents = item.amount_cents
    other_account_id = item.paired_account_id or _int_or_none((item.paired_transaction or {}).get("account_id"))
    paired = item.paired_transaction or {}
    if not amount_cents or not other_account_id or other_account_id == item.account_id:
        return {}, "rule_proposal_advanced_incomplete_transfer"
    if not _has_import_provenance(item):
        return {}, "rule_proposal_advanced_missing_provenance"
    if paired:
        paired_amount = _int_or_none(paired.get("amount_cents"))
        if paired_amount is not None and int(paired_amount) != -int(amount_cents):
            return {}, "rule_proposal_transfer_pair_amount_mismatch"
        paired_type = str(paired.get("ttype") or "").strip()
        if paired_type and paired_type != _opposite_transfer_type(item.transaction_type):
            return {}, "rule_proposal_transfer_pair_type_mismatch"

    transaction_type = "expense" if int(amount_cents) < 0 else "income"
    current_splits, current_remainder_id, current_reason = _transfer_leg_payload(
        item.splits,
        item.remainder_intent,
        target_abs_cents=abs(int(amount_cents)),
    )
    if current_reason:
        return {}, current_reason
    paired_splits = paired.get("splits") if isinstance(paired.get("splits"), list) else []
    paired_remainder = paired.get("remainder_intent") if isinstance(paired.get("remainder_intent"), dict) else {}
    other_splits, other_remainder_id, other_reason = _transfer_leg_payload(
        paired_splits,
        paired_remainder,
        target_abs_cents=abs(int(amount_cents)),
    )
    if other_reason:
        return {}, other_reason

    payload: dict[str, Any] = {
        "transaction_type": transaction_type,
        "other_account_id": int(other_account_id),
        "current_account_splits": current_splits,
        "other_account_splits": other_splits,
        "target_amount_cents": int(amount_cents),
    }
    if current_remainder_id:
        payload["current_account_remainder_envelope_id"] = current_remainder_id
    if other_remainder_id:
        payload["other_account_remainder_envelope_id"] = other_remainder_id
    return payload, None


def _transfer_leg_payload(
    splits: list[dict[str, Any]],
    remainder_intent: dict[str, Any],
    *,
    target_abs_cents: int,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    remainder_id = _int_or_none((remainder_intent or {}).get("envelope_id"))
    cleaned = _clean_abs_splits(splits)
    if cleaned is None:
        return [], None, "rule_proposal_advanced_invalid_transfer_split"
    if remainder_id:
        cleaned = [split for split in cleaned if int(split["envelope_id"]) != remainder_id]
    if not cleaned and not remainder_id:
        return [], None, "rule_proposal_advanced_incomplete_transfer"
    envelope_ids = [int(split["envelope_id"]) for split in cleaned]
    if remainder_id:
        envelope_ids.append(remainder_id)
    if len(envelope_ids) != len(set(envelope_ids)):
        return [], None, "rule_proposal_advanced_duplicate_envelope"
    fixed_total = sum(int(split["amount_cents"]) for split in cleaned)
    if remainder_id:
        if target_abs_cents - fixed_total <= 0:
            return [], None, "rule_proposal_advanced_unbalanced_transfer"
    elif fixed_total != target_abs_cents:
        return [], None, "rule_proposal_advanced_unbalanced_transfer"
    return cleaned, remainder_id, None


def _manual_match_candidate_action(
    items: list[EvidenceItem],
    min_support_examples: int,
    min_distinct_raw_identities: int,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    reasons: list[str] = []
    if _advanced_evidence_mode(items) != "manual_match":
        return {}, ["rule_proposal_advanced_partial_evidence"], {}

    signatures: Counter = Counter()
    raw_keys_by_signature: dict[str, set[tuple[str, str]]] = defaultdict(set)
    payloads: dict[str, dict[str, Any]] = {}
    condition_by_signature: dict[str, dict[str, Any]] = {}
    fitids: set[str] = set()
    fingerprints: set[str] = set()
    for item in items:
        payload, condition, reason = _manual_match_payload_from_item(item)
        if reason:
            reasons.append(reason)
            continue
        if item.import_fitid:
            fitids.add(item.import_fitid)
        if item.row_fingerprint:
            fingerprints.add(item.row_fingerprint)
        key = _canonical_json(payload)
        signatures[key] += 1
        raw_keys_by_signature[key].add(item.raw_identity)
        payloads[key] = payload
        condition_by_signature[key] = condition

    if len(fitids) < min_distinct_raw_identities or len(fingerprints) < min_support_examples:
        reasons.append("rule_proposal_manual_match_insufficient_distinct_provenance")

    payload = _stable_advanced_payload(
        signatures,
        raw_keys_by_signature,
        payloads,
        min_support_examples,
        min_distinct_raw_identities,
        reasons,
        ambiguous_reason="rule_proposal_ambiguous_manual_match",
    )
    condition = condition_by_signature.get(_canonical_json(payload), {}) if payload else {}
    return ({"manual_match": payload} if payload else {}), reasons, condition


def _manual_match_payload_from_item(item: EvidenceItem) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    transaction_id = item.transaction_id
    amount_cents = item.amount_cents
    if item.source != "manual_match" or not transaction_id or not amount_cents:
        return {}, {}, "rule_proposal_advanced_incomplete_manual_match"
    if not _has_import_provenance(item):
        return {}, {}, "rule_proposal_advanced_missing_provenance"
    if item.decision and item.decision.get("source_action") not in {None, "manual_match"}:
        return {}, {}, "rule_proposal_advanced_incomplete_manual_match"
    amount_abs = abs(int(amount_cents))
    return (
        {"transaction_id": int(transaction_id)},
        {"amount_min_cents": amount_abs, "amount_max_cents": amount_abs},
        None,
    )


def _stable_advanced_payload(
    signatures: Counter,
    raw_keys_by_signature: dict[str, set[tuple[str, str]]],
    payloads: dict[str, dict[str, Any]],
    min_support_examples: int,
    min_distinct_raw_identities: int,
    reasons: list[str],
    *,
    ambiguous_reason: str,
) -> dict[str, Any] | None:
    if not signatures:
        return None
    if len(signatures) > 1:
        reasons.append(ambiguous_reason)
        return None
    key, support = signatures.most_common(1)[0]
    if support < min_support_examples:
        reasons.append("rule_proposal_advanced_insufficient_support")
        return None
    if len(raw_keys_by_signature.get(key, set())) < min_distinct_raw_identities:
        reasons.append("rule_proposal_advanced_insufficient_distinct_raw_identities")
        return None
    return payloads[key]


def _clean_signed_splits(splits: list[dict[str, Any]], *, expected_negative: bool) -> list[dict[str, int]] | None:
    cleaned: list[dict[str, int]] = []
    seen: set[int] = set()
    for split in splits or []:
        envelope_id = _int_or_none(split.get("envelope_id"))
        amount_cents = _int_or_none(split.get("amount_cents"))
        if not envelope_id or not amount_cents or (int(amount_cents) < 0) != expected_negative or envelope_id in seen:
            return None
        seen.add(envelope_id)
        cleaned.append({"envelope_id": int(envelope_id), "amount_cents": int(amount_cents)})
    return sorted(cleaned, key=lambda split: split["envelope_id"])


def _clean_abs_splits(splits: list[dict[str, Any]]) -> list[dict[str, int]] | None:
    cleaned: list[dict[str, int]] = []
    seen: set[int] = set()
    for split in splits or []:
        envelope_id = _int_or_none(split.get("envelope_id"))
        amount_cents = _int_or_none(split.get("amount_cents"))
        if not envelope_id or not amount_cents or envelope_id in seen:
            return None
        seen.add(envelope_id)
        cleaned.append({"envelope_id": int(envelope_id), "amount_cents": abs(int(amount_cents))})
    return sorted(cleaned, key=lambda split: split["envelope_id"])


def _has_import_provenance(item: EvidenceItem) -> bool:
    return bool(item.import_fitid and item.row_fingerprint)


def _opposite_transfer_type(transaction_type: str | None) -> str | None:
    if transaction_type == "transfer_in":
        return "transfer_out"
    if transaction_type == "transfer_out":
        return "transfer_in"
    return None


def _advanced_rule_validation_errors(
    account_id: int,
    condition: dict[str, Any],
    actions: dict[str, Any],
    *,
    list_envelopes_func: Callable[..., list[dict[str, Any]]] | None,
    list_accounts_func: Callable[..., list[dict[str, Any]]] | None,
) -> list[str]:
    if not any(key in actions for key in ("split_remainder", "transfer", "manual_match")):
        return []
    suggested_rule = {
        "account_id": account_id,
        "priority": 100,
        "name": "Suggested import rule",
        "condition_json": condition,
        "action_json": actions,
    }
    form = _proposal_rule_form(suggested_rule, condition, actions, enabled=False)
    _rule_data, errors = parse_rule_form(
        form,
        list_envelopes_func=list_envelopes_func,
        list_accounts_func=list_accounts_func,
    )
    return errors


def _apply_stable_counter_action(
    actions: dict[str, Any],
    reasons: list[str],
    key: str,
    values: Counter,
    raw_keys: set[tuple[str, str]],
    min_support_examples: int,
    min_distinct_raw_identities: int,
    *,
    ambiguous_reason: str,
) -> None:
    if not values:
        return
    if len(values) > 1:
        reasons.append(ambiguous_reason)
        return
    value, support = values.most_common(1)[0]
    if support >= min_support_examples and len(raw_keys) >= min_distinct_raw_identities:
        actions[key] = value


def _bucket_summary(items: list[EvidenceItem]) -> dict[str, Any]:
    unique = _unique_support_items(items)
    raw_identities = sorted({"|".join(item.raw_identity) for item in items if any(item.raw_identity)})
    sources = Counter(item.source for item in items)
    return {
        "support_examples": sum(item.support_weight for item in unique),
        "distinct_raw_identities": len(raw_identities),
        "distinct_transactions": len({item.transaction_id for item in unique if item.transaction_id}),
        "raw_identities": raw_identities[:10],
        "raw_samples": _raw_samples(items),
        "sources": dict(sorted(sources.items())),
        "learning_example_ids": [
            item.learning_example_id for item in unique if item.learning_example_id
        ][:20],
        "feedback_accepted": sum(item.prediction_feedback.get("accepted", 0) for item in unique),
        "feedback_modified": sum(item.prediction_feedback.get("modified", 0) for item in unique),
        "feedback_rejected": sum(item.prediction_feedback.get("rejected", 0) for item in unique),
        "unsupported_sources": {
            "transaction_edits": "available through transaction_learning_examples when import evidence is present",
            "existing_rule_outcomes": "limited to import_matching_rules use_count/last_used_at; no per-row rule outcome table exists",
        },
    }


def _unique_support_items(items: list[EvidenceItem]) -> list[EvidenceItem]:
    by_key: dict[str, EvidenceItem] = {}
    for item in items:
        key = item.dedupe_key or f"raw:{'|'.join(item.raw_identity)}"
        current = by_key.get(key)
        if current is None or item.support_weight > current.support_weight:
            by_key[key] = item
    return list(by_key.values())


def _unique_learning_items(items: list[EvidenceItem]) -> list[EvidenceItem]:
    return [item for item in _unique_support_items(items) if item.source != "payee_cleanup_history"]


def _raw_samples(items: list[EvidenceItem]) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.raw_payee, item.raw_memo)
        if key in seen:
            continue
        seen.add(key)
        samples.append({"payee": item.raw_payee, "memo": item.raw_memo})
        if len(samples) >= 5:
            break
    return samples


def _withheld_from_items(items: list[EvidenceItem], *, reason_codes: list[str], detail: str) -> dict[str, Any]:
    first = items[0]
    return {
        "candidate_key": _candidate_key(first.account_id, {"value": first.cluster_signature or ""}, {}),
        "decision": "withheld",
        "reason_codes": reason_codes,
        "condition_json": {},
        "action_json": {},
        "evidence": {
            **_bucket_summary(items),
            "detail": detail,
        },
    }


def _existing_rule_overlap(
    condition: dict[str, Any],
    actions: dict[str, Any],
    existing_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not actions:
        return []
    condition_value = _normalized_text(condition.get("value"))
    condition_direction = str(condition.get("direction") or "any")
    overlaps: list[dict[str, Any]] = []
    for rule in existing_rules or []:
        if not _truthy(rule.get("enabled"), default=True):
            continue
        rule_condition = rule.get("condition_json") or {}
        rule_action = rule.get("action_json") or {}
        rule_value = _normalized_text(rule_condition.get("value"))
        if rule_value and condition_value and rule_value != condition_value:
            continue
        rule_direction = str(rule_condition.get("direction") or "any")
        if rule_direction not in {"any", condition_direction} and condition_direction != "any":
            continue
        if all(rule_action.get(key) == value for key, value in actions.items()):
            overlaps.append({
                "id": rule.get("id"),
                "name": rule.get("name"),
                "use_count": _safe_int(rule.get("use_count")),
                "last_used_at": rule.get("last_used_at"),
            })
    return overlaps


def _learning_dedupe_key(row: dict[str, Any], raw_identity: tuple[str, str]) -> str:
    evidence = row.get("learning_evidence") if isinstance(row.get("learning_evidence"), dict) else {}
    import_row = evidence.get("import_row") if isinstance(evidence.get("import_row"), dict) else {}
    validation = (
        evidence.get("transaction_import_validation")
        if isinstance(evidence.get("transaction_import_validation"), dict)
        else {}
    )
    for value in (
        validation.get("fitid"),
        import_row.get("fitid"),
        validation.get("row_fingerprint"),
        import_row.get("row_fingerprint"),
        row.get("learning_example_id"),
    ):
        if value:
            return f"learning:{value}"
    return f"learning:{'|'.join(raw_identity)}:{row.get('posted_at')}:{row.get('amount_cents')}"


def _cluster_signature(raw_payee: Any, raw_memo: Any) -> str | None:
    profile = build_transaction_text_profile_from_row({"payee": raw_payee, "memo": raw_memo})
    cluster = merchant_cluster_signature(profile)
    return cluster.signature if cluster else None


def _stable_transaction_type(items: list[EvidenceItem]) -> str | None:
    values = {item.transaction_type for item in _unique_learning_items(items) if item.transaction_type}
    return next(iter(values)) if len(values) == 1 else None


def _candidate_key(account_id: int, condition: dict[str, Any], actions: dict[str, Any]) -> str:
    digest = hashlib.sha1(repr((int(account_id), sorted(condition.items()), sorted(actions.items()))).encode("utf-8")).hexdigest()[:16]
    return f"rule-proposal:{account_id}:{digest}"


def _source_notes(
    source_counts: Counter,
    learning_examples: list[dict[str, Any]],
    payee_rules: list[dict[str, Any]],
    existing_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "source": "transaction_learning_examples",
            "status": "available" if learning_examples else "empty",
            "rows_considered": len(learning_examples or []),
            "evidence_sources": dict(sorted(source_counts.items())),
        },
        {
            "source": "payee_normalization_rules",
            "status": "available" if payee_rules else "empty",
            "rows_considered": len(payee_rules or []),
        },
        {
            "source": "prediction_feedback",
            "status": "available_through_learning_join",
        },
        {
            "source": "import_matching_rules",
            "status": "limited_outcome_counts" if existing_rules else "empty",
            "rows_considered": len(existing_rules or []),
        },
    ]


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)
