"""
Mandanten-Lookup-Tabelle fuer JARVIS.

Liest mandanten.csv (gitignored) aus dem Projekt-Root.
Format: mitgliedsnr,vorname,nachname,id_nr,steuernummer,finanzamt

Wird von pdf_tools.py fuer lokales PDF-Matching genutzt — kein API-Aufruf.
"""

from __future__ import annotations

import csv
import os
import re
from functools import lru_cache

import settings as S

log = S.log

_CSV_PATH = os.path.join(os.path.dirname(__file__), "mandanten.csv")


def _normalize_steuernr(s: str) -> str:
    """Entfernt Leerzeichen und normalisiert Trennzeichen auf '/'.
    '12 345 67890' → '12/345/67890'
    '12-345-67890' → '12/345/67890'
    """
    return re.sub(r"[\s\-]+", "/", s.strip())


def _normalize_idnr(s: str) -> str:
    """Entfernt alle Nicht-Ziffern aus der Identifikationsnummer."""
    return re.sub(r"\D", "", s)


def load() -> list[dict]:
    """Laedt alle Mandanten aus mandanten.csv.

    Returns:
        Liste von dicts mit Schlüsseln:
        mitgliedsnr, vorname, nachname, id_nr, steuernummer, finanzamt, name
        Leere Liste wenn Datei nicht existiert oder leer ist.
    """
    if not os.path.exists(_CSV_PATH):
        log.debug("mandanten.py: Keine mandanten.csv gefunden (%s)", _CSV_PATH)
        return []
    mandanten: list[dict] = []
    try:
        with open(_CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Pflichtfelder normalisieren
                row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
                vorname = row.get("vorname", "")
                nachname = row.get("nachname", "")
                row["name"] = f"{vorname} {nachname}".strip()
                if row.get("id_nr"):
                    row["id_nr"] = _normalize_idnr(row["id_nr"])
                if row.get("steuernummer"):
                    row["steuernummer"] = _normalize_steuernr(row["steuernummer"])
                mandanten.append(row)
    except Exception as exc:
        log.error("mandanten.py: Ladefehler: %s", exc)
    log.info("mandanten.py: %d Mandanten geladen", len(mandanten))
    return mandanten


def find_by_idnr(id_nr: str) -> dict | None:
    """Sucht Mandant per Identifikationsnummer (11 Ziffern, eindeutig)."""
    needle = _normalize_idnr(id_nr)
    if len(needle) != 11:
        return None
    for m in load():
        if m.get("id_nr") == needle:
            return m
    return None


def find_by_steuernummer(steuernr: str) -> dict | None:
    """Sucht Mandant per Steuernummer (normalisiert auf XX/XXX/XXXXX)."""
    needle = _normalize_steuernr(steuernr)
    for m in load():
        if m.get("steuernummer") == needle:
            return m
    return None


def find_by_name(name: str) -> list[dict]:
    """Sucht Mandant per Name (case-insensitiv, Teilstring)."""
    from persons_db import _norm, _name_tokens
    needle_tokens = _name_tokens(name)
    if not needle_tokens:
        return []
    results = []
    for m in load():
        if needle_tokens.issubset(_name_tokens(m.get("name", ""))):
            results.append(m)
    return results
