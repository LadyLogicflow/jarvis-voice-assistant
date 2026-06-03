"""
PDF-Tools fuer JARVIS (Issue #159 / #109).

V1-Stub: empfaengt PDF-Pfad, loggt Empfang und speichert die Datei
unter /tmp/jarvis_pdfs/. Die eigentliche Analyse (Steuerbescheid-Auswertung)
wird in Issue #109 implementiert.
"""

from __future__ import annotations

import os

import settings as S

log = S.log

_PDF_DIR = "/tmp/jarvis_pdfs"


def analyze_pdf_stub(filepath: str) -> str:
    """Stub fuer spätere PDF-Analyse (Issue #109).

    Legt /tmp/jarvis_pdfs/ an (falls nicht vorhanden), loggt den Empfang
    und gibt eine Bestätigungsnachricht zurück.

    Args:
        filepath: Absoluter Pfad zur gespeicherten PDF-Datei.

    Returns:
        Kurze Bestätigungszeichenkette für das Logging.
    """
    filename = os.path.basename(filepath)
    log.info("pdf_tools: PDF empfangen: %s", filename)
    return f"PDF empfangen: {filename}"


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
    # Sanitize: only keep safe characters in the filename.
    safe_name = "".join(
        c if (c.isalnum() or c in "._- ") else "_" for c in filename
    ).strip() or "attachment.pdf"
    dest = os.path.join(_PDF_DIR, safe_name)
    with open(dest, "wb") as f:
        f.write(data)
    log.info("pdf_tools: PDF gespeichert: %s (%d bytes)", dest, len(data))
    return dest
