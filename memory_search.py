"""
Semantisches Gedächtnis via ChromaDB + sentence-transformers (Issue #57).

Indexiert Conversations, Notizen, Personen und Todoist-Tasks und ermöglicht
semantische Volltextsuche — also nicht nur Substring-Matching, sondern
bedeutungsbasierte Ähnlichkeitssuche.

Persistenz: ~/.jarvis_chroma/ (außerhalb des Repos, nie committet).

Graceful Degradation: Wenn ChromaDB oder sentence-transformers nicht
installiert sind, laufen alle Funktionen durch und liefern leere Ergebnisse
zurück — der Server startet trotzdem fehlerfrei.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Optional imports — graceful degradation wenn nicht installiert
# ---------------------------------------------------------------------------
try:
    import chromadb  # noqa: F401
    _CHROMADB_AVAILABLE = True
except ImportError:
    log.warning(
        "memory_search: ChromaDB nicht installiert. "
        "Semantisches Gedächtnis deaktiviert. "
        "Installation: pip install chromadb sentence-transformers"
    )
    _CHROMADB_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer  # noqa: F401
    _ST_AVAILABLE = True
except ImportError:
    if _CHROMADB_AVAILABLE:
        log.warning(
            "memory_search: sentence-transformers nicht installiert. "
            "Semantisches Gedächtnis deaktiviert. "
            "Installation: pip install sentence-transformers"
        )
    _ST_AVAILABLE = False

_AVAILABLE = _CHROMADB_AVAILABLE and _ST_AVAILABLE

# ---------------------------------------------------------------------------
# Lazy-initialisierte Singletons
# ---------------------------------------------------------------------------
_chroma_client: Any | None = None
_collection: Any | None = None
_embedding_model: Any | None = None
_embedding_model_lock = threading.Lock()

_CHROMA_PATH = str(Path.home() / ".jarvis_chroma")
_COLLECTION_NAME = "jarvis_memory"
_EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"


def _get_embedding_model() -> Any | None:
    """Lädt das Embedding-Modell einmalig (lazy, thread-safe via Lock).

    Verwendet double-checked locking: der schnelle Pfad ohne Lock verhindert
    den Lock-Overhead bei bereits initialisiertem Modell. Der zweite Check
    im Lock-Block verhindert doppelte Initialisierung bei gleichzeitigen
    Aufrufen (z. B. Startup-Reindex + RECALL-Action).
    """
    global _embedding_model
    if not _ST_AVAILABLE:
        return None
    if _embedding_model is not None:
        return _embedding_model
    with _embedding_model_lock:
        if _embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                log.info("memory_search: Lade Embedding-Modell %s …", _EMBEDDING_MODEL_NAME)
                _embedding_model = SentenceTransformer(_EMBEDDING_MODEL_NAME)
                log.info("memory_search: Embedding-Modell geladen.")
            except Exception as exc:
                log.warning("memory_search: Embedding-Modell konnte nicht geladen werden: %s", exc)
                return None
    return _embedding_model


def _get_collection() -> Any | None:
    """ChromaDB-Collection lazy initialisieren und zurückgeben.

    Persistiert in ~/.jarvis_chroma/. Gibt None zurück wenn ChromaDB oder
    sentence-transformers nicht verfügbar sind — alle Aufrufer prüfen das.
    """
    global _chroma_client, _collection
    if not _AVAILABLE:
        return None
    if _collection is not None:
        return _collection
    try:
        import chromadb
        os.makedirs(_CHROMA_PATH, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=_CHROMA_PATH)
        _collection = _chroma_client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "memory_search: ChromaDB-Collection '%s' bereit (%d Einträge).",
            _COLLECTION_NAME,
            _collection.count(),
        )
    except Exception as exc:
        log.warning("memory_search: ChromaDB-Init fehlgeschlagen: %s", exc)
        _collection = None
    return _collection


def _embed(text: str) -> list[float] | None:
    """Text in einen Embedding-Vektor umwandeln. Gibt None bei Fehler."""
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        return model.encode(text, normalize_embeddings=True).tolist()
    except Exception as exc:
        log.warning("memory_search: Embedding fehlgeschlagen: %s", exc)
        return None


def _make_doc_id(source: str, content: str) -> str:
    """Stabile doc_id aus Quelle + Inhalt-Hash (8 Zeichen, hex)."""
    h = hashlib.sha256(f"{source}:{content}".encode("utf-8")).hexdigest()[:8]
    return f"{source}_{h}"


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def index_text(
    text: str,
    source: str,
    doc_id: str,
    metadata: dict | None = None,
) -> None:
    """Ein Dokument in den Vektorspeicher schreiben (upsert).

    Args:
        text:     Der zu indexierende Text.
        source:   Herkunft — z. B. "conversation", "note", "person", "task".
        doc_id:   Eindeutiger Bezeichner. Wird für Upsert genutzt, sodass
                  Reindex keine Duplikate erzeugt.
        metadata: Optionale Zusatzfelder (werden in ChromaDB gespeichert).
    """
    collection = _get_collection()
    if collection is None:
        return
    if not text or not text.strip():
        return
    embedding = _embed(text)
    if embedding is None:
        return
    meta = dict(metadata or {})
    meta["source"] = source
    # ChromaDB erlaubt nur str/int/float/bool in metadata
    meta = {k: str(v) for k, v in meta.items()}
    try:
        collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[meta],
        )
    except Exception as exc:
        log.warning("memory_search.index_text upsert fehlgeschlagen: %s", exc)


def search(query: str, n_results: int = 5) -> list[dict]:
    """Semantische Ähnlichkeitssuche.

    Args:
        query:     Suchanfrage in natürlicher Sprache.
        n_results: Maximale Anzahl Treffer.

    Returns:
        Liste von Dicts mit Schlüsseln ``text``, ``source``, ``score``.
        Leer wenn ChromaDB nicht verfügbar oder kein Ergebnis.
    """
    collection = _get_collection()
    if collection is None:
        return []
    if not query or not query.strip():
        return []
    embedding = _embed(query)
    if embedding is None:
        return []
    try:
        count = collection.count()
        if count == 0:
            return []
        actual_n = min(n_results, count)
        result = collection.query(
            query_embeddings=[embedding],
            n_results=actual_n,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        log.warning("memory_search.search fehlgeschlagen: %s", exc)
        return []

    hits: list[dict] = []
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    for doc, meta, dist in zip(docs, metas, distances):
        # ChromaDB cosine-distance: 0 = identisch, 2 = komplett verschieden.
        # Wir rechnen in Score 0..1 um (1 = perfekt).
        score = max(0.0, 1.0 - dist / 2.0)
        hits.append({
            "text": doc,
            "source": meta.get("source", "unbekannt"),
            "score": round(score, 3),
        })
    return hits


# ---------------------------------------------------------------------------
# Index-Funktionen für die einzelnen Datenquellen
# ---------------------------------------------------------------------------

def index_conversation_history() -> None:
    """Liest .jarvis_history.json und indexiert alle User+Jarvis-Turns.

    Jeder Turn wird als eigenständiges Dokument gespeichert, sodass
    semantische Suche einzelne Aussagen findet — nicht nur ganze Sessions.
    """
    import settings as S
    history_path = S.HISTORY_PATH
    if not os.path.exists(history_path):
        return
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("memory_search.index_conversation_history: Lesefehler: %s", exc)
        return

    # Datei kann dict (multi-session) oder list (legacy) sein
    all_turns: list[dict] = []
    if isinstance(data, dict):
        for msgs in data.values():
            if isinstance(msgs, list):
                all_turns.extend(msgs)
    elif isinstance(data, list):
        all_turns = data

    indexed = 0
    for msg in all_turns:
        content = (msg.get("content") or "").strip()
        role = msg.get("role", "unknown")
        if not content:
            continue
        doc_id = _make_doc_id(f"conversation_{role}", content)
        index_text(
            text=content,
            source="conversation",
            doc_id=doc_id,
            metadata={"role": role},
        )
        indexed += 1

    log.info("memory_search: %d Conversation-Turns indexiert.", indexed)


def index_notes() -> None:
    """Liest notes_db und indexiert alle Notizen."""
    try:
        import notes_db
        notes = notes_db.all_notes()
    except Exception as exc:
        log.warning("memory_search.index_notes: notes_db Fehler: %s", exc)
        return

    indexed = 0
    for note in notes:
        text = note.text.strip()
        if not text:
            continue
        doc_id = _make_doc_id("note", note.id)
        index_text(
            text=text,
            source="note",
            doc_id=doc_id,
            metadata={"kind": note.kind, "note_id": note.id},
        )
        indexed += 1

    log.info("memory_search: %d Notizen indexiert.", indexed)


def index_persons() -> None:
    """Liest persons_db und indexiert Personen-Profile (Notizen + offene Punkte)."""
    try:
        import persons_db
        profiles = persons_db.all_profiles()
    except Exception as exc:
        log.warning("memory_search.index_persons: persons_db Fehler: %s", exc)
        return

    indexed = 0
    for prof in profiles:
        # Jede Notiz einer Person als eigenes Dokument
        for note_text in prof.notes:
            if not note_text.strip():
                continue
            full_text = f"Notiz zu {prof.name}: {note_text.strip()}"
            doc_id = _make_doc_id("person_note", f"{prof.contact_id}:{note_text}")
            index_text(
                text=full_text,
                source="person",
                doc_id=doc_id,
                metadata={"person_name": prof.name, "person_id": prof.contact_id},
            )
            indexed += 1

        # Offene Punkte als eigene Dokumente
        for pt in prof.open_points:
            if not pt.strip():
                continue
            full_text = f"Offener Punkt mit {prof.name}: {pt.strip()}"
            doc_id = _make_doc_id("person_open", f"{prof.contact_id}:{pt}")
            index_text(
                text=full_text,
                source="person",
                doc_id=doc_id,
                metadata={"person_name": prof.name, "person_id": prof.contact_id},
            )
            indexed += 1

        # Profil-Kurzfassung (Funktion + Anrede) als Dokument
        if prof.name and (prof.funktion or prof.anrede):
            parts = [f"Person: {prof.name}"]
            if prof.funktion:
                parts.append(f"Funktion: {prof.funktion}")
            if prof.anrede:
                parts.append(f"Anrede: {prof.anrede}")
            full_text = ". ".join(parts) + "."
            doc_id = _make_doc_id("person_profile", prof.contact_id)
            index_text(
                text=full_text,
                source="person",
                doc_id=doc_id,
                metadata={"person_name": prof.name, "person_id": prof.contact_id},
            )
            indexed += 1

    log.info("memory_search: %d Personen-Einträge indexiert.", indexed)


async def index_todoist_tasks() -> None:
    """Ruft todoist_tools.get_tasks() auf und indexiert alle Tasks."""
    try:
        import settings as S
        import todoist_tools
        if not S.TODOIST_TOKEN or S.TODOIST_TOKEN == "YOUR_TODOIST_API_TOKEN":
            return
        tasks_text = await todoist_tools.get_tasks(
            S.TODOIST_TOKEN,
            max_tasks=100,
            project_ids=S.TODOIST_PROJECT_IDS or None,
            section_ids_per_project=S.TODOIST_SECTIONS_PER_PROJECT or None,
        )
        if not tasks_text or tasks_text == "KEINE_TASKS":
            return
    except Exception as exc:
        log.warning("memory_search.index_todoist_tasks: Fehler: %s", exc)
        return

    indexed = 0
    for line in tasks_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Zeilen sehen aus wie "• Aufgabe (heute)" oder "• Aufgabe ⚠ überfällig"
        text = line.lstrip("•").strip()
        if not text:
            continue
        doc_id = _make_doc_id("task", text)
        index_text(
            text=f"Todoist-Aufgabe: {text}",
            source="task",
            doc_id=doc_id,
        )
        indexed += 1

    log.info("memory_search: %d Todoist-Tasks indexiert.", indexed)


async def reindex_all() -> None:
    """Vollständiger Reindex aller Datenquellen.

    Aufgerufen beim Serverstart und als nächtlicher Scheduler-Job.
    Fehler in Einzelquellen werden geloggt, brechen den Rest nicht ab.
    """
    if not _AVAILABLE:
        log.info("memory_search.reindex_all: ChromaDB/sentence-transformers nicht verfügbar — übersprungen.")
        return

    log.info("memory_search: Starte Reindex aller Quellen …")
    try:
        index_conversation_history()
    except Exception as exc:
        log.warning("memory_search.reindex_all: conversation fehlgeschlagen: %s", exc)
    try:
        index_notes()
    except Exception as exc:
        log.warning("memory_search.reindex_all: notes fehlgeschlagen: %s", exc)
    try:
        index_persons()
    except Exception as exc:
        log.warning("memory_search.reindex_all: persons fehlgeschlagen: %s", exc)
    try:
        await index_todoist_tasks()
    except Exception as exc:
        log.warning("memory_search.reindex_all: todoist fehlgeschlagen: %s", exc)

    col = _get_collection()
    count = col.count() if col is not None else 0
    log.info("memory_search: Reindex abgeschlossen. %d Einträge im Vektorspeicher.", count)
