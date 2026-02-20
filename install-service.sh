#!/bin/bash
# Script di installazione del servizio Hyperliquid Bot
# Esegui con: bash install-service.sh

set -e

# ---------------------------------------------------------------------------
# CONFIGURA QUESTI VALORI
# ---------------------------------------------------------------------------
BOT_USER="$USER"                          # utente che eseguirà il bot
BOT_DIR="$(pwd)"                          # directory corrente (dove sta il bot)
PYTHON_BIN="/usr/bin/python3"             # python3
ENV_FILE="opentradenet.env"
SERVICE_NAME="opentradenet_bot"
# ---------------------------------------------------------------------------

echo "=== Installazione servizio $SERVICE_NAME ==="
echo "Utente:    $BOT_USER"
echo "Directory: $BOT_DIR"
echo "Python:    $PYTHON_BIN"
echo ""

# 1. Crea virtualenv se non esiste
if [ ! -d "$BOT_DIR/venv" ]; then
    echo "[1/5] Creo virtualenv..."
    python3 -m venv "$BOT_DIR/venv"
else
    echo "[1/5] Virtualenv già esistente, skip."
fi


# 2. Crea la cartella log
echo "[3/5] Creo cartella log..."
mkdir -p "$BOT_DIR/logs"

# 3. Genera il file .service con i valori reali
echo "[4/5] Genero file systemd..."
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

sudo tee "$SERVICE_FILE" > /dev/null << UNIT
[Unit]
Description=Hyperliquid Price Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
ExecStart=$PYTHON_BIN $BOT_DIR/hyperliquid_bot.py $ENV_FILE
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

echo "    Scritto: $SERVICE_FILE"

# 4. Abilita e avvia il servizio
echo "[5/5] Abilito e avvio il servizio..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo ""
echo "=== Fatto! ==="
echo ""
echo "Comandi utili:"
echo "  sudo systemctl status $SERVICE_NAME     # stato"
echo "  sudo systemctl stop $SERVICE_NAME       # ferma"
echo "  sudo systemctl start $SERVICE_NAME      # avvia"
echo "  sudo systemctl restart $SERVICE_NAME    # riavvia"
echo "  journalctl -u $SERVICE_NAME -f          # log live"
echo "  journalctl -u $SERVICE_NAME --since today  # log di oggi"
