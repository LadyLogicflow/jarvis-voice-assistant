"""
Mandanten-Lookup-Tabelle fuer JARVIS.

Liest mandanten.csv oder Mitglieder.csv (gitignored) aus dem Projekt-Root.
Unterstuetzt zwei Formate:

  Mitglieder.csv (HILO-Export, Semikolon):
    Mandant-Nr.;LP-A Nachname;LP-A Vorname;LP-A Ident-Nr.;LP-A Steuer-Nr.;
    LP-B Nachname;LP-B Vorname;LP-B Ident-Nr.;LP-B Steuer-Nr.;Eintritts-Datum (am)
    → LP-A und LP-B werden jeweils als eigener Eintrag angelegt.

  Einfaches Format (Komma):
    mitgliedsnr,vorname,nachname,id_nr,steuernummer,finanzamt

Delimiter wird automatisch erkannt (';' vs ',').
"""

from __future__ import annotations

import csv
import os
import re

import settings as S

log = S.log

_CSV_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "mandanten.csv"),
    os.path.join(os.path.dirname(__file__), "Mitglieder.csv"),
]


def _find_csv() -> str | None:
    for p in _CSV_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _normalize_steuernr(s: str) -> str:
    """Normalisiert Trennzeichen auf '/': '12 345 67890' → '12/345/67890'."""
    return re.sub(r"[\s\-]+", "/", s.strip()) if s else ""


def _normalize_idnr(s: str) -> str:
    """Entfernt alle Nicht-Ziffern aus der Identifikationsnummer."""
    return re.sub(r"\D", "", s) if s else ""


def _make_entry(mitgliedsnr: str, vorname: str, nachname: str,
                id_nr: str, steuernummer: str, finanzamt: str = "") -> dict:
    return {
        "mitgliedsnr": mitgliedsnr,
        "vorname": vorname,
        "nachname": nachname,
        "id_nr": _normalize_idnr(id_nr),
        "steuernummer": _normalize_steuernr(steuernummer),
        "finanzamt": finanzamt,
        "name": f"{vorname} {nachname}".strip(),
    }


def load() -> list[dict]:
    """Laedt alle Mandanten aus der CSV-Datei.

    Returns:
        Liste von dicts mit Schlüsseln:
        mitgliedsnr, vorname, nachname, id_nr, steuernummer, finanzamt, name
        Leere Liste wenn keine Datei gefunden oder leer.
    """
    csv_path = _find_csv()
    if not csv_path:
        log.debug("mandanten.py: Keine CSV gefunden (%s)", _CSV_CANDIDATES)
        return []

    mandanten: list[dict] = []
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            delimiter = ";" if sample.count(";") >= sample.count(",") else ","
            reader = csv.DictReader(f, delimiter=delimiter)
            for raw_row in reader:
                row = {
                    (k.strip().lower() if k else ""): (v.strip() if v else "")
                    for k, v in raw_row.items() if k
                }

                if "lp-a nachname" in row or "mandant-nr." in row:
                    # Mitglieder.csv (HILO-Export)
                    mnr = row.get("mandant-nr.", "")

                    nachname_a = row.get("lp-a nachname", "")
                    if nachname_a:
                        mandanten.append(_make_entry(
                            mnr,
                            row.get("lp-a vorname", ""),
                            nachname_a,
                            row.get("lp-a ident-nr.", ""),
                            row.get("lp-a steuer-nr.", ""),
                        ))

                    nachname_b = row.get("lp-b nachname", "")
                    if nachname_b:
                        mandanten.append(_make_entry(
                            mnr,
                            row.get("lp-b vorname", ""),
                            nachname_b,
                            row.get("lp-b ident-nr.", ""),
                            row.get("lp-b steuer-nr.", ""),
                        ))
                else:
                    # Einfaches Format
                    mandanten.append(_make_entry(
                        row.get("mitgliedsnr", ""),
                        row.get("vorname", ""),
                        row.get("nachname", ""),
                        row.get("id_nr", ""),
                        row.get("steuernummer", ""),
                        row.get("finanzamt", ""),
                    ))

    except Exception as exc:
        log.error("mandanten.py: Ladefehler: %s", exc)

    log.info("mandanten.py: %d Eintraege aus %s geladen",
             len(mandanten), os.path.basename(csv_path))
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
    """Sucht Mandant per Steuernummer."""
    needle = _normalize_steuernr(steuernr)
    if not needle:
        return None
    for m in load():
        if m.get("steuernummer") == needle:
            return m
    return None


def find_by_name(name: str) -> list[dict]:
    """Sucht Mandant per Name (case-insensitiv, Token-Matching)."""
    from persons_db import _name_tokens
    needle_tokens = _name_tokens(name)
    if not needle_tokens:
        return []
    return [m for m in load() if needle_tokens.issubset(_name_tokens(m.get("name", "")))]
