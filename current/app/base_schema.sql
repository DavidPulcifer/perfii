-- Canonical base schema for a new ledger.
--
-- This file intentionally contains no user data. app.db.run_schema_migrations
-- applies the additive/current schema after these foundational tables exist.

PRAGMA foreign_keys = ON;

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'bank',
    acct_key TEXT NOT NULL UNIQUE,
    opening_balance_cents INTEGER NOT NULL DEFAULT 0,
    opening_date TEXT,
    note TEXT,
    display_order INTEGER NOT NULL DEFAULT 0,
    bankid TEXT,
    acctid TEXT,
    CHECK (account_type IN ('bank', 'credit_card', 'loan', 'investment'))
);

CREATE INDEX idx_accounts_bankid_acctid ON accounts(bankid, acctid);

CREATE TABLE envelopes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    locked_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    default_amount_cents INTEGER DEFAULT 0,
    archived_at TEXT
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ttype TEXT NOT NULL CHECK (
        ttype IN ('income', 'expense', 'transfer_in', 'transfer_out', 'allocation')
    ),
    amount_cents INTEGER NOT NULL,
    posted_at TEXT NOT NULL,
    payee TEXT,
    memo TEXT,
    fitid TEXT,
    ignore_match INTEGER NOT NULL DEFAULT 0,
    xfer_pair_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    external_counterparty TEXT
);

CREATE INDEX idx_tx_account ON transactions(account_id);
CREATE INDEX idx_tx_fitid ON transactions(account_id, fitid);
CREATE INDEX idx_tx_posted ON transactions(posted_at);

CREATE TABLE transaction_splits (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL
);

CREATE INDEX idx_splits_env ON transaction_splits(envelope_id);
CREATE INDEX idx_splits_tx ON transaction_splits(transaction_id);

CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    uploaded_at TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE TABLE credit_cards (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    credit_limit_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE cc_payment_allocations (
    id INTEGER PRIMARY KEY,
    payment_tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL
);

CREATE INDEX idx_ccpa_tx ON cc_payment_allocations(payment_tx_id);

CREATE TABLE cc_budget_adjustments (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
    posted_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    note TEXT
);

CREATE INDEX idx_ccba_acc_env ON cc_budget_adjustments(account_id, envelope_id);

CREATE TABLE loans (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    original_principal_cents INTEGER,
    note TEXT,
    normal_monthly_payment_cents INTEGER
);

CREATE TABLE loan_statements (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    asof_date TEXT NOT NULL,
    ending_principal_cents INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE loan_payment_parts (
    id INTEGER PRIMARY KEY,
    payment_tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    part_type TEXT NOT NULL CHECK (part_type IN ('principal', 'interest', 'fees', 'other')),
    amount_cents INTEGER NOT NULL,
    note TEXT
);

CREATE INDEX idx_loan_parts_paytx ON loan_payment_parts(payment_tx_id);

CREATE TABLE investment_accounts (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE investment_valuations (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    asof_date TEXT NOT NULL,
    value_cents INTEGER NOT NULL,
    source TEXT DEFAULT 'manual',
    note TEXT
);
