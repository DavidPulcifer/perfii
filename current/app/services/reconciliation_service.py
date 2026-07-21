from __future__ import annotations

from datetime import datetime
from typing import Iterable

from app.db import unit_of_work
from app.repositories import reconciliation_repo

OPEN_STATUSES = {"open", "reopened"}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class ReconciliationService:
    """Business rules for statement reconciliation.

    Reconciliation records are intentionally separate from transactions. Closing a
    session updates only reconciliation_items/session metadata; transaction
    amounts, dates, splits, transfer links, FITIDs, and balances are never mutated.
    """

    @staticmethod
    def create_session(
        *,
        account_id: int,
        statement_date: str,
        statement_balance_cents: int,
        label: str | None = None,
        note: str | None = None,
    ) -> int:
        now = _now()
        with unit_of_work() as db:
            previous = reconciliation_repo.latest_closed_session_for_account(
                int(account_id),
                before_statement_date=statement_date,
                db=db,
            )
            if previous:
                starting_balance_cents = int(previous["statement_balance_cents"] or 0)
            else:
                starting_balance_cents = reconciliation_repo.account_opening_balance(int(account_id), db=db)
            return reconciliation_repo.create_session(
                db=db,
                account_id=int(account_id),
                statement_date=statement_date,
                statement_balance_cents=int(statement_balance_cents),
                starting_balance_cents=starting_balance_cents,
                label=label,
                note=note,
                now=now,
            )

    @staticmethod
    def update_session_metadata(
        session_id: int,
        *,
        statement_date: str | None = None,
        statement_balance_cents: int | None = None,
        label: str | None = None,
        note: str | None = None,
    ) -> None:
        with unit_of_work() as db:
            session = ReconciliationService._require_session(session_id, db=db)
            ReconciliationService._require_editable(session)
            fields = {"updated_at": _now()}
            if statement_date is not None:
                fields["statement_date"] = statement_date
            if statement_balance_cents is not None:
                fields["statement_balance_cents"] = int(statement_balance_cents)
            if label is not None:
                fields["label"] = label
            if note is not None:
                fields["note"] = note
            reconciliation_repo.update_session_fields(session_id, fields, db=db)

    @staticmethod
    def set_cleared_transactions(session_id: int, transaction_ids: Iterable[int]) -> None:
        ids = list(dict.fromkeys(int(tx_id) for tx_id in transaction_ids))
        now = _now()
        with unit_of_work() as db:
            session = ReconciliationService._require_session(session_id, db=db)
            ReconciliationService._require_editable(session)
            ReconciliationService._validate_transactions_for_session(session, ids, db=db)
            reconciliation_repo.replace_items(session_id, ids, state="cleared", now=now, db=db)
            reconciliation_repo.update_session_fields(session_id, {"updated_at": now}, db=db)

    @staticmethod
    def compute(session_id: int) -> dict:
        with unit_of_work() as db:
            session = ReconciliationService._require_session(session_id, db=db)
            selected_total = reconciliation_repo.sum_items(session_id, db=db)
            calculated_balance = int(session["starting_balance_cents"] or 0) + selected_total
            statement_balance = int(session["statement_balance_cents"] or 0)
            return {
                "session_id": int(session_id),
                "account_id": int(session["account_id"]),
                "status": session["status"],
                "starting_balance_cents": int(session["starting_balance_cents"] or 0),
                "statement_balance_cents": statement_balance,
                "selected_total_cents": selected_total,
                "calculated_balance_cents": calculated_balance,
                "difference_cents": statement_balance - calculated_balance,
                "item_count": len(reconciliation_repo.list_items(session_id, db=db)),
            }

    @staticmethod
    def close_session(session_id: int) -> None:
        now = _now()
        with unit_of_work() as db:
            session = ReconciliationService._require_session(session_id, db=db)
            ReconciliationService._require_editable(session)
            summary = ReconciliationService._compute_with_db(session_id, session, db=db)
            if int(summary["difference_cents"]) != 0:
                raise ValueError("Cannot close reconciliation until the difference is zero.")
            items = reconciliation_repo.list_items(session_id, db=db)
            ReconciliationService._validate_transactions_for_session(
                session,
                [int(item["transaction_id"]) for item in items],
                db=db,
            )
            reconciliation_repo.set_item_state_for_session(session_id, "reconciled", now=now, db=db)
            reconciliation_repo.update_session_fields(
                session_id,
                {"status": "closed", "closed_at": now, "updated_at": now},
                db=db,
            )

    @staticmethod
    def reopen_session(session_id: int) -> None:
        now = _now()
        with unit_of_work() as db:
            session = ReconciliationService._require_session(session_id, db=db)
            if session["status"] != "closed":
                raise ValueError("Only closed reconciliations can be reopened.")
            reconciliation_repo.set_item_state_for_session(session_id, "cleared", now=now, db=db)
            reconciliation_repo.update_session_fields(
                session_id,
                {"status": "reopened", "reopened_at": now, "updated_at": now},
                db=db,
            )

    @staticmethod
    def void_session(session_id: int) -> None:
        now = _now()
        with unit_of_work() as db:
            session = ReconciliationService._require_session(session_id, db=db)
            if session["status"] == "void":
                return
            reconciliation_repo.set_item_state_for_session(session_id, "cleared", now=now, db=db)
            reconciliation_repo.update_session_fields(
                session_id,
                {"status": "void", "updated_at": now},
                db=db,
            )

    @staticmethod
    def _require_session(session_id: int, *, db) -> dict:
        session = reconciliation_repo.get_session(int(session_id), db=db)
        if not session:
            raise ValueError("Reconciliation session not found.")
        return session

    @staticmethod
    def _require_editable(session: dict) -> None:
        if session["status"] not in OPEN_STATUSES:
            raise ValueError("Only open or reopened reconciliation sessions can be edited.")

    @staticmethod
    def _validate_transactions_for_session(session: dict, transaction_ids: list[int], *, db) -> None:
        if not transaction_ids:
            return
        rows = reconciliation_repo.list_transactions_by_ids(transaction_ids, db=db)
        found_ids = {int(row["id"]) for row in rows}
        missing = sorted(set(transaction_ids) - found_ids)
        if missing:
            raise ValueError(f"Transactions not found: {missing}")
        wrong_account = [int(row["id"]) for row in rows if int(row["account_id"]) != int(session["account_id"])]
        if wrong_account:
            raise ValueError("Cannot reconcile transactions from another account.")
        conflicts = reconciliation_repo.reconciled_transaction_conflicts(
            transaction_ids,
            excluding_session_id=int(session["id"]),
            db=db,
        )
        if conflicts:
            conflict_ids = sorted({int(row["transaction_id"]) for row in conflicts})
            raise ValueError(f"Transactions already reconciled: {conflict_ids}")

    @staticmethod
    def _compute_with_db(session_id: int, session: dict, *, db) -> dict:
        selected_total = reconciliation_repo.sum_items(session_id, db=db)
        calculated_balance = int(session["starting_balance_cents"] or 0) + selected_total
        statement_balance = int(session["statement_balance_cents"] or 0)
        return {
            "selected_total_cents": selected_total,
            "calculated_balance_cents": calculated_balance,
            "difference_cents": statement_balance - calculated_balance,
        }
