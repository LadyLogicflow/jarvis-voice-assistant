#!/usr/bin/env python3
"""
Jarvis — Wake Word Trigger
Lauscht dauerhaft auf "Jarvis" und startet die Session.
Braucht einen kostenlosen Picovoice Access Key: https://console.picovoice.ai/
"""

import pvporcupine
import sounddevice as sd
import numpy as np
import subprocess
import sys
import time
import os
import json

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")

# Load .env from project root if present (Picovoice key is a secret).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass  # python-dotenv optional; key may also come from the shell env

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

ACCESS_KEY = os.environ.get("PICOVOICE_ACCESS_KEY", "").strip()
if not ACCESS_KEY:
    print("FEHLER: PICOVOICE_ACCESS_KEY fehlt im Environment.")
    print("Setze ihn in .env (siehe .env.example) oder in deiner Shell.")
    print("Kostenloser Key: https://console.picovoice.ai/")
    sys.exit(1)

WORKSPACE_PATH = config["workspace_path"]
SCRIPT_PATH = os.path.join(WORKSPACE_PATH, "scripts", "launch-session.sh")
COOLDOWN = 5.0

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keywords=["jarvis"],
)

cooldown_until = 0.0

def audio_callback(indata, frames, status):
    global cooldown_until

    now = time.time()
    if now < cooldown_until:
        return

    # Porcupine expects 16-bit PCM mono
    pcm = (indata[:, 0] * 32767).astype(np.int16)

    # Process in chunks of porcupine.frame_length
    for i in range(0, len(pcm) - porcupine.frame_length + 1, porcupine.frame_length):
        result = porcupine.process(pcm[i:i + porcupine.frame_length])
        if result >= 0:
            print("[jarvis] Wake Word 'Jarvis' erkannt! Starte Session.", flush=True)
            cooldown_until = now + COOLDOWN
            subprocess.Popen(["/bin/bash", SCRIPT_PATH])
            break

with sd.InputStream(
    samplerate=porcupine.sample_rate,
    blocksize=porcupine.frame_length,
    channels=1,
    dtype="float32",
    callback=audio_callback,
):
    print("[jarvis] Hoert auf 'Jarvis'... (laeuft dauerhaft)", flush=True)
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        porcupine.delete()
