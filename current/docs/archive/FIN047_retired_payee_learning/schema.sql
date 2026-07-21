-- Retired legacy payee-learning predictor tables archived by FIN-047.
-- These tables must not be recreated by live application migrations.

CREATE TABLE payee_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_payee TEXT NOT NULL,
    normalized_payee TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used TEXT
);

CREATE UNIQUE INDEX idx_payee_aliases_unique
    ON payee_aliases(raw_payee, normalized_payee);

CREATE TABLE payee_envelope_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    normalized_payee TEXT NOT NULL,
    envelope_id INTEGER NOT NULL DEFAULT 0,
    tx_count INTEGER NOT NULL DEFAULT 0,
    total_amount_cents INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX idx_payee_env_unique
    ON payee_envelope_stats(account_id, normalized_payee, envelope_id);

CREATE INDEX idx_payee_env_lookup
    ON payee_envelope_stats(account_id, normalized_payee);
