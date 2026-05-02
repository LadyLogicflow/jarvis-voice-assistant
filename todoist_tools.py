"""
Jarvis — Todoist Integration
Uses Todoist API v1 (api.todoist.com/api/v1).
"""

import httpx
from typing import Optional
from datetime import date

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


async def get_tasks(token: str, max_tasks: int = 10) -> str:
    """Fetch open tasks assigned to me."""
    my_id = await _my_id(token)

    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(f"{BASE}/tasks", headers=_h(token))
            r.raise_for_status()
            all_tasks = r.json().get("results", [])
        except Exception as e:
            return f"Todoist nicht erreichbar: {e}"

    # Only tasks assigned to me, open, not deleted.
    tasks = [
        t for t in all_tasks
        if not t.get("checked")
        and not t.get("is_deleted")
        and (not my_id or str(t.get("user_id", "")) == my_id)
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


async def add_task(token: str, content: str, due: str = "") -> str:
    """Add a new task."""
    payload: dict = {"content": content}
    if due:
        payload["due_string"] = due
        payload["due_lang"] = "de"
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
