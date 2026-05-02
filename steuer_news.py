"""
Jarvis — Steuerrecht News
- BFH Pressemitteilungen per RSS
- BFH Entscheidungen per RSS
"""

import xml.etree.ElementTree as ET
import httpx
import datetime
from email.utils import parsedate_to_datetime

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

BFH_FEEDS = [
    {"name": "BFH Pressemitteilungen", "url": "https://www.bundesfinanzhof.de/de/news.rss"},
    {"name": "BFH Entscheidungen",     "url": "https://www.bundesfinanzhof.de/de/precedent.rss"},
]


def _parse_rss_items(xml_text: str) -> list:
    """Parse RSS feed, return list of (title, pub_date) tuples."""
    items = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            pub_str = (item.findtext("pubDate") or "").strip()
            pub_date = None
            if pub_str:
                try:
                    pub_date = parsedate_to_datetime(pub_str).date()
                except Exception:
                    pass
            if title:
                items.append((title, pub_date))
    except Exception:
        pass
    return items


async def fetch_all_sources() -> str:
    """Fetch BFH RSS feeds and return them as text (used by [ACTION:STEUERNEWS])."""
    blocks = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
        for feed in BFH_FEEDS:
            try:
                resp = await client.get(feed["url"])
                resp.raise_for_status()
                items = _parse_rss_items(resp.text)
                lines = []
                for title, pub_date in items[:5]:
                    date_str = pub_date.strftime("%d.%m.%Y") if pub_date else ""
                    lines.append(f"• {title} ({date_str})" if date_str else f"• {title}")
                blocks.append(f"=== {feed['name']} ===\n" + ("\n".join(lines) or "Keine Einträge."))
            except Exception as e:
                blocks.append(f"=== {feed['name']} ===\nNicht erreichbar: {e}")
    return "\n\n".join(blocks)


async def fetch_recent(days: int = 3) -> str:
    """Return BFH news items published within the last `days` days
    (used in the morning greeting)."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    recent = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
        for feed in BFH_FEEDS:
            try:
                resp = await client.get(feed["url"])
                resp.raise_for_status()
                for title, pub_date in _parse_rss_items(resp.text):
                    if pub_date and pub_date >= cutoff:
                        recent.append(f"• {title} ({pub_date.strftime('%d.%m.')})")
            except Exception:
                pass
    if not recent:
        return ""
    return "Aktuelle BFH-Neuigkeiten (letzte 3 Tage):\n" + "\n".join(recent)
