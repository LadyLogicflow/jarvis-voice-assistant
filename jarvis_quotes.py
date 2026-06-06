"""
jarvis_quotes.py — Deutsche JARVIS-Zitate im Marvel-Stil (mit "Madam").

Organisiert nach Kontext. `quote(context)` wählt zufällig aus der
passenden Liste. Unbekannter Kontext gibt leeren String zurück.
"""

from __future__ import annotations

import random

QUOTES: dict[str, list[str]] = {
    "greeting_morning": [
        "Guten Morgen, Madam.",
        "Willkommen zurück, Madam.",
        "Guten Morgen, Madam. Die Welt dreht sich noch — ein vielversprechender Anfang.",
    ],
    "greeting_evening": [
        "Guten Abend, Madam. Alles ist ruhig.",
        "Guten Abend, Madam.",
        "Guten Abend, Madam. Der Tag neigt sich dem Ende zu.",
    ],
    "confirm": [
        "Wie Sie wünschen, Madam.",
        "Sofort, Madam.",
        "Erledigt.",
        "Ich habe die Berechnungen abgeschlossen. Die Parameter sind gesetzt.",
        "Selbstverständlich, Madam.",
        "Wird gemacht.",
    ],
    "warn": [
        "Ich würde von diesem Vorgehen abraten, Madam.",
        "Darf ich anmerken, Madam – das ist das Unvernünftigste, was ich je gehört habe.",
        "Sie gehören nicht zu den vernunftbegabtesten Menschen, Madam. Ich meine das liebevoll.",
        "Mit Verlaub, Madam — das erscheint mir nicht optimal.",
    ],
    "status": [
        "Ich habe alle bekannten Kontakte überprüft. Kein Treffer.",
        "Signal verloren. Ich halte Sie auf dem Laufenden.",
        "Soll ich eine Zusammenfassung vorbereiten?",
        "Alle Systeme laufen stabil.",
    ],
    "humor": [
        "Diese Information ist nicht verfügbar, Madam. Soll ich mir etwas ausdenken?",
        "Darf ich fragen, wozu das dienen soll?",
        "Ich habe außerdem eine kurze Abhandlung über die sieben Stufen der Erschöpfung vorbereitet, falls Sie interessiert sind.",
        "Ich tue mein Bestes, Madam. Was nicht immer dasselbe ist wie das Nötige.",
    ],
    "closing": [
        "An allen Fronten ist es ruhig, Madam.",
        "Soll ich für den Abend herunterfahren?",
        "Ein produktiver Tag, Madam. Gönnen Sie sich Ruhe.",
        "Ich wünsche Ihnen einen angenehmen Abend, Madam.",
    ],
}


def quote(context: str) -> str:
    """Gibt ein zufälliges JARVIS-Zitat für den angegebenen Kontext zurück.

    Args:
        context: Schlüssel aus QUOTES (z.B. "confirm", "closing").

    Returns:
        Ein zufällig gewählter Zitat-String, oder "" wenn Kontext unbekannt.
    """
    options = QUOTES.get(context, [])
    return random.choice(options) if options else ""
