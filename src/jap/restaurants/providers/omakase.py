"""OMAKASE (omakase.in) - browser-driven, uses YOUR logged-in session.

OMAKASE only shows availability to signed-in members and its restaurant pages
are behind a bot check, so plain HTTP doesn't work. Instead we drive a real
Chromium (Playwright) with a persistent profile stored in ``.state/omakase-profile``:

1. ``jap restaurants omakase-login`` opens a visible browser; you sign in
   yourself (and pass any check the site shows). The session is kept.
2. ``jap restaurants omakase-probe <slug>`` loads a restaurant page with that
   session and saves the page HTML and every JSON response it receives to
   ``.state/omakase-probe/``. Those captures are what the slot parser is
   built from.

The slot parser itself is NOT written yet: it needs a real logged-in capture
to target (I couldn't see the page without your account). Until then
``fetch`` raises a clear error instead of guessing.

Requires the optional extra:  ``uv sync --extra omakase``  then
``uv run playwright install chromium``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import Restaurant, Slot
from .base import Provider, ProviderError

SITE = "https://omakase.in"


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise ProviderError(
            "OMAKASE needs Playwright: run `uv sync --extra omakase` then `uv run playwright install chromium`"
        ) from e
    return sync_playwright


class OmakaseProvider(Provider):
    name = "omakase"

    def __init__(
        self, *a, state_dir: Path = Path(".state"), headless: bool = True, channel: str | None = "chrome", **kw
    ):
        super().__init__(*a, **kw)
        self.state_dir = Path(state_dir)
        self.profile = self.state_dir / "omakase-profile"
        self.headless = headless
        # "chrome" / "msedge" = use the installed browser; None = Playwright's bundled Chromium
        self.channel = channel or None

    def _launch(self, p, headless: bool):
        kw = {"headless": headless}
        if self.channel:
            kw["channel"] = self.channel
        return p.chromium.launch_persistent_context(str(self.profile), **kw)

    # -- session ---------------------------------------------------------
    def login(self) -> None:
        """Open a visible browser so the user can sign in themselves."""
        self.profile.mkdir(parents=True, exist_ok=True)
        with _playwright()() as p:
            ctx = self._launch(p, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{SITE}/en/users/sign_in")
            print("Sign in to OMAKASE in the opened window, then close the window to finish.")
            page.wait_for_event("close", timeout=0)
            ctx.close()

    def probe(self, slug: str) -> Path:
        """Save HTML + JSON responses of a restaurant page for parser development."""
        out = self.state_dir / "omakase-probe" / re.sub(r"[^\w.-]", "_", slug)
        out.mkdir(parents=True, exist_ok=True)
        captured: list[dict] = []

        with _playwright()() as p:
            ctx = self._launch(p, headless=self.headless)
            page = ctx.new_page()

            def on_response(resp):
                if "json" in (resp.headers.get("content-type") or "") and "omakase.in" in resp.url:
                    try:
                        captured.append({"url": resp.url, "status": resp.status, "body": resp.json()})
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(f"{SITE}/en/r/{slug}", wait_until="networkidle")
            (out / "page.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(out / "page.png"), full_page=True)
            ctx.close()

        (out / "responses.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    # -- availability ----------------------------------------------------
    def fetch(self, r: Restaurant) -> list[Slot]:
        if not self.profile.exists():
            raise ProviderError("not signed in - run `uv run jap restaurants omakase-login` first")
        raise ProviderError(
            "OMAKASE slot parsing isn't implemented yet - run "
            f"`uv run jap restaurants omakase-probe {r.id}` and share the capture so it can be built"
        )
