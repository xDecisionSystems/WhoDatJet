from __future__ import annotations

import pytest

from whodatjet.normalize import (
    InvalidNNumber,
    faa_date,
    is_placeholder_serial,
    normalize_n_number,
    normalize_serial,
)


@pytest.mark.parametrize("raw,expected", [
    ("12345", "N12345"),
    ("N123AB", "N123AB"),
    ("n 123-ab", "N123AB"),
    ("1", "N1"),
    ("1A", "N1A"),
    ("12AB", "N12AB"),
    ("1234Z", "N1234Z"),
    ("  100  ", "N100"),
])
def test_valid_n_numbers(raw, expected):
    assert normalize_n_number(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "N", "0123", "123456", "12I", "12O", "1ABC", "123ABC", "A123", "12A3", "N-",
])
def test_invalid_n_numbers(raw):
    with pytest.raises(InvalidNNumber):
        normalize_n_number(raw)


@pytest.mark.parametrize("raw,expected", [
    ("28-7990123", "287990123"),
    ("172 80001", "17280001"),
    ("  0660  ", "0660"),       # leading zero kept
    ("abc-12 3", "ABC123"),
    ("12.5/A", "12.5/A"),       # other punctuation kept
])
def test_serial_normalization(raw, expected):
    assert normalize_serial(raw) == expected


def test_placeholders():
    for p in ("", "0", "00", "NONE", "UNKNOWN"):
        assert is_placeholder_serial(normalize_serial(p))
    assert not is_placeholder_serial("1")
    assert not is_placeholder_serial("0660")


def test_faa_dates():
    assert faa_date("20261111") == "2026-11-11"
    assert faa_date(" 19401226 ") == "1940-12-26"
    assert faa_date("") is None
    assert faa_date("20260231") is None
    assert faa_date("2026111") is None
