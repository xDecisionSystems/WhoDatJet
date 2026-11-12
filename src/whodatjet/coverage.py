"""Coverage, gaps and staleness (plan: "Coverage, gaps, and staleness").

- Expected publication days: U.S. federal working days (weekdays that are not federal holidays,
  including observed holidays).
- Verified-coverage dates: source dates of imported snapshots. A later download with identical
  content is a duplicate of an existing snapshot and adds no new coverage date, because the FAA has
  not published anything newer.
- Gap: consecutive coverage dates with more than `threshold` expected publication days missing
  between them. Weekends and holidays are never gaps.
- Stale: the latest coverage date is more than `stale_days` calendar days before today.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

import holidays

DEFAULT_GAP_THRESHOLD = 2
DEFAULT_STALE_DAYS = 4


@lru_cache(maxsize=64)
def _federal_holidays(year: int) -> frozenset[date]:
    return frozenset(holidays.US(years=year, observed=True).keys())


def is_publication_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _federal_holidays(d.year)


def publication_days_between(start: date, end: date) -> int:
    """Expected publication days strictly between `start` and `end`."""
    n, d = 0, start + timedelta(days=1)
    while d < end:
        n += is_publication_day(d)
        d += timedelta(days=1)
    return n


@dataclass(frozen=True)
class Gap:
    after: date  # last coverage date before the gap
    before: date  # first coverage date after the gap
    missed_publication_days: int


def coverage_dates(conn: sqlite3.Connection) -> list[date]:
    return [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT source_date FROM snapshot WHERE import_status = 'imported' ORDER BY source_date")]


def find_gaps(dates: list[date], threshold: int = DEFAULT_GAP_THRESHOLD) -> list[Gap]:
    out = []
    for a, b in zip(dates, dates[1:], strict=False):
        missed = publication_days_between(a, b)
        if missed > threshold:
            out.append(Gap(a, b, missed))
    return out


def span_has_gap(a: date, b: date, threshold: int = DEFAULT_GAP_THRESHOLD) -> bool:
    return publication_days_between(a, b) > threshold


def is_stale(latest: date | None, today: date, stale_days: int = DEFAULT_STALE_DAYS) -> bool:
    return latest is None or (today - latest).days > stale_days
