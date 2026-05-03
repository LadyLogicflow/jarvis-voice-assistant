"""Unit tests for normalize_for_tts (Stage 2 of issue #43)."""

from __future__ import annotations

import pytest

import tts


@pytest.mark.parametrize("raw,expected", [
    # Symbols
    ("Es sind 18°C", "Es sind 18 Grad"),
    ("Es sind 18°F", "Es sind 18 Grad Fahrenheit"),
    ("Drehen Sie um 90°", "Drehen Sie um 90 Grad"),
    ("87% Regenrisiko", "87 Prozent Regenrisiko"),
    ("100€ pro Monat", "100 Euro pro Monat"),
    ("50$ Gebuehr", "50 Dollar Gebuehr"),
    ("Schwarz & weiss", "Schwarz und weiss"),
    # Abbreviations
    ("Wie z.B. heute", "Wie zum Beispiel heute"),
    ("Wie z. B. heute", "Wie zum Beispiel heute"),  # space-tolerant
    ("d.h. wir warten", "das heisst wir warten"),
    ("u.a. der BFH", "unter anderem der Bundesfinanzhof"),
    ("ca. 30 Min", "circa 30 Min"),
    ("Mio. Umsatz", "Millionen Umsatz"),
    # Tax law specific
    ("Das BMF hat heute beschlossen", "Das Bundesministerium der Finanzen hat heute beschlossen"),
    ("Der EuGH urteilt", "Der Europaeischer Gerichtshof urteilt"),
    ("USt und GewSt", "Umsatzsteuer und Gewerbesteuer"),
    ("nach EStG §15", "nach Einkommensteuergesetz §15"),
    ("die AO regelt das", "die Abgabenordnung regelt das"),
    # No false positives on substrings
    ("Bachforelle", "Bachforelle"),
    ("Mio Robot", "Mio Robot"),  # no period -> not abbreviation
    # Combined
    ("BFH-Pressemitteilung: 18°C im Saal, 50% Anwesenheit",
     "Bundesfinanzhof-Pressemitteilung: 18 Grad im Saal, 50 Prozent Anwesenheit"),
])
def test_normalize_for_tts_table(raw, expected):
    assert tts.normalize_for_tts(raw) == expected


def test_normalize_is_idempotent():
    """Running it twice should give the same result as once."""
    text = "Heute z.B. 18°C im BFH"
    once = tts.normalize_for_tts(text)
    twice = tts.normalize_for_tts(once)
    assert once == twice


def test_normalize_preserves_plain_text():
    """Vanilla sentences without symbols / abbreviations stay byte-equal
    (modulo the trailing strip)."""
    text = "Guten Morgen, Madam. Es ist halb acht."
    assert tts.normalize_for_tts(text) == text


def test_normalize_collapses_double_spaces():
    """The substitution can leave double spaces; check they collapse."""
    raw = "100% sicher"
    out = tts.normalize_for_tts(raw)
    assert "  " not in out
