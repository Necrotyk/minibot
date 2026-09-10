# Minibots — Autonomous Micro-Bot Framework & Universal Sideloading Engine

> **License:** GNU Affero General Public License v3.0 or later ([AGPL-3.0-or-later](LICENSE))  
> **Maintainer:** Necrotyk — <https://dystopi.cc>  
> **Repository:** <https://github.com/Necrotyk/minibot>  
> **Platform:** Linux POSIX / systemd / Docker | Python 3.11+ | Go 1.22+  
> **Transports:** IRC (TLS/plaintext), Matrix, UNIX Domain Sockets (`0660`)

---

## 1. Overview

**Minibots** is an enterprise-grade, autonomous micro-bot orchestration system and universal plugin sideloading framework designed for resilient ChatOps, local AI inference routing, and automated software engineering.

Built with a strict **Zero-Downtime, Zero-Disconnect** philosophy, Minibots separates connection state from bot logic through a decoupled two-tier architecture. It allows operators to spawn dynamic micro-bots, write and hot-reload plugins in real time, run multi-model local AI reasoning pipelines, and orchestrate coding agents without dropping a single IRC socket.

### Key Capabilities
- **Zero-Disconnect Connection Multiplexing:** Persistent connection broker (`minibots-conn`) in native Go maintains TCP/TLS sockets across logic restarts (`!minibot reload` / `SIGHUP`).
- **Universal Plugin Sideloading:** Drop Python scripts into `sideload/` with automatic AST pre-flight verification, syntax safety gates, and `mtime` hot-reloading.
- **Zero-Dependency Multi-Framework Shims:** Native compatibility layers for **Sopel** (`@module.commands`, `@module.rule`, `@module.interval`), **Limnoria / Supybot**, **Errbot** (`@botcmd`), **Discord.py** (`@bot.command`), and standard single-server IRC bots.
- **DAI (Distributed AI Inference) Engine:** Two-pass search and reasoning pipeline, dynamic local model switching via `llama-server` / Ollama, global inference rate limiter, and multi-provider failover (Local LLM ↔ Google Gemini ↔ Kaggle GPU Workers).
- **Google Jules Autonomous Coding Plane:** Interactive GitHub coding workspace in `#jules` with automated branch provisioning (`jules-<nick>-<label>`), plan generation, code reviews, and pull requests.
- **Dynamic ToolHub & ToolCrafter:** Operator-driven in-channel tool generation with AST parsing and instant sideload deployment.
- **Zero-Trust Hardening:** Anti-SSRF link previewer with DNS pinning blocking RFC1918 and link-local ranges, UNIX domain socket peer credentials validation (`SO_PEERCRED`), and perimeter channel access controls.

---

## 2. Architecture

Minibots enforces clean layer separation between network I/O and bot logic:

```
                      ┌─────────────────────────────────┐
                      │    IRC Networks & Transports    │
                      │  (WireGuard, Libera, OFTC, TLS) │
                      └────────────────┬────────────────┘
                                       │ TCP / TLS Sockets
                                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Tier 1: Connection Daemon (minibots-conn / conn_daemon.py)           │
│  - Persistent socket pool & reconnect state machine                   │
│  - Token-bucket rate limiters per network                             │
│  - UNIX domain socket IPC server (/opt/minibots/data/minibots_ipc.sock)│
└──────────────────────────────────────┬─────────────────────────────────┘
                                       │ Framed JSON-RPC (0660 / SO_PEERCRED)
                                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Tier 2: Logic Engine & Bot Dispatcher (main.py)                      │
│  - In-process hot-reloader (SIGHUP / !minibot reload)                 │
│  - BotRegistry: Primary & dynamically spawned micro-bots              │
│  - Sideload Engine: AST verification & live plugin watcher             │
│  - Multi-Provider LLM Router (Local Llama, Gemini, Kaggle GPU)        │
│  - Dynamic ToolHub & ToolCrafter                                       │
│  - Partitioned SQLite WAL Storage (/opt/minibots/data/*.db)            │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Directory Structure

```
minibots/
├── cmd/
│   └── conn-daemon/          # Native Go connection multiplexer entrypoint
├── internal/                 # Go connection daemon packages (irc, ipc, config)
├── conn_daemon.py            # Python connection multiplexer fallback
├── main.py                   # Logic daemon, hot-reloader & command dispatcher
├── run_bot.py                # Standalone single-bot CLI runner (zero daemons needed)
├── keystore.py               # Secret ingestion and non-destructive environment loader
├── config.example.json       # Sanitized configuration template
├── deploy-minibots.sh        # Zero-downtime production deployment script
├── check-minibots.sh         # Local CI pre-commit verification test suite
├── adapters/                 # Zero-dependency framework shims (Sopel, Limnoria, Errbot, Discord)
├── core/                     # LLM router, AST validator, ToolCrafter, telemetry, prober
├── minibots/                 # MiniBot base class, bot manager, connection wrapper
├── plugins/                  # Core plugins (URLs, Trollbot, Toolbot, Julesbot, DPoP Auth)
├── jules/                    # Google Jules autonomous coding client & git operations
├── sideload/                 # Watched directory for drop-in plugins and custom scripts
├── systemd/                  # Hardened systemd service units (minibots-conn, minibots)
└── tests/                    # Comprehensive unit and integration test suite
```

---

## 4. Prerequisites

- **Operating System:** Linux (Debian 12+, Ubuntu 22.04+, Arch, Fedora)
- **Python:** 3.11 or higher
- **Go Toolchain:** 1.22 or higher (required to build `minibots-conn`)
- **System Packages:** `build-essential`, `git`, `sqlite3`
- **Optional Services:**
  - Optional VPN / Mesh network interface for private cluster control

---

## 5. Setup & Installation

You can install and run Minibots using any of the three workflows below:

### Option A: Automated Quickstart (Recommended)

Clone the repository and run the interactive setup script:

```bash
git clone https://github.com/Necrotyk/minibot.git
cd minibot
./setup.sh
```

`setup.sh` automatically checks Python 3.11+ and Go, sets up a `.venv`, installs all dependencies, compiles `bin/minibots-conn`, and generates a `config.json` template.

### Option B: Containerized Docker Workflow

Run Minibots in an isolated multi-stage container without needing local compilers:

```bash
git clone https://github.com/Necrotyk/minibot.git
cd minibot
cp config.example.json config.json

# Start via Docker Compose
docker compose up -d
```

### Option C: Manual CLI Setup & Build

```bash
git clone https://github.com/Necrotyk/minibot.git
cd minibot

# 1. Python virtualenv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e . --no-deps

# 2. Compile Tier 1 Go connection broker
mkdir -p bin
go build -o bin/minibots-conn ./cmd/conn-daemon
chmod 755 bin/minibots-conn

# 3. Initialize configuration
cp config.example.json config.json
chmod 600 config.json
```

---

### Configuration Reference (`config.json`)

Minibots supports `${VARIABLE}` expansion for secrets. Configure your networks, admin handles, and API keys:

```json
{
  "INTERNAL_SERVER": "127.0.0.1",
  "ADMIN_NICKS": [
    "admin*"
  ],
  "SERVERS": {
    "local": {
      "host": "127.0.0.1",
      "port": 6667,
      "nick": "minibot",
      "channels": [
        "#minibot",
        "#dev"
      ],
      "ssl": false
    },
    "libera": {
      "host": "irc.libera.chat",
      "port": 6697,
      "nick": "minibot",
      "channels": [
        "#minibot"
      ],
      "ssl": true,
      "nickserv_password": "${MINIBOT_LIBERA_NICKSERV_PASSWORD}"
    }
  },
  "PASTEBIN_URL": "${PASTEBIN_URL:-http://127.0.0.1:8000}",
  "JULES": {
    "API_KEY": "${JULES_API_KEY}",
    "DEFAULT_REPO": "owner/repo"
  },
  "LLM": {
    "GEMINI_API_KEY": "${GEMINI_API_KEY}",
    "LOCAL_LLM_URL": "http://127.0.0.1:8080/v1",
    "KAGGLE_GPU_URL": "${KAGGLE_GPU_URL}",
    "MCP_SERVER_URL": "${MCP_SERVER_URL:-http://127.0.0.1:8050/sse}"
  },
  "MATRIX": {
    "ENABLED": false,
    "HOMESERVER_URL": "https://matrix.example.org",
    "USER_ID": "@minibot:example.org",
    "ACCESS_TOKEN": "${MINIBOT_MATRIX_ACCESS_TOKEN}"
  }
}
```

> [!TIP]
> Minibots supports `${VARIABLE}` expansion in `config.json`. You can provide secrets via a local `.env` file (`chmod 600`) or system environment variables without writing secrets into configuration files.

---

## 6. Running Minibots

### Mode A: Standalone CLI Launcher (Zero Background Daemons)

For quick testing, debugging, or running a single bot directly in a terminal or tmux session without installing systemd services:

```bash
# Syntax: python3 run_bot.py <nick> <network> <#channel> [module1,module2,...]
python3 run_bot.py mybot localnet #lobby sideload_infobot,sideload_web_search
```

### Mode B: Two-Tier Development Mode

Run the connection broker and logic engine interactively in two terminal panes:

**Terminal 1 (Connection Multiplexer):**
```bash
./bin/minibots-conn
```

**Terminal 2 (Logic Engine):**
```bash
source venv/bin/activate
python3 main.py
```

Press `Ctrl+C` in Terminal 2 to stop the logic daemon. The connection daemon in Terminal 1 will keep all IRC connections open and alive! When you restart `main.py`, it reconnects to the broker instantly with zero socket drops.

### Mode C: Production Systemd Deployment

Deploy Minibots as hardened systemd services under the dedicated `minibots` system user:

```bash
sudo ./deploy-minibots.sh
```

The installer:
1. Validates all tests and pre-flight gates via `./check-minibots.sh`.
2. Provisions `/opt/minibots/` with strict permissions (`0750`).
3. Installs and starts `minibots-conn.service` and `minibots.service`.
4. Enforces Linux security hardening (`ProtectSystem=strict`, `PrivateTmp=true`, `NoNewPrivileges=true`, `UMask=0027`).

To inspect service logs:
```bash
# Logic engine logs
journalctl -u minibots.service -f

# Connection multiplexer logs
journalctl -u minibots-conn.service -f
```

---

## 7. Command Reference

### Administrative Control Plane (`#minibots` / Admin Channels)
- `!minibot spawn <nick> <#chan> <modules>` — Spawns an isolated micro-bot instance (e.g. `!minibot spawn secbot #alerts urls,sideload_infobot`).
- `!minibot list` — Enumerates all active micro-bots, joined networks/channels, and attached modules.
- `!minibot kill <nick>` — Terminates a specific spawned micro-bot.
- `!minibot reload` — Triggers an in-process zero-downtime hot-reload of all Python logic modules (<20ms).
- `!minibot sideload list` — Lists all custom scripts available in `sideload/`.
- `!minibot sideload load <script>` — Dynamically loads or reloads a sideload script.
- `!minibot usage` / `!minibot stats` — Displays 24h LLM token consumption and latency metrics.

### AI & DAI Inference Commands
- `.dai <query>` — Executes a two-pass web search and reasoning pipeline with inline citations and colorized formatting.
- `.ai <prompt>` — Direct LLM chat bypassing search, running on the active local model.
- `.model list` — Lists available local LLM models and shows the currently loaded model.
- `.model switch <model>` — Hot-swaps the active model on `llama-server` (gated to admin/wireguard channels).
- `.model info` — Displays VRAM usage, context size, and parameter details.

### Dynamic ToolHub & ToolCrafting
- `!toolhub list` — Browses available community and built-in tools.
- `!toolhub search <query>` — Searches the curated tool pack for matching utilities.
- `!toolhub install <tool>` — Installs a curated tool directly into `sideload/`.
- `!craft <name> <code>` — Synthesizes a new tool from Python code with AST safety validation.

### Google Jules Autonomous Coding Plane (`#jules`)
- `!jules init <label> <repo> <prompt>` — Initializes a GitHub coding session and provisions branch `jules-<nick>-<label>`.
- `!jules approve <label>` — Approves generated plan and starts autonomous implementation.
- `!jules status <label>` — Checks live progress and step details of an active coding session.

### Built-in Utilities
- `.info <query>` — Fast encyclopedia search via Grokipedia.
- `.calc <expression>` — Safe arithmetic and mathematical evaluator.
- `.weather <location>` — Real-time weather and forecast lookup.
- `.time <location>` — World clock and timezone converter.

---

## 8. Authoring Sideload Plugins

Minibots makes writing custom IRC bots trivial. Drop any Python script into `sideload/` and the logic engine hot-reloads it automatically.

### Example 1: Standard Minibots Plugin

Save as `sideload/my_plugin.py`:
```python
# SPDX-License-Identifier: AGPL-3.0-or-later

class Plugin:
    def __init__(self, bot):
        self.bot = bot

    async def handle_privmsg(self, target: str, sender: str, text: str):
        if text.startswith("!ping"):
            self.bot.send_msg(target, f"pong {sender}!")
```

### Example 2: Sopel Compatibility Shim

Minibots runs existing **Sopel** modules without requiring `sopel` installed:
```python
# SPDX-License-Identifier: AGPL-3.0-or-later
from sopel import module

@module.commands("hello", "hi")
def hello_command(bot, trigger):
    bot.reply(f"Hello, {trigger.nick}!")

@module.rule(r"^!echo\s+(.*)")
def echo_rule(bot, trigger):
    bot.say(trigger.group(1))
```

### Example 3: Errbot Compatibility Shim

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
from errbot import BotPlugin, botcmd

class GreetPlugin(BotPlugin):
    @botcmd
    def greet(self, msg, args):
        return f"Greetings, {msg.frm}! Arguments: {args}"
```

### Example 4: Discord.py Compatibility Shim

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
from discord_shim import bot

@bot.command(name="status")
async def status_cmd(ctx, *args):
    await ctx.reply("All systems operational.")
```

---

## 9. Verification & Pre-Commit Testing

Minibots includes an automated 5-gate local CI suite:

```bash
./check-minibots.sh
```

The verification gates enforce:
1. **Gate 1:** Python AST Compilation sweep across all files (`python3 -m compileall`).
2. **Gate 2:** Leaked secret and API token regex scanning.
3. **Gate 3:** AGPL-3.0-or-later SPDX license header compliance across all `.py`, `.go`, and `.sh` files.
4. **Gate 4:** Go vet, unit testing, and binary compilation (`go vet ./... && go test ./...`).
5. **Gate 5:** Python hermetic unit test execution (155+ tests with 100% pass rate).

To run individual test files:
```bash
python3 -m unittest tests/test_minibots.py -v
python3 -m unittest tests/test_dai_pipeline.py -v
```

---

## 10. Security Model

- **Least Privilege:** Daemons run under the unprivileged `minibots` user with strict systemd resource sandboxing (`ProtectSystem=strict`, `NoNewPrivileges=true`).
- **UNIX Domain Socket IPC:** Sockets are provisioned with mode `0660` owned by `minibots:minibots`. The Go connection daemon authenticates client UIDs via kernel `SO_PEERCRED`.
- **Anti-SSRF Guard:** The URL parser validates all hostnames against DNS-pinned responses, dropping requests directed at `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, and cloud metadata (`169.254.169.254`).
- **Anti-Smuggling Defanger:** Outgoing IRC messages pass through an anti-smuggling filter that strips raw `\r` and `\n` linebreaks to prevent protocol injection.
- **Admin Perimeter Isolation:** Administrative commands (`!spawn`, `!kill`, `!reload`, `.model switch`) are strictly blocked in public channels and authorized only from configured admin channels on authenticated networks.

---

## 11. License & Legal Notice

**Minibots is Free Software licensed under the GNU Affero General Public License v3.0 or later ([AGPL-3.0-or-later](LICENSE)).**

```
Copyright (C) 2024-2026 Necrotyk <https://dystopi.cc>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

### Why AGPL-3.0-or-later?
Under Section 13 of the GNU AGPLv3, if you run a modified or unmodified version of Minibots as a network service or interactive bot accessible to remote users (e.g. over IRC, Matrix, Discord, or Web), **you must provide the complete corresponding source code of that service to all users connecting to it**. 

This copyleft guarantee ensures that Minibots remains open, community-driven, and permanently protected against closed-source proprietary commercialization or closed-source forks for profit.
