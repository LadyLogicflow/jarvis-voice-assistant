"""
Bild-Anhang-Verarbeitung fuer JARVIS (Issue #177).

Stellt zwei Kernfunktionen bereit:

- ``is_tax_document``: Nutzt Claude Vision (Haiku), um zu pruefen ob ein
  Bild wie ein Steuerdokument aussieht. Gibt (True/False, Typ-Hinweis) zurueck.

- ``images_to_ocr_pdf``: Konvertiert eine Liste von Bildern in ein
  durchsuchbares PDF. Versucht zuerst ``ocrmypdf``, faellt auf
  Pillow-basiertes PDF ohne OCR zurueck.

Unterstuetzte Bildformate: JPEG, PNG, TIFF, HEIC (und alle von Pillow
unterstuetzten Formate).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import tempfile
from typing import Optional

import settings as S

log = logging.getLogger("jarvis")

# MIME-Typen die als Bild-Anhaenge behandelt werden.
IMAGE_MIME_PREFIXES = ("image/jpeg", "image/png", "image/tiff", "image/heic",
                       "image/heif", "image/webp", "image/gif", "image/bmp")

_TAX_CLASSIFIER_SYSTEM = (
    "Du bist ein Dokumentenklassifikator fuer Steuerdokumente. "
    "Analysiere das Bild und antworte NUR mit JSON: "
    "{\"is_tax_document\": true/false, \"document_type\": \"...\"}.\n"
    "is_tax_document = true bei: Steuerbescheid, Steuererklarung, Lohnsteuerbescheinigung, "
    "Einnahmen-Ueberschuss-Rechnung, Jahresabschluss, Bilanz, Gewinn- und Verlustrechnung, "
    "Umsatzsteuervoranmeldung, Kapitalertragsteuer, Freistellungsauftrag, "
    "Grundsteuerbescheid, Kirchensteuerbescheid, Mahnschreiben Finanzamt, "
    "Kontoauszug (Steuer/Finanzamt), Kassenbeleg/Quittung (steuerlich relevant), "
    "Belege fuer Werbungskosten oder Betriebsausgaben.\n"
    "document_type: Kurzer Typ-Hinweis auf Deutsch (z.B. 'Einkommensteuerbescheid 2023', "
    "'Lohnsteuerbescheinigung', 'Quittung', 'Kontoauszug') oder 'Unbekannt'.\n"
    "is_tax_document = false bei: private Fotos, Screenshots ohne Bezug, "
    "Werbung, Verpackungen, allgemeine Dokumente ohne Steuerbezug.\n"
    "Antworte NUR mit dem JSON-Objekt, nichts anderes."
)


async def is_tax_document(
    image_bytes: bytes,
    filename: str,
) -> tuple[bool, str]:
    """Prueft per Claude Vision (Haiku) ob ein Bild ein Steuerdokument ist.

    Args:
        image_bytes: Rohdaten des Bildes (JPEG, PNG, TIFF, HEIC, ...).
        filename:    Originaldateiname — wird als Kontext-Hinweis uebergeben.

    Returns:
        Tuple (is_tax_doc, document_type_hint):
            is_tax_doc:          True wenn das Bild wie ein Steuerdokument aussieht.
            document_type_hint:  Kurzer Typ-String (z.B. 'Einkommensteuerbescheid 2023').
    """
    import json as _json

    # Bild in JPEG konvertieren wenn noetig (Claude Vision akzeptiert
    # jpeg/png/gif/webp). HEIC und TIFF mussen konvertiert werden.
    image_data, media_type = _prepare_for_vision(image_bytes, filename)
    if image_data is None:
        log.warning("image_tools.is_tax_document: Konvertierung fehlgeschlagen fuer %r", filename)
        return False, "Unbekannt"

    b64_image = base64.standard_b64encode(image_data).decode("ascii")

    try:
        resp = await S.ai.messages.create(
            model=S.HAIKU_MODEL,
            max_tokens=100,
            system=_TAX_CLASSIFIER_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": f"Dateiname: {filename}. Ist das ein Steuerdokument?",
                        },
                    ],
                }
            ],
        )
        from prompt import llm_text
        import re
        raw = llm_text(resp).strip()
        # Strip markdown code fences
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        data = _json.loads(raw)
        is_tax = bool(data.get("is_tax_document", False))
        doc_type = str(data.get("document_type", "Unbekannt")).strip() or "Unbekannt"
        log.info(
            "image_tools.is_tax_document: filename=%r -> is_tax=%s type=%r",
            filename, is_tax, doc_type,
        )
        return is_tax, doc_type
    except Exception as e:
        log.warning(
            "image_tools.is_tax_document: Claude-Aufruf fehlgeschlagen fuer %r: %s: %s",
            filename, type(e).__name__, e,
        )
        return False, "Unbekannt"


def _prepare_for_vision(
    image_bytes: bytes,
    filename: str,
) -> tuple[Optional[bytes], str]:
    """Konvertiert Bilddaten in ein Claude-Vision-kompatibles Format.

    Claude Vision unterstuetzt: image/jpeg, image/png, image/gif, image/webp.
    TIFF, HEIC und andere Formate werden per Pillow zu JPEG konvertiert.

    Args:
        image_bytes: Rohdaten des Bildes.
        filename:    Originaldateiname (wird fuer Typ-Erkennung genutzt).

    Returns:
        Tuple (konvertierte_bytes, media_type) oder (None, '') bei Fehler.
    """
    fname_lower = filename.lower()

    # Direkt unterstuetzte Formate erkennen
    if fname_lower.endswith(".jpg") or fname_lower.endswith(".jpeg"):
        return image_bytes, "image/jpeg"
    if fname_lower.endswith(".png"):
        return image_bytes, "image/png"
    if fname_lower.endswith(".gif"):
        return image_bytes, "image/gif"
    if fname_lower.endswith(".webp"):
        return image_bytes, "image/webp"

    # Alle anderen (TIFF, HEIC, HEIF, BMP, ...) per Pillow zu JPEG
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        # HEIC hat manchmal einen Mode der zu JPEG nicht direkt passt
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except ImportError:
        log.warning("image_tools._prepare_for_vision: Pillow nicht installiert")
        # Letzter Ausweg: als JPEG ausgeben und hoffen
        return image_bytes, "image/jpeg"
    except Exception as e:
        log.warning(
            "image_tools._prepare_for_vision: Konvertierung fehlgeschlagen: %s: %s",
            type(e).__name__, e,
        )
        return None, ""


def images_to_ocr_pdf(image_list: list[bytes]) -> bytes:
    """Konvertiert eine Liste von Bildern in ein (moeglichst) durchsuchbares PDF.

    Strategie (Fallback-Kette):
    1. ``ocrmypdf`` (falls installiert): erstellt ein volltextdurchsuchbares PDF.
    2. ``pytesseract`` + Pillow: Bilder per Tesseract OCR zu PDF.
    3. Pillow allein: Bilder ohne OCR zu PDF zusammenfuehren.
    4. Letzter Ausweg: Erstes Bild direkt als minimales PDF ausgeben.

    Args:
        image_list: Liste von Bilddaten (bytes). Jedes Element ist ein Bild.

    Returns:
        PDF-Bytes (kann mit ocrmypdf durchsuchbar sein oder rein bildbasiert).

    Raises:
        ValueError: Wenn image_list leer ist.
    """
    if not image_list:
        raise ValueError("image_list darf nicht leer sein")

    # --- Versuch 1: Pillow-PDF erstellen, dann ocrmypdf drauflaeufen lassen ---
    try:
        from PIL import Image as _PILImage

        images_pil = []
        for img_bytes in image_list:
            img = _PILImage.open(io.BytesIO(img_bytes))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            images_pil.append(img)

        # Pillow-PDF (Bildbasiert, noch kein OCR)
        pdf_buf = io.BytesIO()
        first = images_pil[0]
        rest = images_pil[1:]
        first.save(
            pdf_buf,
            format="PDF",
            save_all=True,
            append_images=rest,
            resolution=150,
        )
        raw_pdf_bytes = pdf_buf.getvalue()
        log.debug("image_tools.images_to_ocr_pdf: Pillow-PDF erstellt (%d bytes)", len(raw_pdf_bytes))

        # --- Versuch 1a: ocrmypdf (Volltext-OCR) ---
        try:
            import ocrmypdf  # type: ignore
            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = os.path.join(tmpdir, "input.pdf")
                output_path = os.path.join(tmpdir, "output.pdf")
                with open(input_path, "wb") as f:
                    f.write(raw_pdf_bytes)
                ocrmypdf.ocr(
                    input_path,
                    output_path,
                    language="deu+eng",
                    skip_text=True,    # bereits-OCR-Seiten nicht doppelt bearbeiten
                    deskew=True,
                    progress_bar=False,
                )
                with open(output_path, "rb") as f:
                    result = f.read()
                log.info(
                    "image_tools.images_to_ocr_pdf: ocrmypdf erfolgreich (%d bytes)", len(result)
                )
                return result
        except ImportError:
            log.debug("image_tools.images_to_ocr_pdf: ocrmypdf nicht verfuegbar, versuche pytesseract")
        except Exception as e:
            log.warning(
                "image_tools.images_to_ocr_pdf: ocrmypdf fehlgeschlagen: %s: %s",
                type(e).__name__, e,
            )

        # --- Versuch 1b: pytesseract ---
        try:
            import pytesseract  # type: ignore

            page_pdfs: list[bytes] = []
            for img in images_pil:
                page_pdf = pytesseract.image_to_pdf_or_hocr(
                    img, extension="pdf", lang="deu+eng"
                )
                page_pdfs.append(page_pdf)

            if len(page_pdfs) == 1:
                log.info(
                    "image_tools.images_to_ocr_pdf: pytesseract (1 Seite, %d bytes)",
                    len(page_pdfs[0]),
                )
                return page_pdfs[0]

            # Mehrere Seiten zusammenfuehren per pypdf falls verfuegbar
            try:
                import pypdf  # type: ignore

                writer = pypdf.PdfWriter()
                for page_pdf in page_pdfs:
                    reader = pypdf.PdfReader(io.BytesIO(page_pdf))
                    for page in reader.pages:
                        writer.add_page(page)
                out_buf = io.BytesIO()
                writer.write(out_buf)
                result = out_buf.getvalue()
                log.info(
                    "image_tools.images_to_ocr_pdf: pytesseract+pypdf (%d Seiten, %d bytes)",
                    len(page_pdfs), len(result),
                )
                return result
            except ImportError:
                log.debug("image_tools.images_to_ocr_pdf: pypdf nicht verfuegbar, nur Seite 1")
                return page_pdfs[0]

        except ImportError:
            log.debug("image_tools.images_to_ocr_pdf: pytesseract nicht verfuegbar")
        except Exception as e:
            log.warning(
                "image_tools.images_to_ocr_pdf: pytesseract fehlgeschlagen: %s: %s",
                type(e).__name__, e,
            )

        # --- Fallback: Pillow-PDF ohne OCR zurueckgeben ---
        log.info(
            "image_tools.images_to_ocr_pdf: Fallback auf Pillow-PDF ohne OCR (%d bytes)",
            len(raw_pdf_bytes),
        )
        return raw_pdf_bytes

    except ImportError:
        log.warning("image_tools.images_to_ocr_pdf: Pillow nicht installiert — erzeuge minimales PDF")

    # --- Letzter Ausweg: Minimal-PDF manuell ---
    return _minimal_pdf(image_list[0])


def _minimal_pdf(image_bytes: bytes) -> bytes:
    """Erzeugt ein minimales PDF aus einem JPEG-Bild ohne externe Bibliotheken.

    Nur als absoluter Fallback wenn weder Pillow noch andere Libs verfuegbar sind.

    Args:
        image_bytes: JPEG-Bilddaten.

    Returns:
        PDF-Bytes.
    """
    # Naive Haesslichkeit die trotzdem ein gultiges PDF ergibt.
    img_len = len(image_bytes)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    pdf = (
        "%PDF-1.4\n"
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Contents 4 0 R /Resources << /XObject << /Im0 5 0 R >> >> >>\nendobj\n"
        "4 0 obj\n<< /Length 32 >>\nstream\nq 595 0 0 842 0 0 cm /Im0 Do Q\nendstream\nendobj\n"
        f"5 0 obj\n<< /Type /XObject /Subtype /Image /Width 595 /Height 842 "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {img_len} >>\n"
        f"stream\n"
    )
    return pdf.encode("latin-1") + image_bytes + b"\nendstream\nendobj\n%%EOF\n"
