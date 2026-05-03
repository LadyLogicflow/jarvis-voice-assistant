"""
Text-to-speech pipeline.

Pre-processes the text via `normalize_for_tts` (issue #43, Stage 2),
splits long replies into <=250-char chunks at sentence boundaries
(`_split_text`), wraps the ElevenLabs HTTP call with tenacity retry
(`_tts_one`), and streams the audio chunks back over the WebSocket
(`speak`).
"""

from __future__ import annotations

import base64
import re

import httpx
from fastapi import WebSocket
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import settings as S

log = S.log


_TTS_SUBSTITUTIONS = [
    # Symbols (most common offenders)
    ("°C", " Grad"),
    ("°F", " Grad Fahrenheit"),
    ("°", " Grad"),
    ("%", " Prozent"),
    ("€", " Euro"),
    ("$", " Dollar"),
    (" & ", " und "),
]
_TTS_REGEX_SUBS = [
    # German abbreviations (word-bounded so we don't break "z.B."-like
    # tokens that aren't really abbreviations).
    (r"\bz\s*\.\s*B\.", "zum Beispiel"),
    (r"\bd\s*\.\s*h\.", "das heisst"),
    (r"\bu\s*\.\s*a\.", "unter anderem"),
    (r"\bz\s*\.\s*T\.", "zum Teil"),
    (r"\bv\s*\.\s*a\.", "vor allem"),
    (r"\bbzw\.", "beziehungsweise"),
    (r"\bggf\.", "gegebenenfalls"),
    (r"\busw\.", "und so weiter"),
    (r"\betc\.", "et cetera"),
    (r"\bca\.", "circa"),
    (r"\bNr\.", "Nummer"),
    (r"\bMrd\.", "Milliarden"),
    (r"\bMio\.", "Millionen"),
    (r"\bSt\.", "Sankt"),
    # Tax / legal abbreviations relevant for Catrin's domain.
    (r"\bBFH\b", "Bundesfinanzhof"),
    (r"\bBMF\b", "Bundesministerium der Finanzen"),
    (r"\bEuGH\b", "Europaeischer Gerichtshof"),
    (r"\bUSt\b", "Umsatzsteuer"),
    (r"\bGewSt\b", "Gewerbesteuer"),
    (r"\bEStG\b", "Einkommensteuergesetz"),
    (r"\bAO\b", "Abgabenordnung"),
    (r"\bDIHAG\b", "D I H A G"),  # spell letters so it isn't read as one word
    # HILO bleibt als Wort stehen — Catrin sagt es ist ein deutsches
    # Wort (Lohnsteuerhilfeverein-Markenname), nicht buchstabiert.
    # Multiple spaces collapse.
    (r" {2,}", " "),
]


def normalize_for_tts(text: str) -> str:
    """Strip / spell out symbols and German abbreviations so the TTS
    voice doesn't read them literally ('°C' -> 'Grad C', 'BFH' -> 'B F H',
    'z.B.' -> 'Z punkt B punkt'). Idempotent. Stage 2 of issue #43 — the
    safety net under the system-prompt rules in case the LLM still emits
    raw symbols (Haiku is empirically stubborn about °C)."""
    out = text
    for needle, repl in _TTS_SUBSTITUTIONS:
        out = out.replace(needle, repl)
    for pattern, repl in _TTS_REGEX_SUBS:
        out = re.sub(pattern, repl, out)
    return out.strip()


def _split_text(text: str) -> list[str]:
    """Split text into <=250-char chunks at sentence boundaries."""
    if len(text) <= 250:
        return [text]
    chunks = []
    sentences = re.split(r'(?<=[.!?])\s+', text)
    current = ""
    for s in sentences:
        if len(current) + len(s) > 250 and current:
            chunks.append(current.strip())
            current = s
        else:
            current = (current + " " + s).strip()
    if current:
        chunks.append(current.strip())
    return chunks


async def _tts_post(text: str) -> bytes:
    """One ElevenLabs request; tenacity wraps retry above. The text is
    pre-normalized so the voice never reads raw symbols / abbreviations."""
    text = normalize_for_tts(text)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{S.ELEVENLABS_VOICE_ID}"
    resp = await S.http.post(url, headers={
        "xi-api-key": S.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }, json={
        "text": text,
        "model_id": S.ELEVENLABS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.85},
    })
    log.info(f"TTS chunk status: {resp.status_code}, size: {len(resp.content)}")
    if resp.status_code != 200:
        log.warning(f"TTS error: {resp.text[:200]}")
        raise httpx.HTTPStatusError("TTS non-200", request=resp.request, response=resp)
    return resp.content


async def _tts_one(text: str) -> bytes:
    """Generate TTS for a single short text chunk, with up to 2 retries
    on transient failures (network blips, occasional 5xx)."""
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                return await _tts_post(text)
    except Exception as e:
        log.warning(f"TTS EXCEPTION: {e}")
    return b""


async def speak(text: str, ws: WebSocket, display: str = "") -> bool:
    """Generate TTS and send each chunk immediately. Returns False if connection lost."""
    if not text.strip():
        return True
    chunks = _split_text(text)
    first = True
    for chunk in chunks:
        audio = await _tts_one(chunk)
        if audio:
            try:
                await ws.send_json({
                    "type": "response",
                    "text": display if first else "",
                    "audio": base64.b64encode(audio).decode("utf-8"),
                })
                first = False
            except Exception:
                log.warning("[speak] WebSocket closed, aborting TTS.")
                return False
    return True
