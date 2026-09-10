#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# deploy-minibots.sh — Zero-Downtime Deployment Delegate for Minibots

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/opt/minibots"

echo "=== Deploying Minibots Micro-Bot Daemon ==="

# 1. Pre-flight check
"$SCRIPT_DIR/check-minibots.sh"

# 2. Ensure system user & group exist
if ! id -u minibots >/dev/null 2>&1; then
    sudo useradd -r -s /usr/sbin/nologin -d "$TARGET_DIR" minibots 2>/dev/null || true
fi

# 3. Target directory setup & sync
sudo mkdir -p "$TARGET_DIR/data" "$TARGET_DIR/sideload" "$TARGET_DIR/bin"

# Backup existing config.json and live data directory if present
TMP_BAK_DIR="$(mktemp -d /tmp/minibots_deploy.XXXXXX)"
if [[ -d "$TARGET_DIR/data" ]]; then
    sudo cp -rf "$TARGET_DIR/data" "$TMP_BAK_DIR/data"
fi
if [[ -f "$TARGET_DIR/config.json" ]]; then
    sudo cp "$TARGET_DIR/config.json" "$TMP_BAK_DIR/config.json"
fi

sudo cp -rfT "$SCRIPT_DIR" "$TARGET_DIR"

if [[ -d "$TMP_BAK_DIR/data" ]]; then
    sudo cp -rfT "$TMP_BAK_DIR/data" "$TARGET_DIR/data"
fi
if [[ -f "$TMP_BAK_DIR/config.json" ]]; then
    sudo mv "$TMP_BAK_DIR/config.json" "$TARGET_DIR/config.json"
elif [[ ! -f "$TARGET_DIR/config.json" ]]; then
    sudo cp "$TARGET_DIR/config.example.json" "$TARGET_DIR/config.json"
fi
rm -rf "$TMP_BAK_DIR"

# Build Go connection broker binary
if command -v go >/dev/null 2>&1; then
    echo "[*] Building minibots-conn Go binary..."
    mkdir -p "$SCRIPT_DIR/bin"
    (cd "$SCRIPT_DIR" && go build -o bin/minibots-conn ./cmd/conn-daemon)
    sudo mkdir -p "$TARGET_DIR/bin"
    sudo cp -pf "$SCRIPT_DIR/bin/minibots-conn" "$TARGET_DIR/bin/minibots-conn"
    sudo chmod 755 "$TARGET_DIR/bin/minibots-conn"
fi

sudo find "$TARGET_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
sudo chown -R minibots:minibots "$TARGET_DIR" 2>/dev/null || sudo chown -R "$(whoami)":"$(whoami)" "$TARGET_DIR" 2>/dev/null || true
sudo chmod 750 "$TARGET_DIR"
sudo chmod 700 "$TARGET_DIR/data"
if [[ -f "$TARGET_DIR/config.json" ]]; then
    sudo chmod 600 "$TARGET_DIR/config.json"
fi

# 4. Install systemd units
if [[ -d /etc/systemd/system ]]; then
    sudo cp "$TARGET_DIR/systemd/minibots-conn.service" /etc/systemd/system/
    sudo cp "$TARGET_DIR/systemd/minibots.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable minibots-conn.service minibots.service 2>/dev/null || true
    
    # Master Rule §9: Connection daemon preservation (zero socket drops)
    if systemctl is-active --quiet minibots-conn.service; then
        if [[ "${RESTART_CONN:-0}" == "1" ]]; then
            echo "[*] Restarting minibots-conn.service (explicit RESTART_CONN=1)..."
            sudo systemctl restart minibots-conn.service
        else
            echo "[+] minibots-conn.service is active; preserving connection daemon (zero socket drops)."
        fi
    else
        echo "[*] Starting minibots-conn.service..."
        sudo systemctl start minibots-conn.service
    fi
    echo "[*] Restarting minibots.service (logic daemon)..."
    sudo systemctl restart minibots.service
fi

echo "[+] Minibots deployment finished successfully."
