# app/services/transactions_service.py
from __future__ import annotations
from typing import Dict, List, Optional
from datetime import datetime
from app.db import get_db, unit_of_work

from app.repositories import transactions_repo, splits_repo, accounts_repo, reconciliation_repo, remainder_intents_repo
from app.services import transaction_learning_service
from app.utils import parse_money_to_cents, parse_money_to_cents_strict

class TransactionsService:    
    @staticmethod
    def _optional_fitid(value) -> Optional[str]:
        fitid = str(value or "").strip()
        return fitid or None

    @staticmethod
    def _transfer_leg_fitids(payload: Dict) -> tuple[Optional[str], Optional[str]]:
        fallback_fitid = TransactionsService._optional_fitid(payload.get('fitid'))
        out_fitid = TransactionsService._optional_fitid(payload.get('out_fitid')) if 'out_fitid' in payload else fallback_fitid
        in_fitid = TransactionsService._optional_fitid(payload.get('in_fitid')) if 'in_fitid' in payload else fallback_fitid
        return out_fitid, in_fitid

    @staticmethod
    def create_expense(
        payload: Dict,
        splits: Optional[List[Dict]] = None,
        remainder_envelope_id: Optional[int] = None,
        remainder_amount_cents: Optional[int] = None,
    ) -> int:
        amt = TransactionsService._to_cents_strict(payload.get('amount_cents', payload.get('amount')), field_name="Expense amount")
        if amt == 0:
            raise ValueError("Expense amount must be greater than 0.")
        if amt > 0:
            amt = -amt  # expenses are negative
        posted_at = TransactionsService._coerce_date_str(payload.get('posted_at') or payload.get('date'))
        account_id = int(payload['account_id'])

        # Normalize splits and compute remainder if needed
        norm_splits, remainder_intent = TransactionsService._normalize_splits_with_remainder(
            parent_amount_cents=amt,
            raw_splits=splits,
            sign=-1,
            remainder_envelope_id=remainder_envelope_id,
            remainder_amount_cents=remainder_amount_cents,
        )
        requested_envelope_ids = TransactionsService._requested_envelope_ids(
            splits,
            remainder_envelope_id,
        )

        with unit_of_work() as db:
            TransactionsService._validate_split_envelopes(
                db,
                account_id=account_id,
                envelope_ids=requested_envelope_ids,
            )
            tx_id = transactions_repo.insert_transaction(
                db=db,
                account_id=account_id,
                ttype='expense',
                amount_cents=amt,
                posted_at=posted_at,
                payee=payload.get('payee', ''),
                memo=payload.get('memo', ''),
                fitid=payload.get('fitid'),
                external_counterparty=payload.get('external_counterparty'),
                ignore_match=int(payload.get('ignore_match', 0)),
            )
            for s in norm_splits:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=tx_id,
                    envelope_id=int(s['envelope_id']),
                    amount_cents=int(s['amount_cents']),
                )
            TransactionsService._replace_remainder_intent(db, tx_id, remainder_intent)
        return tx_id

    @staticmethod
    def create_income(
        payload: Dict,
        splits: Optional[List[Dict]] = None,
        remainder_envelope_id: Optional[int] = None,
        allow_unallocated: bool = False,
        remainder_amount_cents: Optional[int] = None,
    ) -> int:
        amt = abs(TransactionsService._to_cents_strict(payload.get('amount_cents', payload.get('amount')), field_name="Income amount"))
        if amt == 0:
            raise ValueError("Income amount must be greater than 0.")
        posted_at = TransactionsService._coerce_date_str(payload.get('posted_at') or payload.get('date'))
        account_id = int(payload['account_id'])

        if allow_unallocated and not splits and not remainder_envelope_id:
            norm_splits = []
            remainder_intent = None
        else:
            norm_splits, remainder_intent = TransactionsService._normalize_splits_with_remainder(
                parent_amount_cents=amt,
                raw_splits=splits,
                sign=+1,
                remainder_envelope_id=remainder_envelope_id,
                remainder_amount_cents=remainder_amount_cents,
            )
        requested_envelope_ids = TransactionsService._requested_envelope_ids(
            splits,
            remainder_envelope_id,
        )

        with unit_of_work() as db:
            TransactionsService._validate_split_envelopes(
                db,
                account_id=account_id,
                envelope_ids=requested_envelope_ids,
            )
            tx_id = transactions_repo.insert_transaction(
                db=db,
                account_id=account_id,
                ttype='income',
                amount_cents=amt,
                posted_at=posted_at,
                payee=payload.get('payee', ''),
                memo=payload.get('memo', ''),
                fitid=payload.get('fitid'),
                external_counterparty=payload.get('external_counterparty'),
                ignore_match=int(payload.get('ignore_match', 0)),
            )
            for s in norm_splits:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=tx_id,
                    envelope_id=int(s['envelope_id']),
                    amount_cents=int(s['amount_cents']),
                )
            TransactionsService._replace_remainder_intent(db, tx_id, remainder_intent)
        return tx_id

    @staticmethod
    def create_transfer(
        payload: Dict,
        out_splits: List[Dict],
        in_splits: List[Dict],
        allow_unallocated_in: bool = False,
        out_remainder_envelope_id: Optional[int] = None,
        in_remainder_envelope_id: Optional[int] = None,
        out_remainder_amount_cents: Optional[int] = None,
        in_remainder_amount_cents: Optional[int] = None,
        db=None,
    ) -> tuple[int, int]:
        amount = abs(TransactionsService._to_cents_strict(payload.get('amount_cents', payload.get('amount')), field_name="Transfer amount"))
        if amount <= 0:
            raise ValueError("Transfer amount must be > 0.")

        date_str = TransactionsService._coerce_date_str(payload.get('date') or payload.get('posted_at'))
        memo  = payload.get('memo', '')
        from_id = int(payload['from_account_id'])
        to_id   = int(payload['to_account_id'])
        if from_id == to_id:
            raise ValueError("Choose different source and destination accounts for the transfer.")
        out_fitid, in_fitid = TransactionsService._transfer_leg_fitids(payload)

        # Normalize splits
        norm_out, out_remainder_intent = TransactionsService._normalize_splits_with_remainder(
            parent_amount_cents=-amount,
            raw_splits=out_splits,
            sign=-1,
            remainder_envelope_id=out_remainder_envelope_id,
            remainder_amount_cents=out_remainder_amount_cents,
        )

        # For in-leg: allow empty splits if explicitly permitted (e.g., loans)
        if allow_unallocated_in and (not in_splits) and not in_remainder_envelope_id:
            norm_in = []
            in_remainder_intent = None
        else:
            norm_in, in_remainder_intent = TransactionsService._normalize_splits_with_remainder(
                parent_amount_cents=+amount,
                raw_splits=in_splits,
                sign=+1,
                remainder_envelope_id=in_remainder_envelope_id,
                remainder_amount_cents=in_remainder_amount_cents,
            )
        out_envelope_ids = TransactionsService._requested_envelope_ids(
            out_splits,
            out_remainder_envelope_id,
        )
        in_envelope_ids = TransactionsService._requested_envelope_ids(
            in_splits,
            in_remainder_envelope_id,
        )

        from_acct = accounts_repo.get_account(from_id) or {}
        to_acct   = accounts_repo.get_account(to_id) or {}
        from_name = (from_acct.get('name') or str(from_id))
        to_name   = (to_acct.get('name') or str(to_id))

        def write_transfer(active_db) -> tuple[int, int]:
            TransactionsService._validate_split_envelopes(
                active_db,
                account_id=from_id,
                envelope_ids=out_envelope_ids,
            )
            TransactionsService._validate_split_envelopes(
                active_db,
                account_id=to_id,
                envelope_ids=in_envelope_ids,
            )
            tx_out_id = transactions_repo.insert_transaction(
                db=active_db,
                account_id=from_id,
                ttype='transfer_out',
                amount_cents=-amount,
                posted_at=date_str,
                payee=to_name,
                memo=memo,
                fitid=out_fitid,
            )
            tx_in_id = transactions_repo.insert_transaction(
                db=active_db,
                account_id=to_id,
                ttype='transfer_in',
                amount_cents=amount,
                posted_at=date_str,
                payee=from_name,
                memo=memo,
                fitid=in_fitid,
            )
            transactions_repo.link_transfer_pair(db=active_db, tx_out_id=tx_out_id, tx_in_id=tx_in_id)

            for s in norm_out:
                splits_repo.insert_split(db=active_db, transaction_id=tx_out_id,
                                        envelope_id=int(s['envelope_id']), amount_cents=int(s['amount_cents']))
            for s in norm_in:
                splits_repo.insert_split(db=active_db, transaction_id=tx_in_id,
                                        envelope_id=int(s['envelope_id']), amount_cents=int(s['amount_cents']))
            TransactionsService._replace_remainder_intent(active_db, tx_out_id, out_remainder_intent)
            TransactionsService._replace_remainder_intent(active_db, tx_in_id, in_remainder_intent)
            return tx_out_id, tx_in_id

        if db is not None:
            return write_transfer(db)
        with unit_of_work() as active_db:
            return write_transfer(active_db)

    @staticmethod
    def edit_transfer(
        tx_id: int,
        payload: Dict,
        out_splits: List[Dict],
        in_splits: List[Dict],
        allow_unallocated_in: bool = False,
        out_remainder_envelope_id: Optional[int] = None,
        in_remainder_envelope_id: Optional[int] = None,
        out_remainder_amount_cents: Optional[int] = None,
        in_remainder_amount_cents: Optional[int] = None,
    ) -> tuple[int, int]:
        """
        Atomically update an existing linked transfer pair in place.

        Keeps the existing transaction IDs and xfer_pair_id links. If any part
        of the update fails, the original pair and splits remain unchanged.
        """
        tx = transactions_repo.get_transaction(tx_id)
        if not tx or not tx.get('xfer_pair_id'):
            raise ValueError("Transfer not found")

        other = transactions_repo.get_transaction(int(tx['xfer_pair_id']))
        if not other or int(other.get('xfer_pair_id') or 0) != int(tx['id']):
            raise ValueError("Transfer pair is incomplete")

        tx_out = tx if int(tx['amount_cents']) < 0 else other
        tx_in = other if int(tx['amount_cents']) < 0 else tx

        amount = abs(TransactionsService._to_cents_strict(payload.get('amount_cents', payload.get('amount')), field_name="Transfer amount"))
        if amount <= 0:
            raise ValueError("Transfer amount must be > 0.")

        date_str = TransactionsService._coerce_date_str(payload.get('date') or payload.get('posted_at'))
        memo = payload.get('memo', '')
        from_id = int(payload['from_account_id'])
        to_id = int(payload['to_account_id'])
        if from_id == to_id:
            raise ValueError("Choose different source and destination accounts for the transfer.")
        out_fitid, in_fitid = TransactionsService._transfer_leg_fitids(payload)

        if TransactionsService._is_reconciled_transfer_mutating_edit(
            tx_out,
            tx_in,
            from_account_id=from_id,
            to_account_id=to_id,
            amount_cents=amount,
            posted_at=date_str,
            out_fitid=out_fitid,
            in_fitid=in_fitid,
        ):
            TransactionsService._raise_if_reconciled(
                [int(tx_out["id"]), int(tx_in["id"])],
                "Reopen the reconciliation before editing this transfer.",
            )

        norm_out, out_remainder_intent = TransactionsService._normalize_splits_with_remainder(
            parent_amount_cents=-amount,
            raw_splits=out_splits,
            sign=-1,
            remainder_envelope_id=out_remainder_envelope_id,
            remainder_amount_cents=out_remainder_amount_cents,
        )
        if allow_unallocated_in and not in_splits and not in_remainder_envelope_id:
            norm_in = []
            in_remainder_intent = None
        else:
            norm_in, in_remainder_intent = TransactionsService._normalize_splits_with_remainder(
                parent_amount_cents=amount,
                raw_splits=in_splits,
                sign=+1,
                remainder_envelope_id=in_remainder_envelope_id,
                remainder_amount_cents=in_remainder_amount_cents,
            )
        out_envelope_ids = TransactionsService._requested_envelope_ids(
            out_splits,
            out_remainder_envelope_id,
        )
        in_envelope_ids = TransactionsService._requested_envelope_ids(
            in_splits,
            in_remainder_envelope_id,
        )

        from_acct = accounts_repo.get_account(from_id) or {}
        to_acct = accounts_repo.get_account(to_id) or {}
        from_name = from_acct.get('name') or str(from_id)
        to_name = to_acct.get('name') or str(to_id)
        snapshot_db = get_db()
        before_out = transaction_learning_service.snapshot_transaction(snapshot_db, int(tx_out['id']))
        before_in = transaction_learning_service.snapshot_transaction(snapshot_db, int(tx_in['id']))

        with unit_of_work() as db:
            TransactionsService._validate_split_envelopes(
                db,
                account_id=from_id,
                envelope_ids=out_envelope_ids,
            )
            TransactionsService._validate_split_envelopes(
                db,
                account_id=to_id,
                envelope_ids=in_envelope_ids,
            )
            transactions_repo.update_transaction(
                db=db,
                tx_id=int(tx_out['id']),
                data={
                    'account_id': from_id,
                    'ttype': 'transfer_out',
                    'amount_cents': -amount,
                    'posted_at': date_str,
                    'payee': to_name,
                    'memo': memo,
                    'fitid': out_fitid,
                },
            )
            transactions_repo.update_transaction(
                db=db,
                tx_id=int(tx_in['id']),
                data={
                    'account_id': to_id,
                    'ttype': 'transfer_in',
                    'amount_cents': amount,
                    'posted_at': date_str,
                    'payee': from_name,
                    'memo': memo,
                    'fitid': in_fitid,
                },
            )

            splits_repo.delete_splits_for_transaction(db=db, tx_id=int(tx_out['id']))
            for s in norm_out:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=int(tx_out['id']),
                    envelope_id=int(s['envelope_id']),
                    amount_cents=int(s['amount_cents']),
                )

            splits_repo.delete_splits_for_transaction(db=db, tx_id=int(tx_in['id']))
            for s in norm_in:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=int(tx_in['id']),
                    envelope_id=int(s['envelope_id']),
                    amount_cents=int(s['amount_cents']),
                )
            TransactionsService._replace_remainder_intent(db, int(tx_out['id']), out_remainder_intent)
            TransactionsService._replace_remainder_intent(db, int(tx_in['id']), in_remainder_intent)
            transaction_learning_service.record_transaction_write_event(
                db,
                transaction_id=int(tx_out['id']),
                source="transfer_edit",
                event_type="transfer_edit",
                before=before_out,
            )
            transaction_learning_service.record_transaction_write_event(
                db,
                transaction_id=int(tx_in['id']),
                source="transfer_edit",
                event_type="transfer_edit",
                before=before_in,
            )

        return int(tx_out['id']), int(tx_in['id'])

    @staticmethod
    def convert_standard_transaction_to_transfer(
        tx_id: int,
        *,
        other_account_id: int,
        current_splits: List[Dict],
        other_splits: List[Dict],
        current_remainder_envelope_id: Optional[int] = None,
        other_remainder_envelope_id: Optional[int] = None,
        current_remainder_amount_cents: Optional[int] = None,
        other_remainder_amount_cents: Optional[int] = None,
    ) -> tuple[int, int]:
        """Atomically convert one expense/income row into a linked transfer pair.

        The original transaction ID remains the current-account leg so import
        validation/FITID evidence attached to that row stays traceable. A new
        paired leg is inserted for the other account. Existing standard splits
        are replaced by transfer splits inside the same unit of work.
        """
        old = transactions_repo.get_transaction(tx_id)
        if not old:
            raise ValueError("Transaction not found")
        if old.get('xfer_pair_id'):
            raise ValueError("Transaction is already a transfer")

        ttype = (old.get('ttype') or '').lower()
        if ttype not in {'expense', 'income'}:
            raise ValueError("Only expense and income transactions can be converted to transfers.")

        current_account_id = int(old['account_id'])
        other_account_id = int(other_account_id or 0)
        if not other_account_id or other_account_id == current_account_id:
            raise ValueError("Choose a different account for the transfer.")
        other_account = accounts_repo.get_account(other_account_id)
        if not other_account:
            raise ValueError("Choose a valid account for the transfer.")

        TransactionsService._raise_if_reconciled(
            [int(tx_id)],
            "Reopen the reconciliation before converting this transaction to a transfer.",
        )

        amount = abs(int(old.get('amount_cents') or 0))
        if amount <= 0:
            raise ValueError("Transfer amount must be > 0.")

        current_is_out = ttype == 'expense'
        current_sign = -1 if current_is_out else 1
        other_sign = 1 if current_is_out else -1

        norm_current, current_remainder_intent = TransactionsService._normalize_splits_with_remainder(
            parent_amount_cents=amount * current_sign,
            raw_splits=current_splits,
            sign=current_sign,
            remainder_envelope_id=current_remainder_envelope_id,
            remainder_amount_cents=current_remainder_amount_cents,
        )
        norm_other, other_remainder_intent = TransactionsService._normalize_splits_with_remainder(
            parent_amount_cents=amount * other_sign,
            raw_splits=other_splits,
            sign=other_sign,
            remainder_envelope_id=other_remainder_envelope_id,
            remainder_amount_cents=other_remainder_amount_cents,
        )
        current_envelope_ids = TransactionsService._requested_envelope_ids(
            current_splits,
            current_remainder_envelope_id,
        )
        other_envelope_ids = TransactionsService._requested_envelope_ids(
            other_splits,
            other_remainder_envelope_id,
        )

        current_account = accounts_repo.get_account(current_account_id) or {}
        current_name = current_account.get('name') or str(current_account_id)
        other_name = other_account.get('name') or str(other_account_id)

        current_transfer_type = 'transfer_out' if current_is_out else 'transfer_in'
        other_transfer_type = 'transfer_in' if current_is_out else 'transfer_out'
        current_payee = other_name
        other_payee = current_name
        before_current = transaction_learning_service.snapshot_transaction(get_db(), int(tx_id))

        with unit_of_work() as db:
            TransactionsService._validate_split_envelopes(
                db,
                account_id=current_account_id,
                envelope_ids=current_envelope_ids,
            )
            TransactionsService._validate_split_envelopes(
                db,
                account_id=other_account_id,
                envelope_ids=other_envelope_ids,
            )
            other_tx_id = transactions_repo.insert_transaction(
                db=db,
                account_id=other_account_id,
                ttype=other_transfer_type,
                amount_cents=amount * other_sign,
                posted_at=old.get('posted_at'),
                payee=other_payee,
                memo=old.get('memo', ''),
                fitid=None,
            )
            transactions_repo.update_transaction(
                db=db,
                tx_id=int(tx_id),
                data={
                    'account_id': current_account_id,
                    'ttype': current_transfer_type,
                    'amount_cents': amount * current_sign,
                    'posted_at': old.get('posted_at'),
                    'payee': current_payee,
                    'memo': old.get('memo', ''),
                    'fitid': old.get('fitid'),
                    'external_counterparty': old.get('external_counterparty'),
                    'ignore_match': old.get('ignore_match', 0),
                },
            )
            transactions_repo.link_transfer_pair(
                db=db,
                tx_out_id=int(tx_id) if current_is_out else int(other_tx_id),
                tx_in_id=int(other_tx_id) if current_is_out else int(tx_id),
            )

            splits_repo.delete_splits_for_transaction(db=db, tx_id=int(tx_id))
            for split in norm_current:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=int(tx_id),
                    envelope_id=int(split['envelope_id']),
                    amount_cents=int(split['amount_cents']),
                )
            for split in norm_other:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=int(other_tx_id),
                    envelope_id=int(split['envelope_id']),
                    amount_cents=int(split['amount_cents']),
                )
            TransactionsService._replace_remainder_intent(db, int(tx_id), current_remainder_intent)
            TransactionsService._replace_remainder_intent(db, int(other_tx_id), other_remainder_intent)
            transaction_learning_service.record_transaction_write_event(
                db,
                transaction_id=int(tx_id),
                source="transfer_conversion",
                event_type="transfer_conversion",
                before=before_current,
            )
            transaction_learning_service.record_transaction_write_event(
                db,
                transaction_id=int(other_tx_id),
                source="transfer_conversion",
                event_type="transfer_conversion",
                before={},
            )

        tx_out_id = int(tx_id) if current_is_out else int(other_tx_id)
        tx_in_id = int(other_tx_id) if current_is_out else int(tx_id)
        return tx_out_id, tx_in_id

    
    # -------------------------------
    # Edit / Delete
    # -------------------------------
    @staticmethod
    def edit_transaction(
        tx_id: int,
        payload: Dict,
        splits: Optional[List[Dict]] = None,
        remainder_envelope_id: Optional[int] = None,
        remainder_amount_cents: Optional[int] = None,
    ) -> None:
        old = transactions_repo.get_transaction(tx_id)
        if not old:
            raise ValueError("Transaction not found")
        before_snapshot = transaction_learning_service.snapshot_transaction(get_db(), int(tx_id))
        split_update_requested = (
            splits is not None
            or remainder_envelope_id is not None
            or remainder_amount_cents is not None
        )
        split_mutation_marker = splits if splits is not None else ([] if split_update_requested else None)
        if TransactionsService._is_reconciled_mutating_edit(old, payload, split_mutation_marker):
            TransactionsService._raise_if_reconciled(
                [int(tx_id)],
                "Reopen the reconciliation before editing this transaction.",
            )

        ttype = (old.get('ttype') or '').lower()
        sign = -1 if ttype in ('expense', 'transfer_out') else +1

        # Parent amount
        new_amt = payload.get('amount_cents', payload.get('amount'))
        if new_amt is None or str(new_amt).strip() == "":
            amount_cents = int(old['amount_cents'])
        else:
            amount_cents = abs(TransactionsService._to_cents_strict(new_amt, field_name="Transaction amount")) * sign

        updated = {
            'posted_at': payload.get('posted_at') or payload.get('date') or old.get('posted_at'),
            'payee': payload.get('payee', old.get('payee', '')),
            'memo': payload.get('memo', old.get('memo', '')),
            'amount_cents': amount_cents,
            'fitid': payload.get('fitid', old.get('fitid')),
        }
        if 'ignore_match' in payload:
            updated['ignore_match'] = int(payload.get('ignore_match') or 0)

        # Normalize splits (remainder optional). A supplied remainder with no
        # fixed split rows means "use the remainder envelope for the whole
        # amount"; callers that only edit metadata omit both splits and
        # remainder fields.
        norm_splits: Optional[List[Dict]] = None
        remainder_intent = None
        if split_update_requested:
            norm_splits, remainder_intent = TransactionsService._normalize_splits_with_remainder(
                parent_amount_cents=amount_cents,
                raw_splits=splits or [],
                sign=sign,
                remainder_envelope_id=remainder_envelope_id,
                remainder_amount_cents=remainder_amount_cents,
            )
            requested_envelope_ids = TransactionsService._requested_envelope_ids(
                splits,
                remainder_envelope_id,
            )
        else:
            requested_envelope_ids = []

        with unit_of_work() as db:
            TransactionsService._validate_split_envelopes(
                db,
                account_id=int(old['account_id']),
                envelope_ids=requested_envelope_ids,
            )
            transactions_repo.update_transaction(db=db, tx_id=tx_id, data=updated)
            if norm_splits is not None:
                splits_repo.delete_splits_for_transaction(db=db, tx_id=tx_id)
                for s in norm_splits:
                    splits_repo.insert_split(
                        db=db,
                        transaction_id=tx_id,
                        envelope_id=int(s['envelope_id']),
                        amount_cents=int(s['amount_cents']),
                    )
                TransactionsService._replace_remainder_intent(db, tx_id, remainder_intent)
            after_snapshot = transaction_learning_service.snapshot_transaction(db, int(tx_id))
            source, event_type = transaction_learning_service.classify_standard_edit(before_snapshot, after_snapshot)
            transaction_learning_service.record_transaction_write_event(
                db,
                transaction_id=int(tx_id),
                source=source,
                event_type=event_type,
                before=before_snapshot,
                after=after_snapshot,
            )

    @staticmethod
    def delete_transaction(tx_id: int) -> None:
        tx = transactions_repo.get_transaction(tx_id)
        if not tx:
            return
        pair_id = tx.get('xfer_pair_id')
        ids = [int(tx_id)]
        if pair_id:
            ids.append(int(pair_id))
        TransactionsService._raise_if_reconciled(
            ids,
            "Reopen the reconciliation before deleting this transaction.",
        )

        with unit_of_work() as db:
            splits_repo.delete_splits_for_transaction(db=db, tx_id=tx_id)
            transactions_repo.delete_transaction(db=db, tx_id=tx_id)
            if pair_id:
                splits_repo.delete_splits_for_transaction(db=db, tx_id=pair_id)
                transactions_repo.delete_transaction(db=db, tx_id=pair_id)

    @staticmethod
    def create_allocation(
        payload: dict,
        splits: list[dict],
        total_cents: int,
        remainder_envelope_id: int | None = None,
        remainder_amount_cents: int | None = None,
    ) -> int:
        """
        Create a $0 allocation transaction on a single account and attach envelope splits.
        If splits don't sum to total_cents and a remainder_envelope_id is provided,
        add the remainder there. No equality enforcement against the $0 parent.
        """
        account_id = int(payload['account_id'])
        posted_at = TransactionsService._coerce_date_str(payload.get('posted_at') or payload.get('date'))
        memo = payload.get('memo') or None

        # Normalize signed allocation amounts. Positive entries add to an
        # envelope and negative entries subtract from it; the remainder selector
        # fills the signed delta to the requested total.
        norm = []
        allocated = 0
        for s in (splits or []):
            eid = int(s['envelope_id'])
            raw = s.get('amount_cents', s.get('amount', 0))
            cents = TransactionsService._to_cents_strict(raw, field_name="Allocation split amount")
            if cents == 0:
                continue
            norm.append({'envelope_id': eid, 'amount_cents': cents})
            allocated += cents

        remainder_intent = None
        if remainder_envelope_id:
            delta = int(total_cents) - allocated
            if delta != 0:
                norm.append({'envelope_id': int(remainder_envelope_id), 'amount_cents': delta})
                allocated += delta
            remainder_intent = {
                'envelope_id': int(remainder_envelope_id),
                'amount_cents': int(remainder_amount_cents if remainder_amount_cents is not None else delta),
            }
        requested_envelope_ids = TransactionsService._requested_envelope_ids(
            splits,
            remainder_envelope_id,
        )

        with unit_of_work() as db:
            TransactionsService._validate_split_envelopes(
                db,
                account_id=account_id,
                envelope_ids=requested_envelope_ids,
            )
            tx_id = transactions_repo.insert_transaction(
                db=db,
                account_id=account_id,
                ttype='allocation',
                amount_cents=0,
                posted_at=posted_at,
                payee=None,
                memo=memo,
            )
            for s in norm:
                splits_repo.insert_split(
                    db=db,
                    transaction_id=tx_id,
                    envelope_id=int(s['envelope_id']),
                    amount_cents=int(s['amount_cents']),
                )
            TransactionsService._replace_remainder_intent(db, tx_id, remainder_intent)
        return tx_id


    # -------------------------------
    # Helpers
    # -------------------------------
    @staticmethod
    def _requested_envelope_ids(
        raw_splits: Optional[List[Dict]],
        remainder_envelope_id: Optional[int],
    ) -> list[int]:
        """Return every explicitly requested envelope, including zero-value rows."""
        envelope_ids = {
            int(split['envelope_id'])
            for split in (raw_splits or [])
        }
        if remainder_envelope_id:
            envelope_ids.add(int(remainder_envelope_id))
        return sorted(envelope_ids)

    @staticmethod
    def _validate_split_envelopes(
        db,
        *,
        account_id: int,
        envelope_ids: list[int],
    ) -> None:
        """Reject missing, archived, or account-incompatible split envelopes."""
        if not envelope_ids:
            return

        placeholders = ",".join("?" for _ in envelope_ids)
        rows = db.execute(
            f"""
            SELECT id, locked_account_id, archived_at
            FROM envelopes
            WHERE id IN ({placeholders})
            """,
            tuple(envelope_ids),
        ).fetchall()
        envelopes_by_id = {int(row['id']): row for row in rows}

        for envelope_id in envelope_ids:
            envelope = envelopes_by_id.get(int(envelope_id))
            if envelope is None:
                raise ValueError(f"Envelope {envelope_id} does not exist.")
            if envelope['archived_at'] is not None:
                raise ValueError(f"Envelope {envelope_id} is archived. Choose an active envelope.")
            locked_account_id = envelope['locked_account_id']
            if locked_account_id is not None and int(locked_account_id) != int(account_id):
                raise ValueError(f"Envelope {envelope_id} is locked to a different account.")

    @staticmethod
    def _normalize_splits(
        *,
        parent_amount_cents: int,
        raw_splits: Optional[List[Dict]],
        sign: int,  # -1 for expense, +1 for income
        remainder_envelope_id: Optional[int],
    ) -> List[Dict]:
        splits, _intent = TransactionsService._normalize_splits_with_remainder(
            parent_amount_cents=parent_amount_cents,
            raw_splits=raw_splits,
            sign=sign,
            remainder_envelope_id=remainder_envelope_id,
        )
        return splits

    @staticmethod
    def _normalize_splits_with_remainder(
        *,
        parent_amount_cents: int,
        raw_splits: Optional[List[Dict]],
        sign: int,  # -1 for expense, +1 for income
        remainder_envelope_id: Optional[int],
        remainder_amount_cents: Optional[int] = None,
    ) -> tuple[List[Dict], dict | None]:
        """Normalize split rows and preserve optional remainder intent metadata."""
        parsed: list[tuple[int, int]] = []
        for s in (raw_splits or []):
            eid = int(s['envelope_id'])
            raw = s.get('amount_cents', s.get('amount', 0))
            cents = TransactionsService._to_cents_strict(raw, field_name="Split amount")
            if cents == 0:
                continue
            parsed.append((eid, cents))

        explicit_signed = any(cents < 0 for _, cents in parsed)
        compatibility_sign = -1 if int(parent_amount_cents) < 0 and not explicit_signed else 1

        splits: List[Dict] = [
            {'envelope_id': eid, 'amount_cents': compatibility_sign * abs(cents) if compatibility_sign < 0 else cents}
            for eid, cents in parsed
        ]
        allocated = sum(int(s['amount_cents']) for s in splits)

        selected_remainder_id: int | None = None
        if remainder_envelope_id:
            selected_remainder_id = int(remainder_envelope_id)

        computed_remainder = 0
        if selected_remainder_id and allocated != int(parent_amount_cents):
            computed_remainder = int(parent_amount_cents) - allocated
            if computed_remainder != 0:
                splits.append({'envelope_id': selected_remainder_id, 'amount_cents': computed_remainder})
                allocated += computed_remainder

        # Final guard: for non-zero parents, enforce signed equality.
        total = sum(int(s['amount_cents']) for s in splits)
        if int(parent_amount_cents) != 0 and total != int(parent_amount_cents):
            raise ValueError(
                f"Split amounts ({total}) must sum to the transaction amount ({int(parent_amount_cents)})."
            )

        remainder_intent = None
        if selected_remainder_id:
            if remainder_amount_cents is None:
                intent_amount = computed_remainder
            else:
                raw_intent_amount = int(remainder_amount_cents)
                intent_amount = (
                    compatibility_sign * abs(raw_intent_amount)
                    if compatibility_sign < 0
                    else raw_intent_amount
                )
            remainder_intent = {'envelope_id': selected_remainder_id, 'amount_cents': int(intent_amount)}
        return splits, remainder_intent

    @staticmethod
    def _replace_remainder_intent(db, tx_id: int, remainder_intent: dict | None) -> None:
        if remainder_intent:
            remainder_intents_repo.replace_remainder_intent(
                db=db,
                transaction_id=int(tx_id),
                envelope_id=int(remainder_intent['envelope_id']),
                amount_cents=int(remainder_intent.get('amount_cents') or 0),
            )
        else:
            remainder_intents_repo.replace_remainder_intent(db=db, transaction_id=int(tx_id))

    @staticmethod
    def _raise_if_reconciled(tx_ids: list[int], message: str) -> None:
        if reconciliation_repo.reconciled_transaction_ids(tx_ids):
            raise ValueError(message)

    @staticmethod
    def _is_reconciled_mutating_edit(old: dict, payload: Dict, splits: Optional[List[Dict]]) -> bool:
        editable_fields = {
            "amount": "amount_cents",
            "amount_cents": "amount_cents",
            "posted_at": "posted_at",
            "date": "posted_at",
            "fitid": "fitid",
            "account_id": "account_id",
            "ttype": "ttype",
        }
        for incoming, existing in editable_fields.items():
            if incoming not in payload:
                continue
            raw = payload.get(incoming)
            if raw is None or str(raw).strip() == "":
                continue
            if incoming in {"amount", "amount_cents"}:
                ttype = (old.get("ttype") or "").lower()
                sign = -1 if ttype in ("expense", "transfer_out") else 1
                try:
                    new_value = abs(TransactionsService._to_cents_strict(raw, field_name="Transaction amount")) * sign
                except ValueError:
                    return True
                if int(old.get(existing) or 0) != int(new_value):
                    return True
            elif existing == "account_id":
                if int(old.get(existing) or 0) != int(raw):
                    return True
            else:
                if str(old.get(existing) or "") != str(raw):
                    return True
        return False

    @staticmethod
    def _is_reconciled_transfer_mutating_edit(
        tx_out: dict,
        tx_in: dict,
        *,
        from_account_id: int,
        to_account_id: int,
        amount_cents: int,
        posted_at: str,
        out_fitid: Optional[str],
        in_fitid: Optional[str],
    ) -> bool:
        protected_values = (
            (
                tx_out,
                from_account_id,
                "transfer_out",
                -int(amount_cents),
                posted_at,
                out_fitid if out_fitid is not None else tx_out.get("fitid"),
            ),
            (
                tx_in,
                to_account_id,
                "transfer_in",
                int(amount_cents),
                posted_at,
                in_fitid if in_fitid is not None else tx_in.get("fitid"),
            ),
        )
        for tx, account_id, ttype, amount, date_str, fitid in protected_values:
            if int(tx.get("account_id") or 0) != int(account_id):
                return True
            if str(tx.get("ttype") or "") != ttype:
                return True
            if int(tx.get("amount_cents") or 0) != int(amount):
                return True
            if str(tx.get("posted_at") or "") != str(date_str):
                return True
            if str(tx.get("fitid") or "") != str(fitid or ""):
                return True
        return False

    @staticmethod
    def _to_cents(x) -> int:
        if x is None:
            return 0
        if isinstance(x, int):
            return x
        return parse_money_to_cents(str(x))

    @staticmethod
    def _to_cents_strict(x, *, field_name: str) -> int:
        if isinstance(x, int):
            return x
        return parse_money_to_cents_strict(str(x) if x is not None else None, field_name=field_name)

    @staticmethod
    def _coerce_date_str(x) -> str:
        """
        Accept a datetime, ISO/date string, or None; return a YYYY-MM-DD string.
        """
        if isinstance(x, datetime):
            return x.strftime('%Y-%m-%d')
        if not x:
            return datetime.utcnow().strftime('%Y-%m-%d')
        # assume already a date-like string acceptable to your schema
        return str(x)

    @staticmethod
    def _opt_int(x) -> Optional[int]:
        try:
            return int(x) if x not in (None, "", "null") else None
        except Exception:
            return None
