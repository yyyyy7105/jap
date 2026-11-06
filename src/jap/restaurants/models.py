"""Core data types for the restaurant watcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
JST = timezone(timedelta(hours=9), "JST")


def today_jst() -> date:
    """'Today' as Tokyo sees it - all windows are Tokyo dates."""
    return datetime.now(JST).date()


@dataclass(frozen=True)
class Window:
    """A date range plus a time-of-day range you'd accept a booking in.

    Either fixed (start_date..end_date) or rolling (`rolling_days`): today plus
    the next N-1 days, recomputed on every check so long watches move forward.
    """

    start_date: date | None = None
    end_date: date | None = None
    start_time: time = time(0, 0)
    end_time: time = time(23, 59)
    weekdays: frozenset[int] | None = None  # 0=Mon .. 6=Sun; None = any day
    rolling_days: int | None = None

    def span(self, today: date | None = None) -> tuple[date, date]:
        if self.rolling_days is not None:
            start = today or today_jst()
            return start, start + timedelta(days=self.rolling_days - 1)
        return self.start_date, self.end_date

    def dates(self, today: date | None = None) -> Iterator[date]:
        d, end = self.span(today)
        while d <= end:
            if self.weekdays is None or d.weekday() in self.weekdays:
                yield d
            d += timedelta(days=1)

    def contains(self, d: date, t: time, today: date | None = None) -> bool:
        start, end = self.span(today)
        if not (start <= d <= end):
            return False
        if self.weekdays is not None and d.weekday() not in self.weekdays:
            return False
        return self.start_time <= t <= self.end_time


@dataclass(frozen=True)
class Restaurant:
    name: str
    platform: str  # "tablecheck" | "tabelog" | "omakase"
    id: str  # platform-specific id (TableCheck slug, Tabelog rst id, OMAKASE slug)
    party_size: int
    windows: tuple[Window, ...]
    url: str | None = None
    notify: tuple[str, ...] = ()  # email addresses and/or group names from settings.toml
    options: dict = field(default_factory=dict, hash=False, compare=False)

    def dates(self) -> list[date]:
        """Every date covered by at least one window, sorted, de-duplicated."""
        return sorted({d for w in self.windows for d in w.dates()})

    def wants(self, d: date, t: time) -> bool:
        return any(w.contains(d, t) for w in self.windows)


@dataclass(frozen=True, order=True)
class Slot:
    """One bookable time found on a platform (Tokyo local date/time)."""

    date: date
    time: time
    restaurant: str
    platform: str
    party_size: int
    detail: str = ""  # e.g. "dinner", course name
    url: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.restaurant}:{self.date.isoformat()}:{self.time:%H:%M}:{self.party_size}"


def describe_windows(r: "Restaurant", today: date | None = None) -> str:
    """Human-readable date/time ranges a restaurant is scanned for (Tokyo time)."""
    parts = []
    for w in r.windows:
        start, end = w.span(today)
        span = start.isoformat() if start == end else f"{start:%m-%d}..{end:%m-%d}"
        if w.rolling_days is not None:
            span = f"next {w.rolling_days}d ({span})"
        if w.weekdays is not None:
            span += " " + "/".join(WEEKDAYS[i] for i in sorted(w.weekdays))
        parts.append(f"{span} {w.start_time:%H:%M}-{w.end_time:%H:%M}")
    return "; ".join(parts)


@dataclass
class ScanInfo:
    """What one check round covered and when it ran."""

    started: datetime  # timezone-aware, local
    finished: datetime
    ranges: dict[str, str] = field(default_factory=dict)  # restaurant name -> describe_windows()

    @property
    def when(self) -> str:
        s, f = self.started, self.finished
        secs = max(0, round((f - s).total_seconds()))
        end = f"{f:%H:%M:%S}" if f.date() == s.date() else f"{f:%Y-%m-%d %H:%M:%S}"
        return f"{s:%Y-%m-%d %H:%M:%S}-{end} {s.tzname() or ''} ({secs}s)".replace("  ", " ")


@dataclass
class CheckResult:
    restaurant: Restaurant
    slots: list[Slot] = field(default_factory=list)
    error: str | None = None
