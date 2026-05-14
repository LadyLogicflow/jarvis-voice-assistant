#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# jarvis-deploy.command
# Doppelklick auf dem Mac: verbindet mit dem Pi, beendet JARVIS, zieht die
# neuesten Änderungen aus Git und startet JARVIS neu.
#
# EINMALIG ANPASSEN:
#   PI_HOST  → IP-Adresse oder Hostname des Pi (z.B. 192.168.1.42 oder jarvis.local)
#   PI_USER  → SSH-Benutzername auf dem Pi (z.B. catrin oder pi)
#   PI_REPO  → Pfad zum JARVIS-Ordner auf dem Pi
# ─────────────────────────────────────────────────────────────────────────────

PI_HOST="100.126.130.74"   # Tailscale-IP des Pi
PI_USER="caterina"
PI_REPO="/home/caterina/jarvis-voice-assistant-master"

echo "╔══════════════════════════════════════╗"
echo "║        JARVIS Deploy                 ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "▶ Verbinde mit $PI_USER@$PI_HOST …"

ssh -t "$PI_USER@$PI_HOST" bash <<REMOTE
  set -e
  cd "$PI_REPO"

  echo ""
  echo "■ Stoppe JARVIS …"
  sudo systemctl stop jarvis
  echo "  ✓ gestoppt"

  echo ""
  echo "■ Hole neueste Änderungen …"
  git pull origin main
  echo "  ✓ aktuell"

  echo ""
  echo "■ Starte JARVIS neu …"
  sudo systemctl start jarvis
  sleep 2
  STATUS=\$(systemctl is-active jarvis)
  if [ "\$STATUS" = "active" ]; then
    echo "  ✓ JARVIS läuft"
  else
    echo "  ✗ JARVIS konnte nicht starten — Status: \$STATUS"
    echo "    Logs: sudo journalctl -u jarvis -n 30"
    exit 1
  fi

  echo ""
  echo "═══════════════════════════════════════"
  echo "  Fertig. JARVIS ist aktuell und läuft."
  echo "═══════════════════════════════════════"
REMOTE

echo ""
read -r -p "Enter drücken zum Schließen …"
