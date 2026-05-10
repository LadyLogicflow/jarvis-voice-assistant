#!/bin/bash
# Generates a self-signed TLS certificate for local JARVIS access.
# Run once on the Pi: bash scripts/gen-cert.sh
set -euo pipefail

CERT_DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$CERT_DIR"

IP=$(hostname -I | awk '{print $1}')
HOST=$(hostname).local

openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
    -keyout "$CERT_DIR/key.pem" \
    -out    "$CERT_DIR/cert.pem" \
    -subj   "/CN=$HOST" \
    -addext "subjectAltName=IP:$IP,DNS:$HOST,DNS:localhost"

echo ""
echo "Zertifikat erstellt:"
echo "  cert: $CERT_DIR/cert.pem"
echo "  key:  $CERT_DIR/key.pem"
echo ""
echo "Jetzt in config.json eintragen:"
echo "  \"server_ssl_cert\": \"$CERT_DIR/cert.pem\","
echo "  \"server_ssl_key\":  \"$CERT_DIR/key.pem\""
