#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# minibot.sh — Turnkey Single-Entrypoint Lifecycle Automation & Watchdog Controller
# Manages configuration, foreground/background execution, rotating logs, and auto-healing watchdog.
# Compatible with all Linux distros (systemd, sysvinit, runit, custom) — zero root required.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DATA_DIR="$SCRIPT_DIR/data"
PID_FILE="$DATA_DIR/minibot.pid"
LOG_FILE="$DATA_DIR/minibot.log"
CONFIG_FILE="$SCRIPT_DIR/config.json"
STOPPED_FILE="$DATA_DIR/minibot.stopped"

is_bot_running() {
    if [[ ! -f "$PID_FILE" ]]; then
        return 1
    fi
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
        rm -f "$PID_FILE"
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        return 1
    fi
    # Verify process commandline actually matches run_bot.py to prevent stale PID collisions
    local is_match=0
    if [[ -d "/proc/$pid" && -f "/proc/$pid/cmdline" ]]; then
        if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "run_bot.py"; then
            is_match=1
        fi
    elif command -v ps >/dev/null 2>&1; then
        if ps -p "$pid" -o args= 2>/dev/null | grep -q "run_bot.py"; then
            is_match=1
        fi
    else
        # Fallback if neither /proc nor ps can inspect commandline
        is_match=1
    fi

    if [[ "$is_match" -eq 1 ]]; then
        return 0
    else
        rm -f "$PID_FILE"
        return 1
    fi
}

get_python_bin() {
    if python3 -I -c "import ssl, urllib.request, sqlite3" >/dev/null 2>&1; then
        echo "python3 -I"
    elif python3 -c "import ssl, urllib.request, sqlite3" >/dev/null 2>&1; then
        echo "python3"
    elif python -c "import ssl, urllib.request, sqlite3" >/dev/null 2>&1; then
        echo "python"
    else
        echo "python3"
    fi
}

start_bot() {
    local mode="${1:-normal}"
    if [[ "$mode" == "watchdog" ]]; then
        if [[ -f "$STOPPED_FILE" ]]; then
            # Bot was explicitly stopped by operator; do not auto-revive via watchdog
            return 0
        fi
    else
        # Explicit start or restart: clear stopped marker
        rm -f "$STOPPED_FILE"
    fi

    if is_bot_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        if [[ "$mode" != "watchdog" ]]; then
            echo "[i] Minibot is already running (PID: $pid)."
        fi
        return 0
    fi

    if [[ ! -f "$CONFIG_FILE" ]]; then
        if [[ "$mode" == "watchdog" ]]; then
            return 1
        fi
        echo "[!] No config.json found. Running setup wizard first..."
        CALLED_FROM_MINIBOT=1 "$SCRIPT_DIR/setup-lite.sh"
    fi

    mkdir -p "$DATA_DIR"
    local py_bin
    py_bin="$(get_python_bin)"

    nohup $py_bin "$SCRIPT_DIR/run_bot.py" --daemon >/dev/null 2>&1 &
    local new_pid=$!

    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "$new_pid" > "$PID_FILE"
        chmod 644 "$PID_FILE" 2>/dev/null || true
        if [[ "$mode" != "watchdog" ]]; then
            echo "[✓] Started Minibot successfully in background (PID: $new_pid)."
            echo "    Log stream: tail -f data/minibot.log"
            echo "    Management: ./minibot.sh status"
        fi
        return 0
    else
        if [[ "$mode" != "watchdog" ]]; then
            echo "[✗] Failed to start Minibot. Check data/minibot.log for errors."
            if [[ -f "$LOG_FILE" ]]; then
                echo "--- Last 10 lines of $LOG_FILE ---"
                tail -n 10 "$LOG_FILE"
            fi
        fi
        return 1
    fi
}

stop_bot() {
    mkdir -p "$DATA_DIR"
    touch "$STOPPED_FILE"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user stop minibot-lite.service 2>/dev/null || true
    fi

    if is_bot_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "[*] Stopping Minibot (PID: $pid)..."
        kill -TERM "$pid" 2>/dev/null || true
        local count=0
        while kill -0 "$pid" 2>/dev/null && [ "$count" -lt 5 ]; do
            sleep 1
            count=$((count + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "[!] Minibot did not terminate within 5s, sending SIGKILL..."
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PID_FILE"
        echo "[✓] Minibot stopped."
    else
        echo "[i] Minibot is not running."
        rm -f "$PID_FILE" 2>/dev/null || true
    fi
}

status_bot() {
    echo "========================================"
    echo "       Minibot Lite Lifecycle Status    "
    echo "========================================"
    if is_bot_running; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "Status:      RUNNING"
        echo "PID:         $pid"
        if command -v ps >/dev/null 2>&1; then
            local p_etime
            p_etime="$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ' || true)"
            if [[ -n "$p_etime" ]]; then
                echo "Process Age: $p_etime"
            fi
        fi
    else
        echo "Status:      STOPPED"
    fi

    # Check persistence status
    local cron_active=0
    if command -v crontab >/dev/null 2>&1; then
        if crontab -l 2>/dev/null | grep -q "MINIBOT-AUTO-WATCHDOG"; then
            cron_active=1
        fi
    fi
    local systemd_active=0
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl --user is-enabled minibot-lite.service >/dev/null 2>&1; then
            systemd_active=1
        fi
    fi

    if [[ "$cron_active" -eq 1 && "$systemd_active" -eq 1 ]]; then
        echo "Watchdog:    ENABLED (crontab & systemd --user)"
    elif [[ "$cron_active" -eq 1 ]]; then
        echo "Watchdog:    ENABLED (crontab)"
    elif [[ "$systemd_active" -eq 1 ]]; then
        echo "Watchdog:    ENABLED (systemd --user)"
    else
        echo "Watchdog:    DISABLED (run './minibot.sh enable' to activate)"
    fi

    if [[ -f "$CONFIG_FILE" ]]; then
        local py_bin
        py_bin="$(get_python_bin)"
        local cfg_summary
        cfg_summary="$($py_bin -c '
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        d = json.load(f)
    srvs = d.get("SERVERS", {})
    parts = []
    for k, v in srvs.items():
        h = v.get("host", "unknown")
        p = v.get("port", "")
        ch = ",".join(v.get("channels", []))
        parts.append(f"{k} -> {h}:{p} [{ch}]")
    print(" | ".join(parts) if parts else "No servers configured")
except Exception as e:
    print(f"Error reading config: {e}")
' "$CONFIG_FILE" 2>/dev/null || echo "Unable to parse config")"
        echo "Config:      $CONFIG_FILE ($cfg_summary)"
    else
        echo "Config:      Not configured yet."
    fi

    echo "Log file:    $LOG_FILE"
    echo "----------------------------------------"
    if [[ -f "$LOG_FILE" ]]; then
        echo "Recent log output (last 10 lines):"
        tail -n 10 "$LOG_FILE"
    else
        echo "No log file found at $LOG_FILE yet."
    fi
    echo "========================================"
}

enable_bot() {
    rm -f "$STOPPED_FILE"
    local enabled_any=0

    # 1. Primary persistence: user crontab watchdog
    if command -v crontab >/dev/null 2>&1; then
        local existing_cron
        existing_cron="$(crontab -l 2>/dev/null || true)"
        local cleaned_cron
        cleaned_cron="$(echo "$existing_cron" | grep -v "MINIBOT-AUTO-WATCHDOG" | grep -v "minibot.sh watchdog" || true)"

        local new_cron
        if [[ -n "$cleaned_cron" ]]; then
            new_cron="$cleaned_cron"$'\n'
        else
            new_cron=""
        fi
        new_cron+="# MINIBOT-AUTO-WATCHDOG"$'\n'
        new_cron+="@reboot cd \"$SCRIPT_DIR\" && ./minibot.sh watchdog >/dev/null 2>&1 # MINIBOT-AUTO-WATCHDOG"$'\n'
        new_cron+="*/5 * * * * cd \"$SCRIPT_DIR\" && ./minibot.sh watchdog >/dev/null 2>&1 # MINIBOT-AUTO-WATCHDOG"

        if echo "$new_cron" | crontab - 2>/dev/null; then
            echo "[✓] Crontab watchdog installed successfully (@reboot and every 5 minutes)."
            enabled_any=1
            # Clean up user systemd service if present so both mechanisms never fight
            local svc_file="$HOME/.config/systemd/user/minibot-lite.service"
            if [[ -f "$svc_file" ]] && command -v systemctl >/dev/null 2>&1; then
                systemctl --user stop minibot-lite.service >/dev/null 2>&1 || true
                systemctl --user disable minibot-lite.service >/dev/null 2>&1 || true
                rm -f "$svc_file"
                systemctl --user daemon-reload >/dev/null 2>&1 || true
            fi
        else
            echo "[!] Failed to update user crontab."
        fi
    fi

    # 2. Fallback persistence: systemd --user service (ONLY if crontab is NOT available)
    if [[ "$enabled_any" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
        if systemctl --user list-units >/dev/null 2>&1; then
            local user_svc_dir="$HOME/.config/systemd/user"
            mkdir -p "$user_svc_dir"
            local svc_file="$user_svc_dir/minibot-lite.service"
            cat <<EOF > "$svc_file"
[Unit]
Description=Minibots Lite Standalone IRC Bot
After=network.target

[Service]
Type=forking
PIDFile=$PID_FILE
WorkingDirectory=$SCRIPT_DIR
ExecStart=/bin/bash $SCRIPT_DIR/minibot.sh start
ExecStop=/bin/bash $SCRIPT_DIR/minibot.sh stop
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
            systemctl --user daemon-reload >/dev/null 2>&1 || true
            if systemctl --user enable minibot-lite.service >/dev/null 2>&1; then
                echo "[✓] systemd user unit installed and enabled as fallback: $svc_file"
                enabled_any=1
            fi
        fi
    fi

    if [[ "$enabled_any" -eq 0 ]]; then
        echo "[i] Neither crontab nor systemd --user is available in this environment."
        echo "    To automatically start on boot, add this line to /etc/rc.local or ~/.profile:"
        echo "      cd \"$SCRIPT_DIR\" && ./minibot.sh start"
    fi
}

disable_bot() {
    rm -f "$STOPPED_FILE"
    # 1. Remove crontab entries
    if command -v crontab >/dev/null 2>&1; then
        local existing_cron
        existing_cron="$(crontab -l 2>/dev/null || true)"
        if echo "$existing_cron" | grep -q "MINIBOT-AUTO-WATCHDOG"; then
            local cleaned_cron
            cleaned_cron="$(echo "$existing_cron" | grep -v "MINIBOT-AUTO-WATCHDOG" | grep -v "minibot.sh watchdog" || true)"
            if [[ -n "$(echo "$cleaned_cron" | tr -d '[:space:]')" ]]; then
                echo "$cleaned_cron" | crontab - 2>/dev/null || true
            else
                crontab -r 2>/dev/null || true
            fi
            echo "[✓] Removed watchdog entries from crontab."
        fi
    fi

    # 2. Disable systemd user service
    local svc_file="$HOME/.config/systemd/user/minibot-lite.service"
    if [[ -f "$svc_file" ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl --user stop minibot-lite.service >/dev/null 2>&1 || true
        systemctl --user disable minibot-lite.service >/dev/null 2>&1 || true
        rm -f "$svc_file"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        echo "[✓] Removed and disabled systemd user unit."
    fi
    echo "[✓] Auto-restart persistence disabled."
}

smart_mode() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "[*] No config.json found. Launching quickstart setup wizard..."
        CALLED_FROM_MINIBOT=1 "$SCRIPT_DIR/setup-lite.sh"
        start_bot "normal"
        if [[ -t 0 && "${CI:-false}" != "true" ]]; then
            read -r -p "Enable automatic reboot & crash recovery watchdog? [Y/n]: " ans
            ans="${ans:-y}"
            if [[ "$ans" =~ ^[Yy] ]]; then
                enable_bot
            fi
        fi
    else
        if ! is_bot_running; then
            start_bot "normal"
        fi
        status_bot
    fi
}

case "${1:-}" in
    start)
        start_bot "normal"
        ;;
    watchdog)
        start_bot "watchdog"
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        sleep 1
        start_bot "normal"
        ;;
    status)
        status_bot
        ;;
    logs)
        if [[ -f "$LOG_FILE" ]]; then
            exec tail -f "$LOG_FILE"
        else
            echo "[i] Log file $LOG_FILE does not exist yet. Start the bot first."
        fi
        ;;
    enable)
        enable_bot
        ;;
    disable)
        disable_bot
        ;;
    setup)
        "$SCRIPT_DIR/setup-lite.sh"
        ;;
    help|--help|-h)
        echo "Usage: ./minibot.sh [command]"
        echo ""
        echo "Commands:"
        echo "  (none)      Smart mode: setup if needed, start if stopped, show status"
        echo "  start       Start bot in background (daemon mode)"
        echo "  watchdog    Silent auto-restart watchdog (for cron/systemd)"
        echo "  stop        Stop bot gracefully"
        echo "  restart     Stop and restart bot"
        echo "  status      Display live running status, PID, and recent logs"
        echo "  logs        Follow live log output (tail -f data/minibot.log)"
        echo "  enable      Install automatic reboot & crash recovery watchdog"
        echo "  disable     Remove watchdog persistence"
        echo "  setup       Run interactive setup wizard"
        ;;
    "")
        smart_mode
        ;;
    *)
        echo "Unknown command: $1"
        echo "Run './minibot.sh help' for usage instructions."
        exit 1
        ;;
esac
