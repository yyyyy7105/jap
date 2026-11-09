"""Run every restaurant in the watchlist through its platform provider."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

from .models import CheckResult, Restaurant, ScanInfo, Slot, describe_windows
from .providers import PROVIDERS, Provider


class Checker:
    def __init__(self, state_dir: Path = Path(".state"), delay: float = 0.4, headless: bool = True, progress=None):
        self.state_dir = Path(state_dir)
        self.delay = delay
        self.headless = headless
        self.progress = progress  # optional callable(str)
        self.last_scan: ScanInfo | None = None
        self._prefix = ""  # "[i/n]" while a round is running
        self._providers: dict[str, Provider] = {}

    def provider(self, platform: str) -> Provider:
        if platform not in self._providers:
            cls = PROVIDERS[platform]
            kw = {"delay": self.delay}
            if platform == "omakase":
                kw |= {"state_dir": self.state_dir, "headless": self.headless}
            self._providers[platform] = cls(**kw)
            self._providers[platform].progress = self._emit
        return self._providers[platform]

    def set_delay(self, delay: float) -> None:
        """Change the pause between requests, also for providers already created."""
        self.delay = delay
        for p in self._providers.values():
            p.delay = delay

    def _emit(self, msg: str) -> None:
        if self.progress:
            self.progress(f"{self._prefix} {msg}" if self._prefix else msg)

    def check_one(self, r: Restaurant) -> CheckResult:
        try:
            return CheckResult(r, self.provider(r.platform).fetch(r))
        except httpx.HTTPError as e:
            return CheckResult(r, error=f"network error: {e}")
        except Exception as e:  # keep the run going for other restaurants
            return CheckResult(r, error=f"{type(e).__name__}: {e}")

    def check_all(self, restaurants: list[Restaurant], on_result=None) -> list[CheckResult]:
        """on_result(i, total, result, seconds) is called as each restaurant finishes."""
        started = datetime.now().astimezone()
        ranges = {r.name: describe_windows(r) for r in restaurants}  # fix rolling windows at scan start
        out = []
        for i, r in enumerate(restaurants, 1):
            self._prefix = f"[{i}/{len(restaurants)}]"
            self._emit(f"{r.name} ({r.platform})")
            t0 = datetime.now()
            out.append(self.check_one(r))
            if on_result:
                on_result(i, len(restaurants), out[-1], (datetime.now() - t0).total_seconds())
        self._prefix = ""
        self.last_scan = ScanInfo(started, datetime.now().astimezone(), ranges)
        return out

    def close(self) -> None:
        for p in self._providers.values():
            p.close()


class SeenStore:
    """Remembers which slots were already reported so watch mode can flag NEW ones."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seen: dict[str, str] = {}
        if self.path.exists():
            try:
                self.seen = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self.seen = {}

    def split(self, slots: list[Slot]) -> tuple[list[Slot], list[Slot]]:
        new = [s for s in slots if s.key not in self.seen]
        old = [s for s in slots if s.key in self.seen]
        return new, old

    def update(self, results: list[CheckResult]) -> None:
        """For each successfully checked restaurant, keep only its currently-open slots
        (so a slot that closes and reopens is NEW again). Restaurants that weren't
        checked this run, or errored, keep what was remembered for them."""
        now = datetime.now().isoformat(timespec="seconds")
        checked = {f"{res.restaurant.platform}:{res.restaurant.name}:" for res in results if not res.error}
        kept = {k: v for k, v in self.seen.items() if not any(k.startswith(p) for p in checked)}
        for res in results:
            if not res.error:
                kept |= {s.key: self.seen.get(s.key, now) for s in res.slots}
        self.seen = kept
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.seen, indent=2), encoding="utf-8")
