# MINIBOTS — Modular Micro-Bot Framework Context & Agent Rules

> **License:** GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)
> **Maintainer:** Necrotyk — <https://dystopi.cc>
> **Repository:** <https://github.com/Necrotyk/minibots> (org mirror: <https://github.com/Dystopic-Research/minibots>)

---

## Overview

`minibots` is an autonomous micro-bot orchestration and universal sideloading daemon for the Dystopi ecosystem. It operates across the internal WireGuard network, Libera.Chat, and optional Matrix bridges.

---

## Key Architecture Constraints

### 1. Two-Tier Daemon Contract
- `minibots-conn.service` (`conn_daemon.py`) manages all persistent TCP/SSL sockets for primary and spawned minibots.
- `minibots.service` (`main.py`) houses the logic engine, background worker pool, and in-process hot-reloader (`!minibot reload` / `SIGHUP`).
- Reloading or restarting the logic engine causes **zero dropped IRC connections**.

### 2. Multi-Network Routing & Ring 0–4 Boundaries
- **Ring 0 (Admin Plane):** `#minibots` on local/private IRC network (`127.0.0.1:6667`) is the primary administrative control room.
- **Dedicated Coding Plane:** `#jules` is the interactive Google Jules autonomous coding workspace.
- **Ring 1–4 (Public IRC):** Public networks (Libera, OFTC) are constrained to safe, read-only utilities.

### 3. Adaptive Rate Limiting & Loop Defense
- **Flood Control:** Fast unthrottled burst (0.05s) on WireGuard vs 1.5s delay / 4-line burst on public networks, 380-byte line boundary capping.
- **Loop Prevention:** Central `BotRegistry` automatically suppresses cross-minibot conversational recursion.

### 4. Universal Sideloading & Zero-Dependency Shims
- On-demand discovery in `sideload/` with `mtime` hot-reloading and AST pre-flight validation.
- Built-in zero-dependency shims for **Sopel** (`@commands`, `@rule`, `@interval`) and **Limnoria / Supybot** in `sys.modules`.

### 5. Multi-Provider LLM Routing & Failover
- Dynamic routing across **Google Gemini API**, **local llama-server**, **Kaggle GPU Burst Workers** (`trycloudflare.com`), and **dystopi-mcp** IPC.
- Autonomous latency prober automatically fails over to Gemini API if the Kaggle GPU worker drops.

### 6. INV-9 Partitioned SQLite WAL Storage
- Partitioned databases under `/opt/minibots/data/`: `minibots_state.db`, `jules.db`, `user_prefs.db`, `telemetry.db`.
- Strictly enforced `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=30000; PRAGMA synchronous=NORMAL;` with connection rollback and `@sqlite_retry`.
