"""
Jarvis — macOS Notes.app Integration
Creates notes via AppleScript.
"""

import subprocess
import datetime


# AppleScript template that takes title + body as runtime arguments via
# `on run argv`. Passing values this way avoids any string interpolation
# into the script source, so quotes, backslashes, backticks, newlines,
# semicolons or any other shell/AppleScript metacharacter in user input
# cannot break out of the string context.
_NOTES_SCRIPT = '''on run argv
    tell application "Notes"
        make new note with properties {name:(item 1 of argv), body:(item 2 of argv)}
    end tell
    return "OK"
end run
'''


def add_note(title: str, body: str = "") -> str:
    timestamp = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    full_body = f"{timestamp}\n\n{body}" if body else timestamp

    try:
        result = subprocess.run(
            ["osascript", "-e", _NOTES_SCRIPT, title, full_body],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return f"Notiz angelegt: {title}"
        return f"Fehler beim Anlegen der Notiz: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "Timeout beim Zugriff auf Notizen-App."
    except Exception as e:
        return f"Fehler: {e}"
