#!/bin/bash
# Doppelklick auf dem Mac: beendet JARVIS auf dem Pi, zieht Updates, startet neu.

PI_HOST="100.126.130.74"
PI_USER="caterina"
PI_REPO="/home/caterina/jarvis-voice-assistant-master"

echo "==== JARVIS Deploy ===="
echo ""

ssh -t "$PI_USER@$PI_HOST" "
  cd $PI_REPO

  echo '■ Stoppe JARVIS...'
  pkill -f 'python.*server.py' || true
  sleep 2

  echo '■ Hole neueste Aenderungen...'
  git pull origin main

  echo '■ Starte JARVIS neu...'
  nohup .venv/bin/python -u server.py >> jarvis.log 2>&1 &
  sleep 3

  if pgrep -f 'python.*server.py' > /dev/null; then
    echo ''
    echo '==== JARVIS laeuft. ===='
  else
    echo ''
    echo '==== FEHLER: Start fehlgeschlagen ===='
    tail -20 jarvis.log
  fi
"

echo ""
read -rp "Enter zum Schliessen..."
