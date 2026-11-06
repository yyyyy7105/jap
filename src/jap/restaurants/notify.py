"""Daily email digest of open slots (stdlib smtplib, any SMTP server)."""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
import re
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path

from .models import ScanInfo, Slot


class NotifyError(RuntimeError):
    pass


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, doesn't override real env vars."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class EmailConfig:
    default_to: list[str] = field(default_factory=list)  # for restaurants without `notify`
    groups: dict[str, list[str]] = field(default_factory=dict)  # name -> addresses
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    security: str = "starttls"  # "starttls" | "ssl" | "none"
    username: str = ""
    password_env: str = "JAP_SMTP_PASSWORD"
    password_inline: str = field(default="", repr=False)  # `password` key in settings.toml (git-ignored)
    sender: str = ""
    enabled: bool = False  # default for `watch` when no --email/--no-email flag
    send_at: time = time(0, 0)  # daily digest time, this computer's local clock
    send_empty: bool = True  # still email on days with nothing open (shows the watcher is alive)
    subject_prefix: str = "[jap]"

    @classmethod
    def from_settings(cls, raw: dict | None) -> "EmailConfig | None":
        if not raw:
            return None

        def addrs(v, where):
            if isinstance(v, str):
                v = [v]
            out = [str(x).strip() for x in (v or []) if str(x).strip()]
            bad = [x for x in out if "@" not in x]
            if bad:
                raise NotifyError(f"[email] {where}: not an email address: {', '.join(bad)}")
            return out

        groups = {str(k): addrs(v, f"groups.{k}") for k, v in (raw.get("groups") or {}).items()}
        security = str(raw.get("security", "ssl" if int(raw.get("smtp_port", 587)) == 465 else "starttls")).lower()
        if security not in ("starttls", "ssl", "none"):
            raise NotifyError("[email] security must be starttls, ssl or none")
        username = raw.get("username", "")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw.get("send_at", "00:00")).strip())
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise NotifyError(f"[email] send_at: expected HH:MM, got {raw.get('send_at')!r}")
        return cls(
            default_to=addrs(raw.get("default_to"), "default_to"),
            groups=groups,
            smtp_host=raw.get("smtp_host", "smtp.gmail.com"),
            smtp_port=int(raw.get("smtp_port", 587)),
            security=security,
            username=username,
            password_env=raw.get("password_env", "JAP_SMTP_PASSWORD"),
            password_inline=str(raw.get("password", "")).replace(" ", ""),
            sender=raw.get("from") or username,
            enabled=bool(raw.get("enabled", False)),
            send_at=time(int(m[1]), int(m[2])),
            send_empty=bool(raw.get("send_empty", True)),
            subject_prefix=raw.get("subject_prefix", "[jap]"),
        )

    @property
    def password(self) -> str:
        return os.environ.get(self.password_env, "") or self.password_inline

    def resolve(self, entries, where: str = "") -> list[str]:
        """Expand a restaurant's `notify` list (addresses and group names) into
        unique addresses. Empty list -> default_to."""
        if not entries:
            return list(self.default_to)
        out: list[str] = []
        for e in entries:
            if "@" in e:
                found = [e]
            elif e in self.groups:
                found = self.groups[e]
            else:
                known = ", ".join(sorted(self.groups)) or "none defined"
                raise NotifyError(f"{where}: unknown email group {e!r} (groups in settings.toml: {known})")
            have = {x.lower() for x in out}
            out += [a for a in found if a.lower() not in have]
        return out


def _slot_line(s: Slot) -> str:
    extra = f" - {s.detail}" if s.detail else ""
    return f"{s.date:%a %Y-%m-%d} {s.time:%H:%M}  {s.restaurant} ({s.platform}, {s.party_size} pax){extra}"


def build_message(
    cfg: EmailConfig,
    new: list[Slot],
    still_open: list[Slot],
    to: list[str],
    scan: ScanInfo | None = None,
    restaurants: list[str] | None = None,
    failed: list[str] | None = None,
    day: date | None = None,
) -> EmailMessage:
    """Daily digest: NEW slots first, then the ones already reported that are still open.
    `scan` + `restaurants` (names this recipient watches) add a 'what was scanned' section;
    `failed` lists restaurants whose latest check failed."""
    new, still_open = sorted(new), sorted(still_open)
    total = len(new) + len(still_open)
    day = day or datetime.now().date()
    msg = EmailMessage()
    if total:
        msg["Subject"] = f"{cfg.subject_prefix} {day:%m-%d} digest: {len(new)} new, {total} open"
    else:
        msg["Subject"] = f"{cfg.subject_prefix} {day:%m-%d} digest: nothing open"
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(to)

    names = restaurants or sorted({s.restaurant for s in new + still_open})
    scanned = _scanned(scan, names)
    failed = failed or []

    # -- plain text ------------------------------------------------------
    lines = [f"Daily reservation digest for {day:%a %Y-%m-%d} (slot times are Tokyo time)", ""]
    if new:
        lines.append(f"NEW since the last digest ({len(new)}):")
        for s in new:
            lines.append(f"  * {_slot_line(s)}")
            if s.url:
                lines.append(f"      {s.url}")
        lines.append("")
    if still_open:
        lines.append(f"Still open ({len(still_open)}):")
        for s in still_open:
            lines.append(f"  - {_slot_line(s)}")
            if s.url:
                lines.append(f"      {s.url}")
        lines.append("")
    if not total:
        lines += ["No open slots in your watched ranges right now.", ""]
    if failed:
        lines += [f"Couldn't check this round (will retry): {', '.join(failed)}", ""]
    if scan:
        lines += [f"Scan ran {scan.when}", "Scanned ranges (Tokyo time):"]
        lines += [f"  {n}: {r}" for n, r in scanned]
        lines.append("")
    lines.append("Slots can go quickly - book directly on the site.")
    msg.set_content("\n".join(lines))

    # -- html ------------------------------------------------------------
    def row(s: Slot, is_new: bool) -> str:
        link = f'<a href="{html.escape(s.url)}">Book</a>' if s.url else ""
        flag = '<b style="color:#c0392b">NEW</b>' if is_new else ""
        bg = ' style="background:#fff4e5"' if is_new else ""
        return (
            f"<tr{bg}><td>{flag}</td><td>{s.date:%a %Y-%m-%d}</td><td><b>{s.time:%H:%M}</b></td>"
            f"<td>{html.escape(s.restaurant)}</td><td>{s.party_size}</td>"
            f"<td>{html.escape(s.detail)}</td><td>{link}</td></tr>"
        )

    parts = [f"<p>Daily reservation digest for <b>{day:%a %Y-%m-%d}</b> (slot times are Tokyo time)</p>"]
    if total:
        rows = "".join(row(s, True) for s in new) + "".join(row(s, False) for s in still_open)
        parts.append(
            f"<p><b>{len(new)} new</b>, {total} open in total.</p>"
            '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif">'
            "<tr><th></th><th>Date</th><th>Time</th><th>Restaurant</th><th>Pax</th><th>Info</th><th></th></tr>"
            f"{rows}</table>"
        )
    else:
        parts.append("<p>No open slots in your watched ranges right now.</p>")
    if failed:
        parts.append(f"<p style=\"color:#a00\">Couldn't check this round (will retry): {html.escape(', '.join(failed))}</p>")
    parts.append(_scanned_html(scan, scanned))
    parts.append("<p>Slots can go quickly - book directly on the site.</p>")
    msg.add_alternative("".join(parts), subtype="html")
    return msg


def _scanned(scan: ScanInfo | None, names: list[str]) -> list[tuple[str, str]]:
    if not scan:
        return []
    return [(n, scan.ranges[n]) for n in names if n in scan.ranges]


def _scanned_html(scan: ScanInfo | None, scanned: list[tuple[str, str]]) -> str:
    if not scan:
        return ""
    items = "".join(f"<li><b>{html.escape(n)}</b>: {html.escape(r)}</li>" for n, r in scanned)
    return (
        '<p style="color:#555;font-size:13px">'
        f"Scan ran {html.escape(scan.when)}<br>Scanned ranges (Tokyo time):</p>"
        f'<ul style="color:#555;font-size:13px">{items}</ul>'
    )


def send(cfg: EmailConfig, msg: EmailMessage, timeout: float = 30) -> None:
    if cfg.username and not cfg.password:
        raise NotifyError(
            f"SMTP password missing: add `password = \"...\"` under [email] in settings.toml, "
            f"or set {cfg.password_env} in the environment / .env file"
        )
    if not cfg.sender:
        raise NotifyError("[email] needs `from` or `username` in settings.toml")
    try:
        if cfg.security == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
        with server:
            if cfg.security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.username:
                server.login(cfg.username, cfg.password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"sending email failed: {e}") from e


class DailyDigest:
    """One email per recipient per day, at `send_at` (local time).

    Each digest lists every currently open slot for that recipient's restaurants.
    Slots that weren't in any earlier digest are NEW and listed first. Per recipient
    we remember:
      last_sent - date of their last digest (so it goes out once a day)
      emailed   - slots already included in a digest that are still open
    A slot that closes is forgotten (for restaurants checked successfully), so if
    it reopens it's NEW again.
    """

    def __init__(self, cfg: EmailConfig, state_path: Path, sender=None):
        self.cfg = cfg
        self.path = Path(state_path)
        # sender(addr, new, still_open, scan, names, failed, day)
        self._send = sender or (
            lambda addr, new, old, scan, names, failed, day: send(
                cfg, build_message(cfg, new, old, [addr], scan, names, failed, day)
            )
        )
        self.state: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8")).get("recipients", {})
            except (ValueError, AttributeError):
                self.state = {}
        for st in self.state.values():  # tolerate older state files
            st.setdefault("last_sent", None)
            st.setdefault("emailed", st.pop("notified", []))
            st.pop("sent", None)
            st.pop("pending", None)

    def _r(self, addr: str) -> dict:
        return self.state.setdefault(addr.lower(), {"last_sent": None, "emailed": []})

    def next_send(self, now: datetime | None = None) -> datetime:
        """The next send_at moment after `now` (for the countdown)."""
        now = now or datetime.now()
        at = datetime.combine(now.date(), self.cfg.send_at)
        return at if at > now else at + timedelta(days=1)

    def due(self, addr: str, now: datetime) -> bool:
        st = self._r(addr)
        if st["last_sent"] is None:
            # first time we see this person: start counting from today, first digest at the next send_at
            st["last_sent"] = now.date().isoformat()
            return False
        return st["last_sent"] < now.date().isoformat() and now.time() >= self.cfg.send_at

    def process(
        self,
        current: list[Slot],
        recipients: list[str],
        recipients_of,
        now: datetime | None = None,
        scan: ScanInfo | None = None,
        restaurants_of=None,
        failed: set[tuple[str, str]] | None = None,
        force: bool = False,
    ) -> dict:
        """Call after every round.
        current: open slots found this round; recipients: everyone who gets digests;
        recipients_of(slot) -> addresses; restaurants_of(addr) -> names they watch;
        failed: (platform, name) whose check failed this round (their memory is kept);
        force: send now regardless of the schedule.
        Returns {addr: (status, new, still_open)}; status 'sent', 'skipped (nothing open)' or 'failed: <err>'."""
        now = now or datetime.now()
        open_by_key = {s.key: s for s in sorted(current)}
        frozen = tuple(f"{p}:{n}:" for p, n in (failed or ()))
        failed_names = sorted({n for _, n in (failed or ())})

        # forget slots that closed - but only for restaurants we actually checked
        for st in self.state.values():
            st["emailed"] = [k for k in st["emailed"] if k in open_by_key or (frozen and k.startswith(frozen))]

        report = {}
        for addr in {a.lower(): a for a in recipients}.values():
            st = self._r(addr)
            if not force and not self.due(addr, now):
                continue
            mine = [s for k, s in open_by_key.items() if addr.lower() in {a.lower() for a in recipients_of(s)}]
            done = set(st["emailed"])
            new = [s for s in mine if s.key not in done]
            old = [s for s in mine if s.key in done]
            if not mine and not self.cfg.send_empty:
                st["last_sent"] = now.date().isoformat()
                report[addr] = ("skipped (nothing open)", [], [])
                continue
            names = restaurants_of(addr) if restaurants_of else None
            mine_failed = [n for n in failed_names if names is None or n in names]
            try:
                self._send(addr, new, old, scan, names, mine_failed, now.date())
            except NotifyError as e:  # not marked as sent; retried next round
                report[addr] = (f"failed: {e}", new, old)
                continue
            st["last_sent"] = now.date().isoformat()
            st["emailed"] = sorted(done | {s.key for s in mine})
            report[addr] = ("sent", new, old)

        self.state = {a: st for a, st in self.state.items() if st["last_sent"] or st["emailed"]}
        self._save()
        return report

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"recipients": self.state}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash mid-write can't corrupt the record
