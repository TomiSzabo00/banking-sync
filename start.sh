#!/usr/bin/env bash
# Sets up the virtual environment, installs dependencies, installs the
# systemd service, and starts it.  Run once after cloning (or re-run to update).
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$REPO_DIR/banking-sync"
VENV_DIR="$APP_DIR/.venv"
SERVICE_NAME="banking-sync"

# ── 1. Virtual environment & dependencies ─────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "Dependencies installed."

# ── 2. Install systemd service ────────────────────────────────────────────────
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="$REPO_DIR/${SERVICE_NAME}.service"

if [ ! -f "$TEMPLATE" ]; then
    echo "Error: $TEMPLATE not found."
    exit 1
fi

# Substitute the actual install path into the unit file
sed "s|__APP_DIR__|${APP_DIR}|g; s|__VENV_DIR__|${VENV_DIR}|g" "$TEMPLATE" \
    | sudo tee "$UNIT_FILE" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "banking-sync is running.  Useful commands:"
echo "  sudo systemctl status  $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "  sudo systemctl restart $SERVICE_NAME"
