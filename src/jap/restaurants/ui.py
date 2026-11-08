"""Streamlit UI: edit settings.toml and watchlist.toml, start/stop the watcher, read its log.

Run with:  uv run --extra ui jap r ui
(or directly: uv run --extra ui streamlit run src/jap/restaurants/ui.py)

Every option the tool understands has a field here. Fields show the value in the
file, or the built-in default when the file doesn't set it. Saving validates with
the same loaders the watcher uses, keeps a backup of the old file in
.state/backups/, and rewrites the whole file with a comment on each setting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import uuid
from datetime import date, datetime, time
from pathlib import Path

import streamlit as st
import tomlkit

from jap.restaurants.config import PLATFORMS, ConfigError, detect_platform, id_from_url, load_watchlist
from jap.restaurants.notify import EmailConfig, NotifyError

ROOT = Path(os.environ.get("JAP_ROOT", Path.cwd()))
WATCHLIST = ROOT / os.environ.get("JAP_WATCHLIST", "watchlist.toml")
SETTINGS = ROOT / os.environ.get("JAP_SETTINGS", "settings.toml")
STATE = ROOT / os.environ.get("JAP_STATE", ".state")
PID_FILE = STATE / "watch.pid"
LOG_FILE = STATE / "watch.log"

# built-in defaults (what the tool uses when a key is missing)
D_SETTINGS = {"interval_seconds": 600, "request_delay": 0.4, "headless": True}
D_EMAIL = {
    "enabled": False,
    "send_at": "off",
    "send_empty": True,
    "from": "",
    "username": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "security": "auto",
    "password_env": "JAP_SMTP_PASSWORD",
    "subject_prefix": "[jap]",
    "default_to": [],
}
D_PARTY = 2
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KINDS = ["rolling days", "date range", "single date"]
PLATFORM_CHOICES = ["auto (from url)"] + sorted(PLATFORMS)
RESTAURANT_KEYS = {"name", "platform", "id", "url", "party_size", "windows", "enabled", "notify"}
PLATFORM_OPTIONS = {
    "tablecheck": {"menu_item_id": "", "try_all_menus": False},
    "tabelog": {"seat_only": False, "plan_id": ""},
    "pocketconcierge": {"include_waitlist": False},
    "omakase": {},
}
ALL_OPTION_KEYS = {k for opts in PLATFORM_OPTIONS.values() for k in opts}


# -- small helpers ---------------------------------------------------------
def read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def to_time(v, default: str) -> time:
    if isinstance(v, time):
        return v
    s = str(v if v not in (None, "") else default).strip()
    h, m = (s.split(":") + ["0"])[:2] if ":" in s else (s[:-2], s[-2:])
    return time(int(h), int(m))


def to_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def split_list(text: str) -> list[str]:
    return [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = STATE / "backups" / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def write_checked(path: Path, text: str, check) -> str | None:
    """Validate `text` with check(tmp_path); only then back up and replace `path`.
    Returns an error message, or None when saved."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        check(tmp)
    except (ConfigError, NotifyError, ValueError, tomllib.TOMLDecodeError) as e:
        tmp.unlink(missing_ok=True)
        return str(e)
    backup(path)
    os.replace(tmp, path)
    return None


def commented(table, key, value, note: str = ""):
    item = tomlkit.item(value)
    if note:
        item.comment(note)
    table.add(key, item)


# -- windows <-> editor rows ----------------------------------------------
def window_to_row(w: dict) -> dict:
    if "days" in w:
        kind = "rolling days"
    elif "date" in w:
        kind = "single date"
    else:
        kind = "date range"
    return {
        "kind": kind,
        "days": int(w["days"]) if "days" in w else None,
        "start_date": to_date(w.get("date") or w.get("start_date")),
        "end_date": to_date(w.get("end_date")),
        "start_time": to_time(w.get("start_time"), "00:00"),
        "end_time": to_time(w.get("end_time"), "23:59"),
        "weekdays": ", ".join(str(x).lower()[:3] for x in w.get("weekdays") or []),
    }


def is_blank(v) -> bool:
    try:
        return v is None or v != v or v == ""  # None, NaN/NaT, ""
    except (TypeError, ValueError):
        return False


def row_to_window(row: dict) -> dict:
    kind = row.get("kind") or "rolling days"
    w: dict = {}
    if kind == "rolling days":
        w["days"] = int(row["days"]) if not is_blank(row.get("days")) else 14
    elif kind == "single date":
        if is_blank(row.get("start_date")):
            raise ValueError("a 'single date' window needs start_date")
        w["date"] = to_date(row["start_date"]).isoformat()
    else:
        if is_blank(row.get("start_date")):
            raise ValueError("a 'date range' window needs start_date")
        w["start_date"] = to_date(row["start_date"]).isoformat()
        end = row.get("end_date")
        w["end_date"] = to_date(end).isoformat() if not is_blank(end) else w["start_date"]
    st_, et = row.get("start_time"), row.get("end_time")
    w["start_time"] = (to_time(st_, "00:00") if not is_blank(st_) else time(0, 0)).strftime("%H:%M")
    w["end_time"] = (to_time(et, "23:59") if not is_blank(et) else time(23, 59)).strftime("%H:%M")
    days = [d.strip().lower()[:3] for d in str(row.get("weekdays") or "").split(",") if d.strip()]
    if days:
        bad = [d for d in days if d not in WEEKDAY_NAMES]
        if bad:
            raise ValueError(f"weekdays: use mon..sun, got {', '.join(bad)}")
        w["weekdays"] = days
    return w


def windows_editor(key: str, windows: list[dict]) -> list[dict]:
    """Editable table of watch windows; returns the rows as dicts."""
    import pandas as pd

    rows = [window_to_row(w) for w in windows]
    df = pd.DataFrame(rows, columns=["kind", "days", "start_date", "end_date", "start_time", "end_time", "weekdays"])
    edited = st.data_editor(
        df,
        key=key,
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        column_config={
            "kind": st.column_config.SelectboxColumn("Kind", options=KINDS, required=True, default="rolling days",
                                                     help="rolling days = today + the next N-1 days (Tokyo); "
                                                          "date range = start..end; single date = start_date only"),
            "days": st.column_config.NumberColumn("Days", min_value=1, max_value=366, step=1,
                                                  help="rolling days only (1-366)"),
            "start_date": st.column_config.DateColumn("Start date", format="YYYY-MM-DD"),
            "end_date": st.column_config.DateColumn("End date", format="YYYY-MM-DD", help="date range only"),
            "start_time": st.column_config.TimeColumn("From", format="HH:mm", default=time(0, 0),
                                                      help="earliest start time, JST (default 00:00)"),
            "end_time": st.column_config.TimeColumn("To", format="HH:mm", default=time(23, 59),
                                                    help="latest start time, JST (default 23:59)"),
            "weekdays": st.column_config.TextColumn("Weekdays", help="optional, e.g. fri, sat (empty = every day)"),
        },
    )
    return edited.to_dict("records")


def windows_aot(rows: list[dict], where: str):
    aot = tomlkit.aot()
    for i, row in enumerate(rows):
        if all(is_blank(row.get(k)) for k in ("kind", "days", "start_date", "end_date", "weekdays")):
            continue  # the empty row the editor shows for adding
        try:
            w = row_to_window(row)
        except ValueError as e:
            raise ConfigError(f"{where} window {i + 1}: {e}") from e
        t = tomlkit.table()
        for k, v in w.items():
            t.add(k, v)
        aot.append(t)
    return aot


# -- watcher control -------------------------------------------------------
def watcher_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return None
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return pid if str(pid) in out else None


def run_ps(script: str) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / script)],
        capture_output=True, text=True, cwd=ROOT,
    )
    return (r.stdout + r.stderr).strip()


def run_jap(*args: str, timeout: int = 120) -> str:
    env = os.environ | {"PYTHONIOENCODING": "utf-8", "COLUMNS": "120"}
    r = subprocess.run([sys.executable, "-m", "jap", "r", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, env=env, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def tab_watcher():
    pid = watcher_pid()
    if pid:
        st.success(f"Watcher is running (PID {pid}).")
    else:
        st.info("Watcher is not running.")
    c1, c2, c3 = st.columns(3)
    if c1.button("Start watcher", disabled=bool(pid), width="stretch",
                 help="start-watch.ps1: `jap r watch --email` in the background"):
        st.code(run_ps("start-watch.ps1") or "started")
        st.rerun()
    if c2.button("Stop watcher", disabled=not pid, width="stretch", help="stop-watch.ps1"):
        st.code(run_ps("stop-watch.ps1"))
        st.rerun()
    if c3.button("Send test email", width="stretch", help="`jap r test-email`: one short message to everyone"):
        with st.spinner("Sending..."):
            st.code(run_jap("test-email"))
    st.caption("Saved changes are picked up by the running watcher automatically (it re-reads both files and checks again).")

    st.subheader("Log")
    n = st.slider("Lines", 20, 500, 80, step=20)
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        st.code("\n".join(lines[-n:]) or "(empty)", language=None)
        st.caption(f"{LOG_FILE} · updated {datetime.fromtimestamp(LOG_FILE.stat().st_mtime):%Y-%m-%d %H:%M:%S}")
    else:
        st.caption("No log yet. It appears once the watcher has been started.")
    st.button("Refresh")


# -- settings.toml ---------------------------------------------------------
def tab_settings():
    raw = read_toml(SETTINGS)
    s = D_SETTINGS | raw.get("settings", {})
    e = D_EMAIL | (raw.get("email") or {})
    st.caption(f"File: `{SETTINGS}`. Fields show the file's value, or the default when the file doesn't set it.")

    with st.form("settings_form"):
        st.subheader("Run")
        c1, c2, c3 = st.columns(3)
        interval = c1.number_input("Seconds between checks", min_value=60, step=60, value=int(s["interval_seconds"]),
                                   help="interval_seconds (default 600). The watcher waits this long ±15% between rounds.")
        delay = c2.number_input("Pause between requests (s)", min_value=0.0, step=0.1, format="%.1f",
                                value=float(s["request_delay"]),
                                help="request_delay (default 0.4). Higher = gentler on the sites, fewer 429s, slower.")
        headless = c3.checkbox("Hide the OMAKASE browser", value=bool(s["headless"]), help="headless (default on)")

        st.subheader("Email")
        c1, c2 = st.columns(2)
        enabled = c1.checkbox("Email on by default for `watch`", value=bool(e["enabled"]),
                              help="enabled (default off). Off: only `watch --email` sends. start-watch.ps1 always passes --email.")
        send_empty = c2.checkbox("Daily digest also on days with nothing open", value=bool(e["send_empty"]),
                                 help="send_empty (default on). Only matters when the daily digest is on.")
        at = str(e["send_at"]).strip().lower()
        digest_on = c1.checkbox("Daily digest", value=at not in ("off", "none", "", "false"),
                                help="send_at (default off). New slots are always emailed right away; "
                                     "the digest is an extra once-a-day list of everything open.")
        digest_time = c2.time_input("Digest time (this computer's clock)",
                                    value=to_time(e["send_at"], "00:00") if digest_on else time(0, 0), step=900)

        c1, c2 = st.columns(2)
        sender = c1.text_input("From", value=e.get("from") or "", help="from (default: the username)")
        username = c2.text_input("SMTP username", value=e.get("username") or "", help="username")
        has_pw = bool(e.get("password"))
        password = c1.text_input("App password", type="password", value="",
                                 placeholder="saved - leave empty to keep" if has_pw else "not saved in the file",
                                 help="password. Gmail: an App Password. Stored in settings.toml (git-ignored).")
        clear_pw = c2.checkbox("Remove the saved password", value=False, disabled=not has_pw,
                               help="then the password comes from the environment variable below (or .env)")
        password_env = c2.text_input("Password environment variable", value=e["password_env"],
                                     help="password_env (default JAP_SMTP_PASSWORD). Used before the saved password.")
        c1, c2, c3 = st.columns(3)
        smtp_host = c1.text_input("SMTP host", value=e["smtp_host"], help="smtp_host (default smtp.gmail.com)")
        smtp_port = c2.number_input("SMTP port", min_value=1, max_value=65535, value=int(e["smtp_port"]),
                                    help="smtp_port (default 587)")
        sec_opts = ["auto", "starttls", "ssl", "none"]
        security = c3.selectbox("Security", sec_opts, index=sec_opts.index(str(e["security"]).lower())
                                if str(e["security"]).lower() in sec_opts else 0,
                                help="security (default auto: ssl on port 465, else starttls)")
        prefix = c1.text_input("Subject prefix", value=e["subject_prefix"], help="subject_prefix (default [jap])")
        default_to = c2.text_input("Default recipients", value=", ".join(e.get("default_to") or []),
                                   help="default_to: for restaurants with an empty `notify` list. Comma-separated.")

        st.subheader("Recipient groups")
        st.caption("Names you can use in a restaurant's `notify`. Addresses comma-separated.")
        import pandas as pd

        groups = e.get("groups") or {}
        gdf = pd.DataFrame([{"group": k, "addresses": ", ".join(v)} for k, v in groups.items()],
                           columns=["group", "addresses"])
        gedit = st.data_editor(gdf, key="groups_editor", num_rows="dynamic", hide_index=True, width="stretch")

        saved = st.form_submit_button("Save settings.toml", type="primary")

    if not saved:
        return
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Written by the jap UI (jap r ui). Restaurants live in watchlist.toml."))
    doc.add(tomlkit.nl())
    t = tomlkit.table()
    commented(t, "interval_seconds", int(interval), "watch mode: seconds between rounds (+-15% jitter), default 600")
    commented(t, "request_delay", round(float(delay), 2), "pause between HTTP requests, default 0.4")
    commented(t, "headless", bool(headless), "OMAKASE browser runs hidden, default true")
    doc.add("settings", t)
    m = tomlkit.table()
    commented(m, "enabled", bool(enabled), "true = `watch` emails without --email (start-watch.ps1 always passes it)")
    commented(m, "send_at", digest_time.strftime("%H:%M") if digest_on else "off",
              'daily digest time (HH:MM, this computer\'s clock) or "off" (default) = only email new slots')
    commented(m, "send_empty", bool(send_empty), "digest also on days with nothing open, default true")
    commented(m, "from", sender.strip())
    commented(m, "username", username.strip())
    if password.strip():
        commented(m, "password", password.replace(" ", ""), "app password; or leave out and set the variable below")
    elif has_pw and not clear_pw:
        commented(m, "password", e["password"], "app password; or leave out and set the variable below")
    commented(m, "password_env", password_env.strip() or D_EMAIL["password_env"], "used before `password` if set")
    commented(m, "smtp_host", smtp_host.strip())
    commented(m, "smtp_port", int(smtp_port), "587 = STARTTLS, 465 = SSL")
    if security != "auto":
        commented(m, "security", security, "starttls | ssl | none (default: ssl on 465, else starttls)")
    commented(m, "subject_prefix", prefix)
    commented(m, "default_to", split_list(default_to), "for restaurants whose `notify` list is empty")
    g = tomlkit.table()
    for row in gedit.to_dict("records"):
        name = str(row.get("group") or "").strip()
        if name and not is_blank(name):
            g.add(name, split_list(str(row.get("addresses") or "")))
    m.add("groups", g)
    doc.add("email", m)

    def check(p: Path):
        EmailConfig.from_settings(tomllib.loads(p.read_text(encoding="utf-8")).get("email"))

    err = write_checked(SETTINGS, tomlkit.dumps(doc), check)
    if err:
        st.error(f"Not saved: {err}")
    else:
        st.success("Saved settings.toml (old version backed up in .state/backups/). A running watcher picks it up within seconds and checks again.")


# -- watchlist.toml --------------------------------------------------------
def init_watchlist_state(force: bool = False):
    if "wl" in st.session_state and not force:
        return
    raw = read_toml(WATCHLIST)
    rs = []
    for r in raw.get("restaurants", []):
        rs.append({"uid": uuid.uuid4().hex[:8], **r})
    st.session_state.wl = {"defaults": raw.get("defaults", {}), "restaurants": rs}
    st.session_state.gen = st.session_state.get("gen", 0) + 1


def k(name: str) -> str:
    """Widget key for the current watchlist generation."""
    return f"g{st.session_state.gen}_{name}"


def notify_choices() -> list[str]:
    groups = (read_toml(SETTINGS).get("email") or {}).get("groups") or {}
    return list(groups)


def notify_input(label: str, key: str, current: list[str], help_: str) -> list[str]:
    opts = list(dict.fromkeys(notify_choices() + list(current)))
    return st.multiselect(label, opts, default=list(current), key=key, accept_new_options=True, help=help_)


def restaurant_block(r: dict, groups: list[str]) -> dict:
    """Widgets for one restaurant; returns its new raw dict (or None if deleted)."""
    uid = r["uid"]
    title = r.get("name") or r.get("id") or r.get("url") or "new restaurant"
    off = "" if r.get("enabled", True) else "  (disabled)"
    with st.expander(f"{title}{off}", expanded=not (r.get("name") or r.get("url") or r.get("id"))):
        c1, c2, c3 = st.columns([3, 1, 1])
        name = c1.text_input("Name", value=r.get("name", ""), key=k(f"name_{uid}"),
                             help="shown in emails/log; must be unique per platform (default: the id)")
        enabled = c2.checkbox("Enabled", value=r.get("enabled", True), key=k(f"en_{uid}"))
        delete = c3.button("Delete", key=k(f"del_{uid}"))
        url = st.text_input("URL", value=r.get("url", ""), key=k(f"url_{uid}"),
                            help="restaurant page; platform and id are read from it")
        c1, c2, c3 = st.columns(3)
        cur_p = r.get("platform", "")
        platform = c1.selectbox("Platform", PLATFORM_CHOICES,
                                index=PLATFORM_CHOICES.index(cur_p) if cur_p in PLATFORM_CHOICES else 0,
                                key=k(f"plat_{uid}"), help="platform (default: detected from the URL)")
        rid = c2.text_input("ID", value=str(r.get("id", "")), key=k(f"id_{uid}"),
                            help="id (default: read from the URL). Use when there's no URL.")
        dflt_party = int(st.session_state.wl["defaults"].get("party_size", D_PARTY))
        party = c3.number_input("Party size", min_value=1, max_value=20, value=int(r.get("party_size", dflt_party)),
                                key=k(f"party_{uid}"), help=f"party_size (default from [defaults]: {dflt_party})")

        eff_platform = platform if platform in PLATFORMS else (detect_platform(url) if url else None)
        if url and eff_platform:
            try:
                st.caption(f"Detected: {eff_platform} · id `{rid or id_from_url(eff_platform, url)}`")
            except ConfigError as e:
                st.warning(str(e))
        elif url:
            st.warning("Can't tell the platform from this URL; pick one.")

        st.markdown("**Who gets emailed**")
        use_default_notify = st.checkbox("Use [defaults] notify", value="notify" not in r, key=k(f"dn_{uid}"))
        notify = None
        if not use_default_notify:
            notify = notify_input("Notify", k(f"notify_{uid}"), r.get("notify", []),
                                  "group names from settings.toml and/or addresses; empty = settings default_to")

        st.markdown("**When to watch** (Tokyo time)")
        use_default_windows = st.checkbox("Use [defaults] windows", value=not r.get("windows"), key=k(f"dw_{uid}"))
        windows = None
        if not use_default_windows:
            windows = windows_editor(k(f"win_{uid}"), r.get("windows") or [{"days": 14}])

        opts = {}
        if eff_platform in PLATFORM_OPTIONS and PLATFORM_OPTIONS[eff_platform]:
            st.markdown(f"**{eff_platform} options**")
        if eff_platform == "tablecheck":
            c1, c2 = st.columns(2)
            opts["menu_item_id"] = c1.text_input(
                "Course id (menu_item_id)", value=str(r.get("menu_item_id", "")), key=k(f"mi_{uid}"),
                help="pin a course for shops that require one; cuts requests a lot. "
                     "Find ids with `jap r menus <slug>`. Empty = none (default).")
            opts["try_all_menus"] = c2.checkbox(
                "Try every course each time (try_all_menus)", value=bool(r.get("try_all_menus", False)),
                key=k(f"tam_{uid}"), help="slow-mode shops only; default off (retry only the course that worked last)")
        elif eff_platform == "tabelog":
            c1, c2 = st.columns(2)
            opts["seat_only"] = c1.checkbox("Seat-only bookings (seat_only)", value=bool(r.get("seat_only", False)),
                                            key=k(f"so_{uid}"), help="default off")
            opts["plan_id"] = c2.text_input("Course plan id (plan_id)", value=str(r.get("plan_id", "")),
                                            key=k(f"pid_{uid}"), help="a specific course plan; empty = any (default)")
        elif eff_platform == "pocketconcierge":
            opts["include_waitlist"] = st.checkbox(
                "Also report waitlist-only seatings (include_waitlist)",
                value=bool(r.get("include_waitlist", False)), key=k(f"wl_{uid}"), help="default off")
        elif eff_platform == "omakase":
            st.caption("OMAKASE needs `jap r omakase-login` first; its slot parser isn't written yet.")

    if delete:
        return None
    out = {"uid": uid, "name": name.strip(), "enabled": enabled, "url": url.strip(),
           "platform": platform if platform in PLATFORMS else "", "id": rid.strip(), "party_size": int(party),
           "_windows_rows": windows, "_opts": opts}
    if notify is not None:
        out["notify"] = notify
    out["_extra"] = {k: v for k, v in r.items()
                     if k not in RESTAURANT_KEYS | ALL_OPTION_KEYS | {"uid", "_windows_rows", "_opts", "_extra"}}
    return out


def tab_watchlist():
    init_watchlist_state()
    wl = st.session_state.wl
    d = wl["defaults"]
    st.caption(f"File: `{WATCHLIST}`. All dates and times are Tokyo time (JST).")
    c1, c2 = st.columns([1, 5])
    if c1.button("Reload from file"):
        init_watchlist_state(force=True)
        st.rerun()

    st.subheader("Defaults")
    st.caption("Used by any restaurant that doesn't set its own.")
    c1, c2 = st.columns([1, 3])
    d_party = c1.number_input("Party size", min_value=1, max_value=20, value=int(d.get("party_size", D_PARTY)),
                              key=k("d_party"), help=f"[defaults] party_size (built-in default {D_PARTY})")
    with c2:
        d_notify = notify_input("Notify", k("d_notify"), d.get("notify", []),
                                "[defaults] notify: group names from settings.toml and/or addresses; "
                                "empty = settings default_to")
    st.markdown("**Default windows**")
    d_windows = windows_editor(k("d_windows"), d.get("windows", []))

    st.subheader("Restaurants")
    groups = notify_choices()
    new_list = []
    for r in wl["restaurants"]:
        res = restaurant_block(r, groups)
        if res is not None:
            new_list.append(res)
    c1, c2 = st.columns([1, 1])
    if c1.button("+ Add restaurant"):
        wl["restaurants"].append({"uid": uuid.uuid4().hex[:8], "windows": [{"days": 90}]})
        st.rerun()
    if len(new_list) != len(wl["restaurants"]):  # a Delete was pressed
        keep = {r["uid"] for r in new_list}
        wl["restaurants"] = [r for r in wl["restaurants"] if r["uid"] in keep]
        st.rerun()

    if not c2.button("Save watchlist.toml", type="primary"):
        return
    try:
        doc = build_watchlist(d_party, d_notify, d_windows, new_list)
    except ConfigError as e:
        st.error(f"Not saved: {e}")
        return

    def check(p: Path):
        load_watchlist(p)
        rs = tomllib.loads(p.read_text(encoding="utf-8"))
        for r in rs.get("restaurants", []):  # group names must exist in settings.toml
            names = r.get("notify", rs.get("defaults", {}).get("notify", []))
            unknown = [n for n in names if "@" not in n and n not in groups]
            if unknown:
                raise ConfigError(f"{r.get('name') or r.get('id') or r.get('url')}: unknown group(s) "
                                  f"{', '.join(unknown)} (groups in settings.toml: {', '.join(groups) or 'none'})")

    err = write_checked(WATCHLIST, tomlkit.dumps(doc), check)
    if err:
        st.error(f"Not saved: {err}")
    else:
        init_watchlist_state(force=True)
        st.success("Saved watchlist.toml (old version backed up in .state/backups/). A running watcher picks it up within seconds and checks again.")


def build_watchlist(d_party, d_notify, d_windows, restaurants):
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Written by the jap UI (jap r ui). Restaurants only; all dates/times are Tokyo time (JST)."))
    doc.add(tomlkit.comment("Run options and the email account live in settings.toml."))
    doc.add(tomlkit.nl())
    dt = tomlkit.table()
    commented(dt, "party_size", int(d_party))
    commented(dt, "notify", list(d_notify), "group names from settings.toml and/or addresses")
    aot = windows_aot(d_windows, "[defaults]")
    if len(aot):
        dt.add("windows", aot)
    doc.add("defaults", dt)

    out = tomlkit.aot()
    for r in restaurants:
        label = r["name"] or r["id"] or r["url"] or "a restaurant"
        if not (r["url"] or r["id"]):
            raise ConfigError(f"{label}: needs a URL or an ID")
        t = tomlkit.table()
        if r["name"]:
            t.add("name", r["name"])
        if r["url"]:
            t.add("url", r["url"])
        if r["platform"]:
            t.add("platform", r["platform"])
        if r["id"]:
            t.add("id", r["id"])
        t.add("party_size", r["party_size"])
        if not r["enabled"]:
            t.add("enabled", False)
        if "notify" in r:
            t.add("notify", list(r["notify"]))
        for k, v in r["_opts"].items():
            if v not in ("", False, None):
                t.add(k, v)
        for k, v in r["_extra"].items():
            t.add(k, v)
        if r["_windows_rows"] is not None:
            w = windows_aot(r["_windows_rows"], label)
            if not len(w):
                raise ConfigError(f"{label}: add a window or tick 'Use [defaults] windows'")
            t.add("windows", w)
        out.append(t)
    if len(out):
        doc.add("restaurants", out)
    return doc


def main():
    st.set_page_config(page_title="jap watcher", page_icon="🍣", layout="wide")
    st.title("Restaurant watcher")
    t1, t2, t3 = st.tabs(["Watcher", "Settings (settings.toml)", "Watchlist (watchlist.toml)"])
    with t1:
        tab_watcher()
    with t2:
        tab_settings()
    with t3:
        tab_watchlist()


main()
