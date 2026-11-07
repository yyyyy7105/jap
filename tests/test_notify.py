import os
from datetime import date, datetime, time, timedelta, timezone

import pytest

from jap.restaurants import notify
from jap.restaurants.models import ScanInfo, Slot
from jap.restaurants.notify import DailyDigest, EmailConfig, NotifyError, build_message


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def cfg(**kw):
    base = {
        "username": "me@gmail.com",
        "default_to": ["me@gmail.com"],
        "groups": {"fam": ["me@gmail.com", "Mum@x.com"], "friends": ["pal@y.com", "mum@x.com"]},
    }
    return EmailConfig.from_settings(base | kw)


def _slot(h, name="A", day=20):
    return Slot(date(2026, 11, day), time(h), name, "tablecheck", 2, "", f"https://x/{name}/{h}")


# -- config / recipients -------------------------------------------------
def test_config_parsing():
    c = cfg()
    assert c.sender == "me@gmail.com" and c.security == "starttls"
    assert c.send_at is None and c.send_empty is True  # daily digest off by default
    assert cfg(send_at="07:30", send_empty=False).send_at == time(7, 30)
    assert cfg(smtp_port=465).security == "ssl"
    assert EmailConfig.from_settings(None) is None
    with pytest.raises(NotifyError, match="not an email"):
        cfg(groups={"g": ["nope"]})
    with pytest.raises(NotifyError, match="send_at"):
        cfg(send_at="25:00")


def test_resolve_groups_and_addresses():
    c = cfg()
    assert c.resolve([]) == ["me@gmail.com"]  # default_to
    assert c.resolve(["fam", "friends", "x@z.com"]) == ["me@gmail.com", "Mum@x.com", "pal@y.com", "x@z.com"]
    with pytest.raises(NotifyError, match="unknown email group 'famliy'"):
        c.resolve(["famliy"], "restaurant 'A' notify")


# -- message -------------------------------------------------------------
def _scan():
    tz = timezone(timedelta(hours=8), "CST")
    return ScanInfo(
        datetime(2026, 11, 7, 0, 0, 3, tzinfo=tz),
        datetime(2026, 11, 7, 0, 0, 11, tzinfo=tz),
        {"A": "next 14d (11-07..11-20) 18:00-21:00", "B": "2026-12-08 00:00-23:59", "C <x>": "11-20..11-25 18:00-21:00"},
    )


def test_message_new_first_then_still_open():
    new, old = [_slot(20, "A")], [_slot(18, "A"), _slot(19, "C <x>")]
    msg = build_message(cfg(), new, old, ["me@x.com"], _scan(), ["A", "C <x>"], ["B"], date(2026, 11, 7))
    assert msg["To"] == "me@x.com" and msg["Subject"] == "[jap] 11-07 digest: 1 new, 3 open"
    text = msg.get_body(("plain",)).get_content()
    assert text.index("NEW since the last digest (1)") < text.index("Still open (2)")
    assert text.index("20:00") < text.index("18:00")  # new listed before older, even if later in the day
    assert "Couldn't check this round (will retry): B" in text
    assert "Scan ran 2026-11-07 00:00:03-00:00:11 CST (8s)" in text
    assert "  A: next 14d" in text and "C <x>: 11-20..11-25" in text and "  B:" not in text
    html = msg.get_body(("html",)).get_content()
    assert html.count(">NEW<") == 1 and "C &lt;x&gt;" in html


def test_message_nothing_open():
    msg = build_message(cfg(), [], [], ["me@x.com"], day=date(2026, 11, 7))
    assert msg["Subject"] == "[jap] 11-07 digest: nothing open"
    assert "No open slots" in msg.get_body(("plain",)).get_content()


# -- sending -------------------------------------------------------------
def test_send_starttls_with_login(fake_smtp, monkeypatch):
    monkeypatch.setenv("JAP_SMTP_PASSWORD", "secret")
    notify.send(cfg(), build_message(cfg(), [_slot(18)], [], ["a@x.com"]))
    s = fake_smtp.instances[0]
    assert s.calls[:2] == ["starttls", ("login", "me@gmail.com", "secret")] and len(s.sent) == 1


def test_inline_password_and_env_precedence(fake_smtp, monkeypatch):
    msg = build_message(cfg(), [_slot(18)], [], ["a@x.com"])
    monkeypatch.delenv("JAP_SMTP_PASSWORD", raising=False)
    notify.send(cfg(password="abcd efgh"), msg)
    assert ("login", "me@gmail.com", "abcdefgh") in fake_smtp.instances[-1].calls
    monkeypatch.setenv("JAP_SMTP_PASSWORD", "fromenv")
    notify.send(cfg(password="inline"), msg)
    assert ("login", "me@gmail.com", "fromenv") in fake_smtp.instances[-1].calls


def test_missing_password(fake_smtp, monkeypatch):
    monkeypatch.delenv("JAP_SMTP_PASSWORD", raising=False)
    with pytest.raises(NotifyError, match="password missing"):
        notify.send(cfg(), build_message(cfg(), [_slot(18)], [], ["a@x.com"]))
    assert not fake_smtp.instances


def test_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("JAP_T1", raising=False)
    monkeypatch.setenv("JAP_T2", "real")
    (tmp_path / ".env").write_text('# c\nJAP_T1="abc"\nJAP_T2=fromfile\n', encoding="utf-8")
    notify.load_dotenv(tmp_path / ".env")
    assert os.environ["JAP_T1"] == "abc" and os.environ["JAP_T2"] == "real"


# -- alerts + daily digest -----------------------------------------------
BINDING = {"A": ["me@x.com"], "B": ["me@x.com", "pal@y.com"]}
EVERYONE = ["me@x.com", "pal@y.com"]


def rcpt(s):
    return BINDING.get(s.restaurant, [])


def recorder():
    sent = []

    def sender(kind, addr, new, old, scan, names, failed, day):
        sent.append((kind, addr, [s.key for s in new], [s.key for s in old], failed))

    return sent, sender


def test_alert_message():
    msg = notify.build_alert(cfg(), [_slot(18, "A")], ["me@x.com"], _scan(), ["A"])
    assert msg["Subject"] == "[jap] NEW: A Fri 11-20 18:00"
    assert "18:00" in msg.get_body(("plain",)).get_content() and "A: next 14d" in msg.get_body(("plain",)).get_content()
    two = notify.build_alert(cfg(), [_slot(18, "A"), _slot(19, "C <x>")], ["me@x.com"])
    assert two["Subject"] == "[jap] 2 new slots: A, C <x>"
    assert "C &lt;x&gt;" in two.get_body(("html",)).get_content()


def test_new_slots_alerted_right_away_digest_at_midnight(tmp_path):
    sent, sender = recorder()
    d = DailyDigest(cfg(send_at="00:00"), tmp_path / "d.json", sender=sender)
    a1, a2, b1 = _slot(18, "A"), _slot(19, "A"), _slot(20, "B")

    # first run ever (afternoon, empty state): everything open is new -> alert now
    r = d.process([a1], EVERYONE, rcpt, datetime(2026, 11, 6, 15))
    assert r["me@x.com"][0] == "alert sent" and "pal@y.com" not in r
    assert sent == [("alert", "me@x.com", [a1.key], [], [])]

    # same slots next round: no repeat
    sent.clear()
    assert d.process([a1], EVERYONE, rcpt, datetime(2026, 11, 6, 15, 10)) == {}

    # a new slot appears: alert only the people bound to it, only the new slot
    d.process([a1, b1], EVERYONE, rcpt, datetime(2026, 11, 6, 23, 50))
    assert sorted(sent) == [("alert", "me@x.com", [b1.key], [], []), ("alert", "pal@y.com", [b1.key], [], [])]
    assert d.next_send(datetime(2026, 11, 6, 23, 50)) == datetime(2026, 11, 7, 0, 0)

    # midnight: digest with ALL open slots; first digest -> all NEW (alerted today still counts as new)
    sent.clear()
    r = d.process([a1, b1], EVERYONE, rcpt, datetime(2026, 11, 7, 0, 0, 30))
    assert r["me@x.com"][0] == "digest sent"
    assert sorted(sent) == [("digest", "me@x.com", [a1.key, b1.key], [], []), ("digest", "pal@y.com", [b1.key], [], [])]

    # during the day: a2 found -> alert; no second digest
    sent.clear()
    d.process([a1, a2, b1], EVERYONE, rcpt, datetime(2026, 11, 7, 12))
    assert sent == [("alert", "me@x.com", [a2.key], [], [])]

    # next midnight, after a restart: a2 NEW at the top, a1/b1 still open; pal only hears about B
    sent.clear()
    d2 = DailyDigest(cfg(send_at="00:00"), tmp_path / "d.json", sender=sender)
    d2.process([a1, a2, b1], EVERYONE, rcpt, datetime(2026, 11, 8, 0, 1))
    assert sorted(sent) == [
        ("digest", "me@x.com", [a2.key], [a1.key, b1.key], []),
        ("digest", "pal@y.com", [], [b1.key], []),
    ]


def test_closed_slot_reopening_is_alerted_again(tmp_path):
    sent, sender = recorder()
    d = DailyDigest(cfg(send_at="00:00"), tmp_path / "d.json", sender=sender)
    a = _slot(18, "A")
    d.process([a], ["me@x.com"], rcpt, datetime(2026, 11, 6, 9))
    d.process([], ["me@x.com"], rcpt, datetime(2026, 11, 6, 12))  # booked by someone
    d.process([a], ["me@x.com"], rcpt, datetime(2026, 11, 6, 13))  # reopened
    assert [(k, new) for k, _, new, _, _ in sent] == [("alert", [a.key]), ("alert", [a.key])]


def test_failed_check_keeps_record(tmp_path):
    sent, sender = recorder()
    d = DailyDigest(cfg(send_at="00:00"), tmp_path / "d.json", sender=sender)
    a, b = _slot(18, "A"), _slot(20, "B")
    d.process([a, b], ["me@x.com"], rcpt, datetime(2026, 11, 6, 9), force=True)
    # A's check fails at midnight: digest names it, and A's slot isn't re-alerted / NEW once it's back
    d.process([b], ["me@x.com"], rcpt, datetime(2026, 11, 7, 0, 5), failed={("tablecheck", "A")})
    assert sent[-1] == ("digest", "me@x.com", [], [b.key], ["A"])
    n = len(sent)
    d.process([a, b], ["me@x.com"], rcpt, datetime(2026, 11, 7, 9))
    assert len(sent) == n  # no alert for a
    d.process([a, b], ["me@x.com"], rcpt, datetime(2026, 11, 8, 0, 5))
    assert sent[-1] == ("digest", "me@x.com", [], [a.key, b.key], [])


def test_empty_digest_and_send_failures(tmp_path):
    sent, sender = recorder()
    d = DailyDigest(cfg(send_at="00:00"), tmp_path / "d.json", sender=sender)
    assert d.process([], ["me@x.com"], rcpt, datetime(2026, 11, 7, 0, 1), force=True)["me@x.com"][0] == "digest sent"
    assert sent == [("digest", "me@x.com", [], [], [])]  # "nothing open" email by default

    quiet = DailyDigest(cfg(send_at="00:00", send_empty=False), tmp_path / "q.json", sender=sender)
    r = quiet.process([], ["me@x.com"], rcpt, datetime(2026, 11, 7, 0, 1), force=True)
    assert r["me@x.com"][0] == "digest skipped (nothing open)"

    def boom(*a):
        raise NotifyError("smtp down")

    f = DailyDigest(cfg(send_at="00:00"), tmp_path / "f.json", sender=boom)
    r = f.process([_slot(18)], ["me@x.com"], rcpt, datetime(2026, 11, 6, 9))
    assert r["me@x.com"][0].startswith("alert failed")
    assert f._r("me@x.com")["alerted"] == []  # retried next round
    r = f.process([_slot(18)], ["me@x.com"], rcpt, datetime(2026, 11, 7, 0, 1))
    assert r["me@x.com"][0].startswith("digest failed")
    assert f.due("me@x.com", datetime(2026, 11, 7, 0, 11))  # digest retried next round


def test_reads_old_state_file(tmp_path):
    p = tmp_path / "old.json"
    p.write_text('{"recipients": {"me@x.com": {"last_sent": "2026-11-06", "emailed": ["k1"]}}}', encoding="utf-8")
    d = DailyDigest(cfg(), p)
    assert d._r("me@x.com") == {"last_sent": "2026-11-06", "alerted": ["k1"], "digested": ["k1"]}


def test_daily_digest_off_only_alerts(tmp_path):
    assert cfg(send_at="off").send_at is None
    sent, sender = recorder()
    d = DailyDigest(cfg(send_at="off"), tmp_path / "d.json", sender=sender)
    a1, a2 = _slot(18, "A"), _slot(19, "A")
    d.process([a1], EVERYONE, rcpt, datetime(2026, 11, 6, 15))
    assert sent == [("alert", "me@x.com", [a1.key], [], [])]
    # past midnight on later days: no digest, just an alert when something new opens
    sent.clear()
    assert d.process([a1], EVERYONE, rcpt, datetime(2026, 11, 7, 0, 5)) == {}
    d.process([a1, a2], EVERYONE, rcpt, datetime(2026, 11, 8, 0, 5))
    assert sent == [("alert", "me@x.com", [a2.key], [], [])]
    assert d.next_send() is None
    # `jap r digest` still works on demand
    sent.clear()
    d.process([a1, a2], EVERYONE, rcpt, datetime(2026, 11, 8, 9), force=True)
    assert sent[0][0] == "digest"
