"""
Jarvis V2 — Browser Tools
Web search via DuckDuckGo Lite, page visits via Playwright, URL opening.
"""

import asyncio
import logging
import re
import subprocess
import sys
import webbrowser
import xml.etree.ElementTree as ET
from urllib.parse import unquote, parse_qs, urlparse

import httpx
from playwright.async_api import async_playwright

log = logging.getLogger("jarvis.browser")

_browser = None
_context = None

IS_MAC = sys.platform == "darwin"


def _bring_chromium_to_front() -> None:
    """Bring the Playwright Chromium window to the foreground."""
    try:
        if IS_MAC:
            subprocess.run([
                "osascript", "-e",
                'tell application "System Events" to set frontmost of every process whose name contains "Chromium" to true'
            ], capture_output=True, timeout=3)
        else:
            subprocess.run([
                "powershell", "-Command",
                '(Get-Process -Name "chromium","chrome" -ErrorAction SilentlyContinue | '
                'Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -Last 1).MainWindowHandle | '
                'ForEach-Object { Add-Type "using System; using System.Runtime.InteropServices; '
                'public class W { [DllImport(\\\"user32.dll\\\")] public static extern bool SetForegroundWindow(IntPtr h); }"; '
                '[W]::SetForegroundWindow($_) }'
            ], capture_output=True, timeout=3)
    except Exception:
        pass


_playwright = None  # async_playwright handle, kept alive across reboots


def _browser_alive() -> bool:
    """Cheap liveness check. Catches both states the user can produce
    (closed window vs killed Chromium process)."""
    if _browser is None:
        return False
    try:
        return _browser.is_connected()
    except Exception:
        return False


# Lock against concurrent _get_browser() calls — without it two
# parallel SEARCHes during a cold start would each call playwright.
# launch() and the second clobber would leak the first browser.
_browser_lock = asyncio.Lock()


async def _get_browser():  # type: ignore[no-untyped-def]  # playwright BrowserContext
    """Return a usable BrowserContext. Re-launches Chromium when the
    previous instance was closed by the user (was a hard fail before:
    'BrowserContext.new_page: Target page, context or browser has been
    closed' kept happening on every following SEARCH until restart)."""
    global _browser, _context, _playwright
    async with _browser_lock:
        if not _browser_alive():
            if _browser is not None:
                log.info("browser disconnected, relaunching Chromium")
                _browser = None
                _context = None
            if _playwright is None:
                _playwright = await async_playwright().start()
            launch_args = ["--start-maximized"] if not IS_MAC else []
            _browser = await _playwright.chromium.launch(headless=False, args=launch_args)
            ua_string = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                if IS_MAC
                else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            _context = await _browser.new_context(
                user_agent=ua_string,
                no_viewport=True,
            )
        return _context


async def search_and_read(query: str) -> dict:
    """Search DuckDuckGo in visible browser, click first result, read the page."""
    from urllib.parse import quote
    ctx = await _get_browser()
    page = await ctx.new_page()
    try:
        # DuckDuckGo search (no cookie banner, no reCAPTCHA). quote() so
        # multi-word voice queries with &/?/# don't get truncated.
        search_url = f"https://duckduckgo.com/?q={quote(query, safe='')}"
        await page.goto(search_url, timeout=15000)
        _bring_chromium_to_front()
        await page.wait_for_timeout(2000)

        # Click first organic result
        first_link = page.locator('[data-testid="result-title-a"]').first
        if await first_link.count() > 0:
            await first_link.click()
            await page.wait_for_timeout(3000)

            # Read page content
            title = await page.title()
            url = page.url
            text = await page.evaluate("""
                () => {
                    const selectors = ['main', 'article', '[role="main"]', '.content', '#content', 'body'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim().length > 100) {
                            return el.innerText.trim();
                        }
                    }
                    return document.body?.innerText?.trim() || '';
                }
            """)
            return {"title": title, "url": url, "content": text[:3000]}
        else:
            return {"title": "Keine Ergebnisse", "url": search_url, "content": "Keine Ergebnisse gefunden."}
    except Exception as e:
        return {"error": str(e), "url": query}
    finally:
        # Without close(), every SEARCH leaks a Chromium tab — Catrin's
        # Mac fills up after a normal day's use.
        try:
            await page.close()
        except Exception:
            pass


async def visit(url: str, max_chars: int = 5000) -> dict:
    """Visit a URL and extract main text content. Refuses anything
    that's not http(s) — Playwright would otherwise gladly open
    file:///etc/passwd or chrome://settings on an LLM-supplied URL."""
    if not _is_safe_url(url):
        return {"error": "Nur http- und https-URLs sind erlaubt.", "url": url}
    ctx = await _get_browser()
    page = await ctx.new_page()
    try:
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        text = await page.evaluate("""
            () => {
                const selectors = ['main', 'article', '[role="main"]', '.content', '#content', 'body'];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 100) {
                        return el.innerText.trim();
                    }
                }
                return document.body?.innerText?.trim() || '';
            }
        """)
        title = await page.title()
        return {"title": title, "url": url, "content": text[:max_chars]}
    except Exception as e:
        return {"error": str(e), "url": url}
    finally:
        await page.close()


_DEFAULT_NEWS_URL = "https://www.tagesschau.de/infoservices/alle-meldungen-100~rss2.xml"
_DEFAULT_NEWS_NAME = "Tagesschau"


async def fetch_news(url: str = _DEFAULT_NEWS_URL, source_name: str = _DEFAULT_NEWS_NAME) -> str:
    """Fetch current news from an RSS feed (Tagesschau by default).

    `url` is the RSS endpoint, `source_name` is the human label used in
    the leading line so Jarvis can say e.g. 'NDR aktuelle Meldungen' or
    'Reuters Top Stories'."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = root.findall(".//item")[:12]
        lines = []
        for item in items:
            title = (item.findtext("title") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            desc  = re.sub(r'<[^>]+>', '', desc)[:120]
            lines.append(f"• {title}: {desc}" if desc else f"• {title}")
        return f"{source_name} Aktuelle Meldungen:\n" + "\n".join(lines)
    except Exception as e:
        log.warning(f"fetch_news failed: {type(e).__name__}: {e}")
        return f"News konnten nicht geladen werden: {e}"


_ALLOWED_SCHEMES = {"http", "https"}


def _is_safe_url(url: str) -> bool:
    """Allow only http(s) URLs with a non-empty host. Rejects file://,
    javascript:, data:, ftp:// etc. — every other scheme a voice command
    might accidentally produce."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme.lower() in _ALLOWED_SCHEMES and bool(parsed.netloc)


async def open_url(url: str) -> dict:
    """Open URL in user's default browser (non-blocking).
    Refuses anything that is not http(s) — see _is_safe_url()."""
    if not _is_safe_url(url):
        return {"success": False, "url": url, "error": "rejected: only http/https URLs allowed"}
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, webbrowser.open, url)
    return {"success": True, "url": url}


async def close() -> None:
    global _browser, _context
    if _browser:
        await _browser.close()
        _browser = None
        _context = None
