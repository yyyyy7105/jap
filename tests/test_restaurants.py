from datetime import date, time

import httpx
import pytest
import respx

from jap.restaurants.checker import SeenStore
from jap.restaurants.config import ConfigError, id_from_url, load_watchlist
from jap.restaurants.models import CheckResult, Restaurant, Slot, Window
from jap.restaurants.providers import PocketConciergeProvider, TabelogProvider, TableCheckProvider


def W(**kw):
    base = dict(start_date=date(2026, 11, 20), end_date=date(2026, 11, 22), start_time=time(18), end_time=time(20))
    return Window(**(base | kw))


def R(platform="tablecheck", rid="shop", windows=None, **kw):
    return Restaurant(name="Test", platform=platform, id=rid, party_size=2, windows=tuple(windows or [W()]), **kw)


# -- config --------------------------------------------------------------
def test_load_watchlist(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text(
        """
[defaults]
party_size = 3
[[defaults.windows]]
date = "2026-11-20"
[[restaurants]]
url = "https://www.tablecheck.com/en/shops/wakanui-tokyo/reserve"
[[restaurants]]
name = "T"
url = "https://tabelog.com/tokyo/A1301/A130101/13277198/"
party_size = 1
seat_only = true
  [[restaurants.windows]]
  start_date = "2026-11-20"
  end_date = "2026-11-21"
  start_time = "18:00"
  end_time = "21:00"
  weekdays = ["Sat"]
[[restaurants]]
platform = "omakase"
id = "x"
enabled = false
""",
        encoding="utf-8",
    )
    rs = load_watchlist(p)
    assert [(r.platform, r.id, r.party_size) for r in rs] == [("tablecheck", "wakanui-tokyo", 3), ("tabelog", "13277198", 1)]
    assert rs[1].options == {"seat_only": True}
    assert rs[1].dates() == [date(2026, 11, 21)]  # Saturday only


def test_config_errors(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('[[restaurants]]\nplatform="tablecheck"\nid="x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="no watch windows"):
        load_watchlist(p)


def test_id_from_url():
    assert id_from_url("omakase", "https://omakase.in/en/r/ab123") == "ab123"
    assert id_from_url("tabelog", "https://tabelog.com/en/tokyo/A1301/A130101/13277198/") == "13277198"
    assert id_from_url("pocketconcierge", "https://www.pocket-concierge.jp/en/restaurants/244273?date=x") == "244273"
    assert id_from_url("pocketconcierge", "https://pocket-concierge.jp/restaurants/244273") == "244273"


def test_window_filter():
    r = R(windows=[W(weekdays=frozenset({4}))])  # Fridays
    assert r.wants(date(2026, 11, 20), time(19))
    assert not r.wants(date(2026, 11, 20), time(20, 30))
    assert not r.wants(date(2026, 11, 21), time(19))


# -- TableCheck ----------------------------------------------------------
# 1795168800 = 2026-11-20 19:00 JST
@respx.mock
def test_tablecheck_timetable():
    respx.get("https://www.tablecheck.com/en/shops/shop/reserve").mock(return_value=httpx.Response(200, text=""))
    respx.get("https://www.tablecheck.com/en/shops/shop/available/timetable").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "slots": {
                        "2026-11-20": {
                            "1795168800": {"available": True, "meal": "dinner"},
                            "1795170600": {"available": False, "meal": "dinner"},
                            "1795141800": {"available": True, "meal": "lunch"},  # 11:30, outside window
                        },
                        "2026-11-21": {"1795255200": {"available": True, "meal": "dinner"}},  # 19:00
                        "2026-11-22": {},
                    }
                }
            },
        )
    )
    slots = TableCheckProvider(delay=0).fetch(R())
    assert [(s.date, s.time) for s in slots] == [(date(2026, 11, 20), time(19)), (date(2026, 11, 21), time(19))]
    assert "start_time=19:00" in slots[0].url


TYPES_HTML = """<label><input type="radio" name="reservation[service_category]" value="c1">counter</label>
<label><input type="radio" name="reservation[service_category]" value="p1">Private room</label>"""


@respx.mock
def test_tablecheck_timetable_labels_seating():
    reserve = respx.get("https://www.tablecheck.com/en/shops/shop/reserve").mock(
        return_value=httpx.Response(200, text=TYPES_HTML)
    )
    open_by_type = {  # 19:00 open for both, 19:30 counter only, 18:00 private only
        None: ["1795168800", "1795170600", "1795165200"],
        "c1": ["1795168800", "1795170600"],
        "p1": ["1795168800", "1795165200"],
    }

    def timetable(request):
        eps = open_by_type[request.url.params.get("reservation[service_category]")]
        return httpx.Response(200, json={"data": {"slots": {"2026-11-20": {e: {"available": True, "meal": "dinner"} for e in eps}}}})

    respx.get("https://www.tablecheck.com/en/shops/shop/available/timetable").mock(side_effect=timetable)
    slots = TableCheckProvider(delay=0).fetch(R())
    assert [(s.time, s.detail) for s in slots] == [
        (time(18), "dinner · Private room"),
        (time(19), "dinner · counter, Private room"),
        (time(19, 30), "dinner · counter"),
    ]
    assert reserve.call_count == 1


@respx.mock
def test_tablecheck_nothing_open_skips_seating_lookup():
    reserve = respx.get("https://www.tablecheck.com/en/shops/shop/reserve").mock(
        return_value=httpx.Response(200, text=TYPES_HTML)
    )
    respx.get("https://www.tablecheck.com/en/shops/shop/available/timetable").mock(
        return_value=httpx.Response(200, json={"data": {"slots": {"2026-11-20": {"1795168800": {"available": False}}}}})
    )
    assert TableCheckProvider(delay=0).fetch(R()) == []
    assert reserve.call_count == 0


@respx.mock
def test_tablecheck_per_time_seating():
    respx.get("https://www.tablecheck.com/en/shops/shop/available/timetable").mock(
        return_value=httpx.Response(200, json={"status": "disabled"})
    )
    html = TYPES_HTML + """<select name="reservation[start_at_epoch]">
      <option value="1795168800">7:00 PM</option><option value="1795170600">7:30 PM</option></select>"""
    respx.get("https://www.tablecheck.com/en/shops/shop/reserve").mock(return_value=httpx.Response(200, text=html))

    def avail(request):  # shop answers only per seating type; 19:00 is counter only, 19:30 full
        cat = request.url.params.get("reservation[service_category]")
        ep = request.url.params.get("reservation[start_at_epoch]")
        if not cat:
            return httpx.Response(200, json={"status": "warn", "data": "require_service_category"})
        ok = cat == "c1" and ep == "1795168800"
        return httpx.Response(200, json={"status": "success" if ok else "warn", "data": ["x"] if ok else "full"})

    respx.get("https://www.tablecheck.com/en/shops/shop/available").mock(side_effect=avail)
    slots = TableCheckProvider(delay=0).fetch(R(windows=[W(end_date=date(2026, 11, 20))]))
    assert [(s.time, s.detail) for s in slots] == [(time(19), "counter")]


@respx.mock
def test_tablecheck_fallback_per_time():
    respx.get("https://www.tablecheck.com/en/shops/shop/available/timetable").mock(
        return_value=httpx.Response(200, json={"status": "disabled"})
    )
    html = """<select name="reservation[start_at_epoch]"><option value="">-</option>
      <option value="1795168800">7:00 PM</option><option value="1795176000">9:00 PM</option></select>
      <div class="menu-item"><div class="menu-item-name">Seats only</div>
      <input name="reservation[orders_attributes][0][menu_item_id]" value="m1"></div>"""
    respx.get("https://www.tablecheck.com/en/shops/shop/reserve").mock(return_value=httpx.Response(200, text=html))

    def avail(request):
        if "menu_item_id" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": ["x"]})
        return httpx.Response(200, json={"status": "warn", "data": "require_order"})

    respx.get("https://www.tablecheck.com/en/shops/shop/available").mock(side_effect=avail)
    r = R(windows=[W(end_date=date(2026, 11, 20))])
    slots = TableCheckProvider(delay=0).fetch(r)
    assert [(s.time, s.detail) for s in slots] == [(time(19), "Seats only")]  # 21:00 is outside window


# -- Tabelog -------------------------------------------------------------
@respx.mock
def test_tabelog():
    base = "https://tabelog.com/en/booking/calendar"
    respx.get(f"{base}/find_vacancy_date_with_status/").mock(
        return_value=httpx.Response(
            200,
            json={
                "list": [
                    {"year": 2026, "month": 11, "day": 20, "available": 2},
                    {"year": 2026, "month": 11, "day": 21, "available": 0},
                    {"year": 2026, "month": 11, "day": 22, "available": 1},
                ]
            },
        )
    )
    route = respx.get(f"{base}/find_vacancy/").mock(
        return_value=httpx.Response(
            200,
            json={
                "members": 2,
                "selection": {
                    "a": {"time": "17:00", "url": "/en/booking/x?visit_time=1700"},
                    "b": {"time": "19:00", "url": "https://tabelog.com/en/booking/y"},
                },
            },
        )
    )
    slots = TabelogProvider(delay=0).fetch(R("tabelog", "13277198"))
    assert route.call_count == 2  # the full day (21st) is skipped
    assert [(s.date.day, s.time, s.detail) for s in slots] == [(20, time(19), "few left"), (22, time(19), "")]


@respx.mock
def test_pocketconcierge():
    import json

    def seat(day, hhmm, lo=1, hi=2, kind="ReservableAvailability", name=" Omakase Course ", svc="DINNER"):
        return {
            "__typename": kind,
            "startTime": f"2026-11-{day}T{hhmm}:00+09:00",
            "seatingType": "COUNTER",
            "minPartySize": lo,
            "maxPartySize": hi,
            "course": {"id": "1", "name": name, "costPerGuest": 29700, "serviceType": svc},
        }

    by_date = {
        "2026-11-20": [seat(20, "18:00"), seat(20, "18:00", name="Special"), seat(20, "21:00")],  # 21:00 outside window
        "2026-11-22": [seat(22, "19:00", lo=1, hi=1)],  # party of 2 doesn't fit
        "2026-11-21": [seat(21, "18:30", kind="WaitlistableAvailability")],
    }
    asked = []

    def gql(request):
        body = json.loads(request.content)
        if "availabilityCalendar" in body["query"]:
            cal = {"reservationDates": ["2026-11-20", "2026-11-22", "2026-12-30"], "waitlistDates": ["2026-11-21"]}
            return httpx.Response(200, json={"data": {"venue": {"availabilityCalendar": cal}}})
        asked.append(body["variables"]["date"])
        return httpx.Response(200, json={"data": {"availabilitySearch": by_date[body["variables"]["date"]]}})

    respx.post("https://www.pocket-concierge.jp/graphql").mock(side_effect=gql)
    slots = PocketConciergeProvider(delay=0).fetch(R("pocketconcierge", "244273"))
    assert asked == ["2026-11-20", "2026-11-22"]  # only open dates inside the window; waitlist skipped
    assert [(s.date.day, s.time) for s in slots] == [(20, time(18))]
    assert slots[0].detail == "dinner · counter · 2 courses from ¥29,700"
    assert slots[0].url.endswith("/en/restaurants/244273?date=2026-11-20&partySize=2")

    asked.clear()
    w = PocketConciergeProvider(delay=0).fetch(R("pocketconcierge", "244273", options={"include_waitlist": True}))
    assert asked == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert [(s.date.day, s.detail.split(" · ")[0]) for s in w] == [(20, "dinner"), (21, "waitlist")]


def test_pocketconcierge_summary():
    from jap.restaurants.providers.pocketconcierge import summarize

    def a(seat, name="Omakase", price=29700, svc="DINNER"):
        return ({"seatingType": seat, "course": {"name": f" {name} ", "costPerGuest": price, "serviceType": svc}}, False)

    assert summarize([a("COUNTER")]) == "dinner · Omakase · ¥29,700 · counter"
    assert summarize([a("NONE")]) == "dinner · Omakase · ¥29,700"
    many = [a("COUNTER", "A", 36300), a("PRIVATE_ROOM", "A", 36300), a("COUNTER", "B", 22000), a("NONE", "C", 41800)]
    assert summarize(many) == "dinner · counter, private room · 3 courses from ¥22,000"
    assert summarize([(a("COUNTER")[0], True)]) == "waitlist · dinner · Omakase · ¥29,700 · counter"


@respx.mock
def test_pocketconcierge_reports_every_seating():
    seats = [
        {"__typename": "ReservableAvailability", "startTime": "2026-11-20T18:00:00+09:00", "seatingType": st,
         "minPartySize": 1, "maxPartySize": 4, "course": {"name": "X", "costPerGuest": 1000, "serviceType": "DINNER"}}
        for st in ("COUNTER", "PRIVATE_ROOM")
    ] + [
        {"__typename": "ReservableAvailability", "startTime": "2026-11-20T19:00:00+09:00", "seatingType": "PRIVATE_ROOM",
         "minPartySize": 1, "maxPartySize": 4, "course": {"name": "X", "costPerGuest": 1000, "serviceType": "DINNER"}}
    ]

    def gql(request):
        if b"availabilityCalendar" in request.content:
            cal = {"reservationDates": ["2026-11-20"], "waitlistDates": []}
            return httpx.Response(200, json={"data": {"venue": {"availabilityCalendar": cal}}})
        return httpx.Response(200, json={"data": {"availabilitySearch": seats}})

    respx.post("https://www.pocket-concierge.jp/graphql").mock(side_effect=gql)
    p = PocketConciergeProvider(delay=0)
    all_ = p.fetch(R("pocketconcierge", "1"))
    assert [(s.time, s.detail) for s in all_] == [
        (time(18), "dinner · X · ¥1,000 · counter, private room"),
        (time(19), "dinner · X · ¥1,000 · private room"),
    ]


@respx.mock
def test_pocketconcierge_unknown_venue():
    from jap.restaurants.providers import ProviderError

    respx.post("https://www.pocket-concierge.jp/graphql").mock(return_value=httpx.Response(200, json={"data": {"venue": None}}))
    with pytest.raises(ProviderError, match="no restaurant"):
        PocketConciergeProvider(delay=0).fetch(R("pocketconcierge", "1"))


# -- seen tracking -------------------------------------------------------
def test_seen_store(tmp_path):
    a, b = R(), Restaurant("Other", "tabelog", "1", 2, (W(),))
    s1 = Slot(date(2026, 11, 20), time(19), "Test", "tablecheck", 2)
    s2 = Slot(date(2026, 11, 20), time(19), "Other", "tabelog", 2)
    store = SeenStore(tmp_path / "seen.json")
    store.update([CheckResult(a, [s1]), CheckResult(b, [s2])])

    store = SeenStore(tmp_path / "seen.json")
    assert store.split([s1]) == ([], [s1])
    store.update([CheckResult(a, [])])  # only `a` checked: b's memory survives
    assert s2.key in store.seen and s1.key not in store.seen
    store.update([CheckResult(b, error="boom")])  # errors don't wipe memory
    assert s2.key in store.seen


def test_watchlist_notify_and_split(tmp_path):
    from jap.restaurants.config import load_settings

    p = tmp_path / "w.toml"
    p.write_text(
        '[defaults]\nnotify = ["me"]\n[[defaults.windows]]\ndate = "2026-11-20"\n'
        '[[restaurants]]\nplatform = "tablecheck"\nid = "a"\n'
        '[[restaurants]]\nplatform = "tablecheck"\nid = "b"\nnotify = ["fam", "x@y.com"]\n',
        encoding="utf-8",
    )
    a, b = load_watchlist(p)
    assert a.notify == ("me",) and b.notify == ("fam", "x@y.com")

    p.write_text(p.read_text(encoding="utf-8") + '[settings]\ninterval_seconds = 5\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="settings.toml"):
        load_watchlist(p)

    assert load_settings(tmp_path / "missing.toml") == {}
    s = tmp_path / "s.toml"
    s.write_text('[settings]\ninterval_seconds = 5\n[email]\nusername = "a@b.c"\n', encoding="utf-8")
    assert load_settings(s)["interval_seconds"] == 5 and load_settings(s)["email"]["username"] == "a@b.c"


def test_rolling_days_window(tmp_path):
    from datetime import timedelta

    from jap.restaurants.models import today_jst

    p = tmp_path / "w.toml"
    p.write_text(
        '[[restaurants]]\nplatform = "tablecheck"\nid = "a"\n'
        '  [[restaurants.windows]]\n  days = 3\n  start_time = "18:00"\n  end_time = "20:00"\n',
        encoding="utf-8",
    )
    (r,) = load_watchlist(p)
    w = r.windows[0]
    assert w.rolling_days == 3

    # rolls with "today": computed per call, not frozen at load time
    assert list(w.dates(today=date(2026, 11, 6))) == [date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)]
    assert list(w.dates(today=date(2026, 11, 30)))[-1] == date(2026, 12, 2)
    assert w.contains(date(2026, 11, 8), time(19), today=date(2026, 11, 6))
    assert not w.contains(date(2026, 11, 9), time(19), today=date(2026, 11, 6))
    assert not w.contains(date(2026, 11, 7), time(21), today=date(2026, 11, 6))

    t = today_jst()  # default uses Tokyo's today
    assert r.dates() == [t, t + timedelta(days=1), t + timedelta(days=2)]


@pytest.mark.parametrize(
    "body, msg",
    [
        ("days = 0", "between 1 and 366"),
        ('days = "two"', "whole number"),
        ('days = 5\n  date = "2026-11-20"', "only one of"),
    ],
)
def test_rolling_days_errors(tmp_path, body, msg):
    p = tmp_path / "w.toml"
    p.write_text(f'[[restaurants]]\nplatform = "tablecheck"\nid = "a"\n  [[restaurants.windows]]\n  {body}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        load_watchlist(p)


@respx.mock
def test_tablecheck_overlapping_timetable_no_duplicates():
    respx.get("https://www.tablecheck.com/en/shops/shop/reserve").mock(return_value=httpx.Response(200, text=""))
    """Regression: overlapping timetable responses must not duplicate slots."""
    payload = {"data": {"slots": {
        "2026-11-20": {"1795168800": {"available": True, "meal": "dinner"}},
        "2026-11-21": {"1795255200": {"available": True, "meal": "dinner"}},
    }}}
    route = respx.get("https://www.tablecheck.com/en/shops/shop/available/timetable").mock(
        return_value=httpx.Response(200, json=payload)
    )
    # 20th..23rd: the 22nd isn't in the response, so a second request is made and returns the same days again
    r = R(windows=[W(end_date=date(2026, 11, 23))])
    slots = TableCheckProvider(delay=0).fetch(r)
    assert route.call_count >= 2
    assert [(s.date.day, s.time) for s in slots] == [(20, time(19)), (21, time(19))]
