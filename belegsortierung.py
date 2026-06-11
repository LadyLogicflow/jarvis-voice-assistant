"""
BelegSortierung-API-Client (Issue #234).

Laedt Steuerdokumente (PDFs, Bilder) automatisch in die BelegSortierung-API
hoch und pollt den Verarbeitungsstatus.

Verwendung:
    result = await upload_document(pdf_data, "scan.pdf", "Mustermann",
                                   "Max", "12345")
    status = await poll_status(result["review_filename"])

Die Integration ist ein No-op wenn S.BELEGSORTIERUNG_API_URL leer ist.
In diesem Fall loggt upload_document() eine WARNING und gibt ein leeres
dict zurueck.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import urllib.parse

import httpx

import settings as S

log = S.log


def _is_configured() -> bool:
    """Gibt True zurueck wenn API-URL und API-Key konfiguriert sind."""
    return bool(S.BELEGSORTIERUNG_API_URL and S.BELEGSORTIERUNG_API_KEY)


async def upload_document(
    pdf_data: bytes,
    filename: str,
    nachname: str,
    vorname: str,
    mitgliedsnummer: str,
    vz: int | None = None,
) -> dict:
    """Laedt ein Dokument in die BelegSortierung-API hoch.

    Sendet einen multipart/form-data POST an
    BELEGSORTIERUNG_API_URL/api/v1/upload. Authentifizierung per
    X-API-Key-Header.

    Args:
        pdf_data:        Rohe Byte-Daten des PDFs oder Bildes.
        filename:        Dateiname des Dokuments (z.B. "lohnausweis_2024.pdf").
        nachname:        Nachname des Mitglieds.
        vorname:         Vorname des Mitglieds.
        mitgliedsnummer: Mitgliedsnummer aus der Datenbank.
        vz:              Veranlagungszeitraum (Jahr). Standard: aktuelles Jahr - 1.

    Returns:
        Dict mit "review_filename" und "status_url" aus der API-Antwort,
        oder leeres Dict wenn die Integration nicht konfiguriert ist oder
        der Upload fehlschlaegt.
    """
    if not _is_configured():
        log.warning(
            "belegsortierung: BELEGSORTIERUNG_API_URL oder BELEGSORTIERUNG_API_KEY "
            "nicht konfiguriert — Upload uebersprungen"
        )
        return {}

    if vz is None:
        vz = datetime.date.today().year - 1

    base_url = S.BELEGSORTIERUNG_API_URL.rstrip("/")
    upload_url = f"{base_url}/api/v1/upload"

    # Content-Type fuer haengige Dateitypen bestimmen
    fname_lower = filename.lower()
    if fname_lower.endswith(".pdf"):
        content_type = "application/pdf"
    elif fname_lower.endswith(".jpg") or fname_lower.endswith(".jpeg"):
        content_type = "image/jpeg"
    elif fname_lower.endswith(".png"):
        content_type = "image/png"
    elif fname_lower.endswith(".tiff") or fname_lower.endswith(".tif"):
        content_type = "image/tiff"
    else:
        content_type = "application/octet-stream"

    files = {
        "file": (filename, pdf_data, content_type),
    }
    data = {
        "nachname": nachname,
        "vorname": vorname,
        "mitgliedsnummer": mitgliedsnummer,
        "vz": str(vz),
    }
    headers = {
        "X-API-Key": S.BELEGSORTIERUNG_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                upload_url,
                files=files,
                data=data,
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()
            log.info(
                "belegsortierung: Dokument '%s' hochgeladen fuer %s %s (MNr. %s): "
                "review_filename=%r status_url=%r",
                filename, vorname, nachname, mitgliedsnummer,
                result.get("review_filename"), result.get("status_url"),
            )
            return result
    except httpx.HTTPStatusError as exc:
        log.error(
            "belegsortierung: HTTP-Fehler beim Upload von '%s': %s %s",
            filename, exc.response.status_code, exc.response.text[:200],
        )
        return {}
    except httpx.RequestError as exc:
        log.error(
            "belegsortierung: Verbindungsfehler beim Upload von '%s': %s: %s",
            filename, type(exc).__name__, exc,
        )
        return {}
    except Exception as exc:
        log.error(
            "belegsortierung: Unerwarteter Fehler beim Upload von '%s': %s: %s",
            filename, type(exc).__name__, exc,
        )
        return {}


async def poll_status(
    review_filename: str,
    timeout_minutes: int = 30,
) -> str:
    """Pollt den Verarbeitungsstatus eines hochgeladenen Dokuments.

    Fragt GET /api/v1/status/{review_filename} alle 60 Sekunden ab bis
    ein Endstatus erreicht ist oder das Timeout ablaeuft.

    Args:
        review_filename: Der Dateiname aus der Upload-Antwort
                         ("review_filename"-Feld).
        timeout_minutes: Maximale Wartezeit in Minuten (Standard: 30).

    Returns:
        Finaler Status-String aus der API (z.B. "completed", "failed")
        oder "timeout" wenn das Limit erreicht wurde, oder "error" bei
        Verbindungsproblemen.
    """
    if not _is_configured():
        log.warning(
            "belegsortierung: poll_status aufgerufen aber Integration nicht konfiguriert"
        )
        return "error"

    base_url = S.BELEGSORTIERUNG_API_URL.rstrip("/")
    status_url = f"{base_url}/api/v1/status/{urllib.parse.quote(review_filename, safe='')}"
    headers = {
        "X-API-Key": S.BELEGSORTIERUNG_API_KEY,
    }

    poll_interval = 60  # Sekunden
    max_attempts = (timeout_minutes * 60) // poll_interval
    attempt = 0

    log.info(
        "belegsortierung: Starte Status-Polling fuer '%s' (max. %d Minuten)",
        review_filename, timeout_minutes,
    )

    # Terminale Status-Werte — sobald einer davon auftaucht, wird abgebrochen
    _TERMINAL_STATES = frozenset({"completed", "done", "failed", "error", "rejected"})

    async with httpx.AsyncClient(timeout=30.0) as client:
        while attempt < max_attempts:
            await asyncio.sleep(poll_interval)
            attempt += 1
            try:
                resp = await client.get(status_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                status = str(data.get("status", "")).lower()
                log.info(
                    "belegsortierung: Status-Poll %d/%d fuer '%s': %s",
                    attempt, max_attempts, review_filename, status,
                )
                if status in _TERMINAL_STATES:
                    log.info(
                        "belegsortierung: Finaler Status '%s' fuer '%s'",
                        status, review_filename,
                    )
                    return status
            except httpx.HTTPStatusError as exc:
                log.warning(
                    "belegsortierung: HTTP-Fehler beim Status-Poll fuer '%s': %s",
                    review_filename, exc.response.status_code,
                )
            except httpx.RequestError as exc:
                log.warning(
                    "belegsortierung: Verbindungsfehler beim Status-Poll fuer '%s': %s: %s",
                    review_filename, type(exc).__name__, exc,
                )
            except Exception as exc:
                log.warning(
                    "belegsortierung: Unerwarteter Fehler beim Status-Poll fuer '%s': %s: %s",
                    review_filename, type(exc).__name__, exc,
                )

    log.warning(
        "belegsortierung: Timeout nach %d Minuten fuer '%s'",
        timeout_minutes, review_filename,
    )
    return "timeout"
