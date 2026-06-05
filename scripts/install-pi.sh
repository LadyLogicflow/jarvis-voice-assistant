#!/bin/bash
# Einmalige Einrichtung von JARVIS auf dem Raspberry Pi.
# Aufruf: sudo bash scripts/install-pi.sh
set -euo pipefail

JARVIS_USER="${SUDO_USER:-$(logname)}"
JARVIS_HOME="/home/$JARVIS_USER"
REPO_DIR="$JARVIS_HOME/jarvis-voice-assistant-master"
SYSTEMD="/etc/systemd/system"
PYTHON="python3"

echo "=== JARVIS Pi Setup (user: $JARVIS_USER) ==="

# System-Abhängigkeiten
apt-get update -qq
apt-get install -y git python3 python3-venv python3-pip libopenblas-dev

# Virtual environment erstellen (falls noch nicht vorhanden)
if [ ! -f "$REPO_DIR/.venv/bin/python" ]; then
    echo "Erstelle .venv …"
    sudo -u "$JARVIS_USER" $PYTHON -m venv "$REPO_DIR/.venv"
fi

# Python-Pakete ins venv installieren
echo "Installiere Python-Pakete …"
sudo -u "$JARVIS_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$JARVIS_USER" "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

# sudoers: JARVIS-Dienst darf ohne Passwort neu gestartet werden
SUDOERS_LINE="$JARVIS_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart jarvis, /bin/systemctl start jarvis, /bin/systemctl stop jarvis"
if ! grep -qF "$JARVIS_USER ALL=(ALL) NOPASSWD" /etc/sudoers.d/jarvis 2>/dev/null; then
    echo "$SUDOERS_LINE" > /etc/sudoers.d/jarvis
    chmod 440 /etc/sudoers.d/jarvis
    echo "sudoers-Regel für JARVIS-Neustart eingetragen."
fi

# systemd-Units aus Templates generieren (Platzhalter ersetzen)
for f in jarvis.service jarvis-update.service jarvis-update.timer; do
    sed "s|__USER__|$JARVIS_USER|g; s|__HOME__|$JARVIS_HOME|g; s|__REPO__|$REPO_DIR|g" \
        "$REPO_DIR/scripts/$f" > "$SYSTEMD/$f"
done
chmod +x "$REPO_DIR/scripts/jarvis-update.sh"
sed -i "s|__HOME__|$JARVIS_HOME|g; s|__REPO__|$REPO_DIR|g" \
    "$REPO_DIR/scripts/jarvis-update.sh"

systemctl daemon-reload
systemctl enable --now jarvis.service
systemctl enable --now jarvis-update.timer

echo ""
echo "=== Fertig! ==="
echo "Status:   systemctl status jarvis"
echo "Log:      tail -f $REPO_DIR/jarvis.log"
echo "Nächste Schritte:"
echo "  1. .env und config.json vom alten Pi kopieren oder neu anlegen"
echo "  2. sudo systemctl restart jarvis"
