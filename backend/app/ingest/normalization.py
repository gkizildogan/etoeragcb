from __future__ import annotations

import re
import unicodedata

WHITESPACE_RE = re.compile(r"\s+")
TURKISH_MARKERS = frozenset("çğıöşüÇĞİÖŞÜ")
TURKISH_CASE_MAP = str.maketrans({"I": "\u0131", "\u0130": "i"})


def normalize_lexical(text: str, *, language: str = "auto") -> str:
    """Create a separate search form without modifying the stored source text."""

    normalized = unicodedata.normalize("NFKC", text)
    is_turkish = language == "tr" or (
        language == "auto" and any(character in TURKISH_MARKERS for character in normalized)
    )
    folded = normalized.translate(TURKISH_CASE_MAP).lower() if is_turkish else normalized.casefold()
    return WHITESPACE_RE.sub(" ", folded).strip()
