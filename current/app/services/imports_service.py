# app/services/imports_service.py
from __future__ import annotations
from pathlib import Path
from time import perf_counter
import csv, hashlib, json, re, tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from .import_prefill_service import build_import_prefills, prediction_debug_payload
from .import_matching_rule_service import build_import_matching_rule_prefills
from .payee_normalization_service import build_payee_normalization_prefills
from .import_draft_service import build_import_draft_identity, draft_public_metadata
from ..utils import parse_money_to_cents


def _elapsed_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)


def _record_timing(timings: dict | None, name: str, start: float) -> None:
    if timings is not None:
        timings[name] = _elapsed_ms(start)


def _safe_row_index(item: dict, default: int = -1) -> int:
    try:
        return int(item.get("row_index", default))
    except (TypeError, ValueError):
        return default


# --- Helpers ---------------------------------------------------------------

def to_cents_from_str_amount(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 0
    # allow "123.45", "-12.3", "$1,234.56"
    s = s.replace("$", "").replace(",", "")
    sign = -1 if s.startswith("-") else 1
    if s.startswith(("+", "-")):
        s = s[1:]
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "0")[:2]
    else:
        whole, frac = s, "00"
    return sign * (int(whole or 0) * 100 + int(frac or 0))


class CsvColumnMappingRequired(ValueError):
    def __init__(self, payload: dict):
        super().__init__(payload.get("message") or "CSV column assignment is required.")
        self.payload = payload


def normalize_csv_header(header: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (header or "").lower())


CSV_FIELD_SYNONYMS = {
    "date": {"date", "posted", "dtposted", "posteddate", "transactiondate", "transdate", "effectivedate"},
    "amount": {"amount", "amt", "trnamt", "transactionamount"},
    "debit": {"debit", "debits", "withdrawal", "withdrawals", "charge", "charges"},
    "credit": {"credit", "credits", "deposit", "deposits"},
    "payee": {"name", "payee", "description", "merchant"},
    "memo": {"memo", "note", "notes", "type", "address"},
    "fitid": {"fitid", "id", "ref", "reference", "referencenumber", "confirmation", "confirmationnumber"},
}

CSV_POLARITY_NORMAL = "normal"
CSV_POLARITY_INVERTED = "inverted"


def normalize_csv_polarity(value: str | None) -> str:
    return CSV_POLARITY_INVERTED if str(value or "").strip().lower() == CSV_POLARITY_INVERTED else CSV_POLARITY_NORMAL


def apply_csv_polarity(parsed: dict, polarity: str | None) -> dict:
    normalized = normalize_csv_polarity(polarity)
    if _parsed_source_type(parsed) != "csv":
        return parsed

    parsed["_csv_polarity"] = normalized
    if normalized != CSV_POLARITY_INVERTED:
        return parsed

    for row in parsed.get("transactions") or []:
        row["amount_cents"] = -import_transaction_amount_cents(row)
    return parsed


def detect_csv_credit_card_polarity(parsed: dict, account: dict | None) -> str | None:
    if _parsed_source_type(parsed) != "csv" or not account or account.get("account_type") != "credit_card":
        return None

    purchase_rows = []
    payment_rows = []
    for row in parsed.get("transactions") or []:
        amount_cents = import_transaction_amount_cents(row)
        if amount_cents == 0:
            continue
        text = " ".join(str(row.get(key) or "") for key in ("memo", "payee")).lower()
        if "payment" in text or "pmt" in text:
            payment_rows.append(amount_cents)
        elif "clearing" in text or "purchase" in text or "sale" in text or row.get("payee"):
            purchase_rows.append(amount_cents)

    if len(purchase_rows) < 3:
        return None

    positive_purchases = sum(1 for amount in purchase_rows if amount > 0)
    negative_payments = sum(1 for amount in payment_rows if amount < 0)
    purchases_look_reversed = positive_purchases / len(purchase_rows) >= 0.8
    payments_look_reversed = not payment_rows or negative_payments / len(payment_rows) >= 0.8
    if purchases_look_reversed and payments_look_reversed:
        return CSV_POLARITY_INVERTED
    return None


def _parse_csv_money(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return to_cents_from_str_amount(raw.strip("()")) * (-1 if raw.startswith("(") and raw.endswith(")") else 1)
    except Exception:
        return None


def _csv_sample_values(rows: list[dict], header: str, *, limit: int = 3) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = (row.get(header) or "").strip()
        if value:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _csv_candidate_score(field: str, header: str, rows: list[dict]) -> int:
    normalized = normalize_csv_header(header)
    score = 0
    if normalized in CSV_FIELD_SYNONYMS[field]:
        score += 100
    elif field == "date" and "date" in normalized:
        score += 80
    elif field == "amount" and ("amount" in normalized or normalized.endswith("amt")):
        score += 80
    elif field == "payee" and ("description" in normalized or "payee" in normalized or "merchant" in normalized):
        score += 80
    elif field == "fitid" and ("reference" in normalized or normalized.endswith("id")):
        score += 60

    sample_values = _csv_sample_values(rows, header, limit=10)
    if not sample_values:
        return score
    if field == "date":
        parsed = sum(1 for value in sample_values if _normalize_date_or_none(value))
        score += int(40 * parsed / len(sample_values))
    elif field in {"amount", "debit", "credit"}:
        parsed = sum(1 for value in sample_values if _parse_csv_money(value) is not None)
        score += int(40 * parsed / len(sample_values))
    elif field in {"payee", "memo", "fitid"}:
        score += 20
    return score


def detect_csv_column_mapping(headers: list[str], rows: list[dict]) -> tuple[dict[str, str | None], list[str]]:
    mapping: dict[str, str | None] = {"date": None, "amount": None, "debit": None, "credit": None, "payee": None, "memo": None, "fitid": None}
    warnings: list[str] = []
    used: set[str] = set()

    for field in ("date", "amount", "payee", "memo", "fitid"):
        scored = sorted(
            ((_csv_candidate_score(field, header, rows), header) for header in headers),
            key=lambda item: (-item[0], headers.index(item[1])),
        )
        best_score, best_header = scored[0] if scored else (0, None)
        second_score = scored[1][0] if len(scored) > 1 else 0
        threshold = 100 if field in {"date", "amount", "payee"} else 60
        if best_header and best_score >= threshold and best_score > second_score and best_header not in used:
            mapping[field] = best_header
            used.add(best_header)

    if mapping["amount"] is None:
        debit = _best_csv_header("debit", headers, rows, used=used)
        credit = _best_csv_header("credit", headers, rows, used=used | ({debit} if debit else set()))
        if debit and credit:
            mapping["debit"] = debit
            mapping["credit"] = credit

    if not mapping["date"]:
        warnings.append("Choose the date column. CSV imports no longer fall back to today's date.")
    if not mapping["amount"] and not (mapping["debit"] and mapping["credit"]):
        warnings.append("Choose a signed amount column, or both debit and credit columns.")
    if not mapping["payee"]:
        warnings.append("Choose the payee or description column.")
    return mapping, warnings


def _best_csv_header(field: str, headers: list[str], rows: list[dict], *, used: set[str]) -> str | None:
    scored = sorted(
        ((_csv_candidate_score(field, header, rows), header) for header in headers if header not in used),
        key=lambda item: (-item[0], headers.index(item[1])),
    )
    if not scored:
        return None
    score, header = scored[0]
    return header if score >= 100 else None


def csv_mapping_prompt_payload(path: Path, *, message: str | None = None) -> dict:
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as f:
        rdr = csv.DictReader(f)
        headers = list(rdr.fieldnames or [])
        sample_rows = [row for _, row in zip(range(10), rdr)]
    mapping, warnings = detect_csv_column_mapping(headers, sample_rows)
    return {
        "message": message or "Assign CSV columns before import review.",
        "headers": headers,
        "samples": {header: _csv_sample_values(sample_rows, header) for header in headers},
        "suggested_mapping": mapping,
        "warnings": warnings,
    }


def _normalize_date_or_none(s: str | None) -> str | None:
    try:
        return normalize_date(s)
    except Exception:
        return None

def parse_ofx_datetime(dt: str) -> str:
    """
    OFX DTPOSTED format like 20240815120000[-8:UTC] -> return 'YYYY-MM-DD'
    """
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", dt or "")
    if not m:
        return datetime.utcnow().strftime("%Y-%m-%d")
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"

def guess_ttype(amount_cents: int, account_type: str | None) -> str:
    # Simple guess: negative -> expense; positive -> income
    return "expense" if amount_cents < 0 else "income"


@dataclass(frozen=True)
class UploadedStatementParseResult:
    parsed: dict | None = None
    error_message: str | None = None
    csv_mapping_required: dict | None = None

    @property
    def ok(self) -> bool:
        return self.parsed is not None and self.error_message is None

    @property
    def flash_category(self) -> str:
        if (self.error_message or "").startswith("Could not parse file:"):
            return "danger"
        return "warning"


def parse_uploaded_statement_file(file, *, parse_func=None) -> UploadedStatementParseResult:
    filename = (getattr(file, "filename", None) or "").strip() if file else ""
    if not file or not filename:
        return UploadedStatementParseResult(error_message="Please choose a QFX/OFX/CSV file.")

    data = file.read()
    if not data:
        return UploadedStatementParseResult(error_message="Uploaded file is empty.")

    parse_func = parse_func or parse_statement_upload
    try:
        return UploadedStatementParseResult(parsed=parse_func(data, filename))
    except CsvColumnMappingRequired as ex:
        return UploadedStatementParseResult(csv_mapping_required=ex.payload)
    except Exception as ex:
        return UploadedStatementParseResult(error_message=f"Could not parse file: {ex}")


def parsed_statement_identifier(parsed, name: str) -> str:
    if isinstance(parsed, dict):
        return (parsed.get(name) or "").strip()
    return (getattr(parsed, name, None) or "").strip()


def _statement_account_suffix(acctid: str | None) -> str | None:
    raw = (acctid or "").strip()
    if not raw:
        return None
    groups = re.findall(r"\d{4,}", raw)
    if groups:
        return groups[-1][-4:]
    digits = re.sub(r"\D+", "", raw)
    return digits[-4:] if len(digits) >= 4 else None


def _account_text_has_suffix(account: dict, suffix: str) -> bool:
    text = " ".join(str(account.get(key) or "") for key in ("name", "acct_key", "acctid"))
    return bool(re.search(rf"(?<!\d){re.escape(suffix)}(?!\d)", text))




_FILENAME_STOPWORDS = {
    "account", "accounts", "acct", "activity", "bank", "banking", "card", "cash",
    "checking", "csv", "download", "export", "file", "import", "invest", "investment",
    "of", "qfx", "report", "saving", "savings", "statement", "statements", "transaction",
    "transactions", "wealth", "front",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
    "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
    "oct", "october", "nov", "november", "dec", "december",
}


def _normalized_filename_text(filename: str | None) -> str:
    stem = Path(filename or "").stem
    # Split camel-ish institution filenames and separators into searchable word boundaries.
    stem = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def _normalized_account_text(account: dict) -> str:
    parts = [str(account.get(key) or "") for key in ("name", "acct_key")]
    return re.sub(r"[^a-z0-9]+", " ", " ".join(parts).lower()).strip()


def _filename_tokens(text: str) -> set[str]:
    return {token for token in text.split() if len(token) >= 3 and not token.isdigit()}


def _account_filename_tokens(account: dict) -> set[str]:
    return {
        token
        for token in _filename_tokens(_normalized_account_text(account))
        if token not in _FILENAME_STOPWORDS
    }


def _text_contains_token(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text))


def _filename_account_name_score(filename_text: str, account: dict) -> int:
    account_tokens = _account_filename_tokens(account)
    if not account_tokens:
        return 0
    matched = {token for token in account_tokens if _text_contains_token(filename_text, token)}
    if not matched:
        return 0
    # Partial token matches are allowed only when they are uniquely best across all
    # accounts; ties deliberately fall back to manual account selection.
    return len(matched)


def _find_account_by_csv_filename(accounts: list[dict], filename: str | None) -> dict | None:
    filename_text = _normalized_filename_text(filename)
    if not filename_text:
        return None

    suffixes = {
        digits[-4:]
        for digits in re.findall(r"\d{4,}", filename_text)
        if not (digits.startswith(("19", "20")) and len(digits) in (4, 6, 8))
    }
    if suffixes:
        suffix_matches = [
            account
            for account in accounts
            if any(_account_text_has_suffix(account, suffix) for suffix in suffixes)
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        if len(suffix_matches) > 1:
            return None

    scored = [
        (score, account)
        for account in accounts
        if (score := _filename_account_name_score(filename_text, account)) > 0
    ]
    if not scored:
        return None
    best_score = max(score for score, _account in scored)
    best = [account for score, account in scored if score == best_score]
    return best[0] if len(best) == 1 else None

def import_account_by_id(accounts: list[dict], account_id: int | None) -> dict | None:
    if account_id is None:
        return None
    try:
        target_id = int(account_id)
    except (TypeError, ValueError):
        return None
    for account in accounts or []:
        try:
            if int(account.get("id")) == target_id:
                return account
        except (TypeError, ValueError):
            continue
    return None


def _parsed_source_filename(parsed) -> str:
    if isinstance(parsed, dict):
        return (parsed.get("_source_filename") or "").strip()
    return (getattr(parsed, "_source_filename", None) or "").strip()


def _parsed_source_type(parsed) -> str:
    if isinstance(parsed, dict):
        return (parsed.get("_source_type") or "").strip().lower()
    return (getattr(parsed, "_source_type", None) or "").strip().lower()


def _find_account_for_import_with_source(accounts: list[dict], parsed) -> tuple[dict | None, str | None]:
    source_bankid = parsed_statement_identifier(parsed, "bankid")
    source_acctid = parsed_statement_identifier(parsed, "acctid")

    if source_bankid and source_acctid:
        for account in accounts:
            bankid = (account.get("bankid") or "").strip()
            acctid = (account.get("acctid") or "").strip()
            if bankid == source_bankid and acctid == source_acctid:
                return account, "identifier"

    if source_acctid:
        for account in accounts:
            acctid = (account.get("acctid") or "").strip()
            if acctid == source_acctid:
                return account, "identifier"

    acct_suffix = _statement_account_suffix(source_acctid)
    if acct_suffix:
        suffix_matches = [
            account
            for account in accounts
            if not (account.get("acctid") or "").strip()
            and _account_text_has_suffix(account, acct_suffix)
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0], "identifier_suffix"

    if source_bankid and not source_acctid:
        bankid_matches = [
            account
            for account in accounts
            if (account.get("bankid") or "").strip() == source_bankid
        ]
        if len(bankid_matches) == 1:
            return bankid_matches[0], "identifier"

    if _parsed_source_type(parsed) == "csv":
        filename_account = _find_account_by_csv_filename(accounts, _parsed_source_filename(parsed))
        if filename_account is not None:
            return filename_account, "filename"

    return None, None


def find_account_for_import(accounts: list[dict], parsed) -> dict | None:
    account, _source = _find_account_for_import_with_source(accounts, parsed)
    return account


def find_account_for_import_source(accounts: list[dict], parsed) -> str | None:
    _account, source = _find_account_for_import_with_source(accounts, parsed)
    return source

def import_account_for_review(
    accounts: list[dict],
    parsed,
    selected_account_id: int | None = None,
) -> dict | None:
    selected = import_account_by_id(accounts, selected_account_id)
    if selected is not None:
        return selected
    return find_account_for_import(accounts, parsed)


def import_review_account_id(account: dict | None) -> int | None:
    if not account:
        return None
    try:
        return int(account["id"])
    except (KeyError, TypeError, ValueError):
        return None


def import_review_existing_fitids(account: dict | None, list_fitids_func) -> set[str]:
    account_id = import_review_account_id(account)
    if account_id is None:
        return set()
    return set(list_fitids_func(account_id))


def import_transaction_amount_cents(transaction: dict) -> int:
    if transaction.get("amount_cents") is not None:
        try:
            return int(transaction.get("amount_cents") or 0)
        except Exception:
            return 0

    amount_raw = transaction.get("amount")
    if amount_raw is None:
        amount_raw = transaction.get("trnamt")
    try:
        return parse_money_to_cents(str(amount_raw or "0"))
    except Exception:
        return 0


def import_upload_context(
    *,
    list_accounts_func,
    selected_account_id: int | None = None,
    account_detection_message: str | None = None,
) -> dict:
    return {
        "accounts": list_accounts_func(),
        "selected_account_id": selected_account_id,
        "account_detection_message": account_detection_message,
    }


def build_import_row_states(
    transactions: list[dict],
    existing_fitids: set[str] | None = None,
    existing_imported_row_indexes: set[int] | None = None,
    import_prefills: list[dict] | None = None,
    provenance_imported_row_indexes: set[int] | None = None,
) -> list[dict]:
    """Build the server-side contract for import review row state.

    This is intentionally pure: callers provide all duplicate and prefill inputs,
    and the returned states drive template rendering plus duplicate/manual/prefill
    eligibility so those paths do not grow separate business rules.
    """
    existing_fitids = existing_fitids or set()
    existing_imported_row_indexes = existing_imported_row_indexes or set()
    provenance_imported_row_indexes = provenance_imported_row_indexes or set()
    prefills_by_index: dict[int, dict] = {}
    for item in import_prefills or []:
        try:
            prefills_by_index[int(item.get("row_index"))] = item
        except (TypeError, ValueError):
            continue

    states: list[dict] = []
    for idx, row in enumerate(transactions or []):
        amount_cents = import_transaction_amount_cents(row)
        fitid = (row.get("fitid") or "").strip()
        exact_fitid_duplicate = bool(fitid and fitid in existing_fitids)
        provenance_duplicate = idx in provenance_imported_row_indexes
        fuzzy_duplicate = idx in existing_imported_row_indexes
        already_imported = exact_fitid_duplicate or provenance_duplicate or fuzzy_duplicate
        section = "exp" if amount_cents < 0 else "inc" if amount_cents > 0 else "zero"
        state = {
            "row_index": idx,
            "amount_cents": amount_cents,
            "fitid": fitid,
            "section": section,
            "already_imported": already_imported,
            "exact_fitid_duplicate": exact_fitid_duplicate,
            "fuzzy_transfer_duplicate": fuzzy_duplicate,
            "provenance_duplicate": provenance_duplicate,
            "checked": not already_imported,
            "disabled": already_imported,
            "no_fitid": not bool(fitid),
            "duplicate_check_eligible": bool(fitid) or amount_cents != 0,
            "manual_match_eligible": (not already_imported) and amount_cents != 0,
            "prefill_eligible": (not already_imported) and amount_cents != 0,
            "prefill": prefills_by_index.get(idx, {
                "row_index": idx,
                "prefill": False,
                "debug_reason_codes": ["no_prefill"],
            }),
        }
        states.append(state)
    return states


def _fingerprint_text(value) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def import_row_fingerprint(
    row: dict,
    *,
    account_id: int | None = None,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
) -> str:
    """Stable evidence key for durable import provenance.

    FITID is intentionally excluded so an app-created transfer leg with a
    different FITID can still be recognized when the same statement row appears
    again. Account/source identifiers scope the key when available.
    """
    payload = {
        "v": 1,
        "account_id": int(account_id) if account_id is not None else None,
        "source_bankid": _fingerprint_text(source_bankid),
        "source_acctid": _fingerprint_text(source_acctid),
        "posted_at": str(row.get("posted_at") or "").strip(),
        "amount_cents": import_transaction_amount_cents(row),
        "payee": _fingerprint_text(row.get("payee") or row.get("name")),
        "memo": _fingerprint_text(row.get("memo")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def import_row_provenance_indexes(
    transactions: list[dict],
    account_id: int | None,
    *,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
    list_import_provenance_matches_func=None,
) -> set[int]:
    if account_id is None or not transactions or not list_import_provenance_matches_func:
        return set()

    fingerprints_by_index: dict[int, str] = {}
    for idx, row in enumerate(transactions or []):
        try:
            row_index = int(row.get("index"))
        except (TypeError, ValueError):
            row_index = idx
        fingerprints_by_index[row_index] = import_row_fingerprint(
            row,
            account_id=account_id,
            source_bankid=source_bankid,
            source_acctid=source_acctid,
        )
    rows = list_import_provenance_matches_func(
        account_id,
        list(fingerprints_by_index.values()),
        source_bankid=source_bankid,
        source_acctid=source_acctid,
    )
    matched_fingerprints = {str(row.get("row_fingerprint") or "") for row in rows}
    return {idx for idx, fp in fingerprints_by_index.items() if fp in matched_fingerprints}



def import_prefills_for_import_review(
    transactions: list[dict],
    account_id: int | None,
    existing_fitids: set[str],
    *,
    prefill_func=build_import_prefills,
    row_states: list[dict] | None = None,
) -> list[dict]:
    """Compatibility wrapper around the FIN077 matcher output for import review.

    It owns row-index remapping and existing-import suppression only; category,
    transfer, split, and payee decisions stay in the matcher services.
    """
    if account_id is None:
        return []

    all_rows = list(transactions or [])
    if row_states is None:
        row_states = build_import_row_states(all_rows, existing_fitids)
    states_by_index = {int(state["row_index"]): state for state in row_states}
    normalized: list[dict | None] = [None] * len(all_rows)
    eligible_rows: list[dict] = []
    eligible_positions: list[int] = []

    for idx, row in enumerate(all_rows):
        state = states_by_index.get(idx)
        if state and not state.get("prefill_eligible", True):
            normalized[idx] = {
                "row_index": idx,
                "prefill": False,
                "debug_reason_codes": ["already_imported"],
                "prediction_debug": prediction_debug_payload(
                    engine="import_prefill",
                    decision="no_prefill",
                    prediction_type=None,
                    reason_codes=["already_imported"],
                    confidence="none",
                ),
            }
            continue
        eligible_rows.append(row)
        eligible_positions.append(idx)

    prefills = prefill_func(eligible_rows, account_id) if eligible_rows else []
    by_original_index: dict[int, dict] = {}
    for eligible_idx, item in enumerate(prefills or []):
        try:
            eligible_row_index = int(item.get("row_index", eligible_idx))
        except (TypeError, ValueError):
            eligible_row_index = eligible_idx
        if eligible_row_index < 0 or eligible_row_index >= len(eligible_positions):
            continue
        original_idx = eligible_positions[eligible_row_index]
        remapped = dict(item)
        remapped["row_index"] = original_idx
        by_original_index[original_idx] = remapped

    output: list[dict] = []
    for idx in range(len(all_rows)):
        item = normalized[idx]
        if item is None:
            item = by_original_index.get(idx, {
                "row_index": idx,
                "prefill": False,
                "debug_reason_codes": ["no_prefill"],
                "prediction_debug": prediction_debug_payload(
                    engine="import_prefill",
                    decision="no_prefill",
                    prediction_type=None,
                    reason_codes=["no_prefill"],
                    confidence="none",
                ),
            })
        item["row_index"] = idx
        output.append(item)
    return output


def import_review_context(
    parsed: dict,
    *,
    list_accounts_func,
    list_fitids_func,
    list_envelopes_func,
    account_envelope_balances_func=None,
    selected_account_id: int | None = None,
    import_prefills_func=import_prefills_for_import_review,
    payee_prefills_func=build_payee_normalization_prefills,
    rule_prefills_func=build_import_matching_rule_prefills,
    list_transactions_func=None,
    get_transaction_func=None,
    list_import_provenance_matches_func=None,
    get_import_review_draft_func=None,
    cleanup_import_review_drafts_func=None,
    create_import_review_source_func=None,
    cleanup_import_review_sources_func=None,
    timings: dict | None = None,
) -> dict:
    start = perf_counter()
    accounts = list_accounts_func()
    _record_timing(timings, "accounts_ms", start)

    account = import_account_for_review(accounts, parsed, selected_account_id)
    account_id = import_review_account_id(account)
    transactions = parsed.get("transactions", [])

    start = perf_counter()
    existing_fitids = import_review_existing_fitids(account, list_fitids_func)
    _record_timing(timings, "fitids_ms", start)

    start = perf_counter()
    envelopes_all = list_envelopes_func()
    _record_timing(timings, "envelopes_ms", start)

    start = perf_counter()
    balances_json = {}
    if account_envelope_balances_func:
        for (balance_account_id, envelope_id), cents in account_envelope_balances_func().items():
            balances_json.setdefault(str(balance_account_id), {})[str(envelope_id)] = int(cents or 0)
    _record_timing(timings, "balances_ms", start)

    start = perf_counter()
    provenance_imported_row_indexes = import_row_provenance_indexes(
        transactions,
        account_id,
        source_bankid=parsed.get("bankid"),
        source_acctid=parsed.get("acctid"),
        list_import_provenance_matches_func=list_import_provenance_matches_func,
    )
    _record_timing(timings, "provenance_dupes_ms", start)

    start = perf_counter()
    existing_imported_row_indexes = set()
    if account_id is not None and list_transactions_func and get_transaction_func:
        normalized_import_rows = [dict(row, index=idx) for idx, row in enumerate(transactions or [])]
        existing_imported_row_indexes = already_imported_transfer_match_indexes(
            normalized_import_rows,
            account_id,
            list_transactions_func=list_transactions_func,
            get_transaction_func=get_transaction_func,
        )
        existing_imported_row_indexes -= provenance_imported_row_indexes
    _record_timing(timings, "transfer_dupes_ms", start)

    initial_row_states = build_import_row_states(
        transactions,
        existing_fitids,
        existing_imported_row_indexes,
        provenance_imported_row_indexes=provenance_imported_row_indexes,
    )

    start = perf_counter()
    if account_id is not None:
        try:
            rule_prefills_payload = rule_prefills_func(
                transactions,
                account_id,
                get_transaction_func=get_transaction_func,
                existing_fitids=existing_fitids,
                row_states=initial_row_states,
            )
        except TypeError:
            rule_prefills_payload = rule_prefills_func(transactions, account_id)
    else:
        rule_prefills_payload = {
            "import_prefills": [],
            "payee_prefills": [],
        }
    rule_import_prefills = list((rule_prefills_payload or {}).get("import_prefills") or [])
    rule_payee_prefills = list((rule_prefills_payload or {}).get("payee_prefills") or [])
    _record_timing(timings, "rule_prefills_ms", start)

    start = perf_counter()
    try:
        learned_import_prefills = import_prefills_func(transactions, account_id, existing_fitids, row_states=initial_row_states)
    except TypeError:
        learned_import_prefills = import_prefills_func(transactions, account_id, existing_fitids)
    rule_import_indexes = {_safe_row_index(item) for item in rule_import_prefills}
    import_prefills = list(rule_import_prefills)
    import_prefills.extend(
        item for item in learned_import_prefills or []
        if _safe_row_index(item) not in rule_import_indexes
    )
    _record_timing(timings, "prefills_ms", start)

    start = perf_counter()
    rule_payee_indexes = {_safe_row_index(item) for item in rule_payee_prefills}
    payee_prefills = list(rule_payee_prefills)
    learned_payee_prefills = payee_prefills_func(transactions, account_id) if account_id is not None else []
    payee_prefills.extend(
        item for item in learned_payee_prefills or []
        if _safe_row_index(item) not in rule_payee_indexes
    )
    _record_timing(timings, "payee_prefills_ms", start)

    row_states = build_import_row_states(
        transactions,
        existing_fitids,
        existing_imported_row_indexes,
        import_prefills,
        provenance_imported_row_indexes=provenance_imported_row_indexes,
    )
    draft_identity = build_import_draft_identity(parsed, account_id)
    row_fingerprints_by_index = {
        int(item["row_index"]): item["fingerprint"]
        for item in draft_identity.get("row_fingerprints", [])
    }
    for state in row_states:
        state["draft_row_fingerprint"] = row_fingerprints_by_index.get(int(state["row_index"]), "")
    row_states_by_index = {state["row_index"]: state for state in row_states}

    import_review_draft = None
    if account_id is not None and get_import_review_draft_func:
        if cleanup_import_review_drafts_func:
            cleanup_import_review_drafts_func()
        import_review_draft = draft_public_metadata(
            get_import_review_draft_func(draft_identity["fingerprint"], account_id)
        )

    import_source_token = ""
    if account_id is not None and create_import_review_source_func:
        if cleanup_import_review_sources_func:
            cleanup_import_review_sources_func()
        source = create_import_review_source_func(
            account_id=account_id,
            source_bankid=parsed.get("bankid"),
            source_acctid=parsed.get("acctid"),
            file_hash=parsed.get("file_hash"),
            source_type=_parsed_source_type(parsed) or "unknown",
            source_filename=_parsed_source_filename(parsed) or None,
        )
        import_source_token = str((source or {}).get("token") or "")

    if timings is not None:
        timings["transaction_count"] = len(transactions or [])
        timings["existing_fitid_count"] = len(existing_fitids)
        timings["existing_transfer_dupe_count"] = len(existing_imported_row_indexes)
        timings["existing_provenance_dupe_count"] = len(provenance_imported_row_indexes)
        timings["prediction_eligible_count"] = sum(
            1
            for row in transactions or []
            if not ((row.get("fitid") or "").strip() and (row.get("fitid") or "").strip() in existing_fitids)
        )
        timings["prefill_count"] = sum(1 for item in import_prefills or [] if item.get("prefill"))

    csv_polarity = None
    if _parsed_source_type(parsed) == "csv" and parsed.get("_csv_upload_token"):
        current_polarity = normalize_csv_polarity(parsed.get("_csv_polarity"))
        next_polarity = CSV_POLARITY_NORMAL if current_polarity == CSV_POLARITY_INVERTED else CSV_POLARITY_INVERTED
        csv_polarity = {
            "enabled": True,
            "current": current_polarity,
            "next": next_polarity,
            "suggested": parsed.get("_csv_polarity_suggestion"),
            "upload_token": parsed.get("_csv_upload_token"),
            "mapping": parsed.get("_csv_mapping") or {},
        }

    return {
        "parsed": parsed,
        "accounts": accounts,
        "acct": account,
        "envelopes_all": envelopes_all,
        "balances_json": balances_json,
        "import_prefills": import_prefills,
        "payee_prefills": payee_prefills,
        "import_row_states": row_states,
        "import_row_states_by_index": row_states_by_index,
        "existing_fitids": existing_fitids,
        "existing_imported_row_indexes": existing_imported_row_indexes,
        "provenance_imported_row_indexes": provenance_imported_row_indexes,
        "show_missing_fitid_badges": _parsed_source_type(parsed) != "csv",
        "import_draft_identity": draft_identity,
        "import_review_draft": import_review_draft,
        "import_source_token": import_source_token,
        "csv_polarity": csv_polarity,
    }


def manual_candidate_date_from(days: int | None, *, now: datetime | None = None) -> str:
    days = days or 3650
    now = now or datetime.utcnow()
    return (now - timedelta(days=days)).date().isoformat()


def manual_import_candidate_item(
    row: dict,
    account_id: int,
    get_transaction_func,
    current_import_fitids: set[str] | None = None,
    existing_fitids: set[str] | None = None,
) -> dict | None:
    if row.get("ignore_match"):
        return None

    transaction_type = (row.get("ttype") or "").lower()
    if transaction_type == "allocation":
        return None

    paired_other = None
    if transaction_type.startswith("transfer"):
        pair_id = row.get("xfer_pair_id")
        if pair_id:
            paired_other = get_transaction_func(int(pair_id))
            if not paired_other or int(paired_other.get("account_id", -1)) == int(account_id):
                return None

    fitid = str(row.get("fitid") or "").strip()
    current_import_fitids = current_import_fitids or set()
    existing_fitids = existing_fitids or set()

    # If the candidate already carries a FITID from the statement currently
    # being reviewed, it has already been matched/imported for this account.
    # Hide it instead of offering it as a fresh manual candidate.
    if fitid and fitid in current_import_fitids:
        return None

    # If this account has already imported/matched the candidate FITID in an
    # earlier statement, do not offer that transaction as a fresh manual match.
    if fitid and fitid in existing_fitids:
        return None

    item = {
        "id": row["id"],
        "posted_at": row["posted_at"],
        "amount_cents": int(row["amount_cents"]),
        "payee": row.get("payee"),
        "memo": row.get("memo"),
        "ttype": transaction_type or None,
        "import_validated": bool(row.get("import_validated")),
    }
    if paired_other is not None:
        item["xfer_pair_id"] = row.get("xfer_pair_id")
        item["paired_account_id"] = paired_other.get("account_id")
    return item


def _date_from_iso(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _normalized_match_text(*values: str | None) -> str:
    text = " ".join(str(value or "") for value in values)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    tokens = [
        token
        for token in text.split()
        if len(token) > 1 and token not in {"the", "and", "inc", "llc", "co"}
    ]
    return " ".join(tokens)


def _text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    sequence = SequenceMatcher(None, left, right).ratio()
    return max(overlap, sequence)


_TRANSFER_PAYMENT_TOKENS = {
    "transfer",
    "payment",
    "pmt",
    "autopay",
    "ach",
    "online",
    "web",
    "billpay",
    "bill",
}

_COUNTERPARTY_GENERIC_TOKENS = {
    "account",
    "acct",
    "bank",
    "checking",
    "savings",
    "withdrawal",
    "deposit",
    "from",
    "to",
}


def _is_cross_account_transfer_candidate(row: dict) -> bool:
    return (row.get("ttype") or "").lower().startswith("transfer") and bool(row.get("xfer_pair_id"))


def _looks_like_transfer_payment_text(text: str) -> bool:
    tokens = set((text or "").split())
    return bool(tokens & _TRANSFER_PAYMENT_TOKENS)


def _counterparty_digit_tokens(text: str) -> set[str]:
    return {token for token in (text or "").split() if token.isdigit() and len(token) >= 4}


def _counterparty_name_tokens(text: str) -> set[str]:
    return {
        token
        for token in (text or "").split()
        if len(token) > 2 and token not in _COUNTERPARTY_GENERIC_TOKENS
    }


def _looks_like_transfer_counterparty_text(import_text: str, manual_row: dict) -> bool:
    """Return true when import text identifies the transfer counterparty account.

    Some CSV exports describe transfers as just the external account, e.g.
    "Example Bank (Account ****0202)".  That is strong evidence when the existing
    transfer leg's payee is "Example Bank - 0202", even though it lacks payment-ish
    words like "transfer" or "web".
    """
    counterparty_text = _normalized_match_text(manual_row.get("payee"))
    if not import_text or not counterparty_text:
        return False

    import_digits = _counterparty_digit_tokens(import_text)
    counterparty_digits = _counterparty_digit_tokens(counterparty_text)
    if import_digits and counterparty_digits and import_digits & counterparty_digits:
        return True

    import_tokens = _counterparty_name_tokens(import_text)
    counterparty_tokens = _counterparty_name_tokens(counterparty_text)
    if not import_tokens or not counterparty_tokens:
        return False
    if not (import_tokens & counterparty_tokens):
        return False
    return _text_similarity(import_text, counterparty_text) >= 0.45


def import_match_score(import_row: dict, manual_row: dict, *, date_window_days: int = 7) -> float | None:
    import_amount = import_transaction_amount_cents(import_row)
    manual_amount = int(manual_row.get("amount_cents") or 0)
    if import_amount == 0 or manual_amount == 0:
        return None
    if (import_amount < 0) != (manual_amount < 0):
        return None

    import_date = _date_from_iso(import_row.get("posted_at"))
    manual_date = _date_from_iso(manual_row.get("posted_at"))
    if import_date is None or manual_date is None:
        return None
    date_distance = abs((import_date - manual_date).days)
    if date_distance > date_window_days:
        return None

    amount_distance = abs(abs(import_amount) - abs(manual_amount))
    if amount_distance != 0:
        return None

    import_text = _normalized_match_text(import_row.get("payee"), import_row.get("memo"))
    manual_text = _normalized_match_text(manual_row.get("payee"), manual_row.get("memo"))
    similarity = _text_similarity(import_text, manual_text)

    if _is_cross_account_transfer_candidate(manual_row) and date_distance <= 3:
        if _looks_like_transfer_payment_text(import_text):
            return 92.0 + max(0, 3 - date_distance) * 2.0 + similarity
        if _looks_like_transfer_counterparty_text(import_text, manual_row):
            return 94.0 + max(0, 3 - date_distance) * 2.0 + similarity

    if similarity < 0.35:
        return None

    amount_score = 70.0
    date_score = max(0.0, 20.0 - (date_distance * 2.5))
    text_score = similarity * 10.0
    return amount_score + date_score + text_score



def _parsed_import_row_index(row: dict) -> int | None:
    try:
        return int(row.get("index"))
    except (TypeError, ValueError):
        return None


def _import_date_bounds(import_rows: list[dict], *, window_days: int = 7) -> tuple[str | None, str | None]:
    dates = [d for d in (_date_from_iso(row.get("posted_at")) for row in import_rows or []) if d is not None]
    if not dates:
        return None, None
    return (min(dates) - timedelta(days=window_days)).isoformat(), (max(dates) + timedelta(days=window_days)).isoformat()


def _already_imported_transfer_match_indexes(
    import_rows: list[dict],
    manual_rows: list[dict],
    *,
    score_func=import_match_score,
) -> set[int]:
    """Return import row indexes that appear to already be committed transfer legs.

    This intentionally stays narrower than generic manual-match suggestions. Exact
    FITID duplicates are handled elsewhere; this catches legacy/manual credit-card
    payment transfers where both account legs already exist but the statement FITID
    differs from the app-created transfer FITID.
    """
    scored_by_import: dict[int, list[tuple[int, float]]] = {}
    for manual in manual_rows or []:
        # Old transfer/source phrase heuristics are useful for suggesting a
        # match to an unvalidated row, but they must not be authoritative for
        # "already imported" state.  Only account-side validation evidence can
        # make a ledger row suppress a statement row here.
        if not manual.get("import_validated"):
            continue
        if not _is_cross_account_transfer_candidate(manual):
            continue
        manual_id = int(manual.get("id") or 0)
        if not manual_id:
            continue
        for import_row in import_rows or []:
            import_index = _parsed_import_row_index(import_row)
            if import_index is None:
                continue
            score = score_func(import_row, manual)
            if score is None or score < 92.0:
                continue
            scored_by_import.setdefault(import_index, []).append((manual_id, float(score)))

    matches: set[int] = set()
    manual_to_imports: dict[int, list[int]] = {}
    best_by_import: dict[int, tuple[int, float]] = {}
    for import_index, scored in scored_by_import.items():
        scored.sort(key=lambda item: item[1], reverse=True)
        if len(scored) > 1 and scored[0][1] - scored[1][1] < 6.0:
            continue
        manual_id, score = scored[0]
        best_by_import[import_index] = (manual_id, score)
        manual_to_imports.setdefault(manual_id, []).append(import_index)

    for import_index, (manual_id, _score) in best_by_import.items():
        if len(manual_to_imports.get(manual_id, [])) == 1:
            matches.add(import_index)
    return matches


def already_imported_transfer_match_indexes(
    import_rows: list[dict],
    account_id: int | None,
    *,
    list_transactions_func,
    get_transaction_func,
) -> set[int]:
    if not account_id or not import_rows:
        return set()

    date_from, date_to = _import_date_bounds(import_rows)
    rows, _total = list_transactions_func(
        limit=5000,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        ttypes=("transfer",),
    )
    candidates = manual_import_candidate_items(
        rows,
        account_id,
        get_transaction_func,
        current_import_fitids=set(),
        existing_fitids=set(),
    )
    return _already_imported_transfer_match_indexes(import_rows, candidates)

def _eligible_import_match_rows(import_rows: list[dict], existing_fitids: set[str]) -> list[dict]:
    eligible: list[dict] = []
    indexed_rows: list[tuple[int, dict]] = []
    for row in import_rows or []:
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        indexed_rows.append((index, row))

    compact_rows = [row for _index, row in indexed_rows]
    compact_states = build_import_row_states(compact_rows, existing_fitids)
    for (index, row), state in zip(indexed_rows, compact_states):
        if not state.get("manual_match_eligible"):
            continue
        eligible.append({
            "index": index,
            "posted_at": row.get("posted_at"),
            "amount_cents": state["amount_cents"],
            "payee": row.get("payee"),
            "memo": row.get("memo"),
            "fitid": state["fitid"] or None,
        })
    return eligible


def auto_match_suggestions(
    import_rows: list[dict],
    manual_rows: list[dict],
    *,
    existing_fitids: set[str] | None = None,
    score_func=import_match_score,
) -> dict[int, int]:
    imports = _eligible_import_match_rows(import_rows, existing_fitids or set())
    tentative: dict[int, tuple[int, float]] = {}

    for manual in manual_rows or []:
        manual_id = int(manual.get("id") or 0)
        if not manual_id:
            continue
        scored: list[tuple[int, float]] = []
        for import_row in imports:
            score = score_func(import_row, manual)
            if score is not None:
                scored.append((int(import_row["index"]), float(score)))
        if not scored:
            continue

        scored.sort(key=lambda item: item[1], reverse=True)
        best_index, best_score = scored[0]
        if best_score < 92.0:
            continue
        if len(scored) > 1 and best_score - scored[1][1] < 6.0:
            continue
        tentative[manual_id] = (best_index, best_score)

    import_to_manuals: dict[int, list[int]] = {}
    for manual_id, (import_index, _score) in tentative.items():
        import_to_manuals.setdefault(import_index, []).append(manual_id)

    suggestions: dict[int, int] = {}
    for manual_id, (import_index, _score) in tentative.items():
        if len(import_to_manuals[import_index]) == 1:
            suggestions[manual_id] = import_index
    return suggestions


def manual_import_rows_from_request_args(args) -> list[dict]:
    raw = args.get("imports") if args else None
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def manual_import_candidate_date_window(import_rows: list[dict], *, window_days: int = 7) -> tuple[date, date] | None:
    dates = [d for d in (_date_from_iso(row.get("posted_at")) for row in import_rows or []) if d is not None]
    if not dates:
        return None
    return min(dates) - timedelta(days=window_days), max(dates) + timedelta(days=window_days)


def manual_import_candidate_in_date_window(item: dict, date_window: tuple[date, date] | None) -> bool:
    if date_window is None:
        return True
    item_date = _date_from_iso(item.get("posted_at"))
    if item_date is None:
        return False
    start, end = date_window
    return start <= item_date <= end


def manual_import_candidate_items(
    rows,
    account_id: int,
    get_transaction_func,
    current_import_fitids: set[str] | None = None,
    existing_fitids: set[str] | None = None,
    excluded_transaction_ids: set[int] | None = None,
) -> list[dict]:
    excluded_transaction_ids = excluded_transaction_ids or set()
    items: list[dict] = []
    for row in rows or []:
        try:
            row_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            row_id = 0
        if row_id and row_id in excluded_transaction_ids:
            continue
        item = manual_import_candidate_item(
            row,
            account_id,
            get_transaction_func,
            current_import_fitids=current_import_fitids,
            existing_fitids=existing_fitids,
        )
        if item is not None:
            items.append(item)
    return items


def manual_import_candidates_response(
    account_id: int | None,
    days: int | None,
    *,
    list_transactions_func,
    get_transaction_func,
    import_rows: list[dict] | None = None,
    existing_fitids: set[str] | None = None,
    excluded_transaction_ids: set[int] | None = None,
) -> dict:
    if not account_id:
        return {"items": [], "overflow_items": []}

    date_from = manual_candidate_date_from(days)
    rows, _ = list_transactions_func(limit=1000, account_id=account_id, date_from=date_from)
    current_import_fitids = {
        str(row.get("fitid") or "").strip()
        for row in (import_rows or [])
        if str(row.get("fitid") or "").strip()
    }
    items = manual_import_candidate_items(
        rows,
        account_id,
        get_transaction_func,
        current_import_fitids=current_import_fitids,
        existing_fitids=existing_fitids,
        excluded_transaction_ids=excluded_transaction_ids,
    )
    suggestions = auto_match_suggestions(import_rows or [], items, existing_fitids=existing_fitids)
    for item in items:
        item["suggested_import_index"] = suggestions.get(int(item["id"]))

    date_window = manual_import_candidate_date_window(import_rows or [])
    if date_window is None:
        return {"items": items, "overflow_items": []}

    default_items: list[dict] = []
    overflow_items: list[dict] = []
    for item in items:
        if manual_import_candidate_in_date_window(item, date_window):
            default_items.append(item)
        else:
            overflow_items.append(item)
    return {"items": default_items, "overflow_items": overflow_items}


def manual_import_candidates_request_response(
    args,
    *,
    list_transactions_func,
    get_transaction_func,
    list_imported_fitid_rows_func=None,
    list_import_matched_transaction_ids_func=None,
) -> dict:
    account_id = args.get("account_id", type=int)
    days = args.get("days", type=int) or 3650
    import_rows = manual_import_rows_from_request_args(args)
    existing_fitids: set[str] = set()
    if account_id and list_imported_fitid_rows_func:
        fitids, _details = imported_fitid_details(list_imported_fitid_rows_func(account_id))
        existing_fitids = set(fitids)
    excluded_transaction_ids: set[int] = set()
    if account_id and list_import_matched_transaction_ids_func:
        excluded_transaction_ids = set(list_import_matched_transaction_ids_func(account_id) or set())
    return manual_import_candidates_response(
        account_id,
        days,
        list_transactions_func=list_transactions_func,
        get_transaction_func=get_transaction_func,
        import_rows=import_rows,
        existing_fitids=existing_fitids,
        excluded_transaction_ids=excluded_transaction_ids,
    )


def imported_fitid_details(rows) -> tuple[list[str], dict[str, dict]]:
    fitids: list[str] = []
    details: dict[str, dict] = {}

    for row in rows or []:
        fitid = str(row["fitid"]).strip()
        if not fitid:
            continue
        fitids.append(fitid)
        details[fitid] = {
            "payee": row["payee"] or "",
            "memo": row["memo"] or "",
        }

    return fitids, details


def imported_fitids_response(
    account_id: int | None,
    *,
    list_imported_fitid_rows_func,
    import_rows: list[dict] | None = None,
    list_transactions_func=None,
    get_transaction_func=None,
    list_import_provenance_matches_func=None,
    source_bankid: str | None = None,
    source_acctid: str | None = None,
) -> dict:
    if not account_id:
        return {"fitids": [], "details": {}, "row_indexes": []}

    fitids, details = imported_fitid_details(list_imported_fitid_rows_func(account_id))
    row_indexes: list[int] = []
    provenance_indexes = import_row_provenance_indexes(
        import_rows or [],
        account_id,
        source_bankid=source_bankid,
        source_acctid=source_acctid,
        list_import_provenance_matches_func=list_import_provenance_matches_func,
    )
    if import_rows and list_transactions_func and get_transaction_func:
        row_indexes = sorted((already_imported_transfer_match_indexes(
            import_rows,
            account_id,
            list_transactions_func=list_transactions_func,
            get_transaction_func=get_transaction_func,
        ) - provenance_indexes) | provenance_indexes)
    else:
        row_indexes = sorted(provenance_indexes)
    return {"fitids": fitids, "details": details, "row_indexes": row_indexes}


def imported_fitids_request_response(
    args,
    *,
    list_imported_fitid_rows_func,
    import_rows: list[dict] | None = None,
    list_transactions_func=None,
    get_transaction_func=None,
    list_import_provenance_matches_func=None,
) -> dict:
    account_id = args.get("account_id", type=int)
    return imported_fitids_response(
        account_id,
        list_imported_fitid_rows_func=list_imported_fitid_rows_func,
        import_rows=import_rows,
        list_transactions_func=list_transactions_func,
        get_transaction_func=get_transaction_func,
        list_import_provenance_matches_func=list_import_provenance_matches_func,
        source_bankid=args.get("source_bankid"),
        source_acctid=args.get("source_acctid"),
    )


def combine_qfx_payee_and_memo(name: str | None, memo: str | None) -> tuple[str | None, str | None]:
    """Normalize QFX NAME/MEMO into the app's payee/memo fields.

    QFX often carries useful description detail in MEMO. Historically the
    importer preserved that detail in payee and blanked memo, but it appended
    unrelated NAME/MEMO values without a separator. Keep the behavior of
    preserving detail in payee, but avoid unreadable concatenation.
    """
    payee = (name or "").strip() or None
    memo = (memo or "").strip() or None
    if not memo:
        return payee, None
    if not payee:
        return memo, None

    payee_low = payee.lower()
    memo_low = memo.lower()
    if memo_low == payee_low:
        return payee, None
    if memo_low.startswith(payee_low):
        return memo, None
    if payee_low.startswith(memo_low):
        return payee, None
    return f"{payee} - {memo}", None

# --- Parsers ---------------------------------------------------------------

def parse_qfx(path: Path) -> dict:
    """
    Minimal QFX/OFX parser pulling STMTTRN blocks: DTPOSTED, TRNAMT, NAME, MEMO, FITID.
    Returns: {"bankid": str|None, "acctid": str|None, "transactions": [ {...} ]}
    """
    text = path.read_text(encoding="utf-8", errors="ignore")

    # BANKID / ACCTID (if present)
    bankid = _tag_value(text, "BANKID")
    acctid = _tag_value(text, "ACCTID")

    txs = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.S | re.I):
        dtposted = _tag_value(block, "DTPOSTED")
        trnamt = _tag_value(block, "TRNAMT")
        name = _tag_value(block, "NAME")
        memo = _tag_value(block, "MEMO")
        fitid = _tag_value(block, "FITID")

        posted_at = parse_ofx_datetime(dtposted)
        amount_cents = to_cents_from_str_amount(trnamt)
        payee, memo = combine_qfx_payee_and_memo(name, memo)

        txs.append({
            "posted_at": posted_at,
            "amount_cents": amount_cents,
            "payee": payee,
            "memo": memo,
            "fitid": (fitid or "").strip() or None,
        })

    return {"bankid": bankid, "acctid": acctid, "transactions": txs}

def _tag_value(s: str, tag: str) -> str | None:
    # Match both <TAG>value and <TAG>value</TAG> styles
    m = re.search(rf"<{tag}>([^<\r\n]+)", s, re.I)
    return m.group(1).strip() if m else None

def parse_csv(path: Path, *, mapping: dict[str, str | None] | None = None) -> dict:
    """
    Generic CSV parser. Detects common column names and requires explicit user
    mapping when vital fields are missing or ambiguous.
    """
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as f:
        rdr = csv.DictReader(f)
        headers = list(rdr.fieldnames or [])
        rows = list(rdr)

    if not headers:
        raise ValueError("CSV file has no header row.")
    if mapping is None:
        mapping, warnings = detect_csv_column_mapping(headers, rows[:10])
        if warnings:
            raise CsvColumnMappingRequired(csv_mapping_prompt_payload(path, message="We need you to confirm the CSV columns before import review."))
    else:
        mapping = validate_csv_column_mapping(headers, rows[:10], mapping)

    txs = []
    for row in rows:
        date_s = _csv_row_value(row, mapping.get("date"))
        payee = _csv_row_value(row, mapping.get("payee"))
        memo = _csv_row_value(row, mapping.get("memo"))
        fitid = _csv_row_value(row, mapping.get("fitid"))

        if mapping.get("amount"):
            amount_cents = to_cents_from_str_amount(_csv_row_value(row, mapping.get("amount")))
        else:
            debit_cents = abs(_parse_csv_money(_csv_row_value(row, mapping.get("debit"))) or 0)
            credit_cents = abs(_parse_csv_money(_csv_row_value(row, mapping.get("credit"))) or 0)
            amount_cents = credit_cents - debit_cents

        txs.append({
            "posted_at": normalize_date(date_s),
            "amount_cents": amount_cents,
            "payee": (payee or "").strip() or None,
            "memo": (memo or "").strip() or None,
            "fitid": (fitid or "").strip() or None,
        })

    return {"bankid": None, "acctid": None, "transactions": txs, "_csv_mapping": dict(mapping)}


def _csv_row_value(row: dict, header: str | None) -> str | None:
    if not header:
        return None
    return row.get(header)


def validate_csv_column_mapping(headers: list[str], sample_rows: list[dict], mapping: dict[str, str | None]) -> dict[str, str | None]:
    cleaned: dict[str, str | None] = {}
    valid_headers = set(headers)
    for key in ("date", "amount", "debit", "credit", "payee", "memo", "fitid"):
        value = (mapping.get(key) or "").strip() if isinstance(mapping.get(key), str) else None
        cleaned[key] = value if value in valid_headers else None

    errors: list[str] = []
    if not cleaned.get("date"):
        errors.append("Date column is required.")
    elif not any(_normalize_date_or_none(_csv_row_value(row, cleaned["date"])) for row in sample_rows):
        errors.append("Selected date column does not contain parseable dates.")

    if cleaned.get("amount"):
        if not any(_parse_csv_money(_csv_row_value(row, cleaned["amount"])) is not None for row in sample_rows):
            errors.append("Selected amount column does not contain parseable amounts.")
        cleaned["debit"] = None
        cleaned["credit"] = None
    elif cleaned.get("debit") and cleaned.get("credit"):
        if not any(
            _parse_csv_money(_csv_row_value(row, cleaned["debit"])) is not None
            or _parse_csv_money(_csv_row_value(row, cleaned["credit"])) is not None
            for row in sample_rows
        ):
            errors.append("Selected debit/credit columns do not contain parseable amounts.")
    else:
        errors.append("Choose a signed amount column, or both debit and credit columns.")

    if not cleaned.get("payee"):
        errors.append("Payee / description column is required.")
    elif not any((_csv_row_value(row, cleaned["payee"]) or "").strip() for row in sample_rows):
        errors.append("Selected payee / description column is blank in the sample rows.")

    if errors:
        raise ValueError(" ".join(errors))
    return cleaned

def normalize_date(s: str | None) -> str:
    s = (s or "").strip()
    # Try a few common formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    raise ValueError(f"Invalid CSV date: {s or '(blank)'}")

def parse_statement_upload(data: bytes, filename: str, *, csv_mapping: dict[str, str | None] | None = None) -> dict:
    file_hash = hashlib.sha256(data).hexdigest()
    suffix = Path(filename).suffix.lower()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        parsed = parse_statement_file(tmp_path, csv_mapping=csv_mapping)
    finally:
        if tmp_path:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    parsed["file_hash"] = file_hash
    parsed["_source_type"] = "csv" if suffix == ".csv" else ("qfx" if suffix in (".qfx", ".ofx") else suffix.lstrip("."))
    if suffix == ".csv":
        parsed["_source_filename"] = Path(filename).name
    return parsed


def parse_statement_file(path: Path, *, csv_mapping: dict[str, str | None] | None = None) -> dict:
    suffix = path.suffix.lower()
    if suffix in (".qfx", ".ofx"):
        return parse_qfx(path)
    elif suffix in (".csv",):
        return parse_csv(path, mapping=csv_mapping)
    else:
        # Try OFX-style tags anyway
        txt = path.read_text(encoding="utf-8", errors="ignore")
        if "<OFX>" in txt.upper():
            return parse_qfx(path)
        raise ValueError(f"Unsupported file type: {suffix}")
