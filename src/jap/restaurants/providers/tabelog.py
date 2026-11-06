"""Tabelog (tabelog.com) online reservations.

Uses the JSON endpoints behind the English site's booking calendar
(``/en/booking/calendar/...``), which answer plain HTTP requests:

* ``find_vacancy_date_with_status?rst_id=`` - per-day status for the bookable
  range; ``available`` is 0 (none), 1 (open) or 2 (few left).
* ``find_vacancy?rst_id=&svd=YYYYMMDD&svps=N&exclude_unavailable_time=1`` -
  the bookable times on that day for a party of N.

The Japanese site (tabelog.com/tokyo/...) sits behind a bot check, so we
never touch it; use the restaurant's 8-digit id or any tabelog URL.
"""

from __future__ import annotations

import re
from datetime import date, time

from ..models import Restaurant, Slot
from .base import Provider, ProviderError

BASE = "https://tabelog.com/en/booking/calendar"
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


def _parse_hhmm(v) -> time | None:
    m = re.search(r"(\d{1,2}):?(\d{2})", str(v or ""))
    if not m:
        return None
    h, mnt = int(m[1]), int(m[2])
    # Tabelog may express late-night slots as 24:00+; clamp onto the clock.
    return time(h % 24, mnt)


class TabelogProvider(Provider):
    name = "tabelog"

    def _extra(self, r: Restaurant) -> list[tuple[str, str]]:
        extra = []
        if r.options.get("plan_id"):
            extra.append(("plan_id", str(r.options["plan_id"])))
        if r.options.get("seat_only"):
            extra.append(("seat_only", "true"))
        return extra

    def _get(self, endpoint: str, params: list[tuple[str, str]]) -> dict:
        resp = self.get(f"{BASE}/{endpoint}/", params=params, headers=XHR)
        self.pause()
        if resp.status_code in (403, 503):
            raise ProviderError(f"Tabelog refused the request (HTTP {resp.status_code}); try again later")
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(f"Tabelog returned non-JSON from {endpoint}") from e

    def day_status(self, r: Restaurant) -> dict[date, int]:
        data = self._get("find_vacancy_date_with_status", [("rst_id", r.id)] + self._extra(r))
        out = {}
        for d in data.get("list") or []:
            try:
                out[date(int(d["year"]), int(d["month"]), int(d["day"]))] = int(d.get("available") or 0)
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def times_on(self, r: Restaurant, d: date) -> list[tuple[time, str]]:
        data = self._get(
            "find_vacancy",
            [
                ("rst_id", r.id),
                ("svd", d.strftime("%Y%m%d")),
                ("svps", str(r.party_size)),
                ("exclude_unavailable_time", "1"),
            ]
            + self._extra(r),
        )
        # The calendar can silently fall back to another party size if N isn't bookable.
        if data.get("members") not in (None, r.party_size, str(r.party_size)):
            return []
        sel = data.get("selection") or {}
        items = sel.values() if isinstance(sel, dict) else sel
        out = []
        for item in items:
            if isinstance(item, dict):
                t = _parse_hhmm(item.get("time"))
                url = item.get("url") or ""
            else:
                t, url = _parse_hhmm(item), ""
            if t is None:
                continue
            if url.startswith("/"):
                url = "https://tabelog.com" + url
            out.append((t, url))
        return out

    def fetch(self, r: Restaurant) -> list[Slot]:
        dates = r.dates()
        if not dates:
            return []
        self.report_date(r.name, dates[0], 1, len(dates))
        status = self.day_status(r)
        if not status and not r.options.get("booking_enabled", True):
            return []
        horizon = max(status) if status else None

        slots: list[Slot] = []
        for n, d in enumerate(dates, 1):
            # Skip days the calendar says are full/closed. Days past the published
            # horizon aren't open for booking yet.
            if d in status:
                if status[d] not in (1, 2):
                    continue
            elif horizon is not None and d > horizon:
                continue
            self.report_date(r.name, d, n, len(dates))
            for t, url in self.times_on(r, d):
                if r.wants(d, t):
                    slots.append(
                        Slot(
                            date=d,
                            time=t,
                            restaurant=r.name,
                            platform=self.name,
                            party_size=r.party_size,
                            detail="few left" if status.get(d) == 2 else "",
                            url=url or r.url or "",
                        )
                    )
        return sorted(slots)
