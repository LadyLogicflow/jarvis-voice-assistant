"""Unit tests for text-parsing helpers in server.py."""

from __future__ import annotations

import pytest

import prompt
import tts


def test_extract_action_no_action():
    text = "Just a normal sentence."
    spoken, action = prompt.extract_action(text)
    assert spoken == "Just a normal sentence."
    assert action is None


def test_extract_action_open():
    text = "Ich oeffne die Seite. [ACTION:OPEN] https://example.com"
    spoken, action = prompt.extract_action(text)
    assert spoken == "Ich oeffne die Seite."
    assert action == {"type": "OPEN", "payload": "https://example.com"}


def test_extract_action_calendar_no_payload():
    text = "[ACTION:CALENDAR]"
    spoken, action = prompt.extract_action(text)
    assert spoken == ""
    assert action == {"type": "CALENDAR", "payload": ""}


def test_extract_action_addtask_with_pipe_separator():
    text = "Aufgabe wird angelegt. [ACTION:ADDTASK] Steuererklaerung pruefen | morgen"
    spoken, action = prompt.extract_action(text)
    assert spoken == "Aufgabe wird angelegt."
    assert action == {"type": "ADDTASK", "payload": "Steuererklaerung pruefen | morgen"}


@pytest.mark.parametrize(
    "text,expected_chunks",
    [
        ("Short.", ["Short."]),
        ("a" * 250, ["a" * 250]),
        # Sentence-boundary split at . / ! / ?
        ("First sentence. Second sentence.", ["First sentence. Second sentence."]),
    ],
)
def test_split_text_under_limit(text, expected_chunks):
    assert tts._split_text(text) == expected_chunks


def test_split_text_long_text_chunks_at_sentence_boundary():
    sentence = "This is a sentence with eleven words inside it for sure."
    text = " ".join([sentence] * 6)  # ~339 chars total
    chunks = tts._split_text(text)
    assert len(chunks) >= 2
    assert all(len(c) <= 250 for c in chunks)
    # Concatenation (with the spaces re-inserted by the splitter) covers
    # every word.
    flat = " ".join(chunks).split()
    assert flat == text.split()
