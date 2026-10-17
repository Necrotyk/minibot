# Minibots — Agent Guidelines & Development Invariants

> **Project:** Minibots — High-Throughput 2-Tier ChatOps & CTI Bot Framework  
> **Licensing:** GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)  
> **Repository:** <https://github.com/Necrotyk/minibots>  

---

## 1. Project Overview & Architecture

Minibots is a modular, high-throughput ChatOps framework architected in two decoupled tiers:

1. **Tier 1 (Connection Daemon):** A lightweight Go daemon (`cmd/conn-daemon/`) that maintains persistent TLS socket connections to IRC networks, Matrix bridges, or chat relays with automatic exponential backoff reconnects and connection multiplexing.
2. **Tier 2 (Logic & Intelligence Daemon):** A Python 3 daemon (`minibots.py`, `core/`, `modules/`, `sideload/`) that handles command routing, AI inference pipelines (DAI), CTI threat intelligence feeds, plugin execution, and database persistence.
3. **IPC Boundary:** Communications between the connection daemon and logic daemon flow over a local UNIX domain stream socket (`data/minibots_ipc.sock`, configurable via `MINIBOTS_IPC_SOCK`) using newline-delimited JSON framing (`{"type": "...", ...}`).

---

## 2. Licensing Invariant (STRICT AGPL-3.0-or-later)

Minibots is strictly licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**.

- **Mandatory SPDX Headers:** Every source file (`.py`, `.go`, `.sh`) must begin with:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  ```
- **Copyleft Preservation:** Any modifications, plugins, extensions, or derivative works distributed or executed over a network must remain free and open source under AGPL-3.0-or-later. Closed-source proprietary forks are prohibited.
- **Dependency Licensing:** New dependencies must have compatible FLOSS licenses (MIT, Apache-2.0, BSD, GPL-3.0, AGPL-3.0). Incompatible proprietary or restrictive libraries are strictly forbidden.

---

## 3. Security & Anti-Leak Invariants

- **Zero Hardcoded Credentials:** NEVER hardcode API tokens, passwords, private keys, or webhook URLs in source code, tests, or documentation.
- **Configuration & Secrets Loading:** Secrets must be ingested dynamically via environment variables (`${VAR_NAME}`) or untracked `.env` / `config.json` files (which are strictly ignored by `.gitignore`).
- **File System Permissions:** Systemd service units and process masks must enforce `UMask=0027` to prevent world-writable or world-readable state.
- **Isolated SQLite Storage:** Local operational SQLite databases (`data/*.db`) must operate with WAL mode enabled and strict exception recovery.

---

## 4. Coding Standards & Conventions

- **Python:**
  - Python 3.11+ compliant, adhering to PEP 8 standards.
  - Asynchronous workflows must use native `asyncio` without blocking the main event loop.
  - Safe subprocess invocations: never execute `shell=True` with untrusted inputs.
  - Complete implementations: no generic `// TODO` or placeholder code.
- **Go:**
  - Go 1.20+ compliant, adhering to standard `go fmt` and `go vet` rules.
  - Hermetic builds: dependencies managed cleanly in `go.mod`.
- **Plugin Architecture:**
  - Dynamic sideload plugins in `sideload/` must inherit from base shims (`SopelBotWrapper`, `ErrbotShim`, etc.) and handle exceptions gracefully without crashing the core dispatcher.

---

## 5. Verification & Pre-Commit Gates

Before committing any code or creating pull requests, all 5 verification gates in `./check-minibots.sh` must pass cleanly (100%):

1. **Gate 1 (AST Syntax Sweep):** `python3 -m compileall -q .`
2. **Gate 2 (Secret & Token Scan):** Automated regex scan for API keys, private keys, and GitHub tokens.
3. **Gate 3 (SPDX License Header Check):** Enforces `SPDX-License-Identifier: AGPL-3.0-or-later` on all source files.
4. **Gate 4 (Go Toolchain Check):** `go vet ./...`, `go test ./...`, and hermetic binary compilation.
5. **Gate 5 (Hermetic Unit Tests):** `python3 -m unittest discover -s tests -p "test_*.py" -v`
