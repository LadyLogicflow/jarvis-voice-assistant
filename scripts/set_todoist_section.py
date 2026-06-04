#!/usr/bin/env python3
"""Einmalig ausfuehren: setzt todoist_default_section in config.json."""
import json, os, subprocess, sys

path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
with open(path) as f:
    c = json.load(f)

c["todoist_default_section"] = "fr-essberger-6fRcMmh69hJHCRfV"

with open(path, "w") as f:
    json.dump(c, f, indent=2, ensure_ascii=False)
    f.write("\n")

print("config.json aktualisiert.")
subprocess.run(["sudo", "systemctl", "restart", "jarvis"])
print("JARVIS neu gestartet.")
