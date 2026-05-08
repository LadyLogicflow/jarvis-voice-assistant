#!/usr/bin/env python3
"""
Findet inhaltlich ähnliche (potenzielle Duplikate) in notes_db und persons_db.
Ausführen: python3 scripts/check_duplicates.py
Optionaler Flag: --fix   → löscht erkannte Duplikate (behält jeweils den ersten Eintrag)
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_FILE = os.path.join(BASE, ".jarvis_notes.json")
PERSONS_FILE = os.path.join(BASE, ".jarvis_persons.json")
FIX = "--fix" in sys.argv


def _tokens(text: str) -> set[str]:
    return set(re.sub(r'[^\w]', ' ', text.lower()).split())


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def find_dups(texts: list[str], threshold: float = 0.75) -> list[tuple[int, int, float]]:
    """Returns (i, j, score) pairs where texts[i] and texts[j] are near-duplicates."""
    dups = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            score = _overlap(texts[i], texts[j])
            if score >= threshold:
                dups.append((i, j, score))
    return dups


# ── notes_db ─────────────────────────────────────────────────────────────────
print("=" * 60)
print("notes_db (.jarvis_notes.json)")
print("=" * 60)

if not os.path.exists(NOTES_FILE):
    print("  Datei nicht gefunden — noch keine Notizen gespeichert.\n")
else:
    with open(NOTES_FILE, encoding="utf-8") as f:
        notes = json.load(f)
    texts = [n["text"] for n in notes]
    dups = find_dups(texts)
    if not dups:
        print("  Keine Duplikate gefunden.\n")
    else:
        to_delete = set()
        for i, j, score in dups:
            print(f"  [{score:.0%} Überlappung]")
            print(f"    [{i}] {texts[i][:100]}")
            print(f"    [{j}] {texts[j][:100]}")
            to_delete.add(j)  # keep first, remove second
        print(f"\n  → {len(to_delete)} Duplikate gefunden.")
        if FIX:
            cleaned = [n for idx, n in enumerate(notes) if idx not in to_delete]
            with open(NOTES_FILE, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            print(f"  ✓ {len(to_delete)} Einträge gelöscht, {len(cleaned)} verbleiben.")
        else:
            print("  Zum Bereinigen: python3 scripts/check_duplicates.py --fix")

# ── persons_db ────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("persons_db (.jarvis_persons.json)")
print("=" * 60)

if not os.path.exists(PERSONS_FILE):
    print("  Datei nicht gefunden — noch keine Personenprofile gespeichert.\n")
else:
    with open(PERSONS_FILE, encoding="utf-8") as f:
        persons = json.load(f)
    total_dups = 0
    persons_changed = {}
    for cid, profile in persons.items():
        name = profile.get("name", cid)
        notes_list = profile.get("notes", [])
        if len(notes_list) < 2:
            continue
        dups = find_dups(notes_list)
        if not dups:
            continue
        to_delete = set()
        print(f"  Person: {name}")
        for i, j, score in dups:
            print(f"    [{score:.0%}] '{notes_list[i][:80]}'")
            print(f"         vs '{notes_list[j][:80]}'")
            to_delete.add(j)
        total_dups += len(to_delete)
        if FIX:
            persons_changed[cid] = [n for idx, n in enumerate(notes_list) if idx not in to_delete]
        print()

    if total_dups == 0:
        print("  Keine Duplikate gefunden.")
    else:
        print(f"  → {total_dups} Duplikate gefunden.")
        if FIX:
            for cid, cleaned_notes in persons_changed.items():
                persons[cid]["notes"] = cleaned_notes
            with open(PERSONS_FILE, "w", encoding="utf-8") as f:
                json.dump(persons, f, ensure_ascii=False, indent=2)
            print(f"  ✓ Bereinigt.")
        else:
            print("  Zum Bereinigen: python3 scripts/check_duplicates.py --fix")
