# app/repositories/aggregates_repo.py
from ..db import get_db

def get_account_totals() -> dict[int, int]:
    """
    Sum of transactions per account.
    - Non-loan accounts: SUM(transactions.amount_cents) (unchanged).
    - Loan accounts: SUM(transactions.amount_cents) + SUM(non-principal loan parts)
      so that only principal reduces the loan balance.
    Returns: { account_id: total_cents }
    """
    db = get_db()
    rows = db.execute(
        """
        WITH tx AS (
          SELECT account_id, COALESCE(SUM(amount_cents), 0) AS total_tx
          FROM transactions
          GROUP BY account_id
        ),
        npr AS (
          -- Non-principal (interest/fees/other) parts summed by the account of the payment transaction
          SELECT t.account_id AS account_id,
                 COALESCE(SUM(lpp.amount_cents), 0) AS non_pr
          FROM loan_payment_parts lpp
          JOIN transactions t
            ON t.id = lpp.payment_tx_id
          WHERE lpp.part_type IN ('interest','fees','other')
          GROUP BY t.account_id
        )
        SELECT
          a.id AS account_id,
          CASE
            WHEN a.account_type = 'loan'
              THEN COALESCE(tx.total_tx, 0) - COALESCE(npr.non_pr, 0)
            ELSE COALESCE(tx.total_tx, 0)
          END AS total
        FROM accounts a
        LEFT JOIN tx  ON tx.account_id  = a.id
        LEFT JOIN npr ON npr.account_id = a.id
        """
    ).fetchall()
    return {int(r["account_id"]): int(r["total"] or 0) for r in rows}

def get_envelope_totals() -> dict[int, int]:
    """
    Sum of transaction_splits per envelope.
    Returns: { envelope_id: total_cents }
    """
    db = get_db()
    rows = db.execute(
        "SELECT envelope_id, COALESCE(SUM(amount_cents),0) AS total "
        "FROM transaction_splits GROUP BY envelope_id"
    ).fetchall()
    return {int(r["envelope_id"]): int(r["total"] or 0) for r in rows}

def get_account_envelope_balances() -> dict[tuple[int,int], int]:
    """
    Sum of splits per (account_id, envelope_id).
    Returns: { (account_id, envelope_id): total_cents }
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT t.account_id AS account_id,
               s.envelope_id AS envelope_id,
               COALESCE(SUM(s.amount_cents),0) AS total
        FROM transaction_splits s
        JOIN transactions t ON t.id = s.transaction_id
        GROUP BY t.account_id, s.envelope_id
        """
    ).fetchall()
    # key as a 2-tuple so templates can do balances.get((a.id, e.id), 0)
    return {(int(r["account_id"]), int(r["envelope_id"])): int(r["total"] or 0) for r in rows}


def account_envelope_balances_json(balances: dict[tuple[int, int], int] | None = None) -> dict[str, dict[str, int]]:
    """
    Return account/envelope balances in the JSON shape used by envelope-selector JS.
    Shape: { account_id: { envelope_id: total_cents } }
    """
    balances = balances if balances is not None else get_account_envelope_balances()
    out: dict[str, dict[str, int]] = {}
    for (account_id, envelope_id), cents in balances.items():
        out.setdefault(str(account_id), {})[str(envelope_id)] = int(cents or 0)
    return out
