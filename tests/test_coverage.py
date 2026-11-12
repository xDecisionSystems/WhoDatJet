from __future__ import annotations

from datetime import date

from whodatjet.coverage import (
    find_gaps,
    is_publication_day,
    is_stale,
    publication_days_between,
    span_has_gap,
)


def test_publication_days():
    assert is_publication_day(date(2026, 11, 12))       # Thursday
    assert not is_publication_day(date(2026, 11, 14))   # Saturday
    assert not is_publication_day(date(2026, 11, 26))   # Thanksgiving
    assert not is_publication_day(date(2026, 7, 3))     # Independence Day observed (Friday)


def test_weekend_is_never_a_gap():
    assert publication_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 0  # Fri -> Mon
    assert not span_has_gap(date(2026, 11, 13), date(2026, 11, 16))


def test_holiday_week_is_not_a_gap():
    # Wed before Thanksgiving -> Mon after: only Friday Nov 27 is a publication day
    assert publication_days_between(date(2026, 11, 25), date(2026, 11, 30)) == 1
    assert not span_has_gap(date(2026, 11, 25), date(2026, 11, 30))


def test_missed_days_beyond_threshold_are_a_gap():
    dates = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 9)]  # Mon, Tue, then next Mon: Wed-Fri missed
    gaps = find_gaps(dates, threshold=2)
    assert len(gaps) == 1 and gaps[0].after == date(2026, 11, 3) and gaps[0].missed_publication_days == 3
    assert find_gaps(dates, threshold=3) == []


def test_staleness():
    assert not is_stale(date(2026, 11, 11), date(2026, 11, 15), 4)
    assert is_stale(date(2026, 11, 11), date(2026, 11, 16), 4)
    assert is_stale(None, date(2026, 11, 16), 4)
