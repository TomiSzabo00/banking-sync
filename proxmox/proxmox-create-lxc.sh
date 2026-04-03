#!/usr/bin/env bash
# =============================================================================
# Banking-Sync — Proxmox LXC Setup Script
# Run this on your Proxmox HOST (not inside a container).
#
# What it does:
#   1. Downloads the Debian 12 template (if not cached)
#   2. Creates an unprivileged LXC container
#   3. Installs Python, creates a venv, installs dependencies
#   4. Pushes the application files and systemd service into the container
#   5. Enables the service (but does NOT start it — edit config first)
# =============================================================================
set -euo pipefail

# ── Configuration — edit these to match your environment ─────────────────────
CT_ID=210                       # any free CT ID
CT_HOSTNAME="banking-sync"      # hostname for the container
CT_IP="192.168.1.60"            # IP address for the container
CT_GW="192.168.1.1"             # gateway for the container (your router's IP, or the IP of your DNS server)
CT_CIDR="24"
CT_DNS="192.168.1.1 8.8.8.8"    # space-separated: primary (router or DNS server) + fallback
CT_STORAGE="local-lvm"          # storage pool for the container (yours may be different)
CT_DISK_GB=4
CT_RAM_MB=256
CT_SWAP_MB=256
CT_CORES=1
CT_TEMPLATE_STORAGE="local"
CT_OS_TEMPLATE="debian-12-standard_12.12-1_amd64.tar.zst" # your template name may be different
APP_DIR="/opt/banking-sync"     # any path will do
APP_USER="banking-sync"         # any username will do
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SOURCE="${SCRIPT_DIR}/banking-sync"
SERVICE_FILE="${SCRIPT_DIR}/banking-sync.service"
TEMPLATE_PATH="${CT_TEMPLATE_STORAGE}:vztmpl/${CT_OS_TEMPLATE}"

# ── Preflight checks ────────────────────────────────────────────────────────
if [ ! -d "$APP_SOURCE" ]; then
  echo "Error: banking-sync/ directory not found next to this script."
  exit 1
fi

if pct status "${CT_ID}" &>/dev/null; then
  echo "Error: Container ${CT_ID} already exists. Delete it first or choose a different CT_ID."
  exit 1
fi

# ── 1. Ensure template is available ─────────────────────────────────────────
echo "==> Checking for Debian 12 template..."
if ! pveam list "${CT_TEMPLATE_STORAGE}" | grep -q "${CT_OS_TEMPLATE}"; then
  echo "    Downloading template..."
  pveam update
  pveam download "${CT_TEMPLATE_STORAGE}" "${CT_OS_TEMPLATE}"
fi

# ── 2. Create container ─────────────────────────────────────────────────────
echo "==> Creating LXC container ${CT_ID} (${CT_HOSTNAME})..."
pct create "${CT_ID}" "${TEMPLATE_PATH}" \
  --hostname "${CT_HOSTNAME}" \
  --storage "${CT_STORAGE}" \
  --rootfs "${CT_STORAGE}:${CT_DISK_GB}" \
  --memory "${CT_RAM_MB}" \
  --swap "${CT_SWAP_MB}" \
  --cores "${CT_CORES}" \
  --net0 "name=eth0,bridge=vmbr0,ip=${CT_IP}/${CT_CIDR},gw=${CT_GW},firewall=0" \
  --nameserver "${CT_DNS}" \
  --unprivileged 1 \
  --features "nesting=0" \
  --start 0

# ── 3. Start and wait for network ───────────────────────────────────────────
echo "==> Starting container..."
pct start "${CT_ID}"
sleep 3

echo "==> Waiting for network..."
for i in $(seq 1 15); do
  if pct exec "${CT_ID}" -- ping -c1 -W2 8.8.8.8 &>/dev/null; then
    echo "    Network is up."
    break
  fi
  if [ "$i" -eq 15 ]; then
    echo "Error: Network not reachable after 75 seconds. Check CT_IP/CT_GW."
    exit 1
  fi
  sleep 5
done

# ── 4. Install packages inside the container ────────────────────────────────
echo "==> Installing system packages..."
pct exec "${CT_ID}" -- bash -s <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq python3 python3-venv ca-certificates
INNER

# ── 5. Push application files ───────────────────────────────────────────────
echo "==> Pushing application files..."
pct exec "${CT_ID}" -- mkdir -p "${APP_DIR}"

for f in "$APP_SOURCE"/*.py "$APP_SOURCE"/requirements.txt "$APP_SOURCE"/config.yaml; do
  [ -f "$f" ] || continue
  pct push "${CT_ID}" "$f" "${APP_DIR}/$(basename "$f")"
done

# ── 6. Push and configure systemd service ────────────────────────────────────
echo "==> Setting up systemd service..."

# Generate the unit file with correct paths
sed "s|__APP_DIR__|${APP_DIR}|g; s|__VENV_DIR__|${APP_DIR}/.venv|g" "${SERVICE_FILE}" \
  | pct exec "${CT_ID}" -- tee /etc/systemd/system/banking-sync.service > /dev/null

# Add User= to the service file
pct exec "${CT_ID}" -- sed -i "/^Type=simple/a User=${APP_USER}" \
  /etc/systemd/system/banking-sync.service

# ── 7. Create venv, install deps, set permissions ───────────────────────────
echo "==> Setting up Python environment..."
pct exec "${CT_ID}" -- bash -s -- "${APP_DIR}" "${APP_USER}" <<'INNER'
set -euo pipefail
APP_DIR="$1"
APP_USER="$2"

# Create service user
id -u "$APP_USER" &>/dev/null || useradd -r -s /usr/sbin/nologin -d "$APP_DIR" "$APP_USER"

# Create venv and install deps
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

# Create data and log directories
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"

# Set ownership
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 700 "${APP_DIR}/data"

# Enable service (don't start — user needs to edit config first)
systemctl daemon-reload
systemctl enable banking-sync
INNER

echo ""
echo "================================================================"
echo " LXC ${CT_ID} (${CT_HOSTNAME}) is ready at ${CT_IP}"
echo ""
echo " Next steps:"
echo "   1. Edit the config:"
echo "      pct exec ${CT_ID} -- nano ${APP_DIR}/config.yaml"
echo ""
echo "   2. Copy your private key into the container:"
echo "      pct push ${CT_ID} /path/to/private.pem ${APP_DIR}/private.pem"
echo "      pct exec ${CT_ID} -- chown ${APP_USER}:${APP_USER} ${APP_DIR}/private.pem"
echo "      pct exec ${CT_ID} -- chmod 600 ${APP_DIR}/private.pem"
echo ""
echo "   3. Start the service:"
echo "      pct exec ${CT_ID} -- systemctl start banking-sync"
echo ""
echo "   4. Authenticate (open in browser):"
echo "      http://${CT_IP}:8080/auth/start"
echo ""
echo " Useful commands:"
echo "   pct exec ${CT_ID} -- systemctl status banking-sync"
echo "   pct exec ${CT_ID} -- journalctl -u banking-sync -f"
echo "================================================================"
