from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ImportUndoResult:
    undone: int = 0
    message: str = ""
    category: str = "info"


def undo_import_session(
    *,
    session_id: int | None,
    get_import_session_undo_candidate_func: Callable[[int], dict | None],
    latest_import_session_id_for_account_func: Callable[[int], int | None],
    delete_transaction_func: Callable[[int], None],
    delete_import_session_provenance_func: Callable[[int], int],
) -> ImportUndoResult:
    if not session_id:
        return ImportUndoResult(message="No recent import was available to undo.", category="warning")

    candidate = get_import_session_undo_candidate_func(int(session_id))
    if not candidate:
        return ImportUndoResult(message="That import can no longer be undone.", category="warning")

    account_id = int(candidate.get("account_id") or 0)
    latest_session_id = latest_import_session_id_for_account_func(account_id)
    if latest_session_id != int(session_id):
        return ImportUndoResult(
            message="Only the most recent import can be undone.",
            category="warning",
        )

    rows = list(candidate.get("rows") or [])
    match_types = {
        (row.get("match_type") or row.get("row_match_type") or "").strip()
        for row in rows
    }
    if not rows:
        return ImportUndoResult(message="That import did not have any rows to undo.", category="warning")
    if match_types != {"created"}:
        return ImportUndoResult(
            message="That import cannot be undone because it included manual matches.",
            category="warning",
        )

    transaction_ids = sorted({
        int(row["transaction_id"])
        for row in rows
        if row.get("transaction_id")
    })
    if not transaction_ids:
        return ImportUndoResult(
            message="That import no longer has transactions to undo.",
            category="warning",
        )

    for tx_id in transaction_ids:
        delete_transaction_func(tx_id)
    delete_import_session_provenance_func(int(session_id))

    return ImportUndoResult(
        undone=len(transaction_ids),
        message=f"Undid import: removed {len(transaction_ids)} transaction(s).",
        category="success",
    )
