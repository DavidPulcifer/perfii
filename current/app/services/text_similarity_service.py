from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - exercised only when optional dependency is absent.
    fuzz = None


def token_set_similarity(left: str | Iterable[str], right: str | Iterable[str]) -> float:
    left_text = _text(left)
    right_text = _text(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    if fuzz is not None:
        return float(fuzz.token_set_ratio(left_text, right_text)) / 100.0
    left_tokens = set(left_text.split())
    right_tokens = set(right_text.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    return max(overlap, SequenceMatcher(None, left_text, right_text).ratio())


def partial_token_similarity(left: str | Iterable[str], right: str | Iterable[str]) -> float:
    left_text = _text(left)
    right_text = _text(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    if fuzz is not None:
        return float(fuzz.partial_token_set_ratio(left_text, right_text)) / 100.0
    return token_set_similarity(left_text, right_text)


def text_similarity(left: str, right: str) -> float:
    return max(token_set_similarity(left, right), partial_token_similarity(left, right))


@lru_cache(maxsize=65536)
def cached_text_similarity(left: str, right: str) -> float:
    return text_similarity(left, right)


def similarity_backend() -> str:
    return "rapidfuzz" if fuzz is not None else "stdlib"


def _text(value: str | Iterable[str]) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return " ".join(str(token) for token in value if token)
