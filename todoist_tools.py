"""
Jarvis — Todoist Integration
Uses Todoist API v1 (api.todoist.com/api/v1).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import httpx

BASE = "https://api.todoist.com/api/v1"


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


async def get_tasks(
    token: str,
    max_tasks: int = 10,
    project_ids: list[str] | None = None,
    section_ids_per_project: dict[str, list[str]] | None = None,
) -> str:
    """Fetch open tasks assigned to me, optionally filtered to a set of
    Todoist project IDs (and per-project section IDs)."""
    my_id = await _my_id(token)

    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(f"{BASE}/tasks", headers=_h(token))
            r.raise_for_status()
            all_tasks = r.json().get("results", [])
        except Exception as e:
            return f"Todoist nicht erreichbar: {e}"

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
        lines.append(f"• {t['content']}{flag}")

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


async def complete_task(token: str, task_name: str) -> str:
    """Find task by name and mark complete."""
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{BASE}/tasks", headers=_h(token))
            r.raise_for_status()
            all_tasks = r.json().get("results", [])
        except Exception as e:
            return f"Todoist nicht erreichbar: {e}"

    needle = task_name.lower()
    match = next(
        (t for t in all_tasks if not t.get("checked") and needle in t["content"].lower()),
        None,
    )
    if not match:
        return f"Keine offene Aufgabe gefunden die '{task_name}' enthält."

    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(f"{BASE}/tasks/{match['id']}/close", headers=_h(token))
            r.raise_for_status()
            return f"Erledigt: {match['content']}"
        except Exception as e:
            return f"Fehler beim Abschließen: {e}"
