from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from .transaction_text_profile_service import (
    ACCOUNT_TYPE_HINTS,
    canonicalize_transaction_text,
)


TYPE_ALIASES_BY_TOKEN = {
    "bank": ("bank",),
    "cash": ("cash",),
    "card": ("card", "cc", "credit"),
    "cc": ("card", "cc", "credit"),
    "checking": ("checking", "chk"),
    "chk": ("checking", "chk"),
    "credit": ("card", "cc", "credit"),
    "investment": ("investment", "invest", "brokerage"),
    "invest": ("investment", "invest", "brokerage"),
    "brokerage": ("investment", "invest", "brokerage"),
    "loan": ("loan",),
    "saving": ("savings", "saving", "sav"),
    "savings": ("savings", "saving", "sav"),
    "sav": ("savings", "saving", "sav"),
}

NON_INSTITUTION_TOKENS = frozenset({
    "acct",
    "account",
    "bank",
    "cash",
    "card",
    "cc",
    "checking",
    "chk",
    "credit",
    "investment",
    "invest",
    "loan",
    "saving",
    "savings",
    "sav",
})


@dataclass(frozen=True)
class AccountMatchProfile:
    account_id: int | None
    name: str
    acct_key: str | None
    bankid: str | None
    acctid: str | None
    account_type: str | None
    name_tokens: tuple[str, ...]
    acctid_suffixes: tuple[str, ...]
    label_suffixes: tuple[str, ...]
    all_suffixes: tuple[str, ...]
    type_aliases: tuple[str, ...]
    institution_tokens: tuple[str, ...]


def build_account_match_profile(account: dict[str, Any]) -> AccountMatchProfile:
    name = str(account.get("name") or "").strip()
    acct_key = _optional_str(account.get("acct_key"))
    bankid = _optional_str(account.get("bankid"))
    acctid = _optional_str(account.get("acctid"))
    account_type = _optional_str(account.get("account_type"))

    name_tokens = _tokens(name)
    label_suffixes = _label_suffixes(name, acct_key)
    acctid_suffixes = _identifier_suffixes(acctid)
    all_suffixes = _unique((*acctid_suffixes, *label_suffixes))
    type_aliases = _type_aliases(account_type, name, acct_key)
    institution_tokens = _institution_tokens(name_tokens, type_aliases)

    return AccountMatchProfile(
        account_id=_optional_int(account.get("id")),
        name=name,
        acct_key=acct_key,
        bankid=bankid,
        acctid=acctid,
        account_type=account_type,
        name_tokens=name_tokens,
        acctid_suffixes=acctid_suffixes,
        label_suffixes=label_suffixes,
        all_suffixes=all_suffixes,
        type_aliases=type_aliases,
        institution_tokens=institution_tokens,
    )


def build_account_match_profiles(accounts: Iterable[dict[str, Any]]) -> tuple[AccountMatchProfile, ...]:
    return tuple(build_account_match_profile(account) for account in accounts)


def find_profiles_by_suffix(
    profiles: Iterable[AccountMatchProfile],
    suffix: str,
) -> tuple[AccountMatchProfile, ...]:
    normalized = re.sub(r"\D+", "", str(suffix or ""))
    if not normalized:
        return ()
    return tuple(profile for profile in profiles if normalized in profile.all_suffixes)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _tokens(value: Any) -> tuple[str, ...]:
    return _unique(
        token
        for token in canonicalize_transaction_text(value).split()
        if token != "#"
    )


def _identifier_suffixes(*values: str | None) -> tuple[str, ...]:
    suffixes: list[str] = []
    for value in values:
        digits = re.sub(r"\D+", "", value or "")
        if len(digits) >= 3:
            suffixes.append(digits[-4:] if len(digits) >= 4 else digits)
    return _unique(suffixes)


def _label_suffixes(*values: str | None) -> tuple[str, ...]:
    suffixes: list[str] = []
    for value in values:
        for match in re.finditer(r"\b\d{3,6}\b", value or ""):
            suffixes.append(match.group(0))
    return _unique(suffixes)


def _type_aliases(*values: str | None) -> tuple[str, ...]:
    aliases: list[str] = []
    for value in values:
        for token in _tokens(value):
            if token in TYPE_ALIASES_BY_TOKEN:
                aliases.extend(TYPE_ALIASES_BY_TOKEN[token])
            elif token in ACCOUNT_TYPE_HINTS and token not in {"acct", "account"}:
                aliases.append(token)
    return _unique(aliases)


def _institution_tokens(
    name_tokens: tuple[str, ...],
    type_aliases: tuple[str, ...],
) -> tuple[str, ...]:
    excluded = set(type_aliases) | NON_INSTITUTION_TOKENS
    return _unique(
        token
        for token in name_tokens
        if not token.isdigit() and token not in excluded
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return tuple(output)
