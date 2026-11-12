"""Normalization of FAA identifiers and fields."""

from __future__ import annotations

import re
from datetime import date

# FAA format: 1-5 characters, first 1-9, no I or O, at most two trailing letters.
_NNUM_RE = re.compile(r"^[1-9](?:[0-9]{0,4}|[0-9]{0,3}[A-HJ-NP-Z]|[0-9]{0,2}[A-HJ-NP-Z]{2})$")
_STRIP_RE = re.compile(r"[\s\-]")
SERIAL_PLACEHOLDERS = frozenset({"", "0", "00", "000", "0000", "NONE", "UNKNOWN", "UNK", "NA", "N/A", "TBD"})


class InvalidNNumber(ValueError):
    pass


def normalize_n_number(value: str) -> str:
    """Return the N-number with its `N` prefix (e.g. `N123AB`), or raise InvalidNNumber."""
    s = _STRIP_RE.sub("", value or "").upper()
    if s.startswith("N"):
        s = s[1:]
    if not _NNUM_RE.match(s):
        raise InvalidNNumber(f"not a valid U.S. N-number: {value!r}")
    return "N" + s


def try_normalize_n_number(value: str) -> str | None:
    try:
        return normalize_n_number(value)
    except InvalidNNumber:
        return None


def normalize_serial(value: str) -> str:
    """Uppercase and remove spaces and hyphens. Leading zeros are kept (stripping them would
    merge 1,149 distinct keys in the Phase 0 snapshot)."""
    return _STRIP_RE.sub("", value or "").upper()


def is_placeholder_serial(norm: str) -> bool:
    return norm in SERIAL_PLACEHOLDERS


def faa_date(value: str) -> str | None:
    """`YYYYMMDD` -> ISO date string; empty or invalid -> None."""
    v = (value or "").strip()
    if len(v) != 8 or not v.isdigit():
        return None
    try:
        return date(int(v[:4]), int(v[4:6]), int(v[6:])).isoformat()
    except ValueError:
        return None


def clean(value: str | None) -> str | None:
    """Strip padding; empty -> None."""
    if value is None:
        return None
    v = value.strip()
    return v or None


def to_int(value: str | None) -> int | None:
    v = clean(value)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None
