#!/usr/bin/env python3
"""
Einmalige Google-Autorisierung fuer Jarvis.

Funktioniert auf dem Raspberry Pi ohne Browser / SSH-Tunnel:
- URL wird ausgegeben und muss manuell im Mac-Browser geoeffnet werden.
- Google leitet auf localhost:54321 weiter — Browser zeigt Fehler, das ist OK.
- Die vollstaendige Redirect-URL aus der Adresszeile des Browsers zurueck
  ins Terminal kopieren.
"""
import os
import sys

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google_contacts_tools import SCOPES, TOKEN_PATH, CREDS_PATH

# Flow statt InstalledAppFlow — kein PKCE, einfacherer Code-Exchange
from google_auth_oauthlib.flow import Flow

PORT = 54321

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

flow = Flow.from_client_secrets_file(
    CREDS_PATH,
    SCOPES,
    redirect_uri=f"http://localhost:{PORT}/",
)

auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

print("\n>>> Google-Autorisierung fuer Jarvis")
print("=" * 60)
print("\nSchritt 1: Diese URL im Browser oeffnen (Mac):\n")
print(auth_url)
print("\nSchritt 2: Google-Konto auswaehlen und Zugriff bestaetigen.")
print("           Falls 'App nicht verifiziert' erscheint:")
print("           'Erweitert' klicken -> 'Weiter zu Jarvis (unsicher)'")
print("\nSchritt 3: Browser zeigt Fehler ('Seite nicht gefunden') — normal.")
print("\nSchritt 4: Die URL aus der Adresszeile kopieren")
print("           (beginnt mit 'http://localhost:54321/?...')")
print("           und hier einfügen:\n")

callback_url = input(">>> Callback-URL einfügen: ").strip()

if not callback_url or "code=" not in callback_url:
    print("\nFEHLER: Keine gueltige Callback-URL. Muss 'code=' enthalten.")
    sys.exit(1)

if callback_url.startswith("localhost"):
    callback_url = "http://" + callback_url

print("\n>>> Tausche Code gegen Token...", flush=True)
try:
    flow.fetch_token(authorization_response=callback_url)
except Exception as e:
    print(f"\nFEHLER beim Token-Tausch: {e}")
    print("-> Auth-Code abgelaufen? Skript neu starten und URL sofort oeffnen.")
    sys.exit(1)

creds = flow.credentials
with open(TOKEN_PATH, "w") as f:
    f.write(creds.to_json())

print(f"\n Token gespeichert: {TOKEN_PATH}")
print("Jarvis hat dauerhaften Zugriff auf Google-Kalender.")
