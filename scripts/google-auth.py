#!/usr/bin/env python3
"""
Einmalige Google-Autorisierung fuer Jarvis.

Funktioniert auch auf dem Raspberry Pi ohne Browser / SSH-Tunnel:
- URL wird ausgegeben und muss manuell im Mac-Browser geoeffnet werden.
- Google leitet auf localhost:54321 weiter — Browser zeigt Fehler, das ist OK.
- Die vollstaendige Redirect-URL aus der Adresszeile des Browsers zurueck
  ins Terminal kopieren.
- Skript tauscht den Code gegen ein dauerhaftes Token.
"""
import os
import sys

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google_contacts_tools import SCOPES, TOKEN_PATH, CREDS_PATH
from google_auth_oauthlib.flow import InstalledAppFlow

PORT = 54321

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
flow.redirect_uri = f"http://localhost:{PORT}/"

auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

print("\n>>> Google-Autorisierung fuer Jarvis")
print("=" * 60)
print("\nSchritt 1: Diese URL im Browser oeffnen (Mac/Windows):\n")
print(auth_url)
print("\nSchritt 2: Google-Konto waehlen und Zugriff bestaetigen.")
print("\nSchritt 3: Der Browser zeigt danach einen Fehler ('Seite kann")
print("           nicht geoeffnet werden') — das ist normal und erwartet.")
print("\nSchritt 4: Die vollstaendige URL aus der Adresszeile des Browsers")
print("           kopieren (beginnt mit 'http://localhost:54321/?...')")
print("           und hier unten einfuegen:\n")

callback_url = input(">>> Callback-URL einfuegen: ").strip()

if not callback_url or "code=" not in callback_url:
    print("\nFEHLER: Keine gueltige Callback-URL. Muss 'code=' enthalten.")
    sys.exit(1)

# Normalize: Safari sometimes shows the URL without the scheme
if callback_url.startswith("localhost"):
    callback_url = "http://" + callback_url

print("\n>>> Tausche Code gegen Token...", flush=True)
try:
    flow.fetch_token(authorization_response=callback_url)
except Exception as e:
    print(f"\nFEHLER beim Token-Tausch: {e}")
    print("Moegliche Ursachen:")
    print("  - Auth-Code ist abgelaufen (> 10 Minuten alt) -> Skript neu starten")
    print("  - URL unvollstaendig kopiert -> auf vollstaendige URL achten")
    sys.exit(1)

creds = flow.credentials
with open(TOKEN_PATH, "w") as f:
    f.write(creds.to_json())

print(f"\n Token gespeichert: {TOKEN_PATH}")
print("Jarvis hat jetzt dauerhaften Zugriff auf Google-Kalender, Gmail und Contacts.")
print("(Solange die App auf 'In Produktion' steht, laeuft der Token nicht mehr ab.)")
