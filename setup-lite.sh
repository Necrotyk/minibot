#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# setup-lite.sh — Zero-Dependency Setup for Minibot Lite (Stock Python Standard Library Only)
# Perfect for legacy hardware (Core 2 Duo, low RAM) and custom/minimal Linux distros.
# Requires NO root, NO pip compilation, NO Go compiler, NO systemd.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║          Minibot Lite — Zero-Dependency Quickstart Setup          ║"
echo "║      Pure Python Stdlib • No Pip • No Go • Low RAM Friendly       ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo

# 1. Check Python version (3.7+)
echo "[*] Checking Python environment..."
PYTHON_BIN=""
for cmd in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" 2>/dev/null; then
            PYTHON_BIN="$cmd"
            PY_VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
            echo "[+] Found compatible Python: $PY_VER ($PYTHON_BIN)"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    echo "[!] Error: Python 3.7 or newer is required to run Minibot Lite."
    echo "    Please install Python 3.7+ using your distribution package manager."
    exit 1
fi

# 2. Create local directories
echo "[*] Creating data and sideload plugin directories..."
mkdir -p "$SCRIPT_DIR/data" "$SCRIPT_DIR/sideload"
chmod 700 "$SCRIPT_DIR/data"
echo "[+] Initialized data/ and sideload/ directories."

# 3. Interactive or automated config generation
CONFIG_FILE="$SCRIPT_DIR/config.json"
SOPEL_FOUND=""
for cand in "$HOME/.sopel" "$HOME/.config/sopel" "$HOME/.sopel/default.cfg" "$HOME/.config/sopel/default.cfg"; do
    if [[ -d "$cand" ]]; then
        if compgen -G "$cand/*.cfg" > /dev/null; then
            SOPEL_FOUND="$cand"
            break
        fi
    elif [[ -f "$cand" ]]; then
        SOPEL_FOUND="$cand"
        break
    fi
done

if [[ -n "$SOPEL_FOUND" && ! -f "$CONFIG_FILE" ]]; then
    if [[ -t 0 && "${CI:-false}" != "true" ]]; then
        read -r -p "Found existing Sopel configuration at $SOPEL_FOUND. Import settings automatically? [Y/n]: " IMPORT_ANS
        IMPORT_ANS="${IMPORT_ANS:-y}"
    else
        IMPORT_ANS="n"
    fi
    if [[ "$IMPORT_ANS" =~ ^[Yy] ]]; then
        echo "[*] Importing configuration and plugins from Sopel..."
        "$PYTHON_BIN" run_bot.py --import-sopel "$SOPEL_FOUND"
    fi
fi

if [[ -f "$CONFIG_FILE" ]]; then
    echo "[+] Detected existing config.json. Leaving existing configuration intact."
else
    echo
    echo "── Configuration Setup ──"
    
    SERVERS_JSON=""
    ADMIN_NETWORKS_JSON=""
    ALL_CHANNELS_JSON=""
    PRIMARY_NET=""
    ADMIN_NICK="admin"
    BOT_NICK="minibot"

    FIRST_ITER=1
    while true; do
        if [[ "$FIRST_ITER" -eq 1 ]]; then
            DEFAULT_NET="libera"
            DEFAULT_HOST="irc.libera.chat"
            DEFAULT_PORT="6697"
            DEFAULT_SSL="true"
            DEFAULT_NICK="minibot"
            DEFAULT_CHANS="#minibot"
            DEFAULT_ADMIN="admin"
        else
            echo
            echo "── Add Additional IRC Network ──"
            DEFAULT_NET="oftc"
            DEFAULT_HOST="irc.oftc.net"
            DEFAULT_PORT="6697"
            DEFAULT_SSL="true"
            DEFAULT_NICK="$BOT_NICK"
            DEFAULT_CHANS="#minibot"
            DEFAULT_ADMIN="$ADMIN_NICK"
        fi

        if [[ -t 0 && "${CI:-false}" != "true" ]]; then
            read -r -p "Network Name [$DEFAULT_NET]: " INPUT_NET
            NET_NAME="${INPUT_NET:-$DEFAULT_NET}"
            NET_NAME="$(echo "$NET_NAME" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"

            read -r -p "IRC Server Host [$DEFAULT_HOST]: " INPUT_HOST
            SERVER_HOST="${INPUT_HOST:-$DEFAULT_HOST}"

            read -r -p "IRC Port [$DEFAULT_PORT]: " INPUT_PORT
            SERVER_PORT="${INPUT_PORT:-$DEFAULT_PORT}"

            read -r -p "Use SSL/TLS (true/false) [$DEFAULT_SSL]: " INPUT_SSL
            SERVER_SSL="${INPUT_SSL:-$DEFAULT_SSL}"

            read -r -p "Bot Nickname [$DEFAULT_NICK]: " INPUT_NICK
            BOT_NICK="${INPUT_NICK:-$DEFAULT_NICK}"

            read -r -p "Channels to join (comma-separated) [$DEFAULT_CHANS]: " INPUT_CHANS
            CHANNELS_STR="${INPUT_CHANS:-$DEFAULT_CHANS}"

            if [[ "$FIRST_ITER" -eq 1 ]]; then
                read -r -p "Admin IRC Nickname [$DEFAULT_ADMIN]: " INPUT_ADMIN
                ADMIN_NICK="${INPUT_ADMIN:-$DEFAULT_ADMIN}"
            fi

            read -r -p "Optional NickServ Password (leave empty if none): " INPUT_PASS
            NICKSERV_PASS="${INPUT_PASS:-}"
        else
            echo "[*] Non-interactive / default configuration mode..."
            NET_NAME="primary"
            SERVER_HOST="$DEFAULT_HOST"
            SERVER_PORT="$DEFAULT_PORT"
            SERVER_SSL="$DEFAULT_SSL"
            BOT_NICK="$DEFAULT_NICK"
            CHANNELS_STR="$DEFAULT_CHANS"
            ADMIN_NICK="$DEFAULT_ADMIN"
            NICKSERV_PASS=""
        fi

        # Format channels into JSON array
        IFS=',' read -r -a CHAN_ARR <<< "$CHANNELS_STR"
        CHAN_JSON=""
        for c in "${CHAN_ARR[@]}"; do
            clean_c=$(echo "$c" | tr -d '[:space:]')
            if [[ ! "$clean_c" =~ ^# ]]; then
                clean_c="#$clean_c"
            fi
            if [[ -n "$clean_c" && "$clean_c" != "#" ]]; then
                if [[ -n "$CHAN_JSON" ]]; then
                    CHAN_JSON="$CHAN_JSON, \"$clean_c\""
                else
                    CHAN_JSON="\"$clean_c\""
                fi
                if [[ -n "$ALL_CHANNELS_JSON" ]]; then
                    ALL_CHANNELS_JSON="$ALL_CHANNELS_JSON, \"$clean_c\""
                else
                    ALL_CHANNELS_JSON="\"$clean_c\""
                fi
            fi
        done
        if [[ -z "$CHAN_JSON" ]]; then
            CHAN_JSON="\"#minibot\""
            if [[ -z "$ALL_CHANNELS_JSON" ]]; then
                ALL_CHANNELS_JSON="\"#minibot\""
            else
                ALL_CHANNELS_JSON="$ALL_CHANNELS_JSON, \"#minibot\""
            fi
        fi

        IS_TRUSTED="false"
        CLEAN_HOST="$(echo "$SERVER_HOST" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
        if [[ "$CLEAN_HOST" == "127.0.0.1" || "$CLEAN_HOST" == "localhost" || "$CLEAN_HOST" == "::1" || "$CLEAN_HOST" == 10.* || "$CLEAN_HOST" == 192.168.* || "$CLEAN_HOST" == 127.* ]]; then
            IS_TRUSTED="true"
        elif [[ "$CLEAN_HOST" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
            IS_TRUSTED="true"
        fi

        if [[ -z "$PRIMARY_NET" ]]; then
            PRIMARY_NET="$NET_NAME"
        fi

        if [[ -n "$ADMIN_NETWORKS_JSON" ]]; then
            ADMIN_NETWORKS_JSON="$ADMIN_NETWORKS_JSON, \"$NET_NAME\""
        else
            ADMIN_NETWORKS_JSON="\"$NET_NAME\""
        fi

        SRV_BLOCK="    \"$NET_NAME\": {
      \"host\": \"$SERVER_HOST\",
      \"port\": $SERVER_PORT,
      \"nick\": \"$BOT_NICK\",
      \"channels\": [$CHAN_JSON],
      \"ssl\": $SERVER_SSL,
      \"trusted\": $IS_TRUSTED,
      \"tls_verify\": true,
      \"nickserv_password\": \"$NICKSERV_PASS\"
    }"

        if [[ -n "$SERVERS_JSON" ]]; then
            SERVERS_JSON="$SERVERS_JSON,
$SRV_BLOCK"
        else
            SERVERS_JSON="$SRV_BLOCK"
        fi

        FIRST_ITER=0

        if [[ -t 0 && "${CI:-false}" != "true" ]]; then
            read -r -p "Add another IRC server/network? [y/N]: " ADD_ANOTHER
            ADD_ANOTHER="${ADD_ANOTHER:-n}"
            if [[ ! "$ADD_ANOTHER" =~ ^[Yy] ]]; then
                break
            fi
        else
            break
        fi
    done

    # Generate minimal config.json
    cat > "$CONFIG_FILE" <<EOF
{
  "PRIMARY_NETWORK": "$PRIMARY_NET",
  "ADMIN_NETWORKS": [$ADMIN_NETWORKS_JSON],
  "ADMIN_CHANNELS": [$ALL_CHANNELS_JSON],
  "ADMIN_NICKS": ["$ADMIN_NICK"],
  "SERVERS": {
$SERVERS_JSON
  }
}
EOF
    chmod 600 "$CONFIG_FILE"
    echo "[+] Generated $CONFIG_FILE (chmod 600) successfully."
fi

echo
echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║                  MINIBOT LITE SETUP COMPLETE!                     ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo
echo "Turnkey Management (./minibot.sh):"
echo "  ./minibot.sh start      - Start bot in background (daemon mode)"
echo "  ./minibot.sh status     - Check live status, PID, and view recent logs"
echo "  ./minibot.sh logs       - Follow live log stream"
echo "  ./minibot.sh restart    - Restart bot in background"
echo "  ./minibot.sh stop       - Stop the bot"
echo "  ./minibot.sh enable     - Install auto-reboot/crash watchdog"
echo "  ./minibot.sh disable    - Remove auto-reboot watchdog"
echo
echo "Manual execution options (zero virtual environment or pip required):"
echo "  $PYTHON_BIN run_bot.py"
echo "  # Or in Frozen-Lib mode:"
echo "  $PYTHON_BIN -I run_bot.py"
echo
echo "Drop existing Sopel plugins into $SCRIPT_DIR/sideload/ for automatic hot-reloading."
echo

if [[ -t 0 && "${CI:-false}" != "true" && "${CALLED_FROM_MINIBOT:-0}" != "1" ]]; then
    read -r -p "Start the bot in the background and enable auto-restart on reboot now? [Y/n]: " START_NOW
    START_NOW="${START_NOW:-y}"
    if [[ "$START_NOW" =~ ^[Yy] ]]; then
        ./minibot.sh start
        ./minibot.sh enable
    fi
fi
