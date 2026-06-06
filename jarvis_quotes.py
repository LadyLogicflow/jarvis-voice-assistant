"""
jarvis_quotes.py — Deutsche JARVIS-Zitate im Marvel-Stil (mit "Madam").

Organisiert nach Kontext. `quote(context)` wählt zufällig aus der
passenden Liste. Unbekannter Kontext gibt leeren String zurück.
"""

from __future__ import annotations

import random
import time as _time

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
    "james_bond": [
        "Jarvis. Catrins Jarvis.",
        "Mein Name ist Jarvis – Catrins Jarvis. Zu Diensten, Madam.",
        "Ich habe bereits drei Fluchtwege berechnet, Madam. Nur für den Fall.",
        "Die Mission ist abgeschlossen. Diskret, versteht sich.",
        "Das ist vertraulich, Madam. Wie so vieles in meinem Leben.",
        "Ich würde das nicht empfehlen, Madam. Aber ich habe schon Unmöglicheres erlebt.",
    ],
    "error_film": [
        "Houston, wir haben ein Problem, Madam.",
        "Das ist kein Mond – das ist ein Problem, Madam.",
    ],
    "bad_news": [
        "Sie können die Wahrheit doch gar nicht ertragen, Madam. Ich sage sie Ihnen trotzdem.",
        "Niemand ist vollkommen, Madam – nicht einmal ich.",
    ],
    "uncertainty": [
        "Das Leben ist wie eine Schachtel Pralinen, Madam. Man weiß nie, was man kriegt.",
        "Das war… unerwartet.",
    ],
    "motivation_film": [
        "Möge die Macht mit Ihnen sein, Madam.",
        "Carpe Diem, Madam.",
    ],
    "suggestion_film": [
        "Gestatten Sie mir ein Angebot, das Sie nicht ablehnen können, Madam.",
    ],
    "closing_film": [
        "Hasta la vista, Madam.",
        "Ich werde wiederkommen, Madam.",
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


# ---------------------------------------------------------------------------
# Cooldown-System für Film-/Bonuszitate (Issue #200)
# ---------------------------------------------------------------------------

_last_used: dict[str, float] = {}

_COOLDOWNS: dict[str, float] = {
    "error_film":      60 * 60,        # 60 min
    "bad_news":        90 * 60,        # 90 min
    "uncertainty":     90 * 60,
    "motivation_film": 120 * 60,       # 2 h
    "closing_film":    120 * 60,
    "james_bond":      120 * 60,
    "suggestion_film": 120 * 60,
}


def quote_maybe(context: str, probability: float = 0.25) -> str:
    """Gibt ein Zitat aus dem angegebenen Kontext zurück, oder '' bei Cooldown
    oder zufälligem Aussetzer.

    Args:
        context: Schlüssel aus QUOTES (z.B. "error_film", "closing_film").
        probability: Wahrscheinlichkeit 0-1 dass überhaupt ein Zitat erscheint.

    Returns:
        Ein zufällig gewählter Zitat-String, oder "" wenn Cooldown aktiv,
        Kontext unbekannt oder Zufallstreffer nicht erreicht.
    """
    if random.random() > probability:
        return ""
    cooldown = _COOLDOWNS.get(context, 0)
    if cooldown > 0:
        last = _last_used.get(context, 0.0)
        if _time.monotonic() - last < cooldown:
            return ""
    options = QUOTES.get(context, [])
    if not options:
        return ""
    chosen = random.choice(options)
    _last_used[context] = _time.monotonic()
    return chosen
