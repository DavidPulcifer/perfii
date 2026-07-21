"""Read-only consistency checks for one explicitly supplied ledger connection.

The auditor deliberately knows nothing about workspaces, users, or filesystem
paths.  Its caller owns the connection and decides which ledger is safe to
inspect.  Results contain only check names and violation counts; no account,
transaction, envelope, or monetary values are returned.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any


def _result(
    name: str,
    passed: bool,
    detail: str,
    *,
    violations: int | None,
) -> dict[str, Any]:
    return {
        "check": name,
        "passed": passed,
        "detail": detail,
        "violations": violations,
    }


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _count_check(
    conn: sqlite3.Connection,
    *,
    name: str,
    required_tables: Iterable[str],
    sql: str,
    tables: set[str],
) -> dict[str, Any]:
    if not set(required_tables).issubset(tables):
        return _result(
            name,
            False,
            "unavailable: required ledger table(s) missing",
            violations=None,
        )

    try:
        violations = int(conn.execute(sql).fetchone()[0])
    except sqlite3.Error:
        # Do not echo SQLite messages: malformed data can be embedded in them.
        return _result(name, False, "inspection failed", violations=None)

    return _result(
        name,
        violations == 0,
        "clean" if violations == 0 else f"{violations} violation(s)",
        violations=violations,
    )


def audit_ledger(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return invariant results for exactly ``conn`` without modifying it."""

    try:
        tables = _table_names(conn)
    except sqlite3.Error:
        return [
            _result(
                "invariant.schema_access",
                False,
                "inspection failed",
                violations=None,
            )
        ]

    checks = (
        {
            "name": "invariant.transaction_split_totals",
            "required_tables": ("transactions", "transaction_splits"),
            "sql": """
                SELECT COUNT(*)
                FROM (
                    SELECT t.id
                    FROM transactions AS t
                    JOIN transaction_splits AS s ON s.transaction_id = t.id
                    GROUP BY t.id, t.amount_cents
                    HAVING SUM(s.amount_cents) <> t.amount_cents
                ) AS mismatched_transactions
            """,
        },
        {
            "name": "invariant.transfer_pairs",
            "required_tables": ("transactions",),
            "sql": """
                SELECT COUNT(*)
                FROM transactions AS t
                LEFT JOIN transactions AS pair ON pair.id = t.xfer_pair_id
                WHERE
                    (
                        t.ttype IN ('transfer_in', 'transfer_out')
                        AND t.xfer_pair_id IS NULL
                        AND NULLIF(TRIM(COALESCE(t.external_counterparty, '')), '') IS NULL
                    )
                    OR
                    (
                        t.xfer_pair_id IS NOT NULL
                        AND (
                            pair.id IS NULL
                            OR pair.xfer_pair_id IS NULL
                            OR pair.xfer_pair_id <> t.id
                            OR pair.account_id = t.account_id
                            OR NOT (
                                (
                                    t.ttype = 'transfer_out'
                                    AND pair.ttype = 'transfer_in'
                                    AND t.amount_cents < 0
                                    AND pair.amount_cents > 0
                                )
                                OR
                                (
                                    t.ttype = 'transfer_in'
                                    AND pair.ttype = 'transfer_out'
                                    AND t.amount_cents > 0
                                    AND pair.amount_cents < 0
                                )
                            )
                            OR t.amount_cents + pair.amount_cents <> 0
                        )
                    )
            """,
        },
        {
            "name": "invariant.locked_envelope_splits",
            "required_tables": ("transactions", "transaction_splits", "envelopes"),
            "sql": """
                SELECT COUNT(*)
                FROM transaction_splits AS s
                JOIN transactions AS t ON t.id = s.transaction_id
                JOIN envelopes AS e ON e.id = s.envelope_id
                WHERE e.locked_account_id IS NOT NULL
                  AND e.locked_account_id <> t.account_id
            """,
        },
        {
            "name": "invariant.savings_transfer_records",
            "required_tables": ("savings_transfer_records", "transactions"),
            "sql": """
                SELECT COUNT(*)
                FROM savings_transfer_records AS record
                LEFT JOIN transactions AS tx_out ON tx_out.id = record.tx_out_id
                LEFT JOIN transactions AS tx_in ON tx_in.id = record.tx_in_id
                WHERE record.tx_out_id IS NULL
                   OR record.tx_in_id IS NULL
                   OR tx_out.id IS NULL
                   OR tx_in.id IS NULL
                   OR tx_out.ttype <> 'transfer_out'
                   OR tx_in.ttype <> 'transfer_in'
                   OR tx_out.amount_cents >= 0
                   OR tx_in.amount_cents <= 0
                   OR tx_out.amount_cents + tx_in.amount_cents <> 0
                   OR tx_out.xfer_pair_id IS NULL
                   OR tx_in.xfer_pair_id IS NULL
                   OR tx_out.xfer_pair_id <> tx_in.id
                   OR tx_in.xfer_pair_id <> tx_out.id
            """,
        },
        {
            "name": "invariant.reconciliation_accounts",
            "required_tables": (
                "reconciliation_sessions",
                "reconciliation_items",
                "transactions",
            ),
            "sql": """
                SELECT COUNT(*)
                FROM reconciliation_items AS item
                LEFT JOIN reconciliation_sessions AS session
                    ON session.id = item.session_id
                LEFT JOIN transactions AS t ON t.id = item.transaction_id
                WHERE session.id IS NULL
                   OR t.id IS NULL
                   OR t.account_id <> session.account_id
            """,
        },
        {
            "name": "invariant.savings_enabled_percentage",
            "required_tables": ("savings_rules",),
            "sql": """
                SELECT COUNT(*)
                FROM (
                    SELECT plan_id
                    FROM savings_rules
                    WHERE enabled = 1
                    GROUP BY plan_id
                    HAVING SUM(contribution_basis_points) > 10000
                ) AS overcommitted_plans
            """,
        },
        {
            "name": "invariant.savings_rule_locks",
            "required_tables": ("savings_rules", "envelopes"),
            "sql": """
                SELECT COUNT(*)
                FROM savings_rules AS rule
                LEFT JOIN envelopes AS accessible
                    ON accessible.id = rule.accessible_envelope_id
                LEFT JOIN envelopes AS long_term
                    ON long_term.id = rule.long_term_envelope_id
                WHERE accessible.id IS NULL
                   OR (
                        accessible.locked_account_id IS NOT NULL
                        AND accessible.locked_account_id <> rule.accessible_account_id
                   )
                   OR (
                        (rule.long_term_account_id IS NULL)
                        <> (rule.long_term_envelope_id IS NULL)
                   )
                   OR (
                        rule.long_term_envelope_id IS NOT NULL
                        AND (
                            long_term.id IS NULL
                            OR (
                                long_term.locked_account_id IS NOT NULL
                                AND long_term.locked_account_id <> rule.long_term_account_id
                            )
                        )
                   )
            """,
        },
    )

    return [
        _count_check(conn, tables=tables, **check)
        for check in checks
    ]
