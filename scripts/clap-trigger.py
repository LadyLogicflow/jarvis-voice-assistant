#!/usr/bin/env python3
"""
Jarvis — Double Clap Trigger
Listens to mic. Detects two claps within 1.2s, min 0.1s apart.
On trigger: runs scripts/launch-session.sh (macOS) or .ps1 (Windows) then exits.
"""

import sounddevice as sd
import numpy as np
import subprocess
import sys
import time
import os
import json

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

WORKSPACE_PATH = config["workspace_path"]
IS_MAC = sys.platform == "darwin"
SCRIPT_NAME = "launch-session.sh" if IS_MAC else "launch-session.ps1"
SCRIPT_PATH = os.path.join(WORKSPACE_PATH, "scripts", SCRIPT_NAME)

# Tunable from config.json (clap_* keys); defaults match the original behavior.
SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
THRESHOLD = float(config.get("clap_threshold", 0.15))   # RMS volume spike; lower = more sensitive
MIN_GAP   = float(config.get("clap_min_gap", 0.1))       # Minimum seconds between claps
MAX_GAP   = float(config.get("clap_max_gap", 1.2))       # Maximum seconds between claps
COOLDOWN  = float(config.get("clap_cooldown", 20.0))     # Seconds to ignore after trigger fires

last_clap_time = 0.0
cooldown_until = 0.0

def audio_callback(indata, frames, time_info, status):
    global last_clap_time, cooldown_until

    now = time.time()

    # Ignore during cooldown
    if now < cooldown_until:
        return

    rms = float(np.sqrt(np.mean(indata ** 2)))

    if rms > THRESHOLD:
        gap = now - last_clap_time

        if gap >= MIN_GAP:
            if gap <= MAX_GAP and last_clap_time > 0:
                # Second clap — fire trigger, then resume after cooldown
                print(f"[jarvis] Doppelklatschen erkannt! Starte Session.", flush=True)
                last_clap_time = 0.0
                cooldown_until = now + COOLDOWN
                if IS_MAC:
                    subprocess.Popen(["/bin/bash", SCRIPT_PATH])
                else:
                    subprocess.Popen(["powershell", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PATH])
            else:
                # First clap
                print(f"[jarvis] Erstes Klatschen (rms={rms:.3f})", flush=True)
                last_clap_time = now

with sd.InputStream(
    samplerate=SAMPLE_RATE,
    blocksize=BLOCK_SIZE,
    channels=1,
    dtype="float32",
    callback=audio_callback,
):
    print("[jarvis] Hoert auf Doppelklatschen... (laeuft dauerhaft)", flush=True)
    while True:
        time.sleep(0.1)
