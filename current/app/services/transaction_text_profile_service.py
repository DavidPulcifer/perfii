from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


GENERIC_BANKING_TOKENS = frozenset({
    "ach",
    "auth",
    "card",
    "check",
    "debit",
    "deposit",
    "mobile",
    "online",
    "payment",
    "pos",
    "purchase",
    "transfer",
    "web",
    "withdrawal",
    "xfer",
})

ACCOUNT_TYPE_HINTS = frozenset({
    "acct",
    "account",
    "card",
    "cc",
    "checking",
    "chk",
    "credit",
    "loan",
    "sav",
    "saving",
    "savings",
})

DIRECTION_TOKENS = frozenset({"to", "from"})
REFERENCE_LABELS = frozenset({
    "auth",
    "authorization",
    "confirmation",
    "id",
    "ref",
    "reference",
    "trace",
    "trans",
    "transaction",
})

WEAK_PROCESSOR_TOKENS = frozenset({
    "paypal",
    "pp",
    "sq",
    "sp",
    "stripe",
    "tst",
})

MERCHANT_DOMAIN_TOKENS = frozenset({
    "com",
    "net",
    "org",
})

STRONG_SINGLE_MERCHANT_TOKENS = frozenset({
    "amazon",
    "costco",
    "doordash",
    "ebay",
    "etsy",
    "hulu",
    "instacart",
    "lyft",
    "netflix",
    "spotify",
    "target",
    "uber",
    "walmart",
})


@dataclass(frozen=True)
class TransactionTextProfile:
    raw_text: str
    canonical_text: str
    merchant_tokens: tuple[str, ...]
    generic_tokens: tuple[str, ...]
    direction: str | None
    account_type_hints: tuple[str, ...]
    account_suffixes: tuple[str, ...]
    reference_numbers: tuple[str, ...]
    noise_tokens: tuple[str, ...]


@dataclass(frozen=True)
class MerchantClusterSignature:
    signature: str
    tokens: tuple[str, ...]
    quality: str
    reason: str


def profile_text_from_row(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("payee", "memo", "name")
        if row.get(key)
    ).strip()


def build_transaction_text_profile(value: Any) -> TransactionTextProfile:
    raw_text = str(value or "").strip()
    canonical_text = canonicalize_transaction_text(raw_text)
    tokens = tuple(canonical_text.split())

    reference_numbers = _reference_numbers(raw_text, tokens)
    account_suffixes = _account_suffixes(raw_text)
    direction = _direction(tokens)
    generic_tokens = _unique(tok for tok in tokens if tok in GENERIC_BANKING_TOKENS)
    account_type_hints = _unique(tok for tok in tokens if tok in ACCOUNT_TYPE_HINTS)
    noise_tokens = _unique(
        tok for tok in tokens
        if tok in GENERIC_BANKING_TOKENS
        or tok in REFERENCE_LABELS
        or _looks_like_dateish_number(tok)
    )
    evidence_numbers = set(reference_numbers) | set(account_suffixes)
    merchant_tokens = _unique(
        tok for tok in tokens
        if tok not in GENERIC_BANKING_TOKENS
        and tok not in ACCOUNT_TYPE_HINTS
        and tok not in REFERENCE_LABELS
        and tok not in DIRECTION_TOKENS
        and tok not in evidence_numbers
        and not _looks_like_dateish_number(tok)
    )

    return TransactionTextProfile(
        raw_text=raw_text,
        canonical_text=canonical_text,
        merchant_tokens=merchant_tokens,
        generic_tokens=generic_tokens,
        direction=direction,
        account_type_hints=account_type_hints,
        account_suffixes=account_suffixes,
        reference_numbers=reference_numbers,
        noise_tokens=noise_tokens,
    )


def build_transaction_text_profile_from_row(row: dict[str, Any]) -> TransactionTextProfile:
    return build_transaction_text_profile(profile_text_from_row(row))


def merchant_cluster_signature(profile: TransactionTextProfile) -> MerchantClusterSignature | None:
    tokens = _unique(
        tok for tok in profile.merchant_tokens
        if _usable_cluster_token(tok)
    )
    if not tokens:
        return None

    non_domain_tokens = tuple(tok for tok in tokens if tok not in MERCHANT_DOMAIN_TOKENS)
    if len(non_domain_tokens) >= 2:
        reason = "multi_token_merchant"
    elif len(non_domain_tokens) == 1 and non_domain_tokens[0] in STRONG_SINGLE_MERCHANT_TOKENS:
        reason = "strong_single_merchant"
    else:
        return None

    signature = " ".join(tokens)
    if not signature:
        return None
    return MerchantClusterSignature(
        signature=signature,
        tokens=tokens,
        quality="strong",
        reason=reason,
    )


def canonicalize_transaction_text(value: Any) -> str:
    text = str(value or "").lower()
    text = text.replace("#", " # ")
    text = re.sub(r"[^a-z0-9#]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _usable_cluster_token(token: str) -> bool:
    token = str(token or "").strip().lower()
    if not token:
        return False
    if token in WEAK_PROCESSOR_TOKENS:
        return False
    if _looks_like_dateish_number(token):
        return False
    if len(token) <= 3 and token not in MERCHANT_DOMAIN_TOKENS:
        return False
    if token.isdigit() and len(token) >= 3:
        return False
    if len(token) >= 6 and any(ch.isalpha() for ch in token) and any(ch.isdigit() for ch in token):
        return False
    return True


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return tuple(output)


def _direction(tokens: tuple[str, ...]) -> str | None:
    for token in tokens:
        if token in DIRECTION_TOKENS:
            return token
    return None


def _account_suffixes(raw_text: str) -> tuple[str, ...]:
    suffixes: list[str] = []
    patterns = [
        r"(?:\.\.\.|xxxx|x{2,}|ending\s+in|acct\s*(?:#|no\.?)?)\s*(?:[-#]\s*)?(\d{3,6})\b",
        r"\b(?:sav|savings|chk|checking|card|loan)\s*(?:\.\.\.|xxxx|x{2,})?\s*(\d{3,6})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE):
            suffixes.append(match.group(1))
    return _unique(suffixes)


def _reference_numbers(raw_text: str, tokens: tuple[str, ...]) -> tuple[str, ...]:
    references: list[str] = []
    label_pattern = r"\b(?:transaction|trans|trace|ref|reference|auth|authorization|confirmation|id)\b\s*(?:(?:number|no\.?)\s*)?[#:]*\s*([a-z0-9-]{4,})"
    for match in re.finditer(label_pattern, raw_text, flags=re.IGNORECASE):
        references.append(_clean_reference(match.group(1)))

    for idx, token in enumerate(tokens[:-1]):
        if token in REFERENCE_LABELS:
            next_token = tokens[idx + 1]
            if len(next_token) >= 4 and any(ch.isdigit() for ch in next_token):
                references.append(_clean_reference(next_token))

    return _unique(ref for ref in references if ref)


def _clean_reference(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _looks_like_dateish_number(token: str) -> bool:
    if not token.isdigit():
        return False
    if len(token) == 8 and token.startswith(("19", "20")):
        return True
    return False
