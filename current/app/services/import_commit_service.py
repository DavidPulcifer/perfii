from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .imports_service import guess_ttype, import_row_fingerprint
from .payee_normalization_service import record_payee_normalization_from_import_row
from .transaction_learning_service import record_prediction_feedback_rows
from ..db import get_db
from ..repositories.transaction_learning_repo import compact_json
from ..utils import parse_money_to_cents_strict


@dataclass(frozen=True)
class StatementIdentifiers:
    bankid: str | None
    acctid: str | None

    @property
    def has_any(self) -> bool:
        return bool(self.bankid or self.acctid)


IMPORT_SOURCE_REUPLOAD_MESSAGE = "This import review expired or could not be verified. Upload the statement again."


class ImportSourceUnavailableError(ValueError):
    pass


@dataclass(frozen=True)
class ImportSplitPlan:
    splits: list[dict]
    remainder_envelope_id: int | None = None
    remainder_amount_cents: int | None = None


@dataclass(frozen=True)
class ImportTransferSplits:
    out_splits: list[dict]
    in_splits: list[dict]
    allow_unallocated_in: bool
    out_remainder_envelope_id: int | None = None
    in_remainder_envelope_id: int | None = None
    out_remainder_amount_cents: int | None = None
    in_remainder_amount_cents: int | None = None


@dataclass
class ImportCommitTally:
    imported: int = 0
    skipped: int = 0
    skipped_reasons: list[str] | None = None
    import_session_id: int | None = None

    def __post_init__(self) -> None:
        if self.skipped_reasons is None:
            self.skipped_reasons = []

    def record_imported(self) -> None:
        self.imported += 1

    def record_skipped(self, reason: str | None = None) -> None:
        self.skipped += 1
        if reason:
            self.skipped_reasons.append(reason)

    def flash(self) -> tuple[str, str]:
        return import_result_flash(self.imported, self.skipped, self.skipped_reasons or [])


@dataclass(frozen=True)
class ImportRowCommitResult:
    imported: bool = False
    skipped: bool = False
    skip_reason: str | None = None

    @classmethod
    def imported_row(cls) -> "ImportRowCommitResult":
        return cls(imported=True)

    @classmethod
    def skipped_row(cls, reason: str | None = None) -> "ImportRowCommitResult":
        return cls(skipped=True, skip_reason=reason)


@dataclass(frozen=True)
class ImportCommitContext:
    count: int
    existing_fitids: set[str]
    source_bankid: str | None = None
    source_acctid: str | None = None
    file_hash: str | None = None


@dataclass(frozen=True)
class ImportCommitPlanItem:
    row: ImportRowForm | None
    action: str
    skip_reason: str | None = None


@dataclass(frozen=True)
class ImportCommitPlan:
    items: list[ImportCommitPlanItem]


@dataclass(frozen=True)
class ImportCommitAccountSelection:
    account_id: int | None = None
    account: dict | None = None
    error_message: str | None = None
    flash_category: str = "warning"

    @property
    def ok(self) -> bool:
        return self.account_id is not None and self.account is not None and self.error_message is None


@dataclass(frozen=True)
class ImportRowForm:
    index: int
    selected: bool
    posted_at: str | None
    amount_cents: int
    payee: str | None
    orig_payee: str | None
    memo: str | None
    orig_memo: str | None
    fitid: str | None
    match_tx_id: int | None
    match_amount_source: str
    is_transfer: bool
    transfer_account_id: int | None


def _blank_to_none(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def load_import_commit_account(form, get_account_func) -> ImportCommitAccountSelection:
    account_id = form.get("account_id", type=int)
    if not account_id:
        return ImportCommitAccountSelection(
            error_message="Pick an account to import into.",
            flash_category="warning",
        )

    account = get_account_func(account_id)
    if not account:
        return ImportCommitAccountSelection(
            account_id=account_id,
            error_message="Account not found.",
            flash_category="danger",
        )

    return ImportCommitAccountSelection(account_id=account_id, account=account)


def statement_identifiers_from_source(source: dict | None) -> StatementIdentifiers:
    return StatementIdentifiers(
        bankid=_blank_to_none((source or {}).get("source_bankid")),
        acctid=_blank_to_none((source or {}).get("source_acctid")),
    )


def import_source_token_from_form(form) -> str | None:
    return _blank_to_none(form.get("import_source_token"))


def load_statement_identifiers_from_source_token(
    form,
    *,
    account_id: int,
    get_import_review_source_func,
) -> tuple[StatementIdentifiers, str | None]:
    token = import_source_token_from_form(form)
    if not token:
        raise ImportSourceUnavailableError(IMPORT_SOURCE_REUPLOAD_MESSAGE)

    source = get_import_review_source_func(token, account_id)
    if not source:
        raise ImportSourceUnavailableError(IMPORT_SOURCE_REUPLOAD_MESSAGE)

    return statement_identifiers_from_source(source), _blank_to_none(source.get("file_hash"))


def should_bind_statement_identifiers(account: dict | None, identifiers: StatementIdentifiers) -> bool:
    if not account or not identifiers.has_any:
        return False
    account_bankid = (account.get("bankid") or "").strip()
    account_acctid = (account.get("acctid") or "").strip()
    return not account_bankid and not account_acctid


def bind_statement_identifiers_if_missing(
    account_id: int,
    account: dict | None,
    identifiers: StatementIdentifiers,
    update_account_func,
) -> bool:
    if not should_bind_statement_identifiers(account, identifiers):
        return False

    update_account_func(account_id, {"bankid": identifiers.bankid, "acctid": identifiers.acctid})
    account["bankid"] = identifiers.bankid
    account["acctid"] = identifiers.acctid
    return True


def prepare_import_commit_context(
    *,
    account_id: int,
    account: dict,
    form,
    list_fitids_func,
    update_account_func,
    get_import_review_source_func=None,
) -> ImportCommitContext:
    count = import_row_count(form)
    existing_fitids = set(list_fitids_func(account_id))
    identifiers = StatementIdentifiers(bankid=None, acctid=None)
    file_hash = None
    if get_import_review_source_func:
        identifiers, file_hash = load_statement_identifiers_from_source_token(
            form,
            account_id=account_id,
            get_import_review_source_func=get_import_review_source_func,
        )
    bind_statement_identifiers_if_missing(account_id, account, identifiers, update_account_func)
    return ImportCommitContext(
        count=count,
        existing_fitids=existing_fitids,
        source_bankid=identifiers.bankid,
        source_acctid=identifiers.acctid,
        file_hash=file_hash,
    )


def perform_import_commit(
    *,
    account_id: int,
    account: dict,
    form,
    list_fitids_func,
    update_account_func,
    get_transaction_func,
    edit_transaction_func,
    get_account_func,
    create_transfer_func,
    create_expense_func,
    create_income_func,
    logger,
    flash_func,
    record_import_provenance_func=None,
    record_payee_normalization_func=record_payee_normalization_from_import_row,
    get_import_review_source_func=None,
) -> tuple[ImportCommitTally, set[int]]:
    context = prepare_import_commit_context(
        account_id=account_id,
        account=account,
        form=form,
        list_fitids_func=list_fitids_func,
        update_account_func=update_account_func,
        get_import_review_source_func=get_import_review_source_func,
    )

    tally, matched_ids = commit_import_rows(
        account_id=account_id,
        account=account,
        form=form,
        count=context.count,
        existing_fitids=context.existing_fitids,
        get_transaction_func=get_transaction_func,
        edit_transaction_func=edit_transaction_func,
        get_account_func=get_account_func,
        create_transfer_func=create_transfer_func,
        create_expense_func=create_expense_func,
        create_income_func=create_income_func,
        logger=logger,
        flash_func=flash_func,
        source_bankid=context.source_bankid,
        source_acctid=context.source_acctid,
        file_hash=context.file_hash,
        record_import_provenance_func=record_import_provenance_func,
        record_payee_normalization_func=record_payee_normalization_func,
    )

    finalize_import_commit(
        tally=tally,
        form=form,
        matched_ids=matched_ids,
        edit_transaction_func=edit_transaction_func,
        logger=logger,
        flash_func=flash_func,
    )
    return tally, matched_ids


def import_row_count(form) -> int:
    try:
        return max(0, int(form.get("count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def selected_import_row_indices(form, count: int) -> list[int]:
    return [row_index for row_index in range(max(0, int(count))) if form.get(f"row_{row_index}")]


def parse_import_row_form(form, row_index: int) -> ImportRowForm:
    return ImportRowForm(
        index=row_index,
        selected=bool(form.get(f"row_{row_index}")),
        posted_at=_blank_to_none(form.get(f"posted_at_{row_index}")),
        amount_cents=parse_money_to_cents_strict(
            form.get(f"amount_{row_index}"),
            field_name=f"Row {row_index + 1} amount",
        ),
        payee=_blank_to_none(form.get(f"payee_{row_index}")),
        orig_payee=_blank_to_none(form.get(f"orig_payee_{row_index}")),
        memo=_blank_to_none(form.get(f"memo_{row_index}")),
        orig_memo=_blank_to_none(form.get(f"orig_memo_{row_index}")),
        fitid=_blank_to_none(form.get(f"fitid_{row_index}")),
        match_tx_id=_get_int(form, f"match_tx_{row_index}"),
        match_amount_source=((form.get(f"match_amt_src_{row_index}") or "").strip().lower()),
        is_transfer=bool(form.get(f"is_transfer_{row_index}")),
        transfer_account_id=_get_int(form, f"transfer_account_{row_index}"),
    )


def determine_import_transaction_type(row: ImportRowForm, account_type: str | None) -> str:
    ttype = guess_ttype(row.amount_cents, account_type)
    if row.is_transfer:
        if row.amount_cents > 0:
            return "transfer_in"
        if row.amount_cents < 0:
            return "transfer_out"
    return ttype


def match_target_belongs_to_account(existing: dict | None, account_id: int) -> bool:
    return bool(existing and int(existing["account_id"]) == int(account_id))


def matched_transaction_amount_cents(row: ImportRowForm, existing: dict) -> int:
    if row.match_amount_source == "import":
        return int(row.amount_cents)
    return int(existing["amount_cents"])


def matched_transaction_payload(row: ImportRowForm, existing: dict, new_amount_cents: int) -> dict:
    return {
        "posted_at": row.posted_at or existing.get("posted_at"),
        "payee": row.payee if (row.payee or "") != "" else existing.get("payee"),
        "memo": row.memo if (row.memo or "") != "" else existing.get("memo"),
        "amount_cents": new_amount_cents,
        "fitid": row.fitid or existing.get("fitid"),
    }


def update_import_matched_transaction(
    *,
    account_id: int,
    row: ImportRowForm,
    form,
    existing_fitids: set[str],
    get_transaction_func,
    edit_transaction_func,
    flash_func,
) -> str | None:
    match_tx_id = row.match_tx_id
    existing = get_transaction_func(match_tx_id)
    if not match_target_belongs_to_account(existing, account_id):
        return flash_invalid_match_skip(row.index, match_tx_id, flash_func)

    new_amount_cents = matched_transaction_amount_cents(row, existing)
    split_plan = collect_import_row_split_plan(form, row.index, new_amount_cents)

    splits_for_edit = split_plan.splits if split_plan.splits else None
    if (
        split_plan.remainder_envelope_id is not None
        and len(split_plan.splits) == 1
        and int(split_plan.splits[0].get("envelope_id")) == int(split_plan.remainder_envelope_id)
        and split_plan.remainder_amount_cents is not None
        and int(split_plan.splits[0].get("amount_cents")) == int(split_plan.remainder_amount_cents)
    ):
        splits_for_edit = None

    edit_transaction_func(
        tx_id=match_tx_id,
        payload=matched_transaction_payload(row, existing, new_amount_cents),
        splits=splits_for_edit,
        remainder_envelope_id=split_plan.remainder_envelope_id,
        remainder_amount_cents=split_plan.remainder_amount_cents,
    )

    remember_imported_fitid(row, existing_fitids)
    return None


def _row_evidence(row: ImportRowForm, *, file_hash: str | None = None) -> dict:
    return {
        "row_index": row.index,
        "posted_at": row.posted_at,
        "amount_cents": row.amount_cents,
        "payee": row.payee,
        "orig_payee": row.orig_payee,
        "memo": row.memo,
        "orig_memo": row.orig_memo,
        "fitid": row.fitid,
        "file_hash": file_hash,
    }


def _transaction_ids_from_commit_result(result) -> list[int]:
    if result is None:
        return []
    if isinstance(result, tuple):
        values = result
    else:
        values = (result,)
    ids: list[int] = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed:
            ids.append(parsed)
    return ids


def _parse_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _get_formlist(form, key: str) -> list[Any]:
    try:
        return list(form.getlist(key))
    except AttributeError:
        raw = form.get(key)
        if raw in (None, ""):
            return []
        return raw if isinstance(raw, list) else [raw]


def import_prediction_feedback_items(form) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in _get_formlist(form, "prediction_feedback_json"):
        parsed = _parse_json_object(raw)
        if isinstance(parsed.get("items"), list):
            items.extend(item for item in parsed["items"] if isinstance(item, dict))
            continue
        if parsed:
            items.append(parsed)

    for key in _form_keys(form):
        if str(key) == "prediction_feedback_json":
            continue
        if not str(key).startswith("prediction_feedback_"):
            continue
        for raw in _get_formlist(form, str(key)):
            parsed = _parse_json_object(raw)
            if parsed:
                items.append(parsed)
    return items


def _clean_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed else None


def _clean_index(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _clean_splits_for_feedback(splits: Iterable[dict[str, Any]] | None) -> list[dict[str, int]]:
    cleaned: list[dict[str, int]] = []
    for split in splits or []:
        envelope_id = _clean_int(split.get("envelope_id"))
        try:
            amount_cents = int(split.get("amount_cents") or 0)
        except (TypeError, ValueError):
            continue
        if envelope_id and amount_cents:
            cleaned.append({"envelope_id": envelope_id, "amount_cents": amount_cents})
    return sorted(cleaned, key=lambda item: (item["envelope_id"], item["amount_cents"]))


def normalized_prediction_json(value: dict[str, Any] | None) -> dict[str, Any]:
    prediction = dict(value or {})
    transfer = prediction.get("transfer") if isinstance(prediction.get("transfer"), dict) else None
    normalized: dict[str, Any] = {
        "prediction_type": prediction.get("prediction_type") or "new_transaction",
        "transaction_type": prediction.get("transaction_type"),
        "single_envelope_id": _clean_int(prediction.get("single_envelope_id")),
        "splits": _clean_splits_for_feedback(prediction.get("splits")),
        "remainder_envelope_id": _clean_int(prediction.get("remainder_envelope_id")),
        "remainder_amount_cents": prediction.get("remainder_amount_cents"),
        "transfer": None,
    }
    if transfer:
        normalized["transfer"] = {
            "other_account_id": _clean_int(transfer.get("other_account_id")),
            "current_account_splits": _clean_splits_for_feedback(transfer.get("current_account_splits")),
            "current_account_remainder_envelope_id": _clean_int(
                transfer.get("current_account_remainder_envelope_id")
            ),
            "current_account_remainder_amount_cents": transfer.get("current_account_remainder_amount_cents"),
            "other_account_splits": _clean_splits_for_feedback(transfer.get("other_account_splits")),
            "other_account_remainder_envelope_id": _clean_int(
                transfer.get("other_account_remainder_envelope_id")
            ),
            "other_account_remainder_amount_cents": transfer.get("other_account_remainder_amount_cents"),
        }
    return normalized


def _standard_feedback_json(transaction_type: str, split_plan: ImportSplitPlan) -> dict[str, Any]:
    splits = _clean_splits_for_feedback(split_plan.splits)
    return normalized_prediction_json({
        "prediction_type": "new_transaction",
        "transaction_type": transaction_type,
        "single_envelope_id": (
            int(splits[0]["envelope_id"])
            if len(splits) == 1 and split_plan.remainder_envelope_id is None
            else None
        ),
        "splits": [] if len(splits) == 1 and split_plan.remainder_envelope_id is None else splits,
        "remainder_envelope_id": split_plan.remainder_envelope_id,
        "remainder_amount_cents": split_plan.remainder_amount_cents,
        "transfer": None,
    })


def final_prediction_json_for_row(
    *,
    account_id: int,
    account: dict,
    row: ImportRowForm | None,
    form,
    action: str,
    get_transaction_func=None,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    if row is None or action == "skip":
        return {"outcome": "skipped", "action": "skip", "skip_reason": skip_reason}

    if action == "match":
        existing = get_transaction_func(row.match_tx_id) if get_transaction_func else None
        amount_cents = matched_transaction_amount_cents(row, existing) if existing else row.amount_cents
        transaction_type = existing.get("ttype") if existing else determine_import_transaction_type(row, account.get("account_type"))
        final = _standard_feedback_json(
            transaction_type or determine_import_transaction_type(row, account.get("account_type")),
            collect_import_row_split_plan(form, row.index, amount_cents),
        )
        final["action"] = "match"
        final["match_tx_id"] = row.match_tx_id
        return final

    transaction_type = determine_import_transaction_type(row, account.get("account_type"))
    if action == "transfer" or is_import_transfer_transaction_type(transaction_type):
        current_plan = collect_import_creation_split_plan(form, row.index, row.amount_cents)
        other_plan = collect_import_transfer_split_plan(form, row.index, row.amount_cents)
        return normalized_prediction_json({
            "prediction_type": "new_transaction",
            "transaction_type": transaction_type,
            "single_envelope_id": None,
            "splits": [],
            "remainder_envelope_id": None,
            "remainder_amount_cents": None,
            "transfer": {
                "other_account_id": row.transfer_account_id,
                "current_account_splits": current_plan.splits,
                "current_account_remainder_envelope_id": current_plan.remainder_envelope_id,
                "current_account_remainder_amount_cents": current_plan.remainder_amount_cents,
                "other_account_splits": other_plan.splits,
                "other_account_remainder_envelope_id": other_plan.remainder_envelope_id,
                "other_account_remainder_amount_cents": other_plan.remainder_amount_cents,
            },
        })

    return _standard_feedback_json(
        transaction_type,
        collect_import_creation_split_plan(form, row.index, row.amount_cents),
    )


def classify_prediction_feedback(
    *,
    predicted_json: dict[str, Any],
    final_json: dict[str, Any],
    posted_status: str | None = None,
    selected: bool = True,
) -> str:
    status = str(posted_status or "").strip().lower()
    if not selected or final_json.get("action") == "skip":
        return "skipped"
    if status in {"rejected", "skipped"}:
        return status
    if status == "cleared":
        return "cleared"
    predicted = normalized_prediction_json(predicted_json)
    final = normalized_prediction_json(final_json)
    has_final_assignment = bool(
        final.get("single_envelope_id")
        or final.get("splits")
        or final.get("remainder_envelope_id")
        or (final.get("transfer") or {}).get("other_account_id")
    )
    if not has_final_assignment:
        return "cleared"
    return "accepted" if predicted == final else "modified"


def prediction_feedback_row(
    *,
    account_id: int,
    row_index: int,
    item: dict[str, Any],
    final_json: dict[str, Any],
    selected: bool,
    transaction_id: int | None = None,
    import_session_row_id: int | None = None,
) -> dict[str, Any]:
    predicted = normalized_prediction_json(item.get("predicted_json") or item.get("predicted") or {})
    outcome = classify_prediction_feedback(
        predicted_json=predicted,
        final_json=final_json,
        posted_status=item.get("status"),
        selected=selected,
    )
    learning_example_id = _clean_int(item.get("learning_example_id"))
    prediction_type = str(item.get("prediction_type") or predicted.get("prediction_type") or "new_transaction")
    prediction_id = item.get("prediction_id")
    if not prediction_id:
        digest = hashlib.sha256(compact_json({
            "account_id": account_id,
            "row_index": row_index,
            "learning_example_id": learning_example_id,
            "predicted": predicted,
        }).encode("utf-8")).hexdigest()[:16]
        prediction_id = f"import-review:{account_id}:{row_index}:{digest}"
    return {
        "prediction_id": prediction_id,
        "learning_example_id": learning_example_id,
        "transaction_id": transaction_id,
        "import_session_row_id": import_session_row_id,
        "prediction_type": prediction_type,
        "accepted": 1 if outcome == "accepted" else 0,
        "modified": 1 if outcome == "modified" else 0,
        "rejected": 1 if outcome in {"rejected", "skipped", "cleared"} else 0,
        "predicted_json": compact_json(predicted),
        "final_json": compact_json({"outcome": outcome, **final_json}),
        "outcome": outcome,
    }


def _import_session_feedback_lookup(import_session_id: int | None) -> dict[int, dict[str, int | None]]:
    if not import_session_id:
        return {}
    db = get_db()
    rows = db.execute(
        """
        SELECT
            r.row_index,
            r.id AS import_session_row_id,
            m.transaction_id,
            le.id AS learning_example_id
        FROM import_session_rows r
        LEFT JOIN import_row_matches m ON m.row_id = r.id
        LEFT JOIN transaction_learning_examples le
          ON le.import_session_row_id = r.id
         AND (le.transaction_id = m.transaction_id OR m.transaction_id IS NULL)
        WHERE r.session_id=?
        ORDER BY r.id, m.id, le.id
        """,
        (int(import_session_id),),
    ).fetchall()
    lookup: dict[int, dict[str, int | None]] = {}
    for row in rows:
        idx = int(row["row_index"])
        lookup.setdefault(idx, {
            "import_session_row_id": row["import_session_row_id"],
            "transaction_id": row["transaction_id"],
            "learning_example_id": row["learning_example_id"],
        })
    return lookup


def build_prediction_feedback_rows_for_commit(
    *,
    account_id: int,
    account: dict,
    form,
    plan: ImportCommitPlan,
    import_session_id: int | None = None,
    get_transaction_func=None,
) -> list[dict[str, Any]]:
    feedback_items = import_prediction_feedback_items(form)
    if not feedback_items:
        return []

    plan_by_index = {
        int(item.row.index): item
        for item in plan.items
        if item.row is not None
    }
    session_lookup = _import_session_feedback_lookup(import_session_id)
    rows: list[dict[str, Any]] = []
    for item in feedback_items:
        row_index = _clean_index(item.get("row_index"))
        if row_index is None:
            continue
        selected = bool(form.get(f"row_{row_index}"))
        plan_item = plan_by_index.get(row_index)
        final_json = final_prediction_json_for_row(
            account_id=account_id,
            account=account,
            row=plan_item.row if plan_item else None,
            form=form,
            action=plan_item.action if plan_item and selected else "skip",
            get_transaction_func=get_transaction_func,
            skip_reason=plan_item.skip_reason if plan_item else None,
        )
        session_row = session_lookup.get(row_index, {})
        if not item.get("learning_example_id") and session_row.get("learning_example_id"):
            item = {**item, "learning_example_id": session_row.get("learning_example_id")}
        rows.append(prediction_feedback_row(
            account_id=account_id,
            row_index=row_index,
            item=item,
            final_json=final_json,
            selected=selected,
            transaction_id=session_row.get("transaction_id"),
            import_session_row_id=session_row.get("import_session_row_id"),
        ))
    return rows


def record_import_prediction_feedback_for_commit(
    *,
    account_id: int,
    account: dict,
    form,
    plan: ImportCommitPlan,
    import_session_id: int | None = None,
    get_transaction_func=None,
) -> int:
    rows = build_prediction_feedback_rows_for_commit(
        account_id=account_id,
        account=account,
        form=form,
        plan=plan,
        import_session_id=import_session_id,
        get_transaction_func=get_transaction_func,
    )
    if not rows:
        return 0
    db = get_db()
    recorded = record_prediction_feedback_rows(db, rows)
    db.commit()
    return recorded


def provenance_record_for_row(
    *,
    account_id: int,
    row: ImportRowForm,
    transaction_ids: list[int],
    match_type: str,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
    file_hash: str | None = None,
) -> dict:
    evidence = _row_evidence(row, file_hash=file_hash)
    row_dict = {
        "posted_at": row.posted_at,
        "amount_cents": row.amount_cents,
        "payee": row.orig_payee or row.payee,
        "memo": row.orig_memo or row.memo,
    }
    return {
        "row_index": row.index,
        "posted_at": row.posted_at,
        "amount_cents": row.amount_cents,
        "payee": row.orig_payee or row.payee,
        "memo": row.memo,
        "orig_memo": row.orig_memo,
        "fitid": row.fitid,
        "row_fingerprint": import_row_fingerprint(
            row_dict,
            account_id=account_id,
            source_bankid=source_bankid,
            source_acctid=source_acctid,
        ),
        "transaction_id": transaction_ids[0] if transaction_ids else None,
        "transaction_ids": transaction_ids,
        "match_type": match_type,
        "evidence": evidence,
    }



def commit_import_row(
    *,
    account_id: int,
    account: dict,
    row: ImportRowForm,
    form,
    existing_fitids: set[str],
    matched_ids: set[int],
    get_transaction_func,
    edit_transaction_func,
    get_account_func,
    create_transfer_func,
    create_expense_func,
    create_income_func,
    flash_func,
    provenance_rows: list[dict] | None = None,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
    file_hash: str | None = None,
) -> ImportRowCommitResult:
    if import_row_is_duplicate(row, existing_fitids):
        return ImportRowCommitResult.skipped_row()

    if row.match_tx_id:
        matched_ids.add(int(row.match_tx_id))
        skip_reason = update_import_matched_transaction(
            account_id=account_id,
            row=row,
            form=form,
            existing_fitids=existing_fitids,
            get_transaction_func=get_transaction_func,
            edit_transaction_func=edit_transaction_func,
            flash_func=flash_func,
        )
        if skip_reason:
            return ImportRowCommitResult.skipped_row(skip_reason)
        if provenance_rows is not None:
            provenance_rows.append(provenance_record_for_row(
                account_id=account_id,
                row=row,
                transaction_ids=[int(row.match_tx_id)],
                match_type="manual_match",
                source_bankid=source_bankid,
                source_acctid=source_acctid,
                file_hash=file_hash,
            ))
        return ImportRowCommitResult.imported_row()

    created_result = create_import_transaction_from_row(
        account_id=account_id,
        account=account,
        row=row,
        form=form,
        get_account_func=get_account_func,
        create_transfer_func=create_transfer_func,
        create_expense_func=create_expense_func,
        create_income_func=create_income_func,
        flash_func=flash_func,
    )
    if isinstance(created_result, str):
        return ImportRowCommitResult.skipped_row(created_result)

    remember_imported_fitid(row, existing_fitids)
    if provenance_rows is not None:
        provenance_rows.append(provenance_record_for_row(
            account_id=account_id,
            row=row,
            transaction_ids=_transaction_ids_from_commit_result(created_result),
            match_type="created",
            source_bankid=source_bankid,
            source_acctid=source_acctid,
            file_hash=file_hash,
        ))
    return ImportRowCommitResult.imported_row()


def _has_form_key_prefix(form, prefix: str) -> bool:
    return any(str(key).startswith(prefix) for key in _form_keys(form))


def import_row_has_standard_split_config(form, row: ImportRowForm) -> bool:
    if row.amount_cents == 0:
        return False
    prefix = "exp" if row.amount_cents < 0 else "inc"
    return (
        _has_form_key_prefix(form, f"{prefix}_amount_{row.index}_")
        or bool(_get_int(form, f"{prefix}_remainder_{row.index}"))
    )


def import_row_has_transfer_split_config(form, row: ImportRowForm) -> bool:
    return (
        _has_form_key_prefix(form, f"trf_from_amt_{row.index}_")
        or _has_form_key_prefix(form, f"trf_amt_{row.index}_")
        or bool(_get_int(form, f"trf_from_remainder_{row.index}"))
        or bool(_get_int(form, f"trf_remainder_{row.index}"))
    )


def import_row_has_conflicting_split_and_transfer_config(form, row: ImportRowForm) -> bool:
    return import_row_has_standard_split_config(form, row) and import_row_has_transfer_split_config(form, row)


def _split_plan_invalid(form, row_index: int, amount_cents: int, *, prefix: str, remainder_key: str, target_cents: int) -> bool:
    has_inputs = _has_form_key_prefix(form, f"{prefix}_{row_index}_") or bool(_get_int(form, remainder_key))
    if not has_inputs:
        return False
    try:
        parts = _signed_split_parts(
            form,
            base=f"{prefix}_{row_index}_",
            row_number=row_index + 1,
            field_label="split amount",
            target_cents=target_cents,
        )
        allocated, _remainder_id, _remainder_amount = _apply_signed_remainder(
            parts,
            form,
            remainder_key=remainder_key,
            target_cents=target_cents,
        )
        return allocated != _split_validation_target(target_cents, parts)
    except ValueError:
        raise


def import_row_split_plan_invalid(form, row: ImportRowForm) -> bool:
    if row.amount_cents == 0:
        return False
    prefix = "exp" if row.amount_cents < 0 else "inc"
    if _split_plan_invalid(
        form,
        row.index,
        row.amount_cents,
        prefix=f"{prefix}_amount",
        remainder_key=f"{prefix}_remainder_{row.index}",
        target_cents=row.amount_cents,
    ):
        return True
    if _split_plan_invalid(
        form,
        row.index,
        row.amount_cents,
        prefix="trf_from_amt",
        remainder_key=f"trf_from_remainder_{row.index}",
        target_cents=row.amount_cents,
    ):
        return True
    if _split_plan_invalid(
        form,
        row.index,
        row.amount_cents,
        prefix="trf_amt",
        remainder_key=f"trf_remainder_{row.index}",
        target_cents=-row.amount_cents,
    ):
        return True
    return False


def import_commit_action_for_row(row: ImportRowForm, account: dict) -> str:
    if row.match_tx_id:
        return "match"
    ttype = determine_import_transaction_type(row, account.get("account_type"))
    if is_import_transfer_transaction_type(ttype):
        return "transfer"
    return "create"


def validate_import_commit_plan_item(
    *,
    account_id: int,
    account: dict,
    row: ImportRowForm,
    form,
    existing_fitids: set[str],
    get_transaction_func,
    get_account_func,
) -> ImportCommitPlanItem:
    if import_row_is_duplicate(row, existing_fitids):
        return ImportCommitPlanItem(row, "skip", None)

    action = import_commit_action_for_row(row, account)
    if action == "match":
        existing = get_transaction_func(row.match_tx_id)
        if not match_target_belongs_to_account(existing, account_id):
            return ImportCommitPlanItem(row, "skip", invalid_match_skip_reason(row.index))
    elif action == "transfer":
        if not row.transfer_account_id:
            return ImportCommitPlanItem(row, "skip", missing_transfer_account_skip_reason(row.index))
        other = get_account_func(row.transfer_account_id)
        if not other or int(other.get("id", row.transfer_account_id)) == int(account_id):
            return ImportCommitPlanItem(row, "skip", missing_transfer_account_skip_reason(row.index))

    if import_row_has_conflicting_split_and_transfer_config(form, row):
        return ImportCommitPlanItem(row, "skip", conflicting_split_transfer_skip_reason(row.index))

    if import_row_split_plan_invalid(form, row):
        return ImportCommitPlanItem(row, "skip", f"Row {row.index + 1}: split amounts do not balance")

    return ImportCommitPlanItem(row, action)


def build_import_commit_plan(
    *,
    account_id: int,
    account: dict,
    form,
    count: int,
    existing_fitids: set[str],
    get_transaction_func,
    get_account_func,
) -> ImportCommitPlan:
    items: list[ImportCommitPlanItem] = []
    planned_fitids = set(existing_fitids)
    for row_index in selected_import_row_indices(form, count):
        row = parse_import_row_form(form, row_index)
        item = validate_import_commit_plan_item(
            account_id=account_id,
            account=account,
            row=row,
            form=form,
            existing_fitids=planned_fitids,
            get_transaction_func=get_transaction_func,
            get_account_func=get_account_func,
        )
        items.append(item)
        if item.action != "skip" and row.fitid:
            planned_fitids.add(row.fitid)
    return ImportCommitPlan(items)


def commit_import_rows(
    *,
    account_id: int,
    account: dict,
    form,
    count: int,
    existing_fitids: set[str],
    get_transaction_func,
    edit_transaction_func,
    get_account_func,
    create_transfer_func,
    create_expense_func,
    create_income_func,
    logger,
    flash_func,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
    file_hash: str | None = None,
    record_import_provenance_func=None,
    record_payee_normalization_func=record_payee_normalization_from_import_row,
    record_prediction_feedback_func=record_import_prediction_feedback_for_commit,
) -> tuple[ImportCommitTally, set[int]]:
    tally = ImportCommitTally()
    matched_ids: set[int] = set()
    provenance_rows: list[dict] = []

    try:
        plan = build_import_commit_plan(
            account_id=account_id,
            account=account,
            form=form,
            count=count,
            existing_fitids=existing_fitids,
            get_transaction_func=get_transaction_func,
            get_account_func=get_account_func,
        )
    except ValueError as ex:
        logger.info("IMPORT COMMIT: invalid commit plan: %s", ex)
        tally.record_skipped(str(ex))
        return tally, matched_ids

    for item in plan.items:
        row = item.row
        if item.action == "skip":
            tally.record_skipped(item.skip_reason)
            continue
        try:
            result = commit_import_row(
                account_id=account_id,
                account=account,
                row=row,
                form=form,
                existing_fitids=existing_fitids,
                matched_ids=matched_ids,
                get_transaction_func=get_transaction_func,
                edit_transaction_func=edit_transaction_func,
                get_account_func=get_account_func,
                create_transfer_func=create_transfer_func,
                create_expense_func=create_expense_func,
                create_income_func=create_income_func,
                flash_func=flash_func,
                provenance_rows=provenance_rows,
                source_bankid=source_bankid,
                source_acctid=source_acctid,
                file_hash=file_hash,
            )
            if result.imported:
                if record_payee_normalization_func:
                    record_payee_normalization_func(row, account_id=account_id)
                tally.record_imported()
            elif result.skipped:
                tally.record_skipped(result.skip_reason)
        except Exception as ex:
            logger.exception("IMPORT COMMIT: failed row %s: %s", row.index if row else "?", ex)
            tally.record_skipped(unexpected_import_error_skip_reason(row.index if row else -1, ex))

    if provenance_rows and record_import_provenance_func:
        tally.import_session_id = record_import_provenance_func(
            account_id=account_id,
            source_bankid=source_bankid,
            source_acctid=source_acctid,
            file_hash=file_hash,
            rows=provenance_rows,
        )

    if record_prediction_feedback_func:
        try:
            record_prediction_feedback_func(
                account_id=account_id,
                account=account,
                form=form,
                plan=plan,
                import_session_id=tally.import_session_id,
                get_transaction_func=get_transaction_func,
            )
        except Exception as ex:
            if logger:
                logger.exception("IMPORT COMMIT: failed to record prediction feedback: %s", ex)

    return tally, matched_ids


def standard_transaction_payload(account_id: int, row: ImportRowForm) -> dict:
    return {
        "account_id": account_id,
        "posted_at": row.posted_at,
        "payee": row.payee,
        "memo": row.memo,
        "fitid": row.fitid,
        "amount_cents": row.amount_cents,
    }


def create_import_standard_transaction_from_row(
    *,
    account_id: int,
    row: ImportRowForm,
    amount_cents: int,
    splits: list[dict],
    form,
    create_expense_func,
    create_income_func,
    remainder_envelope_id: int | None = None,
    remainder_amount_cents: int | None = None,
) -> None:
    if amount_cents < 0:
        selected_remainder_id = remainder_envelope_id or _get_int(form, f"exp_remainder_{row.index}")
        return create_expense_func(
            payload=standard_transaction_payload(account_id, row),
            splits=splits,
            remainder_envelope_id=selected_remainder_id,
            remainder_amount_cents=remainder_amount_cents,
        )
    else:
        selected_remainder_id = remainder_envelope_id or _get_int(form, f"inc_remainder_{row.index}")
        return create_income_func(
            payload=standard_transaction_payload(account_id, row),
            splits=splits,
            remainder_envelope_id=selected_remainder_id,
            remainder_amount_cents=remainder_amount_cents,
        )


def create_import_transaction_from_row(
    *,
    account_id: int,
    account: dict,
    row: ImportRowForm,
    form,
    get_account_func,
    create_transfer_func,
    create_expense_func,
    create_income_func,
    flash_func,
) -> str | None:
    amount_cents = row.amount_cents
    ttype = determine_import_transaction_type(row, account.get("account_type"))
    split_plan = collect_import_creation_split_plan(form, row.index, amount_cents)

    if is_import_transfer_transaction_type(ttype):
        return create_import_transfer_from_row(
            account_id=account_id,
            row=row,
            amount_cents=amount_cents,
            is_out=import_transfer_is_out(ttype),
            from_leg_split_plan=split_plan,
            form=form,
            get_account_func=get_account_func,
            create_transfer_func=create_transfer_func,
            flash_func=flash_func,
        )

    return create_import_standard_transaction_from_row(
        account_id=account_id,
        row=row,
        amount_cents=amount_cents,
        splits=split_plan.splits,
        form=form,
        create_expense_func=create_expense_func,
        create_income_func=create_income_func,
        remainder_envelope_id=split_plan.remainder_envelope_id,
        remainder_amount_cents=split_plan.remainder_amount_cents,
    )
    return None


def import_row_is_duplicate(row: ImportRowForm, existing_fitids: set[str]) -> bool:
    return bool(row.fitid and row.fitid in existing_fitids)


def remember_imported_fitid(row: ImportRowForm, existing_fitids: set[str]) -> None:
    if row.fitid:
        existing_fitids.add(row.fitid)


def transfer_transaction_payload(account_id: int, other_account_id: int, row: ImportRowForm, *, is_out: bool) -> dict:
    return {
        "amount_cents": abs(row.amount_cents),
        "date": row.posted_at,
        "memo": row.memo,
        "from_account_id": account_id if is_out else other_account_id,
        "to_account_id": other_account_id if is_out else account_id,
        "out_fitid": row.fitid if is_out else None,
        "in_fitid": row.fitid if not is_out else None,
        "payee": row.payee,
    }


def is_import_transfer_transaction_type(transaction_type: str) -> bool:
    return transaction_type in ("transfer_in", "transfer_out")


def import_transfer_is_out(transaction_type: str) -> bool:
    return transaction_type == "transfer_out"


def import_transfer_splits(
    *,
    is_out: bool,
    from_leg_splits: list[dict],
    other_leg_splits: list[dict],
    other_account: dict | None,
    from_leg_remainder_envelope_id: int | None = None,
    other_leg_remainder_envelope_id: int | None = None,
    from_leg_remainder_amount_cents: int | None = None,
    other_leg_remainder_amount_cents: int | None = None,
) -> ImportTransferSplits:
    return ImportTransferSplits(
        out_splits=from_leg_splits if is_out else other_leg_splits,
        in_splits=other_leg_splits if is_out else from_leg_splits,
        allow_unallocated_in=bool(other_account and other_account.get("account_type") == "loan"),
        out_remainder_envelope_id=from_leg_remainder_envelope_id if is_out else other_leg_remainder_envelope_id,
        in_remainder_envelope_id=other_leg_remainder_envelope_id if is_out else from_leg_remainder_envelope_id,
        out_remainder_amount_cents=from_leg_remainder_amount_cents if is_out else other_leg_remainder_amount_cents,
        in_remainder_amount_cents=other_leg_remainder_amount_cents if is_out else from_leg_remainder_amount_cents,
    )


def create_import_transfer_from_row(
    *,
    account_id: int,
    row: ImportRowForm,
    amount_cents: int,
    is_out: bool,
    from_leg_split_plan: ImportSplitPlan | None = None,
    from_leg_splits: list[dict] | None = None,
    form=None,
    get_account_func,
    create_transfer_func,
    flash_func,
) -> str | None:
    other_account_id = row.transfer_account_id
    if not other_account_id:
        return flash_missing_transfer_account_skip(row.index, flash_func)

    if from_leg_split_plan is None:
        from_leg_split_plan = ImportSplitPlan(from_leg_splits or [])
    other_leg_split_plan = collect_import_transfer_split_plan(form, row.index, amount_cents)

    split_plan = import_transfer_splits(
        is_out=is_out,
        from_leg_splits=from_leg_split_plan.splits,
        other_leg_splits=other_leg_split_plan.splits,
        other_account=get_account_func(other_account_id),
        from_leg_remainder_envelope_id=from_leg_split_plan.remainder_envelope_id,
        other_leg_remainder_envelope_id=other_leg_split_plan.remainder_envelope_id,
        from_leg_remainder_amount_cents=from_leg_split_plan.remainder_amount_cents,
        other_leg_remainder_amount_cents=other_leg_split_plan.remainder_amount_cents,
    )

    return create_transfer_func(
        payload=transfer_transaction_payload(account_id, other_account_id, row, is_out=is_out),
        out_splits=split_plan.out_splits,
        in_splits=split_plan.in_splits,
        allow_unallocated_in=split_plan.allow_unallocated_in,
        out_remainder_envelope_id=split_plan.out_remainder_envelope_id,
        in_remainder_envelope_id=split_plan.in_remainder_envelope_id,
        out_remainder_amount_cents=split_plan.out_remainder_amount_cents,
        in_remainder_amount_cents=split_plan.in_remainder_amount_cents,
    )
    return None


def ignored_transaction_ids(form, matched_ids: set[int]) -> list[int]:
    try:
        raw_ids = form.getlist("ignore_tx[]") or form.getlist("ignore_tx")
    except AttributeError:
        raw = form.get("ignore_tx[]") or form.get("ignore_tx") or []
        raw_ids = raw if isinstance(raw, list) else [raw]

    ids: list[int] = []
    for raw_id in raw_ids:
        try:
            tx_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if tx_id in matched_ids:
            continue
        ids.append(tx_id)
    return ids


def ignored_transaction_payload(tx_id: int) -> dict:
    return {
        "fitid": f"Ignore-{int(tx_id)}",
        "ignore_match": 1,
    }


def mark_ignored_transactions(form, matched_ids: set[int], edit_transaction_func, logger=None) -> tuple[int, int]:
    marked = 0
    failed = 0
    for tx_id in ignored_transaction_ids(form, matched_ids):
        try:
            edit_transaction_func(
                tx_id=tx_id,
                payload=ignored_transaction_payload(tx_id),
                splits=None,
            )
            marked += 1
        except Exception as ex:
            failed += 1
            if logger:
                logger.exception("Failed to mark tx %s ignored: %s", tx_id, ex)
    return marked, failed


def finalize_import_commit(
    *,
    tally: ImportCommitTally,
    form,
    matched_ids: set[int],
    edit_transaction_func,
    logger,
    flash_func,
) -> tuple[str, str]:
    mark_ignored_transactions(form, matched_ids, edit_transaction_func, logger)
    message, category = tally.flash()
    if category == "success" and tally.import_session_id:
        flash_func(
            {
                "text": message,
                "import_undo_session_id": tally.import_session_id,
            },
            category,
        )
    else:
        flash_func(message, category)
    return message, category


def invalid_match_flash_message(row_index: int, match_tx_id: int) -> str:
    return f"Match target {match_tx_id} not found in this account; skipped row {row_index + 1}."


def invalid_match_skip_reason(row_index: int) -> str:
    return f"Row {row_index + 1}: match target not found"


def flash_invalid_match_skip(row_index: int, match_tx_id: int, flash_func) -> str:
    flash_func(invalid_match_flash_message(row_index, match_tx_id), "warning")
    return invalid_match_skip_reason(row_index)


def missing_transfer_account_flash_message(row_index: int) -> str:
    return f"Transfer row {row_index + 1} needs a destination/source account."


def missing_transfer_account_skip_reason(row_index: int) -> str:
    return f"Row {row_index + 1}: transfer needs another account"


def conflicting_split_transfer_skip_reason(row_index: int) -> str:
    return f"Row {row_index + 1}: choose split or transfer, not both"


def flash_missing_transfer_account_skip(row_index: int, flash_func) -> str:
    flash_func(missing_transfer_account_flash_message(row_index), "warning")
    return missing_transfer_account_skip_reason(row_index)


def unexpected_import_error_skip_reason(row_index: int, error: Exception) -> str:
    error_type = type(error).__name__
    return f"Row {row_index + 1}: unexpected error ({error_type})"


def import_result_flash(imported: int, skipped: int, skipped_reasons: list[str]) -> tuple[str, str]:
    if skipped:
        detail = ""
        if skipped_reasons:
            shown = "; ".join(skipped_reasons[:3])
            more = f"; plus {len(skipped_reasons) - 3} more" if len(skipped_reasons) > 3 else ""
            detail = f" ({shown}{more})"
        return f"Imported {imported} transaction(s). Skipped {skipped}.{detail}", "warning"
    return f"Imported {imported} transaction(s). Skipped {skipped}.", "success"


def _form_keys(form) -> Iterable[str]:
    return form.keys()


def _get_int(form, key: str) -> int | None:
    try:
        getter = getattr(form, "get")
        value = getter(key, type=int)
    except TypeError:
        raw = form.get(key)
        if raw in (None, ""):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
    return value


def _signed_split_parts(
    form,
    *,
    base: str,
    row_number: int,
    field_label: str,
    target_cents: int,
) -> dict[int, int]:
    parsed: dict[int, int] = {}
    for key in _form_keys(form):
        if not key.startswith(base):
            continue
        try:
            envelope_id = int(key[len(base):])
        except ValueError:
            continue
        raw = (form.get(key) or "").strip()
        if raw in ("", "0", "0.00", "-0", "-0.00"):
            continue
        cents = parse_money_to_cents_strict(raw, field_name=f"Row {row_number} {field_label}")
        if cents == 0:
            continue
        parsed[envelope_id] = parsed.get(envelope_id, 0) + int(cents)

    return parsed


def _has_explicit_negative(parts: dict[int, int]) -> bool:
    return any(cents < 0 for cents in parts.values())


def _split_validation_target(target_cents: int, parts: dict[int, int]) -> int:
    if int(target_cents) < 0 and not _has_explicit_negative(parts):
        return abs(int(target_cents))
    return int(target_cents)


def _apply_signed_remainder(
    parts: dict[int, int],
    form,
    *,
    remainder_key: str,
    target_cents: int,
) -> tuple[int, int | None, int | None]:
    target = _split_validation_target(target_cents, parts)
    allocated = sum(parts.values())
    remainder_envelope_id = _get_int(form, remainder_key)
    remainder_amount: int | None = None
    if remainder_envelope_id:
        remainder_amount = int(target - allocated)
        if allocated != target:
            parts[remainder_envelope_id] = parts.get(remainder_envelope_id, 0) + remainder_amount
            allocated = target
    return allocated, remainder_envelope_id, remainder_amount


def _parts_to_split_list(parts: dict[int, int]) -> list[dict]:
    return [
        {"envelope_id": envelope_id, "amount_cents": cents}
        for envelope_id, cents in parts.items()
        if cents != 0
    ]


def collect_import_row_split_plan(form, row_index: int, amount_cents: int) -> ImportSplitPlan:
    target_cents = int(amount_cents)
    if target_cents == 0:
        return ImportSplitPlan([])

    row_number = row_index + 1
    is_expense = target_cents < 0
    prefix = "exp" if is_expense else "inc"
    parts = _signed_split_parts(
        form,
        base=f"{prefix}_amount_{row_index}_",
        row_number=row_number,
        field_label="split amount",
        target_cents=target_cents,
    )

    if is_expense and not parts:
        quick_envelope_id = _get_int(form, f"exp_single_{row_index}")
        if quick_envelope_id:
            parts[quick_envelope_id] = abs(target_cents)

    allocated, remainder_id, remainder_amount = _apply_signed_remainder(
        parts,
        form,
        remainder_key=f"{prefix}_remainder_{row_index}",
        target_cents=target_cents,
    )
    if allocated != _split_validation_target(target_cents, parts):
        return ImportSplitPlan([])
    return ImportSplitPlan(
        _parts_to_split_list(parts),
        remainder_envelope_id=remainder_id,
        remainder_amount_cents=remainder_amount,
    )


def collect_import_row_splits(form, row_index: int, amount_cents: int) -> list[dict]:
    return collect_import_row_split_plan(form, row_index, amount_cents).splits


def collect_import_transfer_from_split_plan(form, row_index: int, amount_cents: int) -> ImportSplitPlan:
    return _collect_import_transfer_split_plan(
        form,
        row_index,
        target_cents=int(amount_cents),
        base_prefix="trf_from_amt",
        remainder_prefix="trf_from_remainder",
    )


def collect_import_transfer_from_splits(form, row_index: int, amount_cents: int) -> list[dict]:
    return collect_import_transfer_from_split_plan(form, row_index, amount_cents).splits


def collect_import_transfer_split_plan(form, row_index: int, amount_cents: int) -> ImportSplitPlan:
    return _collect_import_transfer_split_plan(
        form,
        row_index,
        target_cents=-int(amount_cents),
        base_prefix="trf_amt",
        remainder_prefix="trf_remainder",
    )


def collect_import_transfer_splits(form, row_index: int, amount_cents: int) -> list[dict]:
    return collect_import_transfer_split_plan(form, row_index, amount_cents).splits


def collect_import_creation_split_plan(form, row_index: int, amount_cents: int) -> ImportSplitPlan:
    """Return the split plan used when creating a transaction from an import row.

    Transfer rows may provide dedicated current/imported-account split fields;
    those take precedence. Non-transfer and rows without current-account fields
    use the normal income/expense split fields.
    """
    transfer_from_plan = collect_import_transfer_from_split_plan(form, row_index, amount_cents)
    if transfer_from_plan.splits or transfer_from_plan.remainder_envelope_id:
        return transfer_from_plan
    return collect_import_row_split_plan(form, row_index, amount_cents)


def collect_import_creation_splits(form, row_index: int, amount_cents: int) -> list[dict]:
    return collect_import_creation_split_plan(form, row_index, amount_cents).splits


def _collect_import_transfer_split_plan(
    form,
    row_index: int,
    *,
    target_cents: int,
    base_prefix: str,
    remainder_prefix: str,
) -> ImportSplitPlan:
    if int(target_cents) == 0:
        return ImportSplitPlan([])

    parts = _signed_split_parts(
        form,
        base=f"{base_prefix}_{row_index}_",
        row_number=row_index + 1,
        field_label="transfer split amount",
        target_cents=int(target_cents),
    )
    allocated, remainder_id, remainder_amount = _apply_signed_remainder(
        parts,
        form,
        remainder_key=f"{remainder_prefix}_{row_index}",
        target_cents=int(target_cents),
    )
    if allocated != _split_validation_target(int(target_cents), parts):
        return ImportSplitPlan([])
    return ImportSplitPlan(
        _parts_to_split_list(parts),
        remainder_envelope_id=remainder_id,
        remainder_amount_cents=remainder_amount,
    )
