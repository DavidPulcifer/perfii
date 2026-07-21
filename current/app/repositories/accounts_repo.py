import re
from ..db import get_db

def list_accounts():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM accounts ORDER BY LOWER(name), id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int):
    db = get_db()
    row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None

def get_account_balance(account_id: int):
    """
    Returns the account balance in cents.

    - For non-loan accounts: sum of all transaction amounts (unchanged).
    - For loan accounts: sum of all transaction amounts, then subtract any
      non-principal loan payment parts (interest/fees/other) so that only
      principal reduces the loan balance.
    """
    db = get_db()
    acct = db.execute(
        "SELECT account_type FROM accounts WHERE id=?",
        (account_id,)
    ).fetchone()
    if acct and acct["account_type"] == "loan":
        # A) naive total of all transactions on the loan account
        row_tx = db.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total_tx FROM transactions WHERE account_id=?",
            (account_id,)
        ).fetchone()
        total_tx = row_tx["total_tx"] or 0

        # B) sum of non-principal parts on any transaction in this loan account
        row_non_pr = db.execute(
            """
            SELECT COALESCE(SUM(lpp.amount_cents),0) AS non_pr
            FROM loan_payment_parts lpp
            WHERE lpp.part_type IN ('interest','fees','other')
            AND lpp.payment_tx_id IN (SELECT id FROM transactions WHERE account_id=?)
            """,
            (account_id,)
        ).fetchone()
        non_pr = row_non_pr["non_pr"] or 0

        # Loan debt is represented as a negative balance. A payment transaction
        # increases the account by the full payment amount, but interest/fees do
        # not reduce principal. Subtract non-principal parts so only principal
        # changes the outstanding loan balance.
        return total_tx - non_pr
    else:
        row = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS bal FROM transactions WHERE account_id=?",
            (account_id,)
        ).fetchone()
        return row["bal"]

def _make_unique_acct_key(db, base_name: str) -> str:
    """
    Builds a unique, URL-safe acct_key like 'acct:checking' or 'acct:test-card-a-2'.
    """
    slug = re.sub(r'[^a-z0-9]+', '-', (base_name or '').strip().lower()).strip('-') or 'account'
    base = f'acct:{slug}'
    key = base
    i = 2
    while db.execute("SELECT 1 FROM accounts WHERE acct_key=?", (key,)).fetchone():
        key = f"{base}-{i}"
        i += 1
    return key

def insert_account(data: dict, *, db=None) -> int:
    should_commit = db is None
    db = db or get_db()
    name = (data.get('name') or '').strip() or 'Account'
    account_type = data.get('account_type', 'bank')
    acct_key = data.get('acct_key') or _make_unique_acct_key(db, name)

    # Next display order (safe even if NULLs exist)
    row = db.execute("SELECT COALESCE(MAX(display_order), 0) + 1 AS next FROM accounts").fetchone()
    display_order = row["next"] if row else 1

    opening_balance_cents = int(data.get('opening_balance_cents') or 0)

    opening_date = data.get('opening_date')
    bankid = data.get('bankid')
    acctid = data.get('acctid')

    db.execute(
        "INSERT INTO accounts "
        "(name, account_type, acct_key, opening_balance_cents, opening_date, bankid, acctid, display_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, account_type, acct_key, opening_balance_cents, opening_date, bankid, acctid, display_order),
    )
    if should_commit:
        db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def update_account(account_id: int, data: dict) -> None:
    db = get_db()
    fields, values = [], []
    if 'name' in data:
        fields.append("name=?")
        values.append(data['name'])
    if 'account_type' in data:
        fields.append("account_type=?")
        values.append(data['account_type'])
    if 'opening_balance_cents' in data:
        fields.append("opening_balance_cents=?")
        values.append(int(data['opening_balance_cents'] or 0))
    if 'opening_date' in data:
        fields.append("opening_date=?")
        values.append(data['opening_date'])
    if 'bankid' in data:
        fields.append("bankid=?")
        values.append(data['bankid'])
    if 'acctid' in data:
        fields.append("acctid=?")
        values.append(data['acctid'])
    if not fields:
        return
    values.append(account_id)
    db.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id=?", values)
    db.commit()

def delete_account(account_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    db.commit()
