#!/usr/bin/env python3
"""
Einmalige Google-Autorisierung für Jarvis.
Öffnet den Browser — einmal einloggen und bestätigen, fertig.
"""
import os, sys, subprocess

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google_calendar_tools import SCOPES, TOKEN_PATH, CREDS_PATH
from google_auth_oauthlib.flow import InstalledAppFlow

PORT = 54321

# google-auth-oauthlib refuses non-HTTPS redirect_uri values by default.
# This is intentionally lenient ONLY for the one-shot, interactive
# authorization flow on localhost (loopback adress, no network exposure)
# and ONLY for this script. NEVER set this variable on a long-running
# server or anything reachable from outside this machine.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
flow.redirect_uri = f"http://localhost:{PORT}/"

auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")

print("\n>>> Google-Kalender Autorisierung")
print(f">>> URL:\n{auth_url}\n", flush=True)

# Open browser on macOS
result = subprocess.run(["open", auth_url])
if result.returncode == 0:
    print(">>> Browser geöffnet. Bitte im Browser autorisieren...", flush=True)
else:
    print(">>> Bitte die URL oben manuell im Browser öffnen.", flush=True)

# Catch OAuth callback
from wsgiref.simple_server import make_server, WSGIRequestHandler

callback_url = None

class SilentHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass

def callback_app(environ, start_response):
    global callback_url
    host = environ.get("HTTP_HOST", f"localhost:{PORT}")
    path = environ.get("PATH_INFO", "/")
    qs = environ.get("QUERY_STRING", "")
    callback_url = f"http://{host}{path}?{qs}" if qs else f"http://{host}{path}"
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
    return [
        b"<html><body style='font-family:sans-serif;padding:40px'>"
        b"<h2>&#10003; Autorisierung erfolgreich!</h2>"
        b"<p>Jarvis hat Zugriff auf den Google-Kalender. Dieses Fenster kann geschlossen werden.</p>"
        b"</body></html>"
    ]

print(f">>> Warte auf Callback (Port {PORT})...", flush=True)
httpd = make_server("localhost", PORT, callback_app, handler_class=SilentHandler)
httpd.handle_request()

if not callback_url or "code=" not in callback_url:
    print("FEHLER: Kein Auth-Code erhalten.")
    sys.exit(1)

print(">>> Code empfangen, tausche gegen Token...", flush=True)
flow.fetch_token(authorization_response=callback_url)
creds = flow.credentials

with open(TOKEN_PATH, "w") as f:
    f.write(creds.to_json())

print(f"\n✓ Token gespeichert: {TOKEN_PATH}")
print("Jarvis kann jetzt auf den Google-Kalender zugreifen.")
