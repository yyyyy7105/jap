"""Load the restaurant watchlist from a TOML file."""

from __future__ import annotations

import re
import tomllib
from datetime import date, time
from pathlib import Path
from typing import Any

from .models import WEEKDAYS, Restaurant, Window

PLATFORMS = {"tablecheck", "tabelog", "pocketconcierge", "omakase"}


class ConfigError(ValueError):
    pass


def _parse_time(v: Any, field: str) -> time:
    if isinstance(v, time):
        return v
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})", str(v).strip())
    if not m:
        raise ConfigError(f"{field}: expected HH:MM, got {v!r}")
    return time(int(m[1]), int(m[2]))


def _parse_date(v: Any, field: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ConfigError(f"{field}: expected YYYY-MM-DD, got {v!r}") from e


def _parse_window(raw: dict, where: str) -> Window:
    kinds = [k for k in ("days", "date", "start_date") if k in raw]
    if len(kinds) > 1:
        raise ConfigError(f"{where}: use only one of `days`, `date` or `start_date`/`end_date` (got {', '.join(kinds)})")
    start = end = rolling = None
    if "days" in raw:  # rolling: today (Tokyo) + the next N-1 days
        try:
            rolling = int(raw["days"])
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{where}.days: expected a whole number, got {raw['days']!r}") from e
        if not 1 <= rolling <= 366:
            raise ConfigError(f"{where}.days: must be between 1 and 366, got {rolling}")
    elif "date" in raw:  # single-day shorthand
        start = end = _parse_date(raw["date"], f"{where}.date")
    else:
        if "start_date" not in raw:
            raise ConfigError(f"{where}: needs `days`, `date` or `start_date`")
        start = _parse_date(raw["start_date"], f"{where}.start_date")
        end = _parse_date(raw.get("end_date", raw["start_date"]), f"{where}.end_date")
        if end < start:
            raise ConfigError(f"{where}: end_date is before start_date")

    weekdays = None
    if raw.get("weekdays"):
        try:
            weekdays = frozenset(WEEKDAYS.index(str(w).lower()[:3]) for w in raw["weekdays"])
        except ValueError as e:
            raise ConfigError(f"{where}.weekdays: use mon..sun, got {raw['weekdays']!r}") from e

    return Window(
        start_date=start,
        end_date=end,
        start_time=_parse_time(raw.get("start_time", "00:00"), f"{where}.start_time"),
        end_time=_parse_time(raw.get("end_time", "23:59"), f"{where}.end_time"),
        weekdays=weekdays,
        rolling_days=rolling,
    )


def id_from_url(platform: str, url: str) -> str:
    """Pull the platform id out of a restaurant URL."""
    patterns = {
        # .../en/shops/<slug>/reserve  or the short form  .../ja/<slug>
        "tablecheck": r"tablecheck\.com/(?:[a-z]{2}(?:-[a-zA-Z]+)?/)?(?:shops/)?(?!shops\b)([^/?#]+)",
        "tabelog": r"tabelog\.com/.*?/(\d{8})(?:/|$|\?)",
        "omakase": r"omakase\.in/(?:[a-z]{2}/)?r/([^/?#]+)",
        "pocketconcierge": r"pocket-concierge\.jp/(?:[a-z]{2}(?:-[a-zA-Z]+)?/)?restaurants/(\d+)",
    }
    m = re.search(patterns[platform], url)
    if not m:
        raise ConfigError(f"couldn't find a {platform} restaurant id in {url!r}")
    return m[1]


def detect_platform(url: str) -> str | None:
    for p, host in (
        ("tablecheck", "tablecheck.com"),
        ("tabelog", "tabelog.com"),
        ("omakase", "omakase.in"),
        ("pocketconcierge", "pocket-concierge.jp"),
    ):
        if host in url:
            return p
    return None


def _str_list(v, where: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise ConfigError(f"{where}: expected a list of strings")
    return tuple(str(x).strip() for x in v if str(x).strip())


def load_watchlist(path: str | Path) -> list[Restaurant]:
    """Restaurants file: [defaults] + [[restaurants]] only."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"watchlist not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    misplaced = [k for k in ("settings", "notify", "email") if k in data]
    if misplaced:
        raise ConfigError(
            f"{path.name}: [{'], ['.join(misplaced)}] belong in settings.toml now "
            "(the watchlist only holds [defaults] and [[restaurants]])"
        )

    defaults = data.get("defaults", {})
    default_party = int(defaults.get("party_size", 2))
    default_windows = [_parse_window(w, f"defaults.windows[{i}]") for i, w in enumerate(defaults.get("windows", []))]
    default_notify = _str_list(defaults.get("notify"), "defaults.notify")

    restaurants: list[Restaurant] = []
    for i, r in enumerate(data.get("restaurants", [])):
        where = f"restaurants[{i}]"
        if r.get("enabled", True) is False:
            continue
        url = r.get("url")
        platform = (r.get("platform") or (detect_platform(url) if url else None) or "").lower()
        if platform not in PLATFORMS:
            raise ConfigError(f"{where}: platform must be one of {sorted(PLATFORMS)} (or give a recognisable url)")
        rid = str(r.get("id") or (id_from_url(platform, url) if url else ""))
        if not rid:
            raise ConfigError(f"{where}: needs `id` or `url`")

        windows = [_parse_window(w, f"{where}.windows[{j}]") for j, w in enumerate(r.get("windows", []))]
        if not windows:
            windows = default_windows
        if not windows:
            raise ConfigError(f"{where}: no watch windows (add [[restaurants.windows]] or [[defaults.windows]])")

        notify = _str_list(r["notify"], f"{where}.notify") if "notify" in r else default_notify
        known = {"name", "platform", "id", "url", "party_size", "windows", "enabled", "notify"}
        restaurants.append(
            Restaurant(
                name=r.get("name") or rid,
                platform=platform,
                id=rid,
                party_size=int(r.get("party_size", default_party)),
                windows=tuple(windows),
                url=url,
                notify=notify,
                options={k: v for k, v in r.items() if k not in known},
            )
        )
    names = [(r.platform, r.name) for r in restaurants]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate restaurant names: {', '.join(n for _, n in dupes)} (give each a unique `name`)")
    return restaurants


def load_settings(path: str | Path) -> dict:
    """Settings file: [settings] (run options) and [email] (account, cap, groups).
    A missing file just means defaults and no email."""
    path = Path(path)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = dict(data.get("settings", {}))
    out["email"] = data.get("email")
    return out
