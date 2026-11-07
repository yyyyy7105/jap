"""Pocket Concierge (pocket-concierge.jp) reservations.

The site's own GraphQL API (``POST /graphql``) answers plain requests, no login:

* ``venue(id).availabilityCalendar`` - every bookable date (~90 days ahead) in one
  request, plus dates that only take waitlist requests.
* ``availabilitySearch(venueId, date)`` - the seatings on one date: start time,
  course, seating type and the allowed party size (min/max).

So a round costs 1 request + 1 per open date in the watch windows.
Members-only "secret seats" are a separate listing and aren't checked.

Options (watchlist ``options`` table):
  include_waitlist = true   also report waitlist-only seatings (detail says "waitlist")
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

from ..models import Restaurant, Slot
from .base import JST, Provider, ProviderError

SITE = "https://www.pocket-concierge.jp"

CALENDAR = """query VenueAvailabilityCalendar($id: ID!) {
  venue(id: $id) { availabilityCalendar { reservationDates waitlistDates } }
}"""

SEARCH = """query AvailabilitySearch($venueId: ID!, $date: ISO8601Date!) {
  availabilitySearch(venueId: $venueId, date: $date) {
    __typename
    ... on ReservableAvailability {
      startTime seatingType maxPartySize minPartySize
      course { id name costPerGuest serviceType }
    }
    ... on WaitlistableAvailability {
      startTime maxPartySize minPartySize
      course { id name costPerGuest serviceType }
    }
  }
}"""


class PocketConciergeProvider(Provider):
    name = "pocketconcierge"

    def _gql(self, r: Restaurant, query: str, variables: dict) -> dict:
        resp = self.post(
            f"{SITE}/graphql",
            json={"query": query, "variables": variables},
            headers={
                "Accept": "application/json",
                "Origin": SITE,
                "Referer": f"{SITE}/en/restaurants/{r.id}",
            },
        )
        self.pause()
        if resp.status_code == 403:
            raise ProviderError("Pocket Concierge refused the request (HTTP 403, bot protection); try again later")
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as e:
            raise ProviderError("Pocket Concierge returned non-JSON") from e
        if body.get("errors"):
            raise ProviderError(f"Pocket Concierge: {body['errors'][0].get('message', body['errors'][0])}")
        return body.get("data") or {}

    def calendar(self, r: Restaurant) -> tuple[set[date], set[date]]:
        venue = self._gql(r, CALENDAR, {"id": r.id}).get("venue")
        if not venue:
            raise ProviderError(f"Pocket Concierge has no restaurant {r.id!r}")
        cal = venue.get("availabilityCalendar") or {}
        return (
            {date.fromisoformat(d) for d in cal.get("reservationDates") or []},
            {date.fromisoformat(d) for d in cal.get("waitlistDates") or []},
        )

    def seatings(self, r: Restaurant, d: date) -> list[dict]:
        return self._gql(r, SEARCH, {"venueId": r.id, "date": d.isoformat()}).get("availabilitySearch") or []

    def fetch(self, r: Restaurant) -> list[Slot]:
        dates = r.dates()
        if not dates:
            return []
        waitlist_ok = bool(r.options.get("include_waitlist"))
        self.report_date(r.name, dates[0], 1, len(dates))
        open_days, wait_days = self.calendar(r)
        wanted = open_days | (wait_days if waitlist_ok else set())

        found: dict[str, Slot] = {}
        for n, d in enumerate(dates, 1):
            if d not in wanted:
                continue
            self.report_date(r.name, d, n, len(dates))
            for a in self.seatings(r, d):
                is_wait = a.get("__typename") == "WaitlistableAvailability"
                if is_wait and not waitlist_ok:
                    continue
                lo, hi = a.get("minPartySize") or 1, a.get("maxPartySize") or 99
                if not lo <= r.party_size <= hi:
                    continue
                try:
                    start = datetime.fromisoformat(a["startTime"]).astimezone(JST)
                except (KeyError, TypeError, ValueError):
                    continue
                t = start.time().replace(second=0, microsecond=0)
                if start.date() != d or not r.wants(d, t):
                    continue
                slot = Slot(
                    date=d,
                    time=t,
                    restaurant=r.name,
                    platform=self.name,
                    party_size=r.party_size,
                    detail=_detail(a, is_wait),
                    url=f"{SITE}/en/restaurants/{r.id}?date={d.isoformat()}&partySize={r.party_size}",
                )
                if slot.key in found:  # several courses at the same seating: list them together
                    prev = found[slot.key]
                    if slot.detail not in prev.detail:
                        found[slot.key] = replace(prev, detail=f"{prev.detail}; {slot.detail}")
                else:
                    found[slot.key] = slot
        return sorted(found.values())


def _detail(a: dict, is_wait: bool) -> str:
    c = a.get("course") or {}
    parts = []
    if is_wait:
        parts.append("waitlist")
    if c.get("serviceType"):
        parts.append(str(c["serviceType"]).lower())
    if (c.get("name") or "").strip():
        parts.append(c["name"].strip())
    if c.get("costPerGuest"):
        parts.append(f"¥{int(c['costPerGuest']):,}")
    if a.get("seatingType"):
        parts.append(str(a["seatingType"]).lower())
    return " · ".join(parts)
