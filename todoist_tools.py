"""
Jarvis — Todoist Integration
Uses Todoist API v1 (api.todoist.com/api/v1).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import httpx

BASE = "https://api.todoist.com/api/v1"
log = logging.getLogger("jarvis")


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _my_id(token: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{BASE}/user", headers=_h(token))
            r.raise_for_status()
            return str(r.json().get("id", ""))
        except Exception:
            return None


def _task_in_scope(
    task: dict,
    project_ids: set[str] | None,
    section_ids_per_project: dict[str, set[str]] | None,
) -> bool:
    """True iff a task belongs to one of the wanted projects.
    For projects listed in `section_ids_per_project`, additionally
    requires the task to live in one of the listed section ids."""
    if not project_ids:
        return True  # no filter = all projects
    pid = str(task.get("project_id", ""))
    if pid not in project_ids:
        return False
    if section_ids_per_project and pid in section_ids_per_project:
        wanted_sections = section_ids_per_project[pid]
        if str(task.get("section_id", "")) not in wanted_sections:
            return False
    return True


async def _fetch_all_tasks(token: str) -> list[dict] | str:
    """Paginierter Fetch aller Tasks ueber alle Seiten.

    Spiegelt exakt die Paginierungslogik aus get_tasks():
    - Todoist v1-API: Ergebnisse in ``results``, Cursor in ``next_cursor``
    - Max. 20 Seiten (~1000 Tasks) als Schutz vor Endlos-Schleifen
    - Erste Seite nicht erreichbar -> Fehler-String zurueck
    - Spaetere Seiten: Warnung loggen, mit bisherigen Daten weiterarbeiten

    Returns:
        list[dict]: Alle gesammelten Task-Dicts, oder
        str: Fehlermeldung falls bereits die erste Seite fehlschlaegt.
    """
    all_tasks: list[dict] = []
    async with httpx.AsyncClient(timeout=15) as c:
        cursor = None
        for _ in range(20):  # max 20 Seiten ≈ 1000 Tasks — Schutz vor Endlos
            try:
                params = {"cursor": cursor} if cursor else None
                r = await c.get(f"{BASE}/tasks", headers=_h(token), params=params)
                r.raise_for_status()
                payload = r.json()
                all_tasks.extend(payload.get("results", []))
                cursor = payload.get("next_cursor")
                if not cursor:
                    break
            except Exception as e:
                # Erste Seite nicht erreichbar -> Fehler. Spaetere Seiten:
                # mit dem zurueck was wir haben weiter.
                if not all_tasks:
                    return f"Todoist nicht erreichbar: {e}"
                log.warning(f"todoist pagination broke at cursor={cursor!r}: {e}")
                break
    return all_tasks


async def get_tasks(
    token: str,
    max_tasks: int = 10,
    project_ids: list[str] | None = None,
    section_ids_per_project: dict[str, list[str]] | None = None,
) -> str:
    """Fetch open tasks assigned to me, optionally filtered to a set of
    Todoist project IDs (and per-project section IDs)."""
    my_id = await _my_id(token)

    # Pagination follow — die Todoist v1-API liefert nur ~50 Tasks pro
    # Seite + ein next_cursor. Wenn Catrin viele offene Tasks hat,
    # wuerde alles ab Page 2 unsichtbar bleiben.
    result = await _fetch_all_tasks(token)
    if isinstance(result, str):
        return result  # Fehlermeldung direkt weiterreichen
    all_tasks = result

    pid_set = set(project_ids) if project_ids else None
    sec_sets = (
        {pid: set(secs) for pid, secs in section_ids_per_project.items()}
        if section_ids_per_project else None
    )

    tasks = [
        t for t in all_tasks
        if not t.get("checked")
        and not t.get("is_deleted")
        and (not my_id or str(t.get("user_id", "")) == my_id)
        and _task_in_scope(t, pid_set, sec_sets)
    ]

    if not tasks:
        return "KEINE_TASKS"

    today = date.today().isoformat()

    def sort_key(t):
        return (t.get("due") or {}).get("date", "9999")

    tasks.sort(key=sort_key)

    lines = []
    for t in tasks[:max_tasks]:
        due_date = (t.get("due") or {}).get("date", "")
        if due_date and due_date < today:
            flag = " ⚠ überfällig"
        elif due_date == today:
            flag = " (heute)"
        elif due_date:
            flag = f" (fällig: {due_date})"
        else:
            flag = ""
        lines.append(f"• {t.get('content', '(ohne Titel)')}{flag}")

    total = len(tasks)
    return f"Todoist — {total} offene Aufgaben:\n" + "\n".join(lines)


async def add_task(
    token: str,
    content: str,
    due: str = "",
    project_id: str | None = None,
    section_id: str | None = None,
) -> str:
    """Add a new task. Optionally pin it to a project / section."""
    payload: dict = {"content": content}
    if due:
        payload["due_string"] = due
        payload["due_lang"] = "de"
    if project_id:
        payload["project_id"] = project_id
    if section_id:
        payload["section_id"] = section_id
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(f"{BASE}/tasks", headers=_h(token), json=payload)
            r.raise_for_status()
            task = r.json()
            return f"Aufgabe angelegt: {task['content']}" + (f" — fällig {due}" if due else "")
        except Exception as e:
            return f"Fehler beim Anlegen: {e}"


async def complete_task(
    token: str,
    task_name: str,
    project_ids: list[str] | None = None,
    section_ids_per_project: dict[str, list[str]] | None = None,
) -> str:
    """Find one of MY open tasks (in scoped projects/sections) by
    substring match and mark complete.

    Filters that the previous version was missing — and that caused a
    real risk on shared HILO/DIHAG projects:
    - user_id == my_id  (don't close colleagues' tasks)
    - project_id in scoped set  (don't close tasks in projects Catrin
      doesn't normally see)
    - section filter inside the HILO project
    On multiple matches: returns the list back to the caller and asks
    Catrin to disambiguate, instead of silently picking one.

    Previously this function fetched only the first page of tasks from
    the Todoist API, making tasks on page 2+ invisible to complete_task.
    Now uses _fetch_all_tasks() for full pagination parity with get_tasks().
    """
    my_id = await _my_id(token)

    result = await _fetch_all_tasks(token)
    if isinstance(result, str):
        return result  # Fehlermeldung direkt weiterreichen
    all_tasks = result

    pid_set = set(project_ids) if project_ids else None
    sec_sets = (
        {pid: set(secs) for pid, secs in section_ids_per_project.items()}
        if section_ids_per_project else None
    )

    needle = task_name.lower()
    candidates = [
        t for t in all_tasks
        if not t.get("checked")
        and not t.get("is_deleted")
        and (not my_id or str(t.get("user_id", "")) == my_id)
        and _task_in_scope(t, pid_set, sec_sets)
        and needle in t.get("content", "").lower()
    ]

    if not candidates:
        return f"Keine offene Aufgabe gefunden die '{task_name}' enthält."

    if len(candidates) > 1:
        names = "\n".join(f"• {t.get('content', '(ohne Titel)')}" for t in candidates[:5])
        return (f"Mehrere passende Aufgaben — welche meinst du?\n{names}\n"
                f"Sag bitte praeziser welche.")

    match = candidates[0]
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(f"{BASE}/tasks/{match['id']}/close", headers=_h(token))
            r.raise_for_status()
            return f"Erledigt: {match.get('content', match['id'])}"
        except Exception as e:
            return f"Fehler beim Abschließen: {e}"
