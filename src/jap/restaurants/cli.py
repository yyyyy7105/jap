"""`jap restaurants ...` subcommands."""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

from .checker import Checker, SeenStore
from .config import ConfigError, load_settings, load_watchlist
from .notify import DailyDigest, EmailConfig, NotifyError, build_message, load_dotenv, send
from .report import _windows, console, print_results

EMAIL_HELP = (
    "email new slots right away + a daily digest at [email] send_at (default: [email] enabled in settings.toml)"
)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("restaurants", aliases=["r"], help="restaurant availability watcher")
    p.add_argument("-c", "--config", default="watchlist.toml", help="restaurants file (default: watchlist.toml)")
    p.add_argument("-s", "--settings", default="settings.toml", help="settings/email file (default: settings.toml)")
    p.add_argument("--state-dir", default=".state", help="where seen-slots, email queue and browser profiles live")
    rs = p.add_subparsers(dest="action", required=True)

    rs.add_parser("list", help="show the parsed watchlist and who gets emailed")
    c = rs.add_parser("check", help="check availability once")
    c.add_argument("--new-only", action="store_true", help="only list slots not seen on the previous run")
    w = rs.add_parser("watch", help="check repeatedly and highlight newly opened slots")
    w.add_argument("-i", "--interval", type=int, default=None, help="seconds between checks (default from settings, else 600)")
    w.add_argument("--new-only", action="store_true", help="only list newly opened slots each round")
    w.add_argument("--email", action=argparse.BooleanOptionalAction, default=None, help=EMAIL_HELP)
    rs.add_parser("digest", help="check now and send everyone their digest email immediately")
    t = rs.add_parser("test-email", help="send a test email (default: everyone any restaurant notifies)")
    t.add_argument("--to", action="append", default=[], help="address or group to send to instead")
    m = rs.add_parser("menus", help="list a TableCheck shop's service types and course ids")
    m.add_argument("shop", help="TableCheck slug or URL")
    m.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    rs.add_parser("omakase-login", help="open a browser to sign in to OMAKASE (session is saved)")
    pr = rs.add_parser("omakase-probe", help="capture an OMAKASE restaurant page for parser development")
    pr.add_argument("slug")
    p.set_defaults(func=run)


def wait_with_countdown(seconds: float, digest_at=None) -> None:
    """Sleep, showing a live 'next check at ...' line instead of a silent console."""
    from datetime import datetime, timedelta

    end = time.monotonic() + seconds
    at = datetime.now() + timedelta(seconds=seconds)
    extra = f" · digest email at {digest_at:%a %H:%M}" if digest_at else ""
    with console.status("", spinner="dots") as status:
        while (left := end - time.monotonic()) > 0:
            m, s = divmod(int(left + 0.999), 60)
            status.update(f"[dim]Next check at {at:%H:%M:%S} (in {m}:{s:02d}){extra} - Ctrl+C to stop[/]")
            time.sleep(min(1.0, left))


def _recipients_by_restaurant(restaurants, email_cfg: EmailConfig | None) -> dict[tuple[str, str], list[str]]:
    if not email_cfg:
        return {}
    return {(r.platform, r.name): email_cfg.resolve(r.notify, where=f"restaurant {r.name!r} notify") for r in restaurants}


def run(args) -> int:
    state = Path(args.state_dir)

    if args.action in ("omakase-login", "omakase-probe"):
        from .providers import OmakaseProvider

        prov = OmakaseProvider(state_dir=state, headless=False)
        if args.action == "omakase-login":
            prov.login()
        else:
            out = prov.probe(args.slug)
            console.print(f"Saved capture to [bold]{out}[/]")
        return 0

    if args.action == "menus":
        from datetime import date

        from .config import id_from_url
        from .models import Restaurant
        from .providers import TableCheckProvider

        slug = id_from_url("tablecheck", args.shop) if "/" in args.shop else args.shop
        d = date.fromisoformat(args.date) if args.date else date.today()
        prov = TableCheckProvider()
        shop = Restaurant(slug, "tablecheck", slug, 2, ())
        _, menu = prov._reserve_page(shop, d)
        types = prov.service_types(shop, d)
        console.print("[bold]Service types[/] (for `service_type`):")
        if not types:
            console.print("  none - this shop doesn't split seating")
        for tid, label in types:
            console.print(f"  {tid}  {label}")
        console.print("[bold]Courses[/] (for `menu_item_id`):")
        if not menu:
            console.print("  none found (the shop may not require one)")
        for mid, label in menu:
            console.print(f"  {mid}  {label}")
        return 0

    load_dotenv()
    try:
        restaurants = load_watchlist(args.config)
        settings = load_settings(args.settings)
        email_cfg = EmailConfig.from_settings(settings.get("email"))
        rcpts = _recipients_by_restaurant(restaurants, email_cfg)
    except (ConfigError, NotifyError) as e:
        console.print(f"[red]Config error:[/] {e}")
        return 2

    if args.action == "test-email":
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

        from .models import Slot

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

    if not restaurants:
        console.print("[yellow]No restaurants to check.[/]")
        return 1

    if args.action == "list":
        for r in restaurants:
            who = ", ".join(rcpts.get((r.platform, r.name), [])) or "[dim]nobody[/]"
            console.print(f"- [bold]{r.name}[/] ({r.platform}:{r.id}) pax={r.party_size}  {_windows(r)}  -> {who}")
        if not email_cfg:
            console.print(f"[dim](no [email] in {args.settings}: email alerts unavailable)[/]")
        return 0

    if args.action == "digest":
        use_email = True
    elif args.action == "watch":
        use_email = email_cfg.enabled if (email_cfg and args.email is None) else bool(args.email)
    else:
        use_email = False
    if use_email and not email_cfg:
        console.print(f"[red]Email needs an [email] section in {args.settings}.[/]")
        return 2
    if use_email:
        silent = [r.name for r in restaurants if not rcpts.get((r.platform, r.name))]
        if silent:
            console.print(f"[yellow]No email recipients for:[/] {', '.join(silent)} (add `notify = [...]`)")

    checker = Checker(
        state_dir=state,
        delay=float(settings.get("request_delay", 0.4)),
        headless=bool(settings.get("headless", True)),
    )
    seen = SeenStore(state / "seen_slots.json")
    digest = DailyDigest(email_cfg, state / "email_digest.json") if use_email else None
    all_recipients = sorted({a for v in rcpts.values() for a in v})

    def run_round():
        with console.status("Checking...") as status:
            checker.progress = lambda m: status.update(f"Checking {m}")
            try:
                return checker.check_all(restaurants)
            finally:
                checker.progress = None

    def one_round(force_digest: bool = False) -> list:
        results = run_round()
        current = [s for res in results for s in res.slots]
        new, _ = seen.split(current)
        scan = checker.last_scan
        print_results(results, {s.key for s in new}, show_all=not args.new_only, scan=scan)
        seen.update(results)
        if digest:
            report = digest.process(
                current,
                all_recipients,
                lambda s: rcpts.get((s.platform, s.restaurant), []),
                scan=scan,
                restaurants_of=lambda addr: [
                    name for (_, name), who in rcpts.items() if addr.lower() in {a.lower() for a in who}
                ],
                failed={(res.restaurant.platform, res.restaurant.name) for res in results if res.error},
                force=force_digest,
            )
            for addr, (status, n_new, n_old) in sorted(report.items()):
                if status == "digest sent":
                    console.print(f"[green]Digest sent to {addr}: {len(n_new)} new, {len(n_new) + len(n_old)} open[/]")
                elif status == "alert sent":
                    console.print(f"[green]New-slot alert sent to {addr}: {len(n_new)} slot(s)[/]")
                elif "skipped" in status:
                    console.print(f"[dim]{addr}: {status}[/]")
                else:
                    console.print(f"[red]{addr}: {status}[/] (will retry next round)")
        return new

    try:
        if args.action == "check":
            one_round()
            return 0
        if args.action == "digest":
            one_round(force_digest=True)
            return 0

        interval = args.interval or int(settings.get("interval_seconds", 600))
        mail = ""
        if use_email:
            mail = (
                f", emailing {len(all_recipients)} recipient(s): new slots right away"
                f" + daily digest at {email_cfg.send_at:%H:%M}"
            )
        console.print(f"Watching {len(restaurants)} restaurant(s) every ~{interval}s{mail}. Ctrl+C to stop.")
        while True:
            try:
                if one_round():
                    console.bell()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # never let one bad round end an overnight watch
                console.print(f"[red]Round failed:[/] {type(e).__name__}: {e} - will try again next round")
            # jitter so we don't hit the sites on an exact schedule; wake up for the digest time
            wait = interval * random.uniform(0.85, 1.15)
            digest_at = None
            if digest:
                from datetime import datetime

                digest_at = digest.next_send()
                wait = min(wait, (digest_at - datetime.now()).total_seconds() + 1)
            wait_with_countdown(max(wait, 1), digest_at)
    except KeyboardInterrupt:
        console.print("\nStopped.")
        return 0
    finally:
        checker.close()
