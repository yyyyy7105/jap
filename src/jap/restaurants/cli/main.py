"""`jap restaurants ...` (alias `jap r`) commands.

The checking, email and watch loop live in ``jap.restaurants.watch`` and are shared
with the UI; this module only parses arguments and prints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from jap.restaurants import watch
from jap.restaurants.config import ConfigError, load_settings, load_watchlist
from jap.restaurants.notify import EmailConfig, NotifyError, build_message, load_dotenv, send
from jap.restaurants.report import _windows, console

EMAIL_HELP = (
    "email new slots right away (+ a daily digest if [email] send_at is set; default: [email] enabled in settings.toml)"
)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("restaurants", aliases=["r"], help="restaurant availability watcher")
    p.add_argument("-c", "--config", default="watchlist.toml", help="restaurants file (default: watchlist.toml)")
    p.add_argument("-s", "--settings", default="settings.toml", help="settings/email file (default: settings.toml)")
    p.add_argument("--state-dir", default=".state", help="where seen-slots, email state, the log and the PID file live")
    rs = p.add_subparsers(dest="action", required=True)

    rs.add_parser("list", help="show the parsed watchlist and who gets emailed")
    c = rs.add_parser("check", help="check availability once")
    c.add_argument("--new-only", action="store_true", help="only list slots not seen on the previous run")
    w = rs.add_parser("watch", help="check repeatedly in this terminal; NEW slots are highlighted")
    w.add_argument("-i", "--interval", type=int, default=None, help="seconds between checks (default from settings, else 600)")
    w.add_argument("--new-only", action="store_true", help="only list newly opened slots each round")
    w.add_argument("--email", action=argparse.BooleanOptionalAction, default=None, help=EMAIL_HELP)
    s = rs.add_parser("start", help="start the watcher in the background (with email); same as the UI's Start")
    s.add_argument("--no-email", action="store_true", help="don't send email")
    rs.add_parser("stop", help="stop the background watcher")
    rs.add_parser("status", help="is the background watcher running? + the end of the log")
    rs.add_parser("digest", help="check now and send everyone the full list of open slots")
    t = rs.add_parser("test-email", help="send a test email (default: everyone any restaurant notifies)")
    t.add_argument("--to", action="append", default=[], help="address or group to send to instead")
    m = rs.add_parser("menus", help="list a TableCheck shop's course ids (for `menu_item_id`)")
    m.add_argument("shop", help="TableCheck slug or URL")
    m.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    u = rs.add_parser("ui", help="open the settings/watchlist UI in the browser (needs: uv sync --extra ui)")
    u.add_argument("--port", type=int, default=8501)
    u.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    u.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0 = whole network; 127.0.0.1 = this PC only)")
    rs.add_parser("omakase-login", help="open a browser to sign in to OMAKASE (session is saved)")
    pr = rs.add_parser("omakase-probe", help="capture an OMAKASE restaurant page for parser development")
    pr.add_argument("slug")
    p.set_defaults(func=run)


def run(args) -> int:
    state = Path(args.state_dir)
    action = args.action

    if action in ("omakase-login", "omakase-probe"):
        from jap.restaurants.providers import OmakaseProvider

        prov = OmakaseProvider(state_dir=state, headless=False)
        if action == "omakase-login":
            prov.login()
        else:
            console.print(f"Saved capture to [bold]{prov.probe(args.slug)}[/]")
        return 0
    if action == "ui":
        return _ui(args)
    if action == "menus":
        return _menus(args)
    if action == "start":
        try:
            pid = watch.start_background(args.config, args.settings, state, email=not args.no_email)
        except RuntimeError as e:
            console.print(f"[yellow]Not started:[/] {e}")
            return 1
        console.print(f"Watcher started in the background (PID {pid}). Log: {watch.log_path(state)}")
        return 0
    if action == "stop":
        try:
            pid = watch.stop_background(state)
        except RuntimeError as e:
            console.print(f"[red]{e}[/]")
            return 1
        console.print(f"Watcher stopped (PID {pid})." if pid else "Watcher is not running.")
        return 0
    if action == "status":
        pid = watch.running_pid(state)
        console.print(f"[green]Running[/] (PID {pid})" if pid else "Not running")
        log = watch.log_path(state)
        if log.exists():
            console.print(f"[dim]--- last lines of {log} ---[/]")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]))
        return 0

    load_dotenv()
    if action in ("list", "test-email"):
        try:
            restaurants = load_watchlist(args.config)
            email_cfg = EmailConfig.from_settings(load_settings(args.settings).get("email"))
            rcpts = watch.recipients_by_restaurant(restaurants, email_cfg)
        except (ConfigError, NotifyError) as e:
            console.print(f"[red]Config error:[/] {e}")
            return 2
        if action == "list":
            if not restaurants:
                console.print("[yellow]No restaurants to check.[/]")
                return 1
            for r in restaurants:
                who = ", ".join(rcpts.get((r.platform, r.name), [])) or "[dim]nobody[/]"
                console.print(f"- [bold]{r.name}[/] ({r.platform}:{r.id}) pax={r.party_size}  {_windows(r)}  -> {who}")
            if not email_cfg:
                console.print(f"[dim](no [email] in {args.settings}: email alerts unavailable)[/]")
            return 0
        return _test_email(args, email_cfg, rcpts)

    # check / digest / watch: the shared session (prints here and appends to the log)
    email = {"check": False, "digest": True}.get(action, getattr(args, "email", None))
    background = os.environ.get("JAP_BACKGROUND") == "1"
    out = watch.Output(state, terminal=not background)
    try:
        session = watch.WatchSession(args.config, args.settings, state, out, email=email,
                                     interval=getattr(args, "interval", None),
                                     show_all=not getattr(args, "new_only", False),
                                     source=os.environ.get("JAP_SOURCE", "cli"))
    except (ConfigError, NotifyError) as e:
        out.log(f"[red]Config error:[/] {e}")
        out.close()
        return 2
    if not session.restaurants:
        out.print("[yellow]No restaurants to check.[/]")
        return 1
    try:
        if action == "check":
            session.one_round()
        elif action == "digest":
            session.warn_silent()
            session.one_round(force_digest=True)
        else:
            session.run(bell=console.bell)
        return 0
    except KeyboardInterrupt:
        out.log("Stopped.")
        return 0
    finally:
        session.close()
        out.close()


def _test_email(args, email_cfg, rcpts) -> int:
    if not email_cfg:
        console.print(f"[red]No [email] section in {args.settings}.[/]")
        return 2
    try:
        to = email_cfg.resolve(args.to, "--to") if args.to else sorted({a for v in rcpts.values() for a in v})
    except NotifyError as e:
        console.print(f"[red]{e}[/]")
        return 2
    if not to:
        console.print("[red]Nobody to send to: no restaurant has `notify` and there's no [email] default_to.[/]")
        return 2
    from datetime import date, time as dtime

    from jap.restaurants.models import Slot

    sample = Slot(date.today(), dtime(19, 0), "Test restaurant", "test", 2, "test message", "https://example.com")
    failed = False
    for addr in to:  # one message per person, like real alerts
        try:
            send(email_cfg, build_message(email_cfg, [sample], [], [addr]))
            console.print(f"[green]sent[/] {addr}")
        except NotifyError as e:
            console.print(f"[red]failed[/] {addr}: {e}")
            failed = True
    return 1 if failed else 0


def _menus(args) -> int:
    from datetime import date

    from jap.restaurants.config import id_from_url
    from jap.restaurants.models import Restaurant
    from jap.restaurants.providers import TableCheckProvider

    slug = id_from_url("tablecheck", args.shop) if "/" in args.shop else args.shop
    d = date.fromisoformat(args.date) if args.date else date.today()
    _, menu, _ = TableCheckProvider()._reserve_page(Restaurant(slug, "tablecheck", slug, 2, ()), d)
    if not menu:
        console.print("No course menu items found (the shop may not require one).")
    for mid, label in menu:
        console.print(f"{mid}  {label}")
    return 0


def _ui(args) -> int:
    import importlib.util
    import subprocess

    if importlib.util.find_spec("streamlit") is None or importlib.util.find_spec("tomlkit") is None:
        console.print("[red]The UI needs extra packages:[/] uv sync --extra ui  (then: uv run jap r ui)")
        return 2
    env = os.environ | {"JAP_WATCHLIST": args.config, "JAP_SETTINGS": args.settings, "JAP_STATE": args.state_dir}
    app = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    # headless: skips Streamlit's first-run email prompt, which hangs without a real terminal
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port),
           "--server.address", args.host, "--server.headless", "true", "--server.runOnSave", "true",
           "--browser.gatherUsageStats", "false"]
    url = f"http://localhost:{args.port}"
    console.print(f"UI at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(3, webbrowser.open, [url]).start()
    return subprocess.call(cmd, env=env)
