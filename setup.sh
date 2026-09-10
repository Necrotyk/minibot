#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# setup.sh — Automated & Interactive Setup for Minibots Framework

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║          Minibots Framework — Automated Setup & Installer        ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo

# 1. Check Python
echo "[*] Checking Python environment..."
PYTHON_BIN=""
for cmd in python3 python3.12 python3.11; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PY_VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        PY_MAJOR=$("$cmd" -c "import sys; print(sys.version_info.major)")
        PY_MINOR=$("$cmd" -c "import sys; print(sys.version_info.minor)")
        if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
            PYTHON_BIN="$cmd"
            echo "[+] Found compatible Python: $($cmd --version) ($PYTHON_BIN)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    echo "[!] Error: Python 3.11 or newer is required."
    exit 1
fi

# 2. Virtual Environment Setup
VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "[*] Creating virtual environment in $VENV_DIR..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    echo "[+] Virtual environment created."
fi

VENV_PIP="$VENV_DIR/bin/pip"
VENV_PY="$VENV_DIR/bin/python3"

echo "[*] Installing Python dependencies..."
"$VENV_PIP" install --upgrade pip -q
"$VENV_PIP" install -r requirements.txt -q
"$VENV_PIP" install -e . -q --no-deps
echo "[+] Python dependencies installed."

# 3. Check and Compile Go Connection Broker
echo "[*] Checking Go toolchain for Tier 1 connection daemon..."
if command -v go >/dev/null 2>&1; then
    GO_VER=$(go version)
    echo "[+] Found Go: $GO_VER"
    echo "[*] Compiling Go connection daemon (bin/minibots-conn)..."
    mkdir -p "$SCRIPT_DIR/bin"
    go build -o "$SCRIPT_DIR/bin/minibots-conn" ./cmd/conn-daemon
    echo "[+] Built bin/minibots-conn successfully."
else
    echo "[!] Notice: 'go' compiler not found. Tier 1 connection broker can run via Python fallback (conn_daemon.py) or Docker."
fi

# 4. Configuration Setup
echo "[*] Checking configuration..."
if [[ ! -f "$SCRIPT_DIR/config.json" ]]; then
    if [[ -t 0 && "${CI:-false}" != "true" ]]; then
        echo
        echo "No config.json found. Would you like to initialize one from template? [Y/n]"
        read -r init_cfg
        if [[ "$init_cfg" =~ ^[Nn] ]]; then
            echo "[*] Skipping config creation. Please copy config.example.json to config.json manually."
        else
            cp "$SCRIPT_DIR/config.example.json" "$SCRIPT_DIR/config.json"
            chmod 600 "$SCRIPT_DIR/config.json"
            echo "[+] Initialized config.json (chmod 600) from config.example.json."
        fi
    else
        cp "$SCRIPT_DIR/config.example.json" "$SCRIPT_DIR/config.json"
        chmod 600 "$SCRIPT_DIR/config.json"
        echo "[+] Non-interactive mode: initialized config.json from config.example.json."
    fi
else
    echo "[+] Existing config.json detected."
fi

# 5. Create Data & Sideload Directories
mkdir -p "$SCRIPT_DIR/data" "$SCRIPT_DIR/sideload"
chmod 700 "$SCRIPT_DIR/data"

echo
echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║                     SETUP COMPLETE!                               ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo
echo "Quickstart execution options:"
echo
echo "1. Standalone Single-Process Dev Mode:"
echo "   source .venv/bin/activate"
echo "   python3 run_bot.py --network local --channel '#minibot' --nick minibot"
echo
echo "2. Two-Tier Daemon (High Throughput):"
echo "   Terminal 1: ./bin/minibots-conn"
echo "   Terminal 2: source .venv/bin/activate && python3 main.py"
echo
echo "3. Docker Containerized Mode:"
echo "   docker compose up -d"
echo
echo "4. Verification Test Battery:"
echo "   ./check-minibots.sh"
echo
