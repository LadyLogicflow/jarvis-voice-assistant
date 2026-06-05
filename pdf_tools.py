"""
PDF-Tools fuer JARVIS (Issue #159 / #109).

Bietet:
- save_pdf()             — Speichert rohe PDF-Bytes nach /tmp/jarvis_pdfs/.
- extract_text()         — Extrahiert den Volltext einer PDF via PyMuPDF.
- analyze_steuerbescheid() — Klassifiziert + extrahiert Felder via Claude Haiku
                             und speichert das Ergebnis in persons_db.
- analyze_pdf_stub()     — Wrapper; frueherer Stub ersetzt durch echten Aufruf.
"""

from __future__ import annotations

import json
import os
import re

import settings as S

log = S.log

_PDF_DIR = os.path.join(os.path.dirname(__file__), "jarvis_pdfs")

# Analyse-Ergebnisse des laufenden Tages — werden um 20:30 in die
# Abendzusammenfassung einbezogen und danach geleert.
_daily_pdf_results: list[str] = []


def pop_daily_pdf_results() -> list[str]:
    """Gibt alle PDF-Analyse-Ergebnisse des Tages zurueck und leert die Liste."""
    results = list(_daily_pdf_results)
    _daily_pdf_results.clear()
    return results

# Haiku-Modell — konsistent mit dem Rest des Projekts.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Lokale Extraktion via Regex (kein API-Aufruf)
# ---------------------------------------------------------------------------

# Steuerart-Schlüsselwörter → Kürzel
_STEUERART_MAP = [
    (re.compile(r"einkommensteuer", re.I),       "ESt"),
    (re.compile(r"k[oö]rperschaftsteuer", re.I), "KSt"),
    (re.compile(r"umsatzsteuer", re.I),           "USt"),
    (re.compile(r"gewerbesteuer", re.I),          "GewSt"),
    (re.compile(r"erbschaftsteuer", re.I),        "ErbSt"),
    (re.compile(r"schenkungsteuer", re.I),        "SchenkSt"),
    (re.compile(r"solidarit[aä]tszuschlag", re.I), "SolZ"),
    (re.compile(r"kirchensteuer", re.I),          "KiSt"),
]

# VZ-Titel erscheint als eigene Zeile oben — nicht irgendwo im Body
_RE_VZ_TITLE = re.compile(r"^\s*Vorauszahlungsbescheid\s*$", re.MULTILINE)

_RE_IDNR = re.compile(
    r"(?:Identifikationsnummer|Id\.?\s*Nr\.?|IdNr\.?)\s*[:.]?\s*([\d][\d\s]{8,13}[\d])",
    re.I,
)
_RE_STEUERNR = re.compile(
    # Matches 2-3/3-4/4-5 digits with any separator (132/2648/0171, 12/345/67890 etc.)
    r"(?:Steuernummer|St\.?\s*(?:-\s*)?Nr\.?)\s*[:.]?\s*([\d]{2,3}[\s/\-][\d]{3,4}[\s/\-][\d]{4,5})",
    re.I,
)
_RE_JAHR = re.compile(
    r"(?:für\s+das\s+(?:Kalender)?[Jj]ahr"
    r"|Veranlagungszeitraum"
    r"|Steuerjahr"
    r"|Bescheid\s+für"       # "Bescheid für  2025  über"
    r"|für)\s+(\d{4})\b",   # "für 2026 zum" / "für 2025 über"
    re.I,
)
_RE_DATUM = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b")
_RE_BETRAG = re.compile(
    r"(?:festgesetzt(?:e(?:r|n|s)?)?\s+(?:Steuer|Einkommensteuer|K[oö]rperschaftsteuer|"
    r"Umsatzsteuer|Gewerbesteuer)"
    r"|Nachzahlung"
    r"|Erstattung"
    r")\b"
    r"[^\d\-]{0,60}([\-]?\s*\d[\d\s]*[.,]\d{2})\s*(?:EUR|€)?",
    re.I | re.S,
)
# Pipe-Tabellen-Format: "verbleibende Beträge | ESt | SolZ | Insgesamt |" — letzter Wert
_RE_BETRAG_TABELLE = re.compile(
    r"verbleibende\s+Betr[äa]ge[^|]*\|[^|]+\|[^|]+\|\s*(" + r"[\-]?\d{1,3}(?:[.\s]\d{3})*[,]\d{2}" + r")",
    re.I,
)
# Fälligkeit: "spätestens bis zum 08.06.26" (2-digit year) OR "08.06.2026" (4-digit)
_RE_FAELLIG = re.compile(
    r"(?:Zahlungstermin|fällig\s+am|zu\s+zahlen\s+bis|spätestens\s+bis\s+zum)\s*[:.]?\s*"
    r"(\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}))\b",
    re.I,
)
# VZ-Quartale: Finanzamt-Bescheide benutzen Monatsnamen (März=Q1, Juni=Q2, Sep=Q3, Dez=Q4)
# Format: "| 10.März ... | ESt | SolZ | Insgesamt |" — letzter Wert = Insgesamt
_NUM_DE = r"[\-]?\d{1,3}(?:[.\s]\d{3})*[,]\d{2}"  # deutsches Zahlenformat: 1.715,00
_RE_VZ_MAERZ = re.compile(r"10\.März[^|]*\|[^|]+\|[^|]+\|\s*(" + _NUM_DE + r")", re.I)
_RE_VZ_JUNI  = re.compile(r"10\.Juni[^|]*\|[^|]+\|[^|]+\|\s*(" + _NUM_DE + r")", re.I)
_RE_VZ_SEP   = re.compile(r"10\.September[^|]*\|[^|]+\|[^|]+\|\s*(" + _NUM_DE + r")", re.I)
_RE_VZ_DEZ   = re.compile(r"10\.Dezember[^|]*\|[^|]+\|[^|]+\|\s*(" + _NUM_DE + r")", re.I)
# Fallback: klassische Vierteljahresmuster
_RE_VZ_QUARTALE = re.compile(
    r"(?:1\.?\s*Viertelj|Q1)\D{0,20}([\-]?\s*\d[\d\s]*[.,]\d{2})\s*(?:EUR|€)?.*?"
    r"(?:2\.?\s*Viertelj|Q2)\D{0,20}([\-]?\s*\d[\d\s]*[.,]\d{2})\s*(?:EUR|€)?.*?"
    r"(?:3\.?\s*Viertelj|Q3)\D{0,20}([\-]?\s*\d[\d\s]*[.,]\d{2})\s*(?:EUR|€)?.*?"
    r"(?:4\.?\s*Viertelj|Q4)\D{0,20}([\-]?\s*\d[\d\s]*[.,]\d{2})\s*(?:EUR|€)?",
    re.I | re.S,
)
# Mandant-Fallback: "Dieser Bescheid ergeht an Sie für\n Herrn/Frau NAME"
_RE_MANDANT = re.compile(
    r"Dieser\s+Bescheid\s+ergeht\s+an\s+Sie\s+für\s+(?:Herrn|Frau)\s+([\w\s\-]+?)(?:\n|,|\d)",
    re.I,
)


def _parse_german_amount(s: str) -> float | None:
    """Konvertiert deutschen Betragstring in float.
    '1.234,56' → 1234.56  |  '-1 234,56' → -1234.56
    """
    s = re.sub(r"\s", "", s)
    negative = s.startswith("-")
    s = s.lstrip("-")
    # Punkt als Tausendertrennzeichen, Komma als Dezimaltrennzeichen
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


def _extract_local(text: str) -> dict:
    """Extrahiert Steuerbescheid-Felder per Regex aus OCR-/Maschinentext.

    Gibt ein dict zurueck das mit dem Haiku-Schema kompatibel ist.
    Bei unzureichendem Ergebnis (kein Jahr, kein Betrag) → {"typ": "unbekannt"}.
    """
    import mandanten as _mdb

    # --- Identifikatoren ---
    idnr_raw = ""
    m = _RE_IDNR.search(text)
    if m:
        idnr_raw = re.sub(r"\s", "", m.group(1))

    steuernr_raw = ""
    m = _RE_STEUERNR.search(text)
    if m:
        steuernr_raw = re.sub(r"\s", "", m.group(1))

    # --- Mandant per ID-Nr / Steuernummer aus Lookup-Tabelle ---
    mandant_row: dict | None = None
    if idnr_raw and len(re.sub(r"\D", "", idnr_raw)) == 11:
        mandant_row = _mdb.find_by_idnr(idnr_raw)
    if not mandant_row and steuernr_raw:
        mandant_row = _mdb.find_by_steuernummer(steuernr_raw)
    if mandant_row:
        mandant_name = mandant_row["name"]
    else:
        # Fallback: Name direkt aus Bescheid-Adresszeile
        m = _RE_MANDANT.search(text)
        mandant_name = m.group(1).strip() if m else ""

    # --- Steuerart ---
    steuerart = ""
    for pattern, kuerzel in _STEUERART_MAP:
        if pattern.search(text):
            steuerart = kuerzel
            break

    # VZ-Erkennung: Titel muss als eigene Zeile oben stehen.
    # "Vorauszahlung" taucht auch in normalen Steuerbescheiden (Rechtsbehelfsbelehrung) auf.
    is_vz = bool(_RE_VZ_TITLE.search(text))

    # --- Steuerjahr ---
    jahr: int | None = None
    m = _RE_JAHR.search(text)
    if m:
        try:
            jahr = int(m.group(1))
        except ValueError:
            pass
    if not jahr:
        # Fallback: letztes vierstelliges Jahr das wie ein Steuerjahr aussieht
        jahre = [int(y) for y in re.findall(r"\b(20\d{2}|19\d{2})\b", text)]
        if jahre:
            jahr = sorted(jahre)[0]  # ältestes Jahr = Veranlagungsjahr

    # --- Ausstellungsdatum: erstes Datum im Dokument ---
    ausstellungsdatum = ""
    m = _RE_DATUM.search(text)
    if m:
        ausstellungsdatum = m.group(1)

    # --- Vorauszahlungsbescheid ---
    if is_vz and steuerart:
        vz: dict = {
            "typ": "Vorauszahlungsbescheid",
            "mandant": mandant_name,
            "steuerart": steuerart,
            "vorauszahlungsjahr": jahr,
            "ausstellungsdatum": ausstellungsdatum,
            "q1": None, "q2": None, "q3": None, "q4": None,
        }
        # Zuerst: Monatsnamen-Muster (DE Finanzamt-Format: 10.März/Juni/Sep/Dez)
        for regex, key in [(_RE_VZ_MAERZ, "q1"), (_RE_VZ_JUNI, "q2"),
                           (_RE_VZ_SEP, "q3"), (_RE_VZ_DEZ, "q4")]:
            m = regex.search(text)
            if m:
                vz[key] = _parse_german_amount(m.group(1))
        # Fallback: Vierteljahres-Muster (Q1/Q2/Q3/Q4)
        if all(vz[k] is None for k in ["q1", "q2", "q3", "q4"]):
            m = _RE_VZ_QUARTALE.search(text)
            if m:
                for i, key in enumerate(["q1", "q2", "q3", "q4"], 1):
                    vz[key] = _parse_german_amount(m.group(i)) if m.group(i) else None
        if idnr_raw:
            vz["id_nr"] = idnr_raw
        if steuernr_raw:
            vz["steuernummer"] = steuernr_raw
        return vz

    # --- Normaler Steuerbescheid ---
    betrag: float | None = None
    m = _RE_BETRAG.search(text)
    if m:
        betrag = _parse_german_amount(m.group(1))
        ctx = text[max(0, m.start() - 30):m.start()].lower()
        if "erstattung" in ctx and betrag is not None and betrag < 0:
            betrag = -betrag
        elif "nachzahlung" in ctx and betrag is not None and betrag > 0:
            betrag = -betrag
    # Fallback: Pipe-Tabelle "verbleibende Beträge | ESt | SolZ | Insgesamt"
    # Positiver Wert = Nachzahlung (Steuerpflichtiger schuldet dem Finanzamt)
    if betrag is None:
        m = _RE_BETRAG_TABELLE.search(text)
        if m:
            val = _parse_german_amount(m.group(1))
            if val is not None:
                betrag = -val if val > 0 else val  # positiv → Nachzahlung → negativ

    zahlungstermin = ""
    m = _RE_FAELLIG.search(text)
    if m:
        raw_date = m.group(1)
        # Normalisiere 2-stelliges Jahr: "08.06.26" → "08.06.2026"
        parts = raw_date.split(".")
        if len(parts) == 3 and len(parts[2]) == 2:
            year = int(parts[2])
            parts[2] = str(2000 + year if year < 50 else 1900 + year)
            raw_date = ".".join(parts)
        zahlungstermin = raw_date

    if not jahr and not steuerart:
        return {"typ": "unbekannt", "rohdaten": "Keine Steuerbescheid-Merkmale gefunden."}

    result: dict = {
        "typ": "Steuerbescheid",
        "mandant": mandant_name,
        "steuerart": steuerart or "?",
        "steuerjahr": jahr,
        "ausstellungsdatum": ausstellungsdatum,
        "betrag_eur": betrag,
        "zahlungstermin": zahlungstermin or None,
    }
    if idnr_raw:
        result["id_nr"] = idnr_raw
    if steuernr_raw:
        result["steuernummer"] = steuernr_raw
    return result


def extract_text(filepath: str) -> str:
    """Extrahiert den Volltext einer PDF-Datei via PyMuPDF.

    Args:
        filepath: Absoluter Pfad zur PDF-Datei.

    Returns:
        Zusammengefuehrter Text aller Seiten, Seiten durch Zeilenumbruch
        getrennt. Leerer String wenn die Datei leer ist oder kein Text
        extrahiert werden konnte.

    Raises:
        ImportError:  Wenn PyMuPDF (fitz) nicht installiert ist.
        FileNotFoundError: Wenn filepath nicht existiert.
        Exception:    Weiterleitung aller PyMuPDF-Fehler an den Aufrufer.
    """
    import fitz  # PyMuPDF
    doc = fitz.open(filepath)
    pages = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(pages)


def _format_summary(data: dict) -> str:
    """Erstellt eine menschenlesbare Zusammenfassung aus dem Analyse-Dict.

    Args:
        data: Rueckgabe-Dict von analyze_steuerbescheid().

    Returns:
        Einzeiliger deutschsprachiger Zusammenfassungs-String.
    """
    typ = data.get("typ", "unbekannt")

    if typ == "Steuerbescheid":
        mandant = data.get("mandant", "Unbekannt")
        steuerart = data.get("steuerart", "?")
        jahr = data.get("steuerjahr", "?")
        betrag = data.get("betrag_eur")
        faellig = data.get("zahlungstermin") or ""

        if betrag is not None:
            try:
                betrag_f = float(betrag)
                betrag_str = f"{abs(betrag_f):,.2f}€".replace(",", "X").replace(".", ",").replace("X", ".")
                richtung = "Erstattung" if betrag_f >= 0 else "Nachzahlung"
                betrag_info = f"{richtung} {betrag_str}"
            except (ValueError, TypeError):
                betrag_info = f"Betrag: {betrag}"
        else:
            betrag_info = "Betrag unbekannt"

        faellig_info = f", fällig {faellig}" if faellig and faellig != "null" else ""
        return f"Steuerbescheid {mandant} {jahr} ({steuerart}): {betrag_info}{faellig_info}"

    if typ == "Vorauszahlungsbescheid":
        mandant = data.get("mandant", "Unbekannt")
        steuerart = data.get("steuerart", "?")
        jahr = data.get("vorauszahlungsjahr", "?")
        quartale = []
        for q_key, q_label in [("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"), ("q4", "Q4")]:
            val = data.get(q_key)
            if val is not None:
                try:
                    quartale.append(f"{q_label} {float(val):,.0f}€".replace(",", "."))
                except (ValueError, TypeError):
                    quartale.append(f"{q_label} {val}€")
        quartale_str = ", ".join(quartale) if quartale else "keine Quartals-Angaben"
        return f"Vorauszahlungsbescheid {mandant} {jahr} ({steuerart}): {quartale_str}"

    # Unbekannter Typ
    rohdaten = data.get("rohdaten", "")
    return f"PDF empfangen (Typ unbekannt): {rohdaten}"


async def analyze_steuerbescheid(filepath: str) -> dict:
    """Analysiert eine PDF-Datei als Steuerbescheid.

    Strategie (kein API-Aufruf wenn moeglich):
    1. Text extrahieren (PyMuPDF, lokal)
    2. Lokale Regex-Extraktion (_extract_local) — kein Datentransfer
    3. Nur wenn lokale Extraktion "unbekannt" liefert UND keine mandanten.csv
       vorliegt: Fallback auf Claude Haiku

    Args:
        filepath: Absoluter Pfad zur PDF-Datei.

    Returns:
        Strukturiertes Dict + "summary"-Schluessel.
    """
    filename = os.path.basename(filepath)

    # Schritt 1: Text extrahieren
    try:
        text = extract_text(filepath)
    except ImportError:
        log.error("pdf_tools: PyMuPDF (fitz) nicht installiert.")
        return {
            "typ": "fehler",
            "fehler": "PyMuPDF nicht installiert",
            "summary": f"PDF-Analyse fehlgeschlagen ({filename}): PyMuPDF nicht installiert.",
        }
    except FileNotFoundError:
        log.error("pdf_tools: Datei nicht gefunden: %s", filepath)
        return {
            "typ": "fehler",
            "fehler": f"Datei nicht gefunden: {filepath}",
            "summary": f"PDF nicht gefunden: {filename}",
        }
    except Exception as exc:
        log.error("pdf_tools: extract_text fehlgeschlagen fuer %s: %s", filepath, exc)
        return {
            "typ": "fehler",
            "fehler": str(exc),
            "summary": f"PDF-Textextraktion fehlgeschlagen ({filename}): {exc}",
        }

    if not text.strip():
        log.warning("pdf_tools: Kein Text in PDF extrahiert: %s", filepath)
        return {
            "typ": "unbekannt",
            "rohdaten": "Kein lesbarer Text im PDF (moeglicherweise Bild-Scan ohne OCR).",
            "summary": f"PDF empfangen (Typ unbekannt): Kein lesbarer Text in {filename}.",
        }

    # Schritt 2: Lokale Extraktion (kein API-Aufruf)
    data = _extract_local(text)
    log.info("pdf_tools: Lokale Extraktion fuer %s: typ=%s mandant=%r",
             filename, data.get("typ"), data.get("mandant"))

    # Schritt 3: Haiku-Fallback wenn lokal nicht erkannt
    if data.get("typ") == "unbekannt":
        log.info("pdf_tools: Lokale Extraktion unzureichend, Haiku-Fallback fuer %s", filename)
        data = await _analyze_with_haiku(text, filename)

    # Schritt 4: In persons_db speichern
    import persons_db as _pdb
    typ = data.get("typ", "unbekannt")
    mandant = data.get("mandant", "").strip()
    try:
        if typ == "Steuerbescheid" and mandant:
            _pdb.save_tax_assessment(mandant, data)
        elif typ == "Vorauszahlungsbescheid" and mandant:
            _pdb.save_advance_payment(mandant, data)
    except Exception as exc:
        log.warning("pdf_tools: persons_db-Speicherung fehlgeschlagen: %s", exc)

    data["summary"] = _format_summary(data)
    log.info("pdf_tools: Analyse abgeschlossen fuer %s: %s", filename, data["summary"])
    return data


async def _analyze_with_haiku(text: str, filename: str) -> dict:
    """Haiku-Fallback — nur wenn lokale Extraktion versagt und keine Mandantenliste."""
    _MAX_CHARS = 12_000
    _HAIKU_MODEL = "claude-haiku-4-5-20251001"
    _PROMPT = """\
Du analysierst einen deutschen Steuerbescheid als PDF-Text.
Bestimme den Typ und extrahiere die relevanten Felder.
Antworte NUR mit einem JSON-Objekt, kein weiterer Text.

Typ "Steuerbescheid":
{{"typ":"Steuerbescheid","mandant":"<Name>","steuerart":"<ESt|KSt|USt|GewSt|...>",\
"steuerjahr":<YYYY>,"ausstellungsdatum":"<DD.MM.YYYY>","betrag_eur":<float>,\
"zahlungstermin":"<DD.MM.YYYY oder null>"}}

Typ "Vorauszahlungsbescheid":
{{"typ":"Vorauszahlungsbescheid","mandant":"<Name>","steuerart":"<ESt|KSt|GewSt|...>",\
"vorauszahlungsjahr":<YYYY>,"ausstellungsdatum":"<DD.MM.YYYY>",\
"q1":<float|null>,"q2":<float|null>,"q3":<float|null>,"q4":<float|null>}}

Falls kein Steuerbescheid: {{"typ":"unbekannt","rohdaten":"<kurze Beschreibung>"}}

PDF-Text:
""" + text[:_MAX_CHARS]
    try:
        resp = await S.ai.messages.create(
            model=_HAIKU_MODEL, max_tokens=512,
            messages=[{"role": "user", "content": _PROMPT}],
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        clean = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
        return json.loads(clean.strip())
    except json.JSONDecodeError:
        return {"typ": "unbekannt", "rohdaten": raw[:500]}
    except Exception as exc:
        log.error("pdf_tools: Haiku-Fallback fehlgeschlagen fuer %s: %s", filename, exc)
        return {"typ": "fehler", "fehler": str(exc),
                "summary": f"PDF-Analyse fehlgeschlagen ({filename}): LLM-Fehler."}


def analyze_pdf_stub(filepath: str) -> str:
    """Backward-kompatibler Einstiegspunkt fuer PDF-Empfang via E-Mail-Trigger.

    Frueherer Stub (Issue #159) — jetzt vollstaendige Implementierung.
    Startet analyze_steuerbescheid() in einer neuen asyncio-Task bzw.
    fuehrt sie synchron aus und gibt den Summary-String zurueck.

    Args:
        filepath: Absoluter Pfad zur gespeicherten PDF-Datei.

    Returns:
        Kurzer menschenlesbarer Summary-String; im Fehlerfall eine
        Fehlermeldung.
    """
    import asyncio

    filename = os.path.basename(filepath)
    log.info("pdf_tools: PDF empfangen (analyze_pdf_stub): %s", filename)

    try:
        loop = asyncio.get_running_loop()
        # In einem laufenden Loop eine Task erstellen und nicht warten —
        # der E-Mail-Trigger benoetigt nur ein schnelles Acknowledge.
        # Der Aufruf wird im Hintergrund ausgefuehrt; der Summary wird
        # geloggt sobald er fertig ist.
        async def _run_and_log() -> None:
            result = await analyze_steuerbescheid(filepath)
            summary = result.get("summary", "")
            log.info("pdf_tools: Hintergrund-Analyse fertig: %s", summary)
            if summary:
                _daily_pdf_results.append(summary)

        loop.create_task(_run_and_log())
        return f"PDF empfangen, Analyse laeuft im Hintergrund: {filename}"
    except RuntimeError:
        # Kein laufender Loop — synchron ausfuehren
        try:
            result = asyncio.run(analyze_steuerbescheid(filepath))
            return result.get("summary", f"PDF analysiert: {filename}")
        except Exception as exc:
            log.error("pdf_tools: analyze_pdf_stub fehlgeschlagen: %s", exc)
            return f"PDF empfangen, Analyse fehlgeschlagen: {filename}"


def save_pdf(data: bytes, filename: str) -> str:
    """Speichert rohe PDF-Bytes nach /tmp/jarvis_pdfs/<filename>.

    Legt das Verzeichnis bei Bedarf an.

    Args:
        data:     Rohbytes der PDF-Datei.
        filename: Dateiname (ohne Pfad).

    Returns:
        Absoluter Pfad der gespeicherten Datei.

    Raises:
        OSError: Wenn die Datei nicht geschrieben werden kann.
    """
    os.makedirs(_PDF_DIR, exist_ok=True)
    safe_name = "".join(
        c if (c.isalnum() or c in "._- ") else "_" for c in filename
    ).strip() or "attachment.pdf"
    dest = os.path.join(_PDF_DIR, safe_name)
    if os.path.exists(dest):
        base, ext = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(_PDF_DIR, f"{base}_{counter}{ext}")
            counter += 1
    with open(dest, "wb") as f:
        f.write(data)
    log.info("pdf_tools: PDF gespeichert: %s (%d bytes)", dest, len(data))
    return dest
