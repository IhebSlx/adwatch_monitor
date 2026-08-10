"""TIER 1.5 — render a page in a real browser, for the sites plain HTTP cannot read.

The crawler is `requests.get` plus tag-stripping, which is right for ~95% of the
book: it is fast, has no dependencies and costs nothing. But a site built as a
single-page app ships an empty shell — `<div id="root"></div>` and a script tag —
so the fetch "succeeds", returns 200, and yields no text at all. The company then
enriches to nothing and silently drops out of every list it belongs on.

This module is the fallback for exactly those. It is:

  * FREE — Chromium runs locally, there is no API and no per-page charge. The
    cost is wall-clock (a few seconds) and one browser process.
  * LAST RESORT — only pages whose plain fetch came back under a threshold get
    here, so the common case never pays for it.
  * OPTIONAL — if Playwright is not installed the whole thing degrades to
    "no render available" and the pipeline behaves exactly as it did before.
    Nobody has to install a browser to run AdWatch.

It also repairs LINK DISCOVERY, which is the less obvious half of the problem: a
SPA's navigation is built by JavaScript, so the raw HTML has no <a href> either.
Without rendering we lose not just the homepage text but every subpage with it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Below this many characters of visible text, a plain fetch is treated as having
# failed rather than as having found a small site. Chosen from the observed data:
# genuine one-page shops still yield 800+ characters, while the SPA shells that
# motivated this land near zero.
RENDER_BELOW_CHARS = 400

_NAV_TIMEOUT_MS = 20_000
_SETTLE_MS = 1_200          # let client-side rendering paint after load
_MAX_BYTES = 3_000_000

_available: bool | None = None


def available() -> bool:
    """Is a usable browser present? Cached — the import is the expensive part."""
    global _available
    if _available is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            _available = True
        except Exception:  # noqa: BLE001 — not installed is a normal state
            _available = False
    return _available


def render_html(url: str, timeout_ms: int = _NAV_TIMEOUT_MS) -> str | None:
    """Fully rendered HTML for `url`, or None if it cannot be produced.

    Never raises: a browser failure must degrade to the plain-fetch result, not
    take down the enrichment of a company whose site merely happens to be slow.
    """
    if not available():
        return None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
            try:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    # Same identity the plain crawler uses, so a site cannot show
                    # us one thing over HTTP and another in the browser.
                    user_agent=_ua(),
                    locale="es-ES",
                )
                page = ctx.new_page()
                # Images and fonts cost seconds and tell us nothing — we only ever
                # read text. Stylesheets stay: display:none is how sites hide
                # cookie walls, and dropping CSS would let that text back in.
                page.route("**/*", _block_heavy_assets)
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
                except Exception:  # noqa: BLE001 — a chatty page never idles; take what we have
                    page.wait_for_timeout(_SETTLE_MS)
                html = page.content()
                return html[:_MAX_BYTES] if html else None
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.info("render failed for %s: %s", url, str(exc)[:160])
        return None


def _ua() -> str:
    from ..identity import website_source as ws
    return ws._UA


_HEAVY = ("image", "media", "font")


def _block_heavy_assets(route, request) -> None:
    try:
        if request.resource_type in _HEAVY:
            route.abort()
        else:
            route.continue_()
    except Exception:  # noqa: BLE001 — routing is an optimisation, never a gate
        try:
            route.continue_()
        except Exception:  # noqa: BLE001
            pass
