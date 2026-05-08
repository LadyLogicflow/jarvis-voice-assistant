"""
Jarvis — Mail Tools (macOS)
Reads unread emails from Mail.app via AppleScript.
Efficient approach: unread count (fast metadata) + scan only last 30 messages
instead of filtering all messages (would be minutes with large inboxes).
"""

import subprocess


def get_unread_mails(max_count: int = 5) -> str:
    """Fetch unread emails from Mail.app via AppleScript."""
    # Defensive: if a future caller passes a string (LLM payload, env
    # var, etc.) we refuse to interpolate that into the AppleScript
    # source. Cast to int — raises ValueError if not coercible, which
    # is what we want.
    max_count = max(1, min(int(max_count), 100))
    script = f"""
tell application "Mail"
    set theInbox to inbox

    -- Unread count is a fast metadata property
    set totalUnread to unread count of theInbox
    if totalUnread = 0 then return "KEINE_MAILS"

    -- Only scan the last 30 messages instead of all messages
    set msgCount to count of messages of theInbox
    set scanFrom to msgCount - 29
    if scanFrom < 1 then set scanFrom to 1

    set recentMsgs to messages scanFrom through msgCount of theInbox
    set found to 0
    set limit to {max_count}
    set mailOutput to "Ungelesen insgesamt: " & totalUnread & "\\n\\n"

    repeat with msg in recentMsgs
        if read status of msg is false then
            set msgSender to sender of msg
            set msgSubject to subject of msg
            set msgDate to date received of msg as string
            set mailOutput to mailOutput & "---\\nVon: " & msgSender & "\\nBetreff: " & msgSubject & "\\nEmpfangen: " & msgDate & "\\n"
            set found to found + 1
            if found >= limit then exit repeat
        end if
    end repeat

    if found = 0 then
        return "Ungelesen insgesamt: " & totalUnread & " (keine davon in den letzten 30 Nachrichten)"
    end if
    return mailOutput
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return f"Fehler beim Zugriff auf Mail.app: {result.stderr.strip()}"
        output = result.stdout.strip()
        if output == "KEINE_MAILS":
            return "KEINE_MAILS"
        return output
    except subprocess.TimeoutExpired:
        return "Mail.app hat nicht rechtzeitig geantwortet."
    except Exception as e:
        return f"Fehler: {e}"


def create_draft(to: str, subject: str, body: str) -> str:
    """Create a draft in Mail.app via AppleScript. Does not send.
    Returns a status string."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
tell application "Mail"
    set newMsg to make new outgoing message with properties {{\\
        subject:"{esc(subject)}",\\
        content:"{esc(body)}"\\
    }}
    tell newMsg
        if "{esc(to)}" is not "" then
            make new to recipient at end of to recipients with properties {{address:"{esc(to)}"}}
        end if
        set visible to false
    end tell
end tell
return "ok"
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return f"Fehler beim Erstellen des Entwurfs: {result.stderr.strip()}"
        return "Entwurf erstellt."
    except subprocess.TimeoutExpired:
        return "Mail.app hat nicht rechtzeitig geantwortet."
    except Exception as e:
        return f"Fehler: {e}"
