#!/usr/bin/env python3
"""Einmalig ausfuehren: setzt PICNIC_EMAIL und PICNIC_PASSWORD in der .env."""
import os, subprocess, sys

env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

print("Picnic-Zugangsdaten fuer JARVIS einrichten")
print("=" * 42)
email = input("Picnic E-Mail-Adresse: ").strip()
if not email:
    print("Abgebrochen.")
    sys.exit(1)

import getpass
password = getpass.getpass("Picnic Passwort: ")
if not password:
    print("Abgebrochen.")
    sys.exit(1)

# Bestehende Eintraege entfernen (falls Skript nochmal laeuft)
if os.path.exists(env_path):
    with open(env_path) as f:
        lines = [l for l in f if not l.startswith(("PICNIC_EMAIL=", "PICNIC_PASSWORD="))]
else:
    lines = []

lines.append(f"PICNIC_EMAIL={email}\n")
lines.append(f"PICNIC_PASSWORD={password}\n")

with open(env_path, "w") as f:
    f.writelines(lines)

print(".env aktualisiert.")
subprocess.run(["sudo", "systemctl", "restart", "jarvis"])
print("JARVIS neu gestartet.")
