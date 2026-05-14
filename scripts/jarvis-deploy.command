#!/bin/bash
# Doppelklick auf dem Mac: fuehrt jarvis-restart.sh auf dem Pi aus.
# Das Skript laeuft vollstaendig auf dem Pi — die SSH-Verbindung
# muss nicht offen bleiben waehrend Jarvis neu startet.

PI_HOST="100.126.130.74"
PI_USER="caterina"
PI_REPO="/home/caterina/jarvis-voice-assistant-master"

echo "==== JARVIS Deploy ===="
echo ""

# Skript auf dem Pi ausfuehren — kein interaktives Terminal (-t),
# damit ein Verbindungsabbruch beim Neustart nicht schadet.
ssh "$PI_USER@$PI_HOST" "bash $PI_REPO/scripts/jarvis-restart.sh"

RESULT=$?
echo ""
if [ $RESULT -eq 0 ]; then
    echo "Fertig. JARVIS ist aktuell und laeuft."
else
    echo "Fehler aufgetreten — siehe Ausgabe oben."
fi

read -rp "Enter zum Schliessen..."
