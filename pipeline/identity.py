from __future__ import annotations

import re
import unicodedata

_COUNTRY_SUFFIX_RE = re.compile(r"\s*\(([A-Z]{2,3})\)\s*$")


def normalise_horse_name(name: object) -> str:
    """Stable matching key for horse names across results and racecards.

    Removes country suffixes like "(IRE)", punctuation differences and repeated spaces.
    Horse IDs are still preferred, but this rescues profiles when one file has a missing
    or inconsistent ID.
    """
    text = str(name or "").strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _COUNTRY_SUFFIX_RE.sub("", text.upper()).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def display_horse_name(name: object) -> str:
    return str(name or "").strip()
