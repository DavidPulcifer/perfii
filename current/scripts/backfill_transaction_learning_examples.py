#!/usr/bin/env python3
"""Backfill import provenance into transaction_learning_examples.

The default mode is a read-only preview. Writes require both --write and --yes.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db import ensure_transaction_learning_schema, table_exists
from app.services.transaction_text_profile_service import build_transaction_text_profile_from_row


REQUIRED_READ_TABLES = (
    "accounts",
    "import_sessions",
    "import_session_rows",
    "import_row_matches",
    "transaction_import_validations",
    "transactions",
)


@dataclass(frozen=True)
class BackfillSummary:
    candidates: int
    inserted: int
    skipped_duplicates: int
    skipped_missing_required_tables: list[str]
    dry_run: bool
    preview: list[dict[str, Any]]


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {"unparsed": str(value)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def sqlite_connect_existing(path: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    uri = f"file:{quote(str(path.expanduser().resolve()), safe='/')}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def missing_required_tables(conn: sqlite3.Connection, *, for_write: bool) -> list[str]:
    required = list(REQUIRED_READ_TABLES)
    if for_write:
        required.append("transaction_learning_examples")
    return [table for table in required if not table_exists(conn, table)]


def fetch_candidates(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    sql = """
        WITH candidate_tx AS (
            SELECT
                row_id AS import_session_row_id,
                transaction_id,
                match_type,
                evidence_json,
                created_at,
                'import_row_match' AS link_source
            FROM import_row_matches
            WHERE transaction_id IS NOT NULL

            UNION ALL

            SELECT
                import_session_row_id,
                transaction_id,
                match_type,
                evidence_json,
                created_at,
                'transaction_import_validation' AS link_source
            FROM transaction_import_validations
            WHERE import_session_row_id IS NOT NULL
              AND transaction_id IS NOT NULL

            UNION ALL

            SELECT
                id AS import_session_row_id,
                transaction_id,
                match_type,
                evidence_json,
                created_at,
                'import_session_row' AS link_source
            FROM import_session_rows
            WHERE transaction_id IS NOT NULL
        ),
        deduped AS (
            SELECT
                import_session_row_id,
                transaction_id,
                MAX(CASE WHEN link_source='transaction_import_validation' THEN 1 ELSE 0 END) AS has_validation_link,
                MAX(CASE WHEN link_source='import_row_match' THEN 1 ELSE 0 END) AS has_match_link
            FROM candidate_tx
            GROUP BY import_session_row_id, transaction_id
        )
        SELECT
            s.account_id,
            s.source_bankid,
            s.source_acctid,
            s.file_hash,
            s.created_at AS import_session_created_at,
            r.id AS import_session_row_id,
            r.row_index,
            r.posted_at AS import_posted_at,
            r.amount_cents AS import_amount_cents,
            r.payee AS raw_payee,
            r.memo AS raw_memo,
            r.fitid AS import_fitid,
            r.row_fingerprint,
            r.evidence_json AS import_row_evidence_json,
            r.match_type AS import_row_match_type,
            r.created_at AS import_row_created_at,
            m.id AS import_row_match_id,
            m.match_type AS import_row_match_match_type,
            m.evidence_json AS import_row_match_evidence_json,
            m.created_at AS import_row_match_created_at,
            v.id AS transaction_import_validation_id,
            v.source AS validation_source,
            v.fitid AS validation_fitid,
            v.row_fingerprint AS validation_row_fingerprint,
            v.match_type AS validation_match_type,
            v.evidence_json AS validation_evidence_json,
            v.validated_at,
            d.has_validation_link,
            d.has_match_link,
            t.id AS transaction_id,
            t.posted_at AS transaction_posted_at,
            t.amount_cents AS transaction_amount_cents,
            t.payee AS final_payee,
            t.memo AS final_memo,
            t.ttype AS transaction_type,
            t.fitid AS transaction_fitid,
            t.xfer_pair_id,
            t.external_counterparty,
            pair.account_id AS transfer_other_account_id
        FROM deduped d
        JOIN import_session_rows r ON r.id = d.import_session_row_id
        JOIN import_sessions s ON s.id = r.session_id
        JOIN transactions t ON t.id = d.transaction_id
        LEFT JOIN import_row_matches m
            ON m.row_id = r.id AND m.transaction_id = d.transaction_id
           AND m.id = (
               SELECT MIN(m2.id)
               FROM import_row_matches m2
               WHERE m2.row_id = r.id AND m2.transaction_id = d.transaction_id
           )
        LEFT JOIN transaction_import_validations v
            ON v.account_id = s.account_id AND v.transaction_id = d.transaction_id
        LEFT JOIN transactions pair ON pair.id = t.xfer_pair_id
        WHERE t.account_id = s.account_id
        ORDER BY r.id, t.id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += "\n        LIMIT ?"
        params = (int(limit),)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def fetch_existing_example_keys(conn: sqlite3.Connection) -> tuple[set[str], set[tuple[int, int]]]:
    dedupe_keys: set[str] = set()
    row_transaction_pairs: set[tuple[int, int]] = set()
    if not table_exists(conn, "transaction_learning_examples"):
        return dedupe_keys, row_transaction_pairs
    rows = conn.execute(
        """
        SELECT dedupe_key, import_session_row_id, transaction_id
        FROM transaction_learning_examples
        """
    ).fetchall()
    for row in rows:
        if row["dedupe_key"]:
            dedupe_keys.add(str(row["dedupe_key"]))
        if row["import_session_row_id"] and row["transaction_id"]:
            row_transaction_pairs.add((int(row["import_session_row_id"]), int(row["transaction_id"])))
    return dedupe_keys, row_transaction_pairs


def fetch_splits(conn: sqlite3.Connection, transaction_id: int) -> list[dict[str, Any]]:
    if not table_exists(conn, "transaction_splits"):
        return []
    return [
        {
            "id": int(row["id"]),
            "envelope_id": int(row["envelope_id"]),
            "amount_cents": int(row["amount_cents"]),
        }
        for row in conn.execute(
            """
            SELECT id, envelope_id, amount_cents
            FROM transaction_splits
            WHERE transaction_id=?
            ORDER BY id
            """,
            (int(transaction_id),),
        ).fetchall()
    ]


def fetch_remainder_intent(conn: sqlite3.Connection, transaction_id: int) -> dict[str, Any]:
    if not table_exists(conn, "transaction_remainder_intents"):
        return {}
    row = conn.execute(
        """
        SELECT envelope_id, amount_cents, created_at, updated_at
        FROM transaction_remainder_intents
        WHERE transaction_id=?
        """,
        (int(transaction_id),),
    ).fetchone()
    if not row:
        return {}
    return {
        "envelope_id": int(row["envelope_id"]),
        "amount_cents": int(row["amount_cents"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def evidence_quality(candidate: dict[str, Any]) -> str:
    has_raw_text = bool((candidate.get("raw_payee") or "").strip() or (candidate.get("raw_memo") or "").strip())
    if candidate.get("transaction_import_validation_id") and has_raw_text:
        return "high"
    if candidate.get("import_row_match_id") or candidate.get("transaction_import_validation_id"):
        return "medium"
    return "low"


def transaction_kind(ttype: str | None) -> str | None:
    if not ttype:
        return None
    return "transfer" if str(ttype).startswith("transfer_") else str(ttype)


def build_example(conn: sqlite3.Connection, candidate: dict[str, Any], *, now: str) -> dict[str, Any]:
    transaction_id = int(candidate["transaction_id"])
    row_id = int(candidate["import_session_row_id"])
    raw_profile = asdict(build_transaction_text_profile_from_row({
        "payee": candidate.get("raw_payee"),
        "memo": candidate.get("raw_memo"),
    }))
    final_profile = asdict(build_transaction_text_profile_from_row({
        "payee": candidate.get("final_payee"),
        "memo": candidate.get("final_memo"),
    }))
    splits = fetch_splits(conn, transaction_id)
    remainder_intent = fetch_remainder_intent(conn, transaction_id)
    quality = evidence_quality(candidate)
    ttype = candidate.get("transaction_type")
    transfer_other_account_id = candidate.get("transfer_other_account_id")

    decision = {
        "kind": transaction_kind(ttype),
        "transaction_type": ttype,
        "transfer_other_account_id": transfer_other_account_id,
        "transfer_pair_id": candidate.get("xfer_pair_id"),
        "external_counterparty": candidate.get("external_counterparty"),
        "split_count": len(splits),
        "has_remainder_intent": bool(remainder_intent),
    }
    evidence = {
        "import_session": {
            "source_bankid": candidate.get("source_bankid"),
            "source_acctid": candidate.get("source_acctid"),
            "file_hash": candidate.get("file_hash"),
            "created_at": candidate.get("import_session_created_at"),
        },
        "import_row": {
            "row_index": candidate.get("row_index"),
            "fitid": candidate.get("import_fitid"),
            "row_fingerprint": candidate.get("row_fingerprint"),
            "match_type": candidate.get("import_row_match_type"),
            "evidence": parse_json_object(candidate.get("import_row_evidence_json")),
            "created_at": candidate.get("import_row_created_at"),
        },
        "import_row_match": {
            "id": candidate.get("import_row_match_id"),
            "match_type": candidate.get("import_row_match_match_type"),
            "evidence": parse_json_object(candidate.get("import_row_match_evidence_json")),
            "created_at": candidate.get("import_row_match_created_at"),
        },
        "transaction_import_validation": {
            "id": candidate.get("transaction_import_validation_id"),
            "source": candidate.get("validation_source"),
            "fitid": candidate.get("validation_fitid"),
            "row_fingerprint": candidate.get("validation_row_fingerprint"),
            "match_type": candidate.get("validation_match_type"),
            "evidence": parse_json_object(candidate.get("validation_evidence_json")),
            "validated_at": candidate.get("validated_at"),
        },
        "backfill": {
            "has_validation_link": bool(candidate.get("has_validation_link")),
            "has_match_link": bool(candidate.get("has_match_link")),
        },
    }

    return {
        "account_id": int(candidate["account_id"]),
        "transaction_id": transaction_id,
        "import_session_row_id": row_id,
        "transaction_import_validation_id": candidate.get("transaction_import_validation_id"),
        "source": "backfill",
        "evidence_quality": quality,
        "dedupe_key": f"backfill:import-row:{row_id}:transaction:{transaction_id}",
        "posted_at": candidate.get("transaction_posted_at") or candidate.get("import_posted_at"),
        "amount_cents": (
            candidate.get("transaction_amount_cents")
            if candidate.get("transaction_amount_cents") is not None
            else candidate.get("import_amount_cents")
        ),
        "raw_payee": candidate.get("raw_payee"),
        "raw_memo": candidate.get("raw_memo"),
        "raw_profile_json": compact_json(raw_profile),
        "final_payee": candidate.get("final_payee"),
        "final_memo": candidate.get("final_memo"),
        "final_profile_json": compact_json(final_profile),
        "transaction_type": ttype,
        "transfer_other_account_id": transfer_other_account_id,
        "splits_json": compact_json(splits),
        "remainder_intent_json": compact_json(remainder_intent),
        "decision_json": compact_json(decision),
        "evidence_json": compact_json(evidence),
        "created_at": now,
        "updated_at": now,
    }


def insert_example(conn: sqlite3.Connection, example: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO transaction_learning_examples(
            account_id, transaction_id, import_session_row_id,
            transaction_import_validation_id, source, evidence_quality,
            dedupe_key, posted_at, amount_cents, raw_payee, raw_memo,
            raw_profile_json, final_payee, final_memo, final_profile_json,
            transaction_type, transfer_other_account_id, splits_json,
            remainder_intent_json, decision_json, evidence_json, created_at, updated_at
        )
        VALUES (
            :account_id, :transaction_id, :import_session_row_id,
            :transaction_import_validation_id, :source, :evidence_quality,
            :dedupe_key, :posted_at, :amount_cents, :raw_payee, :raw_memo,
            :raw_profile_json, :final_payee, :final_memo, :final_profile_json,
            :transaction_type, :transfer_other_account_id, :splits_json,
            :remainder_intent_json, :decision_json, :evidence_json, :created_at, :updated_at
        )
        """,
        example,
    )


def preview_example(example: dict[str, Any]) -> dict[str, Any]:
    raw_profile = parse_json_object(example["raw_profile_json"])
    decision = parse_json_object(example["decision_json"])
    return {
        "dedupe_key": example["dedupe_key"],
        "account_id": example["account_id"],
        "transaction_id": example["transaction_id"],
        "import_session_row_id": example["import_session_row_id"],
        "evidence_quality": example["evidence_quality"],
        "raw_payee": example["raw_payee"],
        "raw_memo": example["raw_memo"],
        "final_payee": example["final_payee"],
        "final_memo": example["final_memo"],
        "transaction_type": example["transaction_type"],
        "transfer_other_account_id": example["transfer_other_account_id"],
        "raw_profile": {
            "merchant_tokens": raw_profile.get("merchant_tokens", []),
            "account_suffixes": raw_profile.get("account_suffixes", []),
            "direction": raw_profile.get("direction"),
        },
        "decision": decision,
    }


def backfill_transaction_learning_examples(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    preview_limit: int = 20,
    now: str | None = None,
) -> BackfillSummary:
    if not dry_run:
        ensure_transaction_learning_schema(conn)

    missing = missing_required_tables(conn, for_write=not dry_run)
    if missing:
        return BackfillSummary(
            candidates=0,
            inserted=0,
            skipped_duplicates=0,
            skipped_missing_required_tables=missing,
            dry_run=dry_run,
            preview=[],
        )

    now = now or datetime.now(UTC).isoformat(timespec="seconds")
    existing_dedupe_keys, existing_pairs = fetch_existing_example_keys(conn)
    candidates = fetch_candidates(conn, limit=limit)
    inserted = 0
    skipped_duplicates = 0
    preview: list[dict[str, Any]] = []

    for candidate in candidates:
        row_id = int(candidate["import_session_row_id"])
        transaction_id = int(candidate["transaction_id"])
        dedupe_key = f"backfill:import-row:{row_id}:transaction:{transaction_id}"
        if dedupe_key in existing_dedupe_keys or (row_id, transaction_id) in existing_pairs:
            skipped_duplicates += 1
            continue

        example = build_example(conn, candidate, now=now)
        if len(preview) < preview_limit:
            preview.append(preview_example(example))
        if dry_run:
            continue

        insert_example(conn, example)
        inserted += 1
        existing_dedupe_keys.add(dedupe_key)
        existing_pairs.add((row_id, transaction_id))

    if not dry_run:
        conn.commit()

    return BackfillSummary(
        candidates=len(candidates),
        inserted=inserted,
        skipped_duplicates=skipped_duplicates,
        skipped_missing_required_tables=[],
        dry_run=dry_run,
        preview=preview,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite database to preview or backfill.")
    parser.add_argument("--write", action="store_true", help="Insert backfilled learning examples.")
    parser.add_argument("--yes", action="store_true", help="Required with --write.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum source candidates to scan.")
    parser.add_argument("--preview-limit", type=int, default=20, help="Maximum examples to print in preview.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def print_human(summary: BackfillSummary) -> None:
    mode = "dry-run preview" if summary.dry_run else "write"
    print(f"Mode: {mode}")
    if summary.skipped_missing_required_tables:
        print("Missing required tables: " + ", ".join(summary.skipped_missing_required_tables))
        return
    print(f"Candidates: {summary.candidates}")
    print(f"Inserted: {summary.inserted}")
    print(f"Skipped duplicates: {summary.skipped_duplicates}")
    if summary.preview:
        print("Preview:")
        for item in summary.preview:
            print(
                "  "
                + compact_json({
                    "dedupe_key": item["dedupe_key"],
                    "quality": item["evidence_quality"],
                    "raw": [item["raw_payee"], item["raw_memo"]],
                    "final": [item["final_payee"], item["final_memo"]],
                    "transaction_type": item["transaction_type"],
                    "transfer_other_account_id": item["transfer_other_account_id"],
                    "raw_profile": item["raw_profile"],
                })
            )


def main() -> int:
    args = build_parser().parse_args()
    if args.write and not args.yes:
        raise SystemExit("Refusing write mode without --yes")

    with sqlite_connect_existing(args.db, writable=args.write) as conn:
        summary = backfill_transaction_learning_examples(
            conn,
            dry_run=not args.write,
            limit=args.limit,
            preview_limit=args.preview_limit,
        )
    payload = asdict(summary)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_human(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
