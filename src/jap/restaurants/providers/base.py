from __future__ import annotations

import time as _time
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta

import httpx

from ..models import Restaurant, Slot

JST = timezone(timedelta(hours=9), "JST")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0 Safari/537.36"
)


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    """Fetches bookable slots for one platform."""

    name: str = ""

    def __init__(self, client: httpx.Client | None = None, delay: float = 0.4):
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=20,
            follow_redirects=True,
        )
        self.delay = delay  # politeness pause between requests
        self.progress = None  # optional callable(str) for live status

    def report(self, msg: str) -> None:
        if self.progress:
            self.progress(msg)

    def report_date(self, name: str, d, n: int, total: int, t=None) -> None:
        """One status format for every site: '<restaurant> · <date> [<time>] · day n/total'."""
        when = f"{d:%a %Y-%m-%d}" + (f" {t:%H:%M}" if t is not None else "")
        self.report(f"{name} · {when} · day {n}/{total}")

    def pause(self, factor: float = 1.0) -> None:
        if self.delay:
            _time.sleep(self.delay * factor)

    def get(self, url: str, *, retries: int = 3, **kw) -> httpx.Response:
        """GET with polite backoff on 429/503 (honours Retry-After)."""
        for attempt in range(retries + 1):
            resp = self.client.get(url, **kw)
            if resp.status_code not in (429, 503) or attempt == retries:
                return resp
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                wait = 0
            wait = min(max(wait, 5 * 2**attempt), 120)
            self.report(f"rate-limited by site, waiting {wait:.0f}s")
            _time.sleep(wait)
        return resp

    @abstractmethod
    def fetch(self, restaurant: Restaurant) -> list[Slot]:
        """Return every open slot inside the restaurant's watch windows."""

    def close(self) -> None:
        self.client.close()


def epoch_to_jst(epoch: int | str) -> datetime:
    return datetime.fromtimestamp(int(epoch), JST)
