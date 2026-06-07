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
_RECIPIENT_ADDRESSES = ("Cranachstr. 60", "Reuschenberger Str. 11")

# Max. Zeichen aus dem PDF an den LLM uebergeben (Kostenkontrolle)
_MAX_PDF_CHARS = 3000
# Max. Seiten die ausgelesen werden
_MAX_PAGES = 3

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

    Nutzt pypdf (PdfReader). Bei verschluesselten oder bild-basierten PDFs
    wird ein leerer String zurueckgegeben.

    Args:
        pdf_bytes: Rohe PDF-Bytes.

    Returns:
        Extrahierter Text (bis zu _MAX_PDF_CHARS Zeichen) oder leerer String.
    """
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            log.debug("invoice_detector: PDF ist verschluesselt — ueberspringe")
            return ""

        texts: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= _MAX_PAGES:
                break
            try:
                t = page.extract_text() or ""
                texts.append(t)
            except Exception as page_exc:
                log.debug(
                    "invoice_detector: Seite %d konnte nicht extrahiert werden: %s",
                    i, page_exc,
                )
        combined = "\n".join(texts).strip()
        return combined[:_MAX_PDF_CHARS]
    except Exception as exc:
        log.debug("invoice_detector: pypdf-Extraktion fehlgeschlagen: %s: %s",
                  type(exc).__name__, exc)
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
    # LLM-Aufruf sparen.
    text_lower = text.lower()
    name_lower = _RECIPIENT_NAME.lower()
    addr_lower = [a.lower() for a in _RECIPIENT_ADDRESSES]
    has_hint = name_lower in text_lower or any(a in text_lower for a in addr_lower)
    if not has_hint:
        return False, "Kein Empfaenger-Hinweis im PDF-Text gefunden"

    # LLM-Aufruf (Haiku)
    user_msg = f"PDF-Text (erste {_MAX_PAGES} Seiten):\n\n{text}"
    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=80,
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
