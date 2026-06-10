"""
Rechnung-Erkennung fuer Caterina Essberger-Brenscheidt (Issue #208).

Prueft ob ein PDF eine Rechnung AN Caterina Essberger-Brenscheidt ist,
anhand von Name und Adresse als Rechnungsempfaenger. Nutzt pypdf fuer
Textextraktion und Claude Haiku fuer die Klassifikation.

Rechnungen AN Mandanten oder Dritte werden NICHT erkannt und
NICHT weitergeleitet.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("jarvis")

# Weiterleitung-Zieladresse fuer Rechnungen an Caterina Essberger-Brenscheidt.
INVOICE_FORWARD_TO = (
    "lohnsteuerhilfevereinhiloevberatungsstellewuppertalbarmen@getmyinvoices.net"
)

# Erkennungsmerkmale des Rechnungsempfaengers (autorisiert von Caterina)
_RECIPIENT_NAME = "Caterina Essberger-Brenscheidt"
# Kurzform: Nachname allein reicht als Vorfilter-Treffer (Issue #226)
_RECIPIENT_NAME_SHORT = "essberger-brenscheidt"
# Alle gaengigen Schreibweisen der Adressen (abgekuerzt, ausgeschrieben, mit/ohne ß)
_RECIPIENT_ADDRESSES = (
    "cranachstr. 60",
    "cranachstr.60",
    "cranachstraße 60",
    "cranachstrasse 60",
    "reuschenberger str. 11",
    "reuschenberger str.11",
    "reuschenberger straße 11",
    "reuschenberger strasse 11",
)

# Max. Zeichen aus dem PDF an den LLM uebergeben (Kostenkontrolle)
_MAX_PDF_CHARS = 3000
# Max. Seiten die ausgelesen werden
_MAX_PAGES = 3
# Mindestzeichen pro Seite — unter dieser Schwelle wird OCR als Fallback versucht
_MIN_TEXT_CHARS = 50
# Rendering-Aufloesung fuer Pixmap bei Bild-PDFs (DPI)
_OCR_DPI = 200

_DETECTOR_SYSTEM = (
    "Du bist ein Rechnungspruefer. Antworte AUSSCHLIESSLICH mit JSON.\n"
    "Pruefe: Ist dieses Dokument eine Rechnung (oder Abrechnung / Zahlungsaufforderung) "
    "deren Empfaenger Caterina Essberger-Brenscheidt ist "
    "(Adressen: Cranachstr. 60 oder Reuschenberger Str. 11)?\n"
    "Antworte mit: {\"is_invoice_to_catrin\": true/false, \"reason\": \"Ein Satz auf Deutsch\"}\n"
    "Bei Unsicherheit IMMER false. Nur true wenn Name UND/ODER Adresse eindeutig passen."
)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extrahiert den Plaintext aus den ersten _MAX_PAGES Seiten eines PDFs.

    Nutzt PyMuPDF (fitz) fuer die Textextraktion. Bei Bild-PDFs (gescannte
    Dokumente ohne Text-Layer) wird Tesseract-OCR als Fallback eingesetzt,
    sofern pytesseract und Pillow verfuegbar sind.

    Fallback-Logik pro Seite:
    - Liefert fitz weniger als _MIN_TEXT_CHARS Zeichen, wird die Seite als
      Pixmap gerendert und per Tesseract OCR gelesen.
    - Fehlt das Tesseract-Binary oder schlaegt die OCR fehl, wird der
      vorhandene (ggf. leere) fitz-Text ohne Absturz verwendet.

    Args:
        pdf_bytes: Rohe PDF-Bytes.

    Returns:
        Extrahierter Text (bis zu _MAX_PDF_CHARS Zeichen) oder leerer String.
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if doc.is_encrypted:
                log.debug("invoice_detector: PDF verschluesselt — ueberspringe")
                return ""
            texts: list[str] = []
            for i in range(min(_MAX_PAGES, doc.page_count)):
                try:
                    page = doc[i]
                    t = page.get_text().strip()
                except Exception as page_exc:
                    log.debug("invoice_detector: Seite %d get_text: %s", i, page_exc)
                    continue
                if len(t) < _MIN_TEXT_CHARS:
                    # Bild-PDF: Seite als Pixmap rendern und per Tesseract OCR lesen
                    try:
                        from PIL import Image
                        import pytesseract
                        mat = fitz.Matrix(_OCR_DPI / 72, _OCR_DPI / 72)
                        pix = page.get_pixmap(matrix=mat)
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        # deu+eng: falls deu-Sprachpaket fehlt, graceful fallback auf eng
                        try:
                            t = pytesseract.image_to_string(img, lang="deu+eng")
                        except pytesseract.TesseractError:
                            t = pytesseract.image_to_string(img, lang="eng")
                    except Exception as ocr_err:
                        log.debug("invoice_detector: OCR Seite %d: %s", i, ocr_err)
                texts.append(t)
            return "\n".join(texts).strip()[:_MAX_PDF_CHARS]
        finally:
            doc.close()
    except Exception as exc:
        log.debug("invoice_detector: fitz-Extraktion: %s: %s", type(exc).__name__, exc)
        return ""


async def detect_invoice_for_catrin(pdf_bytes: bytes) -> tuple[bool, str]:
    """Prueft ob ein PDF eine Rechnung an Caterina Essberger-Brenscheidt ist.

    Extrahiert den PDF-Text (pypdf) und befragt Claude Haiku.
    Bei nicht-lesbaren PDFs (verschluesselt, reine Bild-PDFs) wird
    sicher (False) zurueckgegeben. Bei LLM-Fehler ebenfalls False.

    Args:
        pdf_bytes: Rohe PDF-Bytes des Anhangs.

    Returns:
        Tupel (is_match, reason):
          - is_match: True wenn es sich eindeutig um eine Rechnung
            an Caterina Essberger-Brenscheidt handelt.
          - reason: Kurze deutsche Begruendung oder Fehlerursache.
    """
    import settings as S
    from prompt import llm_text

    # Textextraktion
    text = _extract_pdf_text(pdf_bytes)
    if not text:
        return False, "PDF nicht lesbar (verschluesselt oder Bild-PDF)"

    # Schnellpruefung: Wenn kein Erkennungsmerkmal im Text vorkommt,
    # LLM-Aufruf sparen. _RECIPIENT_ADDRESSES sind bereits lowercase.
    text_lower = text.lower()
    name_lower = _RECIPIENT_NAME.lower()
    has_hint = (
        name_lower in text_lower
        or _RECIPIENT_NAME_SHORT in text_lower
        or any(a in text_lower for a in _RECIPIENT_ADDRESSES)
    )
    if not has_hint:
        return False, "Kein Empfaenger-Hinweis im PDF-Text gefunden"

    # LLM-Aufruf (Haiku)
    user_msg = f"PDF-Text (erste {_MAX_PAGES} Seiten):\n\n{text}"
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=150,
            system=_DETECTOR_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = llm_text(resp).strip()
        # Code-Fence entfernen falls LLM sie setzt
        if raw.startswith("```"):
            import re as _re
            raw = _re.sub(r"^```(?:json)?\s*", "", raw)
            raw = _re.sub(r"\s*```$", "", raw).strip()
        data = json.loads(raw)
        is_match = bool(data.get("is_invoice_to_catrin", False))
        reason = str(data.get("reason", "")).strip()
        return is_match, reason
    except json.JSONDecodeError as jde:
        log.warning("invoice_detector: JSON-Parsefehler in LLM-Antwort: %s", jde)
        return False, "LLM-Fehler (JSON parse)"
    except Exception as exc:
        log.warning("invoice_detector: LLM-Aufruf fehlgeschlagen: %s: %s",
                    type(exc).__name__, exc)
        return False, f"LLM-Fehler ({type(exc).__name__})"
