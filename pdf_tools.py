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

_PDF_DIR = "/tmp/jarvis_pdfs"

# Haiku-Modell — konsistent mit dem Rest des Projekts.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Interner Analyse-Prompt
# ---------------------------------------------------------------------------
_ANALYSIS_PROMPT_TEMPLATE = """\
Du analysierst einen deutschen Steuerbescheid als PDF-Text.
Bestimme den Typ und extrahiere die relevanten Felder.

Antworte NUR mit einem JSON-Objekt, kein weiterer Text.

Typ "Steuerbescheid":
{{
  "typ": "Steuerbescheid",
  "mandant": "<Name des Steuerpflichtigen>",
  "steuerart": "<ESt|KSt|USt|GewSt|...>",
  "steuerjahr": <YYYY>,
  "ausstellungsdatum": "<DD.MM.YYYY>",
  "betrag_eur": <float, positiv=Erstattung, negativ=Nachzahlung>,
  "zahlungstermin": "<DD.MM.YYYY oder null>"
}}

Typ "Vorauszahlungsbescheid":
{{
  "typ": "Vorauszahlungsbescheid",
  "mandant": "<Name>",
  "steuerart": "<ESt|KSt|GewSt|...>",
  "vorauszahlungsjahr": <YYYY>,
  "ausstellungsdatum": "<DD.MM.YYYY>",
  "q1": <float oder null>,
  "q2": <float oder null>,
  "q3": <float oder null>,
  "q4": <float oder null>
}}

Falls kein Steuerbescheid:
{{"typ": "unbekannt", "rohdaten": "<kurze Beschreibung>"}}

PDF-Text:
{text}
"""

# Maximale Zeichenanzahl die an Haiku uebergeben wird (Token-Budget).
_MAX_TEXT_CHARS = 12_000


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
    """Analysiert eine PDF-Datei als Steuerbescheid via Claude Haiku.

    Liest den PDF-Text (PyMuPDF), sendet ihn an Claude Haiku zur
    Klassifizierung und Feldextraktion, speichert das Ergebnis in persons_db
    und gibt das strukturierte Dict zurueck.

    Das zurueckgegebene Dict enthaelt stets den Schluessel "summary" mit
    einem menschenlesbaren Zusammenfassungsstring.

    Args:
        filepath: Absoluter Pfad zur PDF-Datei.

    Returns:
        Strukturiertes Dict (Haiku-Ausgabe + "summary"-Schluessel).
        Bei Fehler: {"typ": "fehler", "fehler": "...", "summary": "..."}.
    """
    filename = os.path.basename(filepath)

    # Schritt 1: Text extrahieren
    try:
        text = extract_text(filepath)
    except ImportError:
        log.error("pdf_tools: PyMuPDF (fitz) nicht installiert — bitte 'pymupdf' installieren.")
        result = {
            "typ": "fehler",
            "fehler": "PyMuPDF nicht installiert",
            "summary": f"PDF-Analyse fehlgeschlagen ({filename}): PyMuPDF nicht installiert.",
        }
        return result
    except FileNotFoundError:
        log.error("pdf_tools: Datei nicht gefunden: %s", filepath)
        result = {
            "typ": "fehler",
            "fehler": f"Datei nicht gefunden: {filepath}",
            "summary": f"PDF nicht gefunden: {filename}",
        }
        return result
    except Exception as exc:
        log.error("pdf_tools: extract_text fehlgeschlagen fuer %s: %s", filepath, exc)
        result = {
            "typ": "fehler",
            "fehler": str(exc),
            "summary": f"PDF-Textextraktion fehlgeschlagen ({filename}): {exc}",
        }
        return result

    if not text.strip():
        log.warning("pdf_tools: Kein Text in PDF extrahiert: %s", filepath)
        result = {
            "typ": "unbekannt",
            "rohdaten": "Kein lesbarer Text im PDF gefunden (moeglicherweise gescannt).",
            "summary": f"PDF empfangen (Typ unbekannt): Kein lesbarer Text in {filename}.",
        }
        return result

    # Schritt 2: Text kuerzen (Token-Budget)
    truncated_text = text[:_MAX_TEXT_CHARS]
    if len(text) > _MAX_TEXT_CHARS:
        log.info("pdf_tools: PDF-Text auf %d Zeichen gekuerzt: %s", _MAX_TEXT_CHARS, filename)

    # Schritt 3: Haiku-Analyse
    prompt = _ANALYSIS_PROMPT_TEMPLATE.format(text=truncated_text)
    try:
        resp = await S.ai.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = resp.content[0].text.strip() if resp.content else ""
    except Exception as exc:
        log.error("pdf_tools: Haiku-Aufruf fehlgeschlagen: %s", exc)
        result = {
            "typ": "fehler",
            "fehler": str(exc),
            "summary": f"PDF-Analyse fehlgeschlagen ({filename}): LLM-Fehler.",
        }
        return result

    # Schritt 4: JSON parsen — Haiku gibt manchmal Markdown-Code-Blocks zurueck
    try:
        # Markdown-Fence-Bereinigung: ```json ... ``` oder ``` ... ```
        clean = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
        clean = re.sub(r"\s*```$", "", clean)
        data: dict = json.loads(clean.strip())
    except json.JSONDecodeError:
        log.warning("pdf_tools: Haiku lieferte kein gueltiges JSON: %s", raw_text[:200])
        data = {
            "typ": "unbekannt",
            "rohdaten": raw_text[:500],
        }

    # Schritt 5: In persons_db speichern
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

    # Schritt 6: Summary anhaengen und zurueckgeben
    data["summary"] = _format_summary(data)
    log.info("pdf_tools: Analyse abgeschlossen fuer %s: %s", filename, data["summary"])
    return data


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
            log.info("pdf_tools: Hintergrund-Analyse fertig: %s", result.get("summary", ""))

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
