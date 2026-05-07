"""
Jarvis -- Steuerrecht News
- BFH Pressemitteilungen per RSS
- BFH Entscheidungen per RSS
"""

import hashlib
import json
import logging
import os
import xml.etree.ElementTree as ET
import httpx

log = logging.getLogger("jarvis")
import datetime
from email.utils import parsedate_to_datetime

# Sentinelwert den actions.py erkennt, um Catrin zu fragen ob die
# bereits gelesenen Meldungen trotzdem vorgelesen werden sollen.
BEREITS_GELESEN = "BEREITS_GELESEN"

# Pfad fuer die Datei mit den gesehenen Titel-Hashes.
_SEEN_FILE = os.path.join(os.path.dirname(__file__), ".jarvis_steuer_seen.json")


def _title_hash(title: str) -> str:
    """MD5-Hash eines Titels als kompakter String-Schluessel."""
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def _load_seen_hashes() -> set[str]:
    """Liest die Liste der bereits gesehenen Titel-Hashes vom Disk."""
    if not os.path.exists(_SEEN_FILE):
        return set()
    try:
        with open(_SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except Exception as e:
        log.warning(f"steuer_news: _load_seen_hashes failed: {e}")
    return set()


def _save_seen_hashes(hashes: set[str]) -> None:
    """Schreibt die gesehenen Titel-Hashes atomar auf Disk."""
    try:
        tmp = _SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(hashes), f, ensure_ascii=False)
        os.replace(tmp, _SEEN_FILE)
    except Exception as e:
        log.warning(f"steuer_news: _save_seen_hashes failed: {e}")

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


async def _fetch_feeds_raw() -> tuple[str, set[str]]:
    """Interner Helper: RSS-Feeds einmalig abrufen (Issue #91).

    Gibt (formatted_text, current_hashes) zurueck.  Die Hashes
    enthalten alle Titel-Hashes der gerade gelieferten Items.  Kein
    Seen-Check, kein Disk-Write -- das bleibt Aufgabe des Aufrufers.
    """
    blocks: list[str] = []
    current_titles: list[str] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
        for feed in BFH_FEEDS:
            try:
                resp = await client.get(feed["url"])
                resp.raise_for_status()
                items = _parse_rss_items(resp.text)
                lines = []
                for title, pub_date in items[:5]:
                    current_titles.append(title)
                    date_str = pub_date.strftime("%d.%m.%Y") if pub_date else ""
                    lines.append(f"\u2022 {title} ({date_str})" if date_str else f"\u2022 {title}")
                blocks.append(f"=== {feed['name']} ===\n" + ("\n".join(lines) or "Keine Eintraege."))
            except Exception as e:
                blocks.append(f"=== {feed['name']} ===\nNicht erreichbar: {e}")
    text = "\n\n".join(blocks)
    current_hashes = {_title_hash(t) for t in current_titles}
    return text, current_hashes


def commit_seen(hashes: set[str]) -> None:
    """Persistiert eine Menge von Titel-Hashes als gesehen (Issue #91).

    Wird vom STEUERNEWS-Handler aufgerufen nachdem der Brief generiert
    wurde, sodass der RSS-Fetch nur einmal stattfindet.
    """
    seen = _load_seen_hashes()
    _save_seen_hashes(seen | hashes)


async def fetch_all_sources(mark_seen: bool = False) -> str:
    """Fetch BFH RSS feeds and return them as text (used by [ACTION:STEUERNEWS]).

    Gibt ``BEREITS_GELESEN`` zurueck wenn alle aktuellen Titel bereits in der
    gesehenen-Liste stehen (Issue #75). Wenn ``mark_seen=True`` uebergeben
    wird (nach dem Vorlesen), werden die Hashes persistiert.
    """
    seen = _load_seen_hashes()
    text, current_hashes = await _fetch_feeds_raw()

    if not current_hashes:
        return text

    # Alle aktuellen Meldungen bereits gesehen?
    if current_hashes.issubset(seen):
        return BEREITS_GELESEN

    if mark_seen:
        _save_seen_hashes(seen | current_hashes)

    return text


def mark_steuer_news_seen(result_text: str) -> None:
    """Persistiert die gesehenen Hashes nachtraeglich -- aufzurufen
    nachdem Jarvis die Nachrichten vorgelesen hat (Issue #75)."""
    # Wir koennen die Titel nicht mehr aus dem formatierten Text
    # rekonstruieren. Daher beim naechsten fetch_all_sources-Aufruf
    # mit mark_seen=True persistieren. Diese Funktion ist ein No-op-
    # Placeholder fuer moegliche kuenftige Erweiterungen.
    pass


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
                        recent.append(f"\u2022 {title} ({pub_date.strftime('%d.%m.')})")
            except Exception as e:
                log.warning(f"steuer_news fetch_recent feed={feed.get('url', '?')!r} "
                            f"failed: {type(e).__name__}: {e}")
    if not recent:
        return ""
    return "Aktuelle BFH-Neuigkeiten (letzte 3 Tage):\n" + "\n".join(recent)
