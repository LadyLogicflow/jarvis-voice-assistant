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

import base64
import io
import logging
import platform
from typing import TYPE_CHECKING

from PIL import ImageGrab

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


async def describe_screen(anthropic_client: "AsyncAnthropic") -> str:
    """Capture screen and describe it using Claude Vision."""
    try:
        loop = __import__("asyncio").get_event_loop()
        png_bytes = await loop.run_in_executor(None, capture_screen)
    except ScreenCaptureError as e:
        log.warning(f"capture_screen failed: {e}")
        return str(e)

    b64 = base64.b64encode(png_bytes).decode("utf-8")
    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": "Beschreibe kurz auf Deutsch was auf diesem Bildschirm zu sehen ist. Maximal 2-3 Saetze. Nenne die wichtigsten offenen Programme und Inhalte.",
                },
            ],
        }],
    )
    return llm_text(response)
