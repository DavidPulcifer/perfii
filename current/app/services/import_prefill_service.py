from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import lru_cache
import math
import re
from typing import Any, Callable, Iterable

from app.repositories import import_prefill_repo
from app.services.account_match_profile_service import (
    AccountMatchProfile,
    build_account_match_profile,
)
from app.services.transaction_text_profile_service import (
    TransactionTextProfile,
    build_transaction_text_profile_from_row,
)
from app.services.text_similarity_service import cached_text_similarity, partial_token_similarity
from app.utils import parse_money_to_cents

NO_PREFILL_REASONS = {
    "no_amount": "no_amount",
    "no_pattern": "no_compatible_pattern",
    "tied": "tied_or_ambiguous_pattern",
}
PREDICTION_DEBUG_SCHEMA_VERSION = 1

_TRANSFER_TYPES = {"transfer_in", "transfer_out"}
_STANDARD_TYPES = {"expense", "income"}
_TOKEN_NOISE = {
    "pos", "debit", "card", "purchase", "payment", "online", "transfer",
    "xfer", "ach", "web", "mobile", "deposit", "withdrawal", "check",
}


@dataclass(frozen=True)
class ImportPrefillCandidate:
    signature: tuple[Any, ...]
    output: dict[str, Any]
    score: float
    posted_at: date | None
    debug_reason_codes: tuple[str, ...] = field(default_factory=tuple)
    debug_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RankedImportPrefillPattern:
    score: float
    signature: tuple[Any, ...]
    representative: ImportPrefillCandidate
    support_count: int
    support_score: float
    current_run_support: float


@dataclass(frozen=True)
class PreparedImportPrefillHistoryRow:
    raw: dict[str, Any]
    account_id: int
    amount_cents: int
    amount_sign: int
    posted_at: date | None
    ttype: str
    text: str
    text_profile: TransactionTextProfile
    paired_text: str
    paired_account_name: str
    paired_account_text: str
    paired_account_profile: AccountMatchProfile | None
    other_account_id: int | None
    current_splits: tuple[dict[str, int], ...]
    current_remainder: dict[str, int] | None
    other_splits: tuple[dict[str, int], ...]
    other_remainder: dict[str, int] | None
    evidence_quality: str = "low"
    evidence_source: str = "final_transaction_history"
    learning_example_id: int | None = None
    feedback_score: float = 0.0


def amount_cents_from_import(row: dict[str, Any]) -> int:
    for key in ("amount_cents", "amount", "trnamt"):
        if key not in row:
            continue
        raw = row.get(key)
        if raw in (None, ""):
            continue
        if key == "amount_cents":
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return parse_money_to_cents(str(raw))
    return 0


def normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b", " ", text)
    text = re.sub(r"\b\d{4,}\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [tok for tok in text.split() if tok not in _TOKEN_NOISE and len(tok) > 1]
    return " ".join(tokens)


def row_text_key(row: dict[str, Any]) -> str:
    return normalize_text(" ".join(str(row.get(key) or "") for key in ("payee", "memo", "name")))


def split_signature(splits: Iterable[dict[str, Any]]) -> tuple[tuple[int, int], ...]:
    normalized: list[tuple[int, int]] = []
    for split in splits or []:
        try:
            envelope_id = int(split.get("envelope_id"))
            amount_cents = int(split.get("amount_cents"))
        except (TypeError, ValueError):
            continue
        if envelope_id and amount_cents:
            normalized.append((envelope_id, amount_cents))
    return tuple(sorted(normalized))


def _clean_remainder_intent(intent: dict[str, Any] | None) -> dict[str, int] | None:
    if not intent:
        return None
    try:
        envelope_id = int(intent.get("envelope_id"))
        amount_cents = int(intent.get("amount_cents") or 0)
    except (TypeError, ValueError):
        return None
    if not envelope_id:
        return None
    return {"envelope_id": envelope_id, "amount_cents": amount_cents}


def _split_template(
    splits: Iterable[dict[str, Any]],
    remainder_intent: dict[str, Any] | None,
) -> tuple[list[dict[str, int]], dict[str, int] | None]:
    """Return fixed split rows plus optional variable-remainder metadata.

    transaction_splits remains the authoritative ledger. The remainder intent
    identifies one component of those splits that should be recomputed for a
    future import amount, so remove that component from the fixed template.
    """
    fixed = _clean_splits(splits or [])
    intent = _clean_remainder_intent(remainder_intent)
    if not intent:
        return fixed, None

    removed = False
    remaining: list[dict[str, int]] = []
    for split in fixed:
        if (
            not removed
            and split["envelope_id"] == intent["envelope_id"]
            and split["amount_cents"] == intent["amount_cents"]
        ):
            removed = True
            continue
        remaining.append(dict(split))

    if not removed and intent["amount_cents"] != 0:
        for idx, split in enumerate(remaining):
            if split["envelope_id"] != intent["envelope_id"]:
                continue
            adjusted = int(split["amount_cents"]) - int(intent["amount_cents"])
            if adjusted:
                remaining[idx] = {"envelope_id": split["envelope_id"], "amount_cents": adjusted}
            else:
                remaining.pop(idx)
            break

    return sorted(remaining, key=lambda s: (s["envelope_id"], s["amount_cents"])), intent


def prefill_false(row_index: int, reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "row_index": int(row_index),
        "prefill": False,
        "debug_reason_codes": [reason],
        "prediction_debug": prediction_debug_payload(
            engine="import_prefill",
            decision="no_prefill",
            prediction_type=None,
            reason_codes=[reason],
            evidence=evidence,
        ),
    }


def prediction_debug_payload(
    *,
    engine: str,
    decision: str,
    prediction_type: str | None,
    reason_codes: Iterable[str],
    score: float | None = None,
    confidence: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PREDICTION_DEBUG_SCHEMA_VERSION,
        "engine": engine,
        "decision": decision,
        "prediction_type": prediction_type,
        "confidence": confidence or _confidence_for_score(score),
        "reason_codes": list(dict.fromkeys(str(code) for code in reason_codes if code)),
        "evidence": dict(evidence or {}),
    }
    if score is not None:
        payload["score"] = round(float(score), 4)
    return payload


def build_import_prefills(
    transactions: list[dict[str, Any]],
    account_id: int,
    *,
    history_rows: list[dict[str, Any]] | None = None,
    history_func: Callable[..., list[dict[str, Any]]] = import_prefill_repo.list_import_prefill_history_with_learning,
) -> list[dict[str, Any]]:
    """Build FIN-045 prefill decisions for import-review rows.

    Keep the behavior read-only/UI-agnostic, but prepare historical transaction
    features once per import instead of recalculating them for every imported row.
    """
    history = history_rows if history_rows is not None else history_func(account_id=account_id)
    prepared_history = _prepare_import_prefill_history(history)
    return [
        _select_import_prefill_prepared(row, idx, account_id, prepared_history)
        for idx, row in enumerate(transactions or [])
    ]


def select_import_prefill(
    import_row: dict[str, Any],
    row_index: int,
    account_id: int,
    history_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    prepared_history = _prepare_import_prefill_history(history_rows)
    return _select_import_prefill_prepared(import_row, row_index, account_id, prepared_history)


def _select_import_prefill_prepared(
    import_row: dict[str, Any],
    row_index: int,
    account_id: int,
    prepared_history: list[PreparedImportPrefillHistoryRow],
) -> dict[str, Any]:
    amount_cents = amount_cents_from_import(import_row)
    if amount_cents == 0:
        return prefill_false(row_index, NO_PREFILL_REASONS["no_amount"])

    candidates = _compatible_candidates(import_row, account_id, amount_cents, prepared_history)
    if not candidates:
        return prefill_false(
            row_index,
            NO_PREFILL_REASONS["no_pattern"],
            _withheld_prediction_evidence(
                build_transaction_text_profile_from_row(import_row),
                [],
                withheld_reason="no_compatible_pattern",
            ),
        )

    ranked_patterns = _rank_current_patterns(candidates)
    selected = _select_current_pattern_from_ranked(ranked_patterns)
    if selected is None:
        return prefill_false(
            row_index,
            NO_PREFILL_REASONS["tied"],
            _withheld_prediction_evidence(
                build_transaction_text_profile_from_row(import_row),
                ranked_patterns,
                withheld_reason="ambiguous_competing_candidates",
            ),
        )

    output = dict(selected.output)
    output["row_index"] = int(row_index)
    output["prefill"] = True
    output["debug_reason_codes"] = list(selected.debug_reason_codes)
    output["prediction_debug"] = prediction_debug_payload(
        engine="import_prefill",
        decision="prefill",
        prediction_type=output.get("prediction_type"),
        reason_codes=selected.debug_reason_codes,
        score=selected.score,
        evidence={"signature": list(selected.signature), **selected.debug_evidence},
    )
    return output


def _prepare_import_prefill_history(
    history_rows: Iterable[dict[str, Any] | PreparedImportPrefillHistoryRow] | None,
) -> list[PreparedImportPrefillHistoryRow]:
    prepared: list[PreparedImportPrefillHistoryRow] = []
    for hist in history_rows or []:
        if isinstance(hist, PreparedImportPrefillHistoryRow):
            prepared.append(hist)
            continue
        try:
            account_id = int(hist.get("account_id") or 0)
            amount_cents = int(hist.get("amount_cents") or 0)
        except (TypeError, ValueError):
            continue
        if not account_id or not amount_cents:
            continue

        paired = hist.get("paired_transaction") or {}
        other_account_id = paired.get("account_id") or hist.get("paired_account_id")
        try:
            other_account_id = int(other_account_id) if other_account_id else None
        except (TypeError, ValueError):
            other_account_id = None

        current_splits, current_remainder = _split_template(
            hist.get("splits") or [],
            hist.get("remainder_intent"),
        )
        other_splits, other_remainder = _split_template(
            paired.get("splits") or hist.get("paired_splits") or [],
            paired.get("remainder_intent") or hist.get("paired_remainder_intent"),
        )
        paired_account_name = paired.get("account_name") or hist.get("paired_account_name") or ""
        paired_account_profile = None
        if other_account_id or paired_account_name:
            paired_account_profile = build_account_match_profile({
                "id": other_account_id,
                "name": paired_account_name,
                "acct_key": paired.get("acct_key") or hist.get("paired_acct_key"),
                "bankid": paired.get("bankid") or hist.get("paired_bankid"),
                "acctid": paired.get("acctid") or hist.get("paired_acctid"),
                "account_type": paired.get("account_type") or hist.get("paired_account_type"),
            })

        prepared.append(PreparedImportPrefillHistoryRow(
            raw=hist,
            account_id=account_id,
            amount_cents=amount_cents,
            amount_sign=_sign(amount_cents),
            posted_at=_parse_date(hist.get("posted_at")),
            ttype=str(hist.get("ttype") or ""),
            text=row_text_key(hist),
            text_profile=build_transaction_text_profile_from_row(hist),
            paired_text=normalize_text(" ".join(
                str(paired.get(key) or hist.get(f"paired_{key}") or "")
                for key in ("account_name", "payee", "memo")
            )),
            paired_account_name=str(paired_account_name or ""),
            paired_account_text=normalize_text(paired_account_name),
            paired_account_profile=paired_account_profile,
            other_account_id=other_account_id,
            current_splits=tuple(current_splits),
            current_remainder=current_remainder,
            other_splits=tuple(other_splits),
            other_remainder=other_remainder,
            evidence_quality=_evidence_quality(hist.get("evidence_quality")),
            evidence_source=str(hist.get("source") or "final_transaction_history"),
            learning_example_id=_optional_int(hist.get("learning_example_id")),
            feedback_score=_prediction_feedback_score(hist.get("prediction_feedback")),
        ))
    return _infer_missing_remainders(prepared)


def _copy_splits(splits: Iterable[dict[str, int]]) -> list[dict[str, int]]:
    return [dict(split) for split in splits or []]


def _infer_missing_remainders(
    rows: list[PreparedImportPrefillHistoryRow],
) -> list[PreparedImportPrefillHistoryRow]:
    inferred: list[PreparedImportPrefillHistoryRow] = []
    for row in rows:
        current_splits, current_remainder = _infer_leg_remainder(
            row,
            rows,
            side="current",
            splits=row.current_splits,
            remainder=row.current_remainder,
            target_cents=row.amount_cents,
        )
        other_splits, other_remainder = _infer_leg_remainder(
            row,
            rows,
            side="other",
            splits=row.other_splits,
            remainder=row.other_remainder,
            target_cents=-row.amount_cents,
        )
        inferred.append(replace(
            row,
            current_splits=tuple(current_splits),
            current_remainder=current_remainder,
            other_splits=tuple(other_splits),
            other_remainder=other_remainder,
        ))
    return inferred


def _infer_leg_remainder(
    row: PreparedImportPrefillHistoryRow,
    rows: list[PreparedImportPrefillHistoryRow],
    *,
    side: str,
    splits: Iterable[dict[str, int]],
    remainder: dict[str, int] | None,
    target_cents: int,
) -> tuple[list[dict[str, int]], dict[str, int] | None]:
    split_list = _copy_splits(splits)
    if remainder or not _leg_can_infer_remainder(split_list, target_cents):
        return split_list, remainder

    peers = [
        peer for peer in rows
        if _leg_group_key(peer, side) == _leg_group_key(row, side)
    ]
    remainder_envelope_id = (
        _explicit_peer_remainder(peers, side)
        or _variable_peer_envelope(split_list, peers, side)
        or _largest_split_envelope(split_list)
    )
    if not remainder_envelope_id:
        return split_list, None

    fixed_splits, inferred_remainder = _split_template(
        split_list,
        _inferred_remainder_intent(split_list, remainder_envelope_id),
    )
    stable_fixed_splits = _stable_fixed_splits(fixed_splits, peers, side)
    return stable_fixed_splits, inferred_remainder


def _leg_can_infer_remainder(splits: list[dict[str, int]], target_cents: int) -> bool:
    return bool(splits) and sum(int(split["amount_cents"]) for split in splits) == int(target_cents)


def _leg_group_key(row: PreparedImportPrefillHistoryRow, side: str) -> tuple[Any, ...]:
    splits = _peer_full_splits(row, side)
    return (
        row.account_id,
        row.ttype,
        row.text,
        row.other_account_id,
        side,
        tuple(sorted(int(split["envelope_id"]) for split in splits or [])),
    )


def _peer_full_splits(row: PreparedImportPrefillHistoryRow, side: str) -> tuple[dict[str, int], ...]:
    if side == "current":
        return tuple(_clean_splits(row.raw.get("splits") or []))
    paired = row.raw.get("paired_transaction") or {}
    return tuple(_clean_splits(paired.get("splits") or row.raw.get("paired_splits") or []))


def _peer_splits(row: PreparedImportPrefillHistoryRow, side: str) -> tuple[dict[str, int], ...]:
    return row.current_splits if side == "current" else row.other_splits


def _peer_remainder(row: PreparedImportPrefillHistoryRow, side: str) -> dict[str, int] | None:
    return row.current_remainder if side == "current" else row.other_remainder


def _split_amounts_by_envelope(splits: Iterable[dict[str, int]]) -> dict[int, int]:
    amounts: dict[int, int] = {}
    for split in splits or []:
        envelope_id = int(split["envelope_id"])
        amounts[envelope_id] = amounts.get(envelope_id, 0) + int(split["amount_cents"])
    return amounts


def _explicit_peer_remainder(
    peers: list[PreparedImportPrefillHistoryRow],
    side: str,
) -> int | None:
    ranked: dict[int, tuple[int, date]] = {}
    for peer in peers:
        remainder = _peer_remainder(peer, side)
        if not remainder:
            continue
        envelope_id = int(remainder["envelope_id"])
        count, newest = ranked.get(envelope_id, (0, date.min))
        ranked[envelope_id] = (count + 1, max(newest, peer.posted_at or date.min))
    if not ranked:
        return None
    return max(ranked.items(), key=lambda item: (item[1][0], item[1][1], item[0]))[0]


def _variable_peer_envelope(
    current_splits: list[dict[str, int]],
    peers: list[PreparedImportPrefillHistoryRow],
    side: str,
) -> int | None:
    current_amounts = _split_amounts_by_envelope(current_splits)
    values_by_envelope: dict[int, list[int]] = {envelope_id: [] for envelope_id in current_amounts}
    for peer in peers:
        amounts = _split_amounts_by_envelope(_peer_full_splits(peer, side))
        if set(amounts) != set(current_amounts):
            continue
        for envelope_id in values_by_envelope:
            values_by_envelope[envelope_id].append(amounts[envelope_id])

    scored: list[tuple[int, int, int, int]] = []
    for envelope_id, values in values_by_envelope.items():
        distinct = set(values)
        if len(distinct) <= 1:
            continue
        amount_range = max(values) - min(values)
        current_abs = abs(current_amounts[envelope_id])
        scored.append((len(distinct), abs(amount_range), current_abs, envelope_id))
    if not scored:
        return None
    return max(scored)[3]


def _largest_split_envelope(splits: list[dict[str, int]]) -> int | None:
    if not splits:
        return None
    return max(
        ((abs(int(split["amount_cents"])), int(split["envelope_id"])) for split in splits),
        key=lambda item: (item[0], item[1]),
    )[1]


def _inferred_remainder_intent(
    splits: list[dict[str, int]],
    envelope_id: int,
) -> dict[str, int] | None:
    for split in splits:
        if int(split["envelope_id"]) == int(envelope_id):
            return {"envelope_id": int(envelope_id), "amount_cents": int(split["amount_cents"])}
    return None


def _stable_fixed_splits(
    fixed_splits: list[dict[str, int]],
    peers: list[PreparedImportPrefillHistoryRow],
    side: str,
) -> list[dict[str, int]]:
    stable: list[dict[str, int]] = []
    for split in fixed_splits:
        envelope_id = int(split["envelope_id"])
        values = [
            _split_amounts_by_envelope(_peer_full_splits(peer, side)).get(envelope_id)
            for peer in peers
        ]
        values = [value for value in values if value is not None]
        if len(values) >= 2 and len(set(values)) == 1:
            stable.append(dict(split))
    return stable


def _compatible_candidates(
    import_row: dict[str, Any],
    account_id: int,
    amount_cents: int,
    history_rows: list[PreparedImportPrefillHistoryRow],
) -> list[ImportPrefillCandidate]:
    anchor = _parse_date(import_row.get("posted_at") or import_row.get("date"))
    import_text = row_text_key(import_row)
    import_profile = build_transaction_text_profile_from_row(import_row)
    import_sign = _sign(amount_cents)
    candidates: list[ImportPrefillCandidate] = []
    for hist in history_rows or []:
        if anchor and hist.posted_at and hist.posted_at > anchor:
            continue
        if hist.account_id != int(account_id):
            continue
        if hist.amount_sign != import_sign:
            continue

        if hist.ttype in _TRANSFER_TYPES:
            candidate = _transfer_candidate(import_text, import_profile, amount_cents, hist, anchor)
        elif hist.ttype in _STANDARD_TYPES:
            candidate = _standard_candidate(import_text, import_profile, amount_cents, hist, anchor)
        else:
            candidate = None
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _standard_candidate(
    import_text: str,
    import_profile: TransactionTextProfile,
    amount_cents: int,
    hist: PreparedImportPrefillHistoryRow,
    anchor: date | None,
) -> ImportPrefillCandidate | None:
    if not hist.current_splits and not hist.current_remainder:
        return None

    merchant_score = _merchant_identity_similarity(import_profile, hist.text_profile)
    text_score = max(_text_similarity(import_text, hist.text), merchant_score)
    amount_score = _amount_similarity(amount_cents, hist.amount_cents)
    recency = _recency_weight(hist.posted_at, anchor, half_life_days=75)
    quality_score = _evidence_quality_score(hist.evidence_quality)
    feedback_score = hist.feedback_score
    if merchant_score < 0.5 and amount_score < 1.0:
        return None
    if text_score < 0.55 and amount_score < 1.0:
        return None
    if _looks_transfer_like(import_profile) and merchant_score < 0.5 and text_score < 0.75:
        return None

    splits = _copy_splits(hist.current_splits)
    remainder_intent = dict(hist.current_remainder) if hist.current_remainder else None
    remainder_envelope_id = int(remainder_intent["envelope_id"]) if remainder_intent else None
    signature = ("standard", hist.ttype, split_signature(splits), remainder_envelope_id)
    output: dict[str, Any] = {
        "prediction_type": "new_transaction",
        "transaction_type": hist.ttype,
        "single_envelope_id": None,
        "splits": [],
        "remainder_envelope_id": remainder_envelope_id,
        "remainder_amount_cents": int(remainder_intent["amount_cents"]) if remainder_intent else None,
        "transfer": None,
    }
    if remainder_intent:
        output["splits"] = splits
    elif len(splits) == 1:
        output["single_envelope_id"] = int(splits[0]["envelope_id"])
    else:
        output["splits"] = splits

    source_account_score = 1.0
    score = (
        2.0 * text_score
        + 1.5 * merchant_score
        + 1.5 * amount_score
        + 0.6 * source_account_score
        + 0.45 * quality_score
        + 0.35 * feedback_score
    ) * (0.35 + recency)
    reasons = ["standard_pattern", "same_source_account"]
    if remainder_intent:
        reasons.append("remainder_pattern")
        if not splits:
            reasons.append("single_envelope_history")
    elif len(splits) == 1:
        reasons.append("single_envelope_history")
    if merchant_score >= 0.5:
        reasons.append("merchant_identity_match")
    if amount_score >= 1:
        reasons.append("same_amount")
    if text_score >= 0.9:
        reasons.append("same_text")
    if recency >= 0.65:
        reasons.append("recent_envelope_pattern")
    if hist.evidence_quality in {"high", "medium"}:
        reasons.append(f"{hist.evidence_quality}_quality_learning_example")
    return ImportPrefillCandidate(
        signature,
        output,
        score,
        hist.posted_at,
        tuple(reasons),
        _candidate_evidence(
            hist,
            {
                "raw_profile_match": text_score,
                "merchant_identity": merchant_score,
                "source_account": source_account_score,
                "amount_similarity": amount_score,
                "recency": recency,
                "evidence_quality": quality_score,
                "prediction_feedback": feedback_score,
            },
            matched_raw_profile_facts=_matched_raw_profile_facts(
                import_profile,
                hist.text_profile,
            ),
        ),
    )


def _transfer_candidate(
    import_text: str,
    import_profile: TransactionTextProfile,
    amount_cents: int,
    hist: PreparedImportPrefillHistoryRow,
    anchor: date | None,
) -> ImportPrefillCandidate | None:
    if not hist.other_account_id:
        return None

    if not hist.current_splits and not hist.other_splits and not hist.current_remainder and not hist.other_remainder:
        return None

    text_score = max(_text_similarity(import_text, hist.text), _text_similarity(import_text, hist.paired_text))
    amount_score = _amount_similarity(amount_cents, hist.amount_cents)
    mentions_account = bool(hist.paired_account_text and hist.paired_account_text in import_text)
    matched_suffixes = _matched_account_suffixes(import_profile, hist.paired_account_profile)
    matched_type_hints = _matched_account_type_hints(import_profile, hist.paired_account_profile)
    direction_matches = _transfer_direction_matches(import_profile, hist.ttype)
    merchant_score = _merchant_identity_similarity(import_profile, hist.text_profile)
    identity_score = _transfer_identity_confidence(
        import_profile,
        hist,
        mentions_account=mentions_account,
        matched_suffixes=matched_suffixes,
        matched_type_hints=matched_type_hints,
        merchant_score=merchant_score,
    )
    recency = _recency_weight(hist.posted_at, anchor, half_life_days=150)
    quality_score = _evidence_quality_score(hist.evidence_quality)
    feedback_score = hist.feedback_score

    if identity_score < 0.4:
        return None
    if amount_score < 1.0 and text_score < 0.55 and identity_score < 0.75:
        return None

    current_splits = _copy_splits(hist.current_splits)
    other_splits = _copy_splits(hist.other_splits)
    current_remainder = dict(hist.current_remainder) if hist.current_remainder else None
    other_remainder = dict(hist.other_remainder) if hist.other_remainder else None
    current_remainder_envelope_id = int(current_remainder["envelope_id"]) if current_remainder else None
    other_remainder_envelope_id = int(other_remainder["envelope_id"]) if other_remainder else None
    signature = (
        "transfer",
        hist.ttype,
        int(hist.other_account_id),
        split_signature(current_splits),
        current_remainder_envelope_id,
        split_signature(other_splits),
        other_remainder_envelope_id,
    )
    output = {
        "prediction_type": "new_transaction",
        "transaction_type": hist.ttype,
        "single_envelope_id": None,
        "splits": [],
        "transfer": {
            "other_account_id": int(hist.other_account_id),
            "other_account_name": hist.paired_account_name,
            "current_account_splits": current_splits,
            "current_account_remainder_envelope_id": current_remainder_envelope_id,
            "current_account_remainder_amount_cents": int(current_remainder["amount_cents"]) if current_remainder else None,
            "other_account_splits": other_splits,
            "other_account_remainder_envelope_id": other_remainder_envelope_id,
            "other_account_remainder_amount_cents": int(other_remainder["amount_cents"]) if other_remainder else None,
        },
    }
    source_account_score = 1.0
    score = (
        0.8 * text_score
        + 1.4 * amount_score
        + 2.4 * identity_score
        + 0.5 * source_account_score
        + 0.4 * quality_score
        + 0.3 * feedback_score
    ) * (0.35 + recency)
    if mentions_account:
        score += 0.3
    if matched_suffixes:
        score += 1.2
    if direction_matches:
        score += 0.45
    if matched_type_hints:
        score += 0.25
    reasons = ["transfer_pattern"]
    if current_remainder or other_remainder:
        reasons.append("remainder_pattern")
    if amount_score >= 1:
        reasons.append("same_amount")
    if mentions_account:
        reasons.append("mentions_other_account")
    if matched_suffixes:
        reasons.append("matched_account_suffix")
    if direction_matches:
        reasons.append("matched_direction")
    if matched_type_hints:
        reasons.append("matched_account_type_hint")
    if identity_score >= 0.75:
        reasons.append("strong_account_identity")
    if hist.evidence_quality in {"high", "medium"}:
        reasons.append(f"{hist.evidence_quality}_quality_learning_example")
    return ImportPrefillCandidate(
        signature,
        output,
        score,
        hist.posted_at,
        tuple(reasons),
        _candidate_evidence(
            hist,
            {
                "raw_profile_match": text_score,
                "merchant_identity": merchant_score,
                "account_identity": identity_score,
                "account_suffix": 1.0 if matched_suffixes else 0.0,
                "source_account": source_account_score,
                "amount_similarity": amount_score,
                "recency": recency,
                "evidence_quality": quality_score,
                "prediction_feedback": feedback_score,
                "direction": 1.0 if direction_matches else 0.0,
                "account_type_hint": 1.0 if matched_type_hints else 0.0,
            },
            matched_raw_profile_facts=_matched_raw_profile_facts(
                import_profile,
                hist.text_profile,
                matched_account_suffixes=matched_suffixes,
                matched_account_type_hints=matched_type_hints,
                direction_matches=direction_matches,
            ),
            extra={
                "matched_account_suffixes": list(matched_suffixes),
                "matched_account_type_hints": list(matched_type_hints),
                "transfer_other_account_id": hist.other_account_id,
            },
        ),
    )


def _matched_account_suffixes(
    import_profile: TransactionTextProfile,
    account_profile: AccountMatchProfile | None,
) -> tuple[str, ...]:
    if not account_profile:
        return ()
    account_suffixes = set(account_profile.all_suffixes)
    return tuple(suffix for suffix in import_profile.account_suffixes if suffix in account_suffixes)


def _matched_account_type_hints(
    import_profile: TransactionTextProfile,
    account_profile: AccountMatchProfile | None,
) -> tuple[str, ...]:
    if not account_profile:
        return ()
    account_aliases = set(account_profile.type_aliases)
    return tuple(hint for hint in import_profile.account_type_hints if hint in account_aliases)


def _transfer_identity_confidence(
    import_profile: TransactionTextProfile,
    hist: PreparedImportPrefillHistoryRow,
    *,
    mentions_account: bool,
    matched_suffixes: tuple[str, ...],
    matched_type_hints: tuple[str, ...],
    merchant_score: float,
) -> float:
    scores: list[float] = []
    if matched_suffixes:
        scores.append(1.0)
    if hist.paired_account_profile:
        institution_score = _account_institution_similarity(import_profile, hist.paired_account_profile)
        if import_profile.account_suffixes and hist.paired_account_profile.all_suffixes and not matched_suffixes:
            institution_score = 0.0
        elif hist.paired_account_profile.all_suffixes and not matched_suffixes:
            institution_score = min(institution_score, 0.35)
        if institution_score and matched_type_hints:
            scores.append(min(0.85, institution_score + 0.2))
        elif institution_score:
            scores.append(institution_score)
    if mentions_account:
        if import_profile.account_suffixes and hist.paired_account_profile and hist.paired_account_profile.all_suffixes and not matched_suffixes:
            scores.append(0.0)
        elif hist.paired_account_profile and hist.paired_account_profile.all_suffixes and not matched_suffixes:
            scores.append(0.35)
        else:
            scores.append(0.9)
    if merchant_score >= 0.75:
        scores.append(0.35)
    if matched_type_hints:
        scores.append(0.35)
    return max(scores or [0.0])


def _account_institution_similarity(
    import_profile: TransactionTextProfile,
    account_profile: AccountMatchProfile,
) -> float:
    import_tokens = set(import_profile.merchant_tokens)
    institution_tokens = set(account_profile.institution_tokens)
    if not import_tokens or not institution_tokens:
        return 0.0
    overlap = import_tokens & institution_tokens
    if not overlap:
        return 0.0
    return len(overlap) / max(len(institution_tokens), 1)


def _transfer_direction_matches(import_profile: TransactionTextProfile, ttype: str) -> bool:
    if import_profile.direction == "to" and ttype == "transfer_out":
        return True
    if import_profile.direction == "from" and ttype == "transfer_in":
        return True
    return False


def _merchant_identity_similarity(
    import_profile: TransactionTextProfile,
    history_profile: TransactionTextProfile,
) -> float:
    import_tokens = set(import_profile.merchant_tokens)
    history_tokens = set(history_profile.merchant_tokens)
    if not import_tokens or not history_tokens:
        return 0.0
    overlap = import_tokens & history_tokens
    if not overlap:
        return 0.0
    precision = len(overlap) / len(import_tokens)
    recall = len(overlap) / len(history_tokens)
    fuzzy_score = partial_token_similarity(import_tokens, history_tokens)
    return min(1.0, max((precision + recall) / 2, fuzzy_score))


def _looks_transfer_like(import_profile: TransactionTextProfile) -> bool:
    transfer_terms = {"transfer", "xfer", "ach"}
    if transfer_terms & set(import_profile.generic_tokens):
        return True
    if import_profile.direction and (import_profile.account_suffixes or import_profile.account_type_hints):
        return True
    return False


def _select_current_pattern(candidates: list[ImportPrefillCandidate]) -> ImportPrefillCandidate | None:
    return _select_current_pattern_from_ranked(_rank_current_patterns(candidates))


def _rank_current_patterns(candidates: list[ImportPrefillCandidate]) -> list[RankedImportPrefillPattern]:
    if not candidates:
        return []

    groups: dict[tuple[Any, ...], list[ImportPrefillCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.signature, []).append(candidate)

    scored: list[RankedImportPrefillPattern] = []
    recent_sorted = sorted(candidates, key=lambda c: (c.posted_at or date.min), reverse=True)
    latest_signatures = [c.signature for c in recent_sorted[:3]]

    for signature, group in groups.items():
        base_score = sum(c.score for c in group)
        newest = max(group, key=lambda c: (c.posted_at or date.min, c.score))
        sample_support = min(len(group), 4) * 0.35
        current_run_support = _current_run_support(signature, latest_signatures)
        score = base_score + sample_support + current_run_support
        scored.append(RankedImportPrefillPattern(
            score=score,
            signature=signature,
            representative=newest,
            support_count=len(group),
            support_score=sample_support + current_run_support,
            current_run_support=current_run_support,
        ))

    return sorted(scored, key=lambda item: item.score, reverse=True)


def _select_current_pattern_from_ranked(
    scored: list[RankedImportPrefillPattern],
) -> ImportPrefillCandidate | None:
    if not scored:
        return None
    if len(scored) > 1:
        top_score = scored[0].score
        top_sig = scored[0].signature
        second_score = scored[1].score
        second_sig = scored[1].signature
        if top_sig != second_sig and second_score >= top_score * 0.88:
            return None

    winning_pattern = scored[0]
    selected = winning_pattern.representative
    runner_up_score = scored[1].score if len(scored) > 1 else None
    ambiguity_margin = winning_pattern.score - runner_up_score if runner_up_score is not None else None
    extra_reasons: list[str] = []
    if winning_pattern.current_run_support >= 1.0:
        extra_reasons.append("latest_allocation_run")
    if winning_pattern.support_count > 1:
        extra_reasons.append("repeated_pattern")
    debug_evidence = dict(selected.debug_evidence)
    debug_evidence.update({
        "support_count": winning_pattern.support_count,
        "support_score": round(float(winning_pattern.support_score), 4),
        "winning_candidate": _candidate_audit(winning_pattern),
        "competing_candidates": [_candidate_audit(pattern) for pattern in scored[1:5]],
        "candidate_count": sum(pattern.support_count for pattern in scored),
        "candidate_group_count": len(scored),
    })
    if runner_up_score is not None:
        debug_evidence["runner_up_score"] = round(float(runner_up_score), 4)
        debug_evidence["ambiguity_margin"] = round(float(ambiguity_margin or 0.0), 4)

    return ImportPrefillCandidate(
        signature=selected.signature,
        output=selected.output,
        score=winning_pattern.score,
        posted_at=selected.posted_at,
        debug_reason_codes=tuple(dict.fromkeys((*selected.debug_reason_codes, *extra_reasons))),
        debug_evidence=debug_evidence,
    )


def _current_run_support(signature: tuple[Any, ...], latest_signatures: list[tuple[Any, ...]]) -> float:
    if not latest_signatures or latest_signatures[0] != signature:
        return 0.0
    run = 0
    for item in latest_signatures:
        if item == signature:
            run += 1
        else:
            break
    if run >= 3:
        return 3.0
    if run == 2:
        return 2.25
    return 0.35


def _clean_splits(splits: Iterable[dict[str, Any]]) -> list[dict[str, int]]:
    cleaned: list[dict[str, int]] = []
    for split in splits or []:
        try:
            envelope_id = int(split.get("envelope_id"))
            amount_cents = int(split.get("amount_cents"))
        except (TypeError, ValueError):
            continue
        if envelope_id and amount_cents:
            cleaned.append({"envelope_id": envelope_id, "amount_cents": amount_cents})
    return sorted(cleaned, key=lambda s: (s["envelope_id"], s["amount_cents"]))


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _sign(value: int) -> int:
    return -1 if int(value) < 0 else 1


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed else None


def _evidence_quality(value: Any) -> str:
    quality = str(value or "low").strip().lower()
    return quality if quality in {"high", "medium", "low"} else "low"


def _evidence_quality_score(value: Any) -> float:
    return {
        "high": 1.0,
        "medium": 0.7,
        "low": 0.35,
    }.get(_evidence_quality(value), 0.35)


def _prediction_feedback_score(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    accepted = _optional_int(value.get("accepted")) or 0
    modified = _optional_int(value.get("modified")) or 0
    rejected = _optional_int(value.get("rejected")) or 0
    total = accepted + modified + rejected
    if total <= 0:
        return 0.0
    raw = (accepted - rejected - (0.35 * modified)) / total
    return max(-1.0, min(1.0, float(raw)))


def _candidate_evidence(
    hist: PreparedImportPrefillHistoryRow,
    score_components: dict[str, float],
    *,
    matched_raw_profile_facts: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "evidence_quality": hist.evidence_quality,
        "evidence_source": hist.evidence_source,
        "score_components": {
            key: round(float(value), 4)
            for key, value in sorted(score_components.items())
        },
        "matched_raw_profile_facts": dict(matched_raw_profile_facts or {}),
    }
    if hist.learning_example_id:
        evidence["learning_example_id"] = hist.learning_example_id
    history_id = _optional_int(hist.raw.get("id"))
    transaction_id = _optional_int(hist.raw.get("transaction_id")) or (
        history_id if str(hist.raw.get("id") or "").isdigit() else None
    )
    if transaction_id:
        evidence["transaction_id"] = transaction_id
    if extra:
        evidence.update(extra)
    return evidence


def _matched_raw_profile_facts(
    import_profile: TransactionTextProfile,
    history_profile: TransactionTextProfile,
    *,
    matched_account_suffixes: Iterable[str] = (),
    matched_account_type_hints: Iterable[str] = (),
    direction_matches: bool = False,
) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    merchant_overlap = sorted(set(import_profile.merchant_tokens) & set(history_profile.merchant_tokens))
    generic_overlap = sorted(set(import_profile.generic_tokens) & set(history_profile.generic_tokens))
    if merchant_overlap:
        facts["merchant_tokens"] = merchant_overlap
    if generic_overlap:
        facts["generic_tokens"] = generic_overlap
    suffixes = list(dict.fromkeys(str(value) for value in matched_account_suffixes if value))
    if suffixes:
        facts["account_suffixes"] = suffixes
    type_hints = list(dict.fromkeys(str(value) for value in matched_account_type_hints if value))
    if type_hints:
        facts["account_type_hints"] = type_hints
    if direction_matches and import_profile.direction:
        facts["direction"] = import_profile.direction
    return facts


def _withheld_prediction_evidence(
    import_profile: TransactionTextProfile,
    ranked_patterns: list[RankedImportPrefillPattern],
    *,
    withheld_reason: str,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "withheld_reason": withheld_reason,
        "candidate_count": sum(pattern.support_count for pattern in ranked_patterns),
        "candidate_group_count": len(ranked_patterns),
        "raw_profile": _raw_profile_summary(import_profile),
        "winning_candidate": _candidate_audit(ranked_patterns[0]) if ranked_patterns else None,
        "competing_candidates": [_candidate_audit(pattern) for pattern in ranked_patterns[1:5]],
    }
    if len(ranked_patterns) > 1:
        top = ranked_patterns[0].score
        runner_up = ranked_patterns[1].score
        evidence["runner_up_score"] = round(float(runner_up), 4)
        evidence["ambiguity_margin"] = round(float(top - runner_up), 4)
        evidence["ambiguity_threshold_ratio"] = 0.88
    return evidence


def _raw_profile_summary(profile: TransactionTextProfile) -> dict[str, Any]:
    return {
        "merchant_tokens": list(profile.merchant_tokens),
        "generic_tokens": list(profile.generic_tokens),
        "direction": profile.direction,
        "account_type_hints": list(profile.account_type_hints),
        "account_suffixes": list(profile.account_suffixes),
        "reference_numbers": list(profile.reference_numbers),
    }


def _candidate_audit(pattern: RankedImportPrefillPattern) -> dict[str, Any]:
    candidate = pattern.representative
    evidence = candidate.debug_evidence
    audit: dict[str, Any] = {
        "signature": _json_safe(candidate.signature),
        "score": round(float(pattern.score), 4),
        "candidate_score": round(float(candidate.score), 4),
        "prediction_type": candidate.output.get("prediction_type"),
        "transaction_type": candidate.output.get("transaction_type"),
        "support_count": pattern.support_count,
        "support_score": round(float(pattern.support_score), 4),
        "reason_codes": list(candidate.debug_reason_codes),
        "evidence_quality": evidence.get("evidence_quality"),
        "evidence_source": evidence.get("evidence_source"),
        "score_components": dict(evidence.get("score_components") or {}),
        "matched_raw_profile_facts": dict(evidence.get("matched_raw_profile_facts") or {}),
    }
    for key in ("learning_example_id", "transaction_id", "transfer_other_account_id"):
        if evidence.get(key) is not None:
            audit[key] = evidence.get(key)
    return audit


def _json_safe(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


@lru_cache(maxsize=65536)
def _text_similarity(left: str, right: str) -> float:
    return cached_text_similarity(left, right)


def _amount_similarity(import_amount: int, history_amount: int) -> float:
    if int(import_amount) == int(history_amount):
        return 1.0
    if abs(int(import_amount)) == abs(int(history_amount)):
        return 0.85
    largest = max(abs(int(import_amount)), abs(int(history_amount)), 1)
    delta = abs(abs(int(import_amount)) - abs(int(history_amount))) / largest
    if delta <= 0.05:
        return 0.65
    if delta <= 0.15:
        return 0.35
    return 0.0


def _recency_weight(posted_at: date | None, anchor: date | None, *, half_life_days: int) -> float:
    if posted_at is None or anchor is None:
        return 0.25
    days = max((anchor - posted_at).days, 0)
    return math.pow(0.5, days / max(int(half_life_days), 1))


def _confidence_for_score(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"
