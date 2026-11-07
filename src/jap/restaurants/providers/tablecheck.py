"""TableCheck (tablecheck.com).

Two public endpoints are used, both plain JSON over HTTP:

* ``/en/shops/{slug}/available/timetable`` - returns slot availability for the
  queried date plus several following days. Many shops have this disabled
  (``{"status": "disabled"}``).
* ``/en/shops/{slug}/available`` - checks one exact start time. Shops that
  require choosing a course answer ``require_order`` unless a menu item is
  passed, so we read the menu item ids off the reserve page and try them.

Some shops split seating into "service types" (e.g. Counter / Private room), shown
as a radio list on the reserve page. Both endpoints accept
``reservation[service_category]=<id>`` and then answer for that type only. The
``service_type`` option picks types by label (or id); each is checked separately.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, time

from bs4 import BeautifulSoup

from ..models import Restaurant, Slot
from .base import Provider, ProviderError, epoch_to_jst

BASE = "https://www.tablecheck.com"
XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
MAX_MENU_ITEMS = 8
PER_TIME_SLOWDOWN = 3.0  # multiply request_delay in per-time mode


class NeedsServiceType(Exception):
    pass


class TableCheckProvider(Provider):
    name = "tablecheck"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._types_cache: dict[str, list[tuple[str, str]]] = {}

    def fetch(self, r: Restaurant) -> list[Slot]:
        dates = r.dates()
        if not dates:
            return []
        # one pass per chosen service type; (None, "") = don't filter (the default)
        found: dict[str, tuple[Slot, list[str]]] = {}
        for cat, label in self.chosen_types(r, dates[0]):
            slots = self._via_timetable(r, dates, cat, label)
            if slots is None:  # timetable disabled for this shop
                slots = self._via_per_time(r, dates, cat, label)
            for sl in slots:
                if not r.wants(sl.date, sl.time):
                    continue
                if sl.key in found:  # same time open for several types: name them all
                    found[sl.key][1].append(label)
                else:
                    found[sl.key] = (sl, [label] if label else [])
        out = []
        for sl, labels in found.values():
            if labels:
                sl = replace(sl, detail=" · ".join(x for x in (sl.detail, ", ".join(labels)) if x))
            out.append(sl)
        return sorted(out)

    # -- service types -----------------------------------------------------
    def service_types(self, r: Restaurant, d: date) -> list[tuple[str, str]]:
        """The shop's service types as (id, label); [] when it has none."""
        if r.id not in self._types_cache:
            self._types_cache[r.id] = _parse_types(self._reserve_soup(r, d))
        return self._types_cache[r.id]

    def chosen_types(self, r: Restaurant, d: date) -> list[tuple[str | None, str]]:
        wanted = r.options.get("service_type")
        if not wanted:
            return [(None, "")]
        wanted = [wanted] if isinstance(wanted, str) else list(wanted)
        types = self.service_types(r, d)
        if not types:
            raise ProviderError("this shop has no service types; remove `service_type`")
        out: list[tuple[str | None, str]] = []
        for w in wanted:
            for t in match_type(str(w), types):
                if t not in out:
                    out.append(t)
        return out

    # -- helpers ---------------------------------------------------------
    def booking_url(self, r: Restaurant, d: date, t: time | None = None) -> str:
        url = f"{BASE}/en/shops/{r.id}/reserve?start_date={d.isoformat()}&num_people={r.party_size}"
        if t:
            url += f"&start_time={t:%H:%M}"
        return url

    def _get_json(self, path: str, params: list[tuple[str, str]], slow: bool = False) -> dict:
        resp = self.get(f"{BASE}{path}", params=params, headers=XHR)
        # per-time checks fire many requests; TableCheck rate-limits those quickly
        self.pause(PER_TIME_SLOWDOWN if slow else 1.0)
        if resp.status_code == 404:
            raise ProviderError(f"shop not found ({path})")
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(f"non-JSON response from {path} (status {resp.status_code})") from e

    # -- strategy 1: timetable -------------------------------------------
    def _via_timetable(self, r: Restaurant, dates: list[date], cat=None, label="") -> list[Slot] | None:
        wanted = set(dates)
        covered: set[date] = set()
        slots: list[Slot] = []
        for d in dates:
            if d in covered:
                continue
            self.report_date(_who(r, label), d, dates.index(d) + 1, len(dates))
            data = self._get_json(
                f"/en/shops/{r.id}/available/timetable",
                [
                    ("reservation[start_date]", d.isoformat()),
                    ("reservation[num_people_adult]", str(r.party_size)),
                ]
                + _cat_param(cat),
            )
            if data.get("status") == "disabled":
                return None
            days = (data.get("data") or {}).get("slots") or {}
            if not days:
                covered.add(d)
                continue
            for day_str, by_epoch in days.items():
                day = date.fromisoformat(day_str)
                # responses overlap (each covers days around the queried one):
                # take each day only from the first response that included it
                if day in covered:
                    continue
                covered.add(day)
                if day not in wanted:
                    continue
                for epoch, info in (by_epoch or {}).items():
                    if not info.get("available"):
                        continue
                    dt = epoch_to_jst(epoch)
                    slots.append(
                        Slot(
                            date=dt.date(),
                            time=dt.time().replace(second=0),
                            restaurant=r.name,
                            platform=self.name,
                            party_size=r.party_size,
                            detail=info.get("meal", ""),
                            url=self.booking_url(r, dt.date(), dt.time()),
                        )
                    )
            covered.add(d)  # guard against a response that omits the queried day
        return slots

    # -- strategy 2: per start time --------------------------------------
    def _reserve_soup(self, r: Restaurant, d: date) -> BeautifulSoup:
        resp = self.get(self.booking_url(r, d))
        self.pause()
        if resp.status_code == 404:
            raise ProviderError("shop not found")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        self._types_cache.setdefault(r.id, _parse_types(soup))
        return soup

    def _reserve_page(self, r: Restaurant, d: date) -> tuple[list[int], list[tuple[str, str]]]:
        soup = self._reserve_soup(r, d)

        epochs = []
        sel = soup.find("select", attrs={"name": "reservation[start_at_epoch]"})
        for opt in sel.find_all("option") if sel else []:
            if (v := opt.get("value", "")).isdigit():
                epochs.append(int(v))

        menu: list[tuple[str, str]] = []  # (menu_item_id, label)
        seen = set()
        for inp in soup.select('input[name$="[menu_item_id]"]'):
            if not inp["name"].startswith("reservation[orders_attributes]"):
                continue
            mid = inp.get("value")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            label = ""
            card = inp.find_parent("div", class_="menu-item")
            if card and (h := card.find(class_="menu-item-name")):
                label = h.get_text(" ", strip=True)[:60]
            menu.append((mid, label))
        return epochs, menu[:MAX_MENU_ITEMS]

    def _check_time(
        self, r: Restaurant, epoch: int, menu: list[tuple[str, str]], st: dict, cat=None
    ) -> tuple[bool, str]:
        """st carries per-shop learning across calls: whether a course is required,
        and which course worked last (tried first next time)."""
        base = [
            ("reservation[start_at_epoch]", str(epoch)),
            ("reservation[num_people_adult]", str(r.party_size)),
        ] + _cat_param(cat)
        if st.get("needs_order") is not True:
            data = self._get_json(f"/en/shops/{r.id}/available", base, slow=True)
            if data.get("status") == "success":
                st["needs_order"] = False
                return True, ""
            if data.get("data") == "require_service_category":
                raise NeedsServiceType()
            if data.get("data") != "require_order":
                return False, ""
            st["needs_order"] = True
        if not menu:
            return False, ""
        last = st.get("last_menu")
        ordered = sorted(menu, key=lambda m: m[0] != last)
        if not r.options.get("menu_item_id") and not r.options.get("try_all_menus") and last:
            # Once a course is known to work, a miss on it almost always means the
            # time is full; only that course is retried to keep request counts sane.
            ordered = ordered[:1]
        for mid, label in ordered:
            data = self._get_json(
                f"/en/shops/{r.id}/available",
                base
                + [
                    ("reservation[orders_attributes][0][menu_item_id]", mid),
                    ("reservation[orders_attributes][0][is_group_order]", "true"),
                ],
                slow=True,
            )
            if data.get("status") == "success":
                st["last_menu"] = mid
                return True, label
        return False, ""

    def _via_per_time(self, r: Restaurant, dates: list[date], cat=None, label="") -> list[Slot]:
        slots: list[Slot] = []
        pinned = r.options.get("menu_item_id")
        st: dict = {"last_menu": str(pinned)} if pinned else {}
        who = _who(r, label)
        for n, d in enumerate(dates, 1):
            self.report_date(who, d, n, len(dates))
            epochs, menu = self._reserve_page(r, d)
            if pinned:  # user picked one course: only test that one
                menu = [(m, label) for m, label in menu if m == pinned] or [(str(pinned), "")]
            for epoch in epochs:
                dt = epoch_to_jst(epoch)
                if dt.date() != d or not r.wants(dt.date(), dt.time()):
                    continue
                self.report_date(who, d, n, len(dates), dt.time())
                try:
                    ok, course = self._check_time(r, epoch, menu, st, cat)
                except NeedsServiceType:
                    names = ", ".join(lbl for _, lbl in self._types_cache.get(r.id, [])) or "see `jap r menus`"
                    raise ProviderError(f"this shop asks for a service type: add service_type = ... ({names})")
                if ok:
                    slots.append(
                        Slot(
                            date=dt.date(),
                            time=dt.time().replace(second=0),
                            restaurant=r.name,
                            platform=self.name,
                            party_size=r.party_size,
                            detail=course,
                            url=self.booking_url(r, dt.date(), dt.time()),
                        )
                    )
        return slots


def _cat_param(cat) -> list[tuple[str, str]]:
    return [("reservation[service_category]", cat)] if cat else []


def _who(r: Restaurant, label: str) -> str:
    return f"{r.name} ({label})" if label else r.name


def _parse_types(soup: BeautifulSoup) -> list[tuple[str, str]]:
    out = []
    for el in soup.select("input[type=radio][name='reservation[service_category]']"):
        cid = el.get("value") or ""
        label = el.parent.get_text(" ", strip=True) if el.parent else ""
        if cid and (cid, label) not in out:
            out.append((cid, label or cid))
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def match_type(wanted: str, types: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """'counter' -> the shop's type labelled e.g. 'Counter (1-5)'. Exact label or id first,
    then a label containing the text, if that's unambiguous."""
    w = _norm(wanted)
    exact = [t for t in types if w in (_norm(t[1]), t[0].lower())]
    if exact:
        return exact
    part = [t for t in types if w in _norm(t[1])]
    if len(part) == 1:
        return part
    choices = ", ".join(repr(lbl) for _, lbl in types)
    if part:
        raise ProviderError(f"service_type {wanted!r} matches several: {', '.join(repr(l) for _, l in part)}; be more specific")
    raise ProviderError(f"unknown service_type {wanted!r}; this shop has: {choices}")
