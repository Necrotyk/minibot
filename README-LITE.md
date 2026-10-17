# Minibot Lite — Zero-Dependency Sopel Drop-In Replacement

> **Target Profile:** 100% Python Standard Library • Zero External Pip Wheels • Minimal RAM (<25 MB)  
> **Hardware Support:** Runs on legacy 64-bit x86 (Intel Core 2 Duo, Merom, SSSE3) and ARM low-spec boards  
> **OS Independence:** Runs on any Linux distribution (no `systemd`, `apt`, Docker, or Go compiler needed)  
> **License:** GNU Affero General Public License v3.0 or later ([AGPL-3.0-or-later](LICENSE))

---

## ⚡ 30-Second Quickstart (Set & Forget)

To set up, configure, background, and protect Minibot Lite forever with auto-reboot and crash recovery, simply run:

```bash
git clone https://github.com/Necrotyk/minibot.git
cd minibot
./minibot.sh
```

If `config.json` is missing, `./minibot.sh` automatically launches the interactive setup wizard (or imports your existing Sopel `.cfg`), starts the bot in daemon mode with rotating logs, and offers to install the auto-healing watchdog.

---

## 🛠️ Turnkey Lifecycle Automation (`./minibot.sh`)

`minibot.sh` is a POSIX/bash-compatible, zero-root controller that manages the full bot lifecycle:

| Command | Description |
| :--- | :--- |
| `./minibot.sh` | **Smart Mode:** Runs setup if unconfigured, starts bot if stopped, prints live status. |
| `./minibot.sh start` | Starts Minibot in the background (daemon mode) with verified PID tracking. |
| `./minibot.sh stop` | Gracefully terminates Minibot via `SIGTERM` (5s timeout with `SIGKILL` fallback). |
| `./minibot.sh restart` | Gracefully stops and restarts the bot in the background. |
| `./minibot.sh status` | Prints running status, verified PID, uptime, watchdog state, and recent log tail. |
| `./minibot.sh logs` | Follows live rotating logs (`tail -f data/minibot.log`). |
| `./minibot.sh enable` | **Set & Forget Persistence:** Installs user crontab watchdog (`@reboot` & every 5 mins) and systemd user unit if available. |
| `./minibot.sh disable` | Uninstalls auto-restart watchdog entries from crontab and systemd. |
| `./minibot.sh watchdog` | Headless auto-healing watchdog check invoked by cron (exits silently if running). |
| `./minibot.sh setup` | Reruns the interactive configuration wizard (`./setup-lite.sh`). |

---

## 💬 Zero-SSH In-Chat Admin Commands

Once Minibot is running, configured bot administrators (`ADMIN_NICKS` in `config.json`) can manage channels and plugins directly from IRC without ever needing SSH access:

| IRC Command | Description |
| :--- | :--- |
| `!join <#channel>` | Joins `<#channel>` immediately **and persists** it to `config.json` so it is rejoined on restarts. |
| `!part [#channel]` | Parts the specified (or current) channel and removes it from `config.json`. |
| `!reload` (or `!minibot reload`) | Live reloads `config.json` and hot-reloads all plugins in `sideload/` without disconnecting. |
| `!botstatus` | Displays uptime, Python runtime version, TLS mode, active channels, and loaded plugin count. |

---

## 📥 Automatic Sopel Config & Plugin Migration

Migrating an existing Sopel installation to Minibot Lite requires zero manual editing:
1. **Interactive Detection:** `./setup-lite.sh` or `./minibot.sh` automatically detects existing Sopel configurations (`~/.sopel/default.cfg` or `~/.config/sopel/default.cfg`) and asks if you wish to import them.
2. **One-Command CLI Import:**
   ```bash
   python3 run_bot.py --import-sopel
   # Or with explicit path:
   python3 run_bot.py --import-sopel /path/to/default.cfg
   ```
   This automatically converts `host`, `port`, `ssl`, `nick`, `owner`, `channels`, and `nickserv_password` to `config.json` (`chmod 600`) and copies any custom `.py` plugins from your Sopel plugins directory to `sideload/`.

---

## 🛡️ Built-in Features & Media Previews (100% Python Stdlib)

Minibot Lite ships with batteries included:

### Link & Media Previews (Zero External Dependencies)
- **Automatic Web URL Previews:** Extracts HTML page titles with gzip/deflate decompression, automatic charset detection, and OpenGraph / Twitter Card fallback. Handles redirects across domains cleanly without HTTP 400 errors.
- **Rich YouTube Previews:** Detailed desktop, mobile (`m.youtube.com`), `youtu.be`, and Shorts summaries displaying video title, channel, duration, and view count.
- **Media & File Downloads:** Non-HTML files (PDFs, images, archives) report content type and file size (e.g., `🔗 [File] application/pdf (2.4 MB)`).

### Informational & Search Utilities
- `!wiki <topic>` — Searches Wikipedia and extracts a clean, factual summary.
- `!grok <topic>` — Searches the Grokipedia knowledge base.
- `!tr [lang] <text>` — Multi-language translation (e.g. `!tr de Good morning`, `!tr es Hello`).
- `!w <location>` — Real-time weather conditions and forecasts via wttr.in.
- `!time <location>` — UTC and world timezone clock lookups.
- `!ddg <query>` — Fast DuckDuckGo web searches.
- `!hn <query>` — Hacker News search and top discussions.
- `!gh <owner/repo>` — GitHub repository overview and metrics.
- `!r <subreddit>` — Top posts from Reddit.

### Diagnostics & Developer Tools
- `!calc <expr>` — Safe arithmetic and mathematical evaluator.
- `!hex <text>` / `!unhex <hex>` — Hexadecimal encoder and decoder.
- `!b64 <text>` / `!unb64 <b64>` — Base64 encoder and decoder.
- `!hash <text>` — MD5, SHA1, and SHA256 checksum generator.
- `!dig <host> [A|AAAA]` — DNS record lookup.
- `!ssl <host>` — SSL/TLS certificate expiry and issuer inspection.
- `!whois <ip|domain>` — WHOIS and RDAP network ownership lookup.

---

## 🔌 Sopel Plugin Compatibility Shim

Minibot Lite includes a native in-memory Sopel compatibility shim (`adapters/sopel_shim.py`) supporting both legacy (`from sopel import module`) and modern (`from sopel import plugin`) import styles.

To add custom plugins:
1. Drop `.py` files into `sideload/`:
   ```bash
   cp ~/my_sopel_plugin.py sideload/
   ```
2. The automatic `mtime` file watcher detects the file and loads it live without disconnecting from IRC!

Supported Sopel decorators and attributes include:
- `@module.commands("cmd1", "cmd2")` / `@plugin.command("cmd")`
- `@module.rule(r"regex")` / `@plugin.find(...)` / `@plugin.search(...)` / `@plugin.url(...)`
- `@module.interval(seconds)`
- `@module.require_admin`, `@module.require_chanmsg`, `@module.require_privmsg`, `@module.require_chan("#chan")`
- `bot.say(text)`, `bot.reply(text)`, `bot.action(text)`, `bot.notice(text)`, `bot.memory`
- `trigger.nick`, `trigger.sender`, `trigger.hostmask`, `trigger.user`, `trigger.host`, `trigger.group(n)`, `trigger.admin`

---

## 🔒 Self-Healing & Legacy OS Resilience

- **Disk Safety (Rotating Logs):** Logs are capped at 5 MB with 2 rotating backups (`data/minibot.log`), preventing runaway disk consumption on small SSDs or CompactFlash cards.
- **Zombie Socket & Half-Open Drop Detection:** Socket keepalive sends `PING :keepalive` after 120s of inactivity; if no response is received within 180s, the socket is recycled and reconnects with exponential backoff.
- **NickServ Collision Recovery:** Automatically joins under `<nick>_` on `433 ERR_NICKNAMEINUSE`, sends `GHOST <nick> <pass>`, reclaims `<nick>`, and identifies.
- **Frozen-Lib Mode (`python3 -I run_bot.py`):** Runs isolated from broken or mismatched global system `site-packages`.
- **Resilient 3-Tier HTTPS / OpenSSL Fallback:** Automatically handles expired root CA certificates and old OpenSSL versions (1.0.2 / 1.1.1) via relaxed ciphers and fallback to `curl -sSLk`.
- **Drop-in `requests` Shim:** Standard-library mock is provided if third-party plugins import `requests`.
