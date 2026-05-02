"""
Jarvis — macOS Notes.app Integration
Creates notes via AppleScript.
"""

import subprocess
import datetime


def add_note(title: str, body: str = "") -> str:
    timestamp = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    full_body = f"{timestamp}\n\n{body}" if body else timestamp

    # Escape for AppleScript
    safe_title = title.replace('"', '\\"').replace("\\", "\\\\")
    safe_body = full_body.replace('"', '\\"').replace("\\", "\\\\")

    script = f'''
tell application "Notes"
    make new note with properties {{name:"{safe_title}", body:"{safe_body}"}}
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return f"Notiz angelegt: {title}"
        else:
            return f"Fehler beim Anlegen der Notiz: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "Timeout beim Zugriff auf Notizen-App."
    except Exception as e:
        return f"Fehler: {e}"
