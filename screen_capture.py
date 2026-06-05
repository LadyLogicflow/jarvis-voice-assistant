"""
Jarvis V2 — Screen Capture
Takes screenshots and describes them via Claude Vision.

Platform notes:
- macOS, Windows: PIL.ImageGrab is sufficient out of the box.
- Linux X11: PIL.ImageGrab works since Pillow 9.4. Requires DISPLAY to
  be set; under SSH/headless setups it raises a clear error.
- Linux Wayland: PIL.ImageGrab does not support Wayland. Workarounds:
  switch the desktop session to X11, or run via XWayland, or shell out
  to `grim`/`gnome-screenshot` (not implemented here — the simple
  fallback below just produces an actionable error message).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import platform
from typing import TYPE_CHECKING

from PIL import ImageGrab

import settings as S
from prompt import llm_text

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger("jarvis.screen")


class ScreenCaptureError(RuntimeError):
    """Raised when the platform / environment cannot produce a screenshot."""


def capture_screen() -> bytes:
    """Capture the entire screen and return PNG bytes.

    Raises ScreenCaptureError with a human-readable explanation when
    Pillow refuses (most common failure: Linux without DISPLAY, or
    Wayland session)."""
    try:
        img = ImageGrab.grab()
    except Exception as e:
        if platform.system() == "Linux":
            raise ScreenCaptureError(
                "Bildschirmaufnahme unter Linux fehlgeschlagen. "
                "Pruefe ob DISPLAY gesetzt ist (X11 noetig). "
                "Wayland-Sessions werden von PIL.ImageGrab nicht unterstuetzt — "
                "wechsle entweder zur X11-Session oder installiere `grim`/`gnome-screenshot`."
            ) from e
        raise ScreenCaptureError(f"Screenshot konnte nicht erstellt werden: {e}") from e

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_VISION_PROMPT = (
    "Beschreibe kurz auf Deutsch was auf diesem Bildschirm zu sehen ist. "
    "Maximal 2-3 Saetze. Nenne die wichtigsten offenen Programme und Inhalte."
)


async def describe_screen(anthropic_client: "AsyncAnthropic") -> str:
    """Capture screen with PIL and describe it using Claude Vision."""
    try:
        loop = asyncio.get_running_loop()
        png_bytes = await loop.run_in_executor(None, capture_screen)
    except ScreenCaptureError as e:
        log.warning(f"capture_screen failed: {e}")
        return str(e)

    b64 = base64.b64encode(png_bytes).decode("utf-8")
    return await _vision_call(b64, "image/png", anthropic_client)


async def describe_screen_from_b64(image_b64: str, anthropic_client: "AsyncAnthropic") -> str:
    """Describe a JPEG screenshot already captured by the browser client."""
    return await _vision_call(image_b64, "image/jpeg", anthropic_client)


async def _vision_call(b64: str, media_type: str, anthropic_client: "AsyncAnthropic") -> str:
    response = await anthropic_client.messages.create(
        model=S.HAIKU_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {"type": "text", "text": _VISION_PROMPT},
            ],
        }],
    )
    return llm_text(response)
