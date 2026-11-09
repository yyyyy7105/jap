"""The watch logic shared by the CLI (``jap r check / digest / watch / start / stop``)
and the UI (its Start / Stop buttons and log view).

* :class:`WatchSession` loads the two config files, runs check rounds, sends email,
  and (in :meth:`WatchSession.run`) loops forever, reloading the files when they change.
* :class:`Output` writes everything a round prints to the terminal (if there is one)
  and appends it to ``<state>/watch.log``. Every run, from the CLI or the UI, logs there.
* :func:`start_background` / :func:`stop_background` / :func:`running_pid` run the
  watcher as a detached process tracked by ``<state>/watch.pid``.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console

from .checker import Checker, SeenStore
from .config import ConfigError, load_settings, load_watchlist
from .notify import DailyDigest, EmailConfig, NotifyError, load_dotenv
from .report import console as terminal_console
from .report import print_results

LOG_NAME = "watch.log"
PID_NAME = "watch.pid"
CHECK_PID_NAME = "check.pid"  # a one-off background check (the UI's Check now)
ERR_NAME = "watch.err.log"  # Python crashes of the background process (normally empty)
MAX_LOG_BYTES = 5_000_000  # rotate to watch.log.1 beyond this


def log_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / LOG_NAME


def pid_path(state_dir: str | Path, name: str = PID_NAME) -> Path:
    return Path(state_dir) / name


# -- output: terminal + shared log file ---------------------------------------
class Output:
    """Prints to the terminal console (optional) and appends the same text to the log."""

    def __init__(self, state_dir: str | Path, terminal: bool = True):
        self.term = terminal_console if terminal else None
        path = log_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        self.file = Console(file=self._fh, width=150, color_system=None, force_terminal=False,
                            highlight=False, emoji=False, log_path=False)

    def print(self, *objects, **kw) -> None:
        if self.term:
            self.term.print(*objects, **kw)
        self.file.print(*objects, **kw)
        self._fh.flush()

    def rule(self, *args, **kw) -> None:
        if self.term:
            self.term.rule(*args, **kw)
        self.file.rule(*args, **kw)

    def log(self, msg: str) -> None:
        """One timestamped line."""
        self.print(f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/] {msg}")

    @contextmanager
    def status(self, msg: str = ""):
        """A live spinner on the terminal only (not logged)."""
        if self.term and self.term.is_terminal:
            with self.term.status(msg, spinner="dots") as st:
                yield st
        else:
            class _Quiet:
                def update(self, *_a, **_k):
                    pass

            yield _Quiet()

    def close(self) -> None:
        self._fh.close()


def _mtimes(*paths) -> tuple:
    return tuple(Path(p).stat().st_mtime if Path(p).exists() else None for p in paths)


def recipients_by_restaurant(restaurants, email_cfg: EmailConfig | None) -> dict[tuple[str, str], list[str]]:
    if not email_cfg:
        return {}
    return {(r.platform, r.name): email_cfg.resolve(r.notify, where=f"restaurant {r.name!r} notify") for r in restaurants}


def wait_with_countdown(out: Output, seconds: float, digest_at=None, stop_early=None) -> bool:
    """Sleep, showing a live 'next check at ...' line on a terminal.
    stop_early() is polled every second; returns True if it ended the wait."""
    end = time.monotonic() + seconds
    at = datetime.now() + timedelta(seconds=seconds)
    extra = f" · digest email at {digest_at:%a %H:%M}" if digest_at else ""
    with out.status() as status:
        while (left := end - time.monotonic()) > 0:
            m, s = divmod(int(left + 0.999), 60)
            status.update(f"[dim]Next check at {at:%H:%M:%S} (in {m}:{s:02d}){extra} - Ctrl+C to stop[/]")
            time.sleep(min(1.0, left))
            if stop_early and stop_early():
                return True
    return False


# -- the session ------------------------------------------------------------------
class WatchSession:
    """Config + checker + email for one CLI command or one background watcher.

    email: True/False forces email on/off; None = settings.toml [email] enabled.
    """

    def __init__(self, config: str | Path, settings: str | Path, state_dir: str | Path, out: Output,
                 email: bool | None = None, interval: int | None = None, show_all: bool = True,
                 source: str = "cli"):
        self.source = source  # shown in the log: where this run was started from
        self.config, self.settings_path, self.state = Path(config), Path(settings), Path(state_dir)
        self.out, self.email_arg, self.interval_arg, self.show_all = out, email, interval, show_all
        load_dotenv()
        self.restaurants = load_watchlist(self.config)  # ConfigError / NotifyError propagate to the caller
        self.settings = load_settings(self.settings_path)
        self.email_cfg = EmailConfig.from_settings(self.settings.get("email"))
        self.rcpts = recipients_by_restaurant(self.restaurants, self.email_cfg)
        self.checker = Checker(
            state_dir=self.state,
            delay=float(self.settings.get("request_delay", 0.4)),
            headless=bool(self.settings.get("headless", True)),
        )
        self.seen = SeenStore(self.state / "seen_slots.json")
        self.digest: DailyDigest | None = None
        if self.wants_email(self.email_cfg):
            if not self.email_cfg:
                raise NotifyError(f"email needs an [email] section in {self.settings_path}")
            self.digest = DailyDigest(self.email_cfg, self.state / "email_digest.json")

    def wants_email(self, ec: EmailConfig | None) -> bool:
        return (ec.enabled if ec else False) if self.email_arg is None else bool(self.email_arg)

    @property
    def interval(self) -> int:
        return self.interval_arg or int(self.settings.get("interval_seconds", 600))

    def recipients(self) -> list[str]:
        return sorted({a for v in self.rcpts.values() for a in v})

    def warn_silent(self) -> None:
        if self.digest:
            silent = [r.name for r in self.restaurants if not self.rcpts.get((r.platform, r.name))]
            if silent:
                self.out.print(f"[yellow]No email recipients for:[/] {', '.join(silent)} (add `notify = [...]`)")

    # -- reload ----------------------------------------------------------------
    def reload(self) -> bool:
        """Re-read both files. A broken file keeps the previous config. True if applied."""
        try:
            rs = load_watchlist(self.config)
            st = load_settings(self.settings_path)
            ec = EmailConfig.from_settings(st.get("email"))
            rc = recipients_by_restaurant(rs, ec)
        except (ConfigError, NotifyError, ValueError) as e:
            self.out.log(f"[red]Config changed but has an error, keeping the previous one:[/] {e}")
            return False
        want = self.wants_email(ec)
        if want and not ec:
            self.out.log(f"[red]Config changed: email is on but {self.settings_path} has no [email]; email paused.[/]")
            want = False
        if want:
            self.digest = self.digest or DailyDigest(ec, self.state / "email_digest.json")
            self.digest.cfg = ec
        else:
            self.digest = None
        self.restaurants, self.settings, self.email_cfg, self.rcpts = rs, st, ec, rc
        self.checker.set_delay(float(st.get("request_delay", 0.4)))
        self.checker.headless = bool(st.get("headless", True))
        self.out.log(f"[cyan]Config reloaded:[/] {len(rs)} restaurant(s), email {'on' if self.digest else 'off'}")
        return True

    # -- one round -------------------------------------------------------------
    def one_round(self, force_digest: bool = False) -> list:
        self.out.log(f"Check started ({self.source}): {len(self.restaurants)} restaurant(s)"
                     + (", email on" if self.digest else ""))

        def progress(msg: str, status) -> None:
            status.update(f"Checking {msg}")
            if "rate-limited" in msg:  # long waits are worth seeing in the log
                self.out.log(f"  [yellow]{msg}[/]")

        def done(i, n, res, secs) -> None:
            if res.error:
                what = f"[red]error[/] {res.error}"
            else:
                what = f"[green]{len(res.slots)} open[/]" if res.slots else "none open"
            self.out.log(f"  [{i}/{n}] {res.restaurant.name}: {what} ({secs:.0f}s)")

        with self.out.status("Checking...") as status:
            self.checker.progress = lambda m: progress(m, status)
            try:
                results = self.checker.check_all(self.restaurants, on_result=done)
            finally:
                self.checker.progress = None
        current = [s for res in results for s in res.slots]
        new, _ = self.seen.split(current)
        scan = self.checker.last_scan
        print_results(results, {s.key for s in new}, show_all=self.show_all, scan=scan, console=self.out)
        self.seen.update(results)
        if self.digest:
            rcpts = self.rcpts
            report = self.digest.process(
                current,
                self.recipients(),
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
                    self.out.log(f"[green]Digest sent to {addr}: {len(n_new)} new, {len(n_new) + len(n_old)} open[/]")
                elif status == "alert sent":
                    self.out.log(f"[green]New-slot alert sent to {addr}: {len(n_new)} slot(s)[/]")
                elif "skipped" in status:
                    self.out.log(f"[dim]{addr}: {status}[/]")
                else:
                    self.out.log(f"[red]{addr}: {status}[/] (will retry next round)")
        return new

    # -- forever ---------------------------------------------------------------
    def run(self, bell=None) -> None:
        """Check, wait, repeat. Edits to either config file are picked up within a
        second and trigger a fresh check. Stops on KeyboardInterrupt."""
        mail = ""
        if self.digest:
            mail = f", emailing {len(self.recipients())} recipient(s): new slots right away"
            if self.email_cfg.send_at is not None:
                mail += f" + daily digest at {self.email_cfg.send_at:%H:%M}"
        self.out.log(f"Watching {len(self.restaurants)} restaurant(s) every ~{self.interval}s{mail}.")
        self.out.print(f"[dim]Edits to {self.config} / {self.settings_path} are picked up automatically.[/]")
        self.warn_silent()
        files = (self.config, self.settings_path)
        stamp = _mtimes(*files)
        while True:
            try:
                if self.one_round() and bell:
                    bell()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # never let one bad round end an overnight watch
                self.out.log(f"[red]Round failed:[/] {type(e).__name__}: {e} - will try again next round")
            # jitter so we don't hit the sites on an exact schedule; wake up for the digest time
            wait = self.interval * random.uniform(0.85, 1.15)
            digest_at = self.digest.next_send() if self.digest else None
            if digest_at:
                wait = min(wait, (digest_at - datetime.now()).total_seconds() + 1)
            changed = wait_with_countdown(self.out, max(wait, 1), digest_at, lambda: _mtimes(*files) != stamp)
            if changed or _mtimes(*files) != stamp:
                time.sleep(1)  # let the writer finish
                stamp = _mtimes(*files)
                self.reload()  # then check right away with the new config

    def close(self) -> None:
        self.checker.close()


# -- background watcher (start / stop by PID) ----------------------------------------
def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(state_dir: str | Path, name: str = PID_NAME) -> int | None:
    """PID of the background watcher (or, with name=CHECK_PID_NAME, of a background
    check), or None. Clears a stale PID file."""
    p = pid_path(state_dir, name)
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except ValueError:
        p.unlink(missing_ok=True)
        return None
    if _alive(pid):
        return pid
    p.unlink(missing_ok=True)
    return None


def start_background(config: str | Path, settings: str | Path, state_dir: str | Path,
                     email: bool | None = True, root: str | Path | None = None, source: str = "cli") -> int:
    """Start `jap r watch` detached (survives closing the terminal / UI). Returns its PID."""
    state = Path(state_dir).resolve()
    if (pid := running_pid(state)) is not None:
        raise RuntimeError(f"already running (PID {pid})")
    args = ["watch"] + ([] if email is None else ["--email" if email else "--no-email"])
    return _spawn(config, settings, state, args, PID_NAME, root, f"background watcher, started from {source}")


def start_check_background(config: str | Path, settings: str | Path, state_dir: str | Path,
                           root: str | Path | None = None, source: str = "ui") -> int:
    """One `jap r check` round in the background (no email); its progress goes to the log."""
    state = Path(state_dir).resolve()
    if (pid := running_pid(state, CHECK_PID_NAME)) is not None:
        raise RuntimeError(f"a check is already running (PID {pid})")
    return _spawn(config, settings, state, ["check"], CHECK_PID_NAME, root, source)


def _spawn(config, settings, state: Path, args: list[str], pid_name: str, root, source: str) -> int:
    state.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "jap", "r", "-c", str(Path(config).resolve()), "-s", str(Path(settings).resolve()),
           "--state-dir", str(state), *args]
    env = os.environ | {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "JAP_BACKGROUND": "1",
                        "JAP_SOURCE": source}
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NO_WINDOW)
    else:
        kw["start_new_session"] = True
    err = open(state / ERR_NAME, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=root or Path.cwd(), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=err, **kw)
    err.close()
    pid_path(state, pid_name).write_text(str(proc.pid), encoding="ascii")
    return proc.pid


def stop_background(state_dir: str | Path) -> int | None:
    """Stop the background watcher. Returns the PID stopped, None if none was running.
    Raises RuntimeError if it couldn't be stopped (the PID file is then kept)."""
    pid = running_pid(state_dir)
    if pid is None:
        return None
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(f"could not stop PID {pid} (access denied? it may belong to another user, "
                           "an Administrator window or a scheduled task)")
    pid_path(state_dir).unlink(missing_ok=True)
    Output(state_dir, terminal=False).log(f"Watcher stopped (PID {pid}).")
    return pid


__all__ = ["Output", "WatchSession", "log_path", "pid_path", "running_pid", "start_background",
           "stop_background", "recipients_by_restaurant"]
