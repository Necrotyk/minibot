# Contributing to Minibots

Thank you for contributing to Minibots! We welcome contributions, bug reports, and feature requests.

---

## Licensing Invariant (AGPL-3.0-or-later)

Minibots is licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**.
By contributing to this repository, you agree that your contributions will be licensed under the AGPLv3+.

- Every source file (`.py`, `.go`, `.sh`) must include:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  ```
- All dependencies must have permissive FLOSS licenses compatible with AGPLv3+ (e.g. MIT, Apache-2.0, BSD, GPL-3.0, AGPL-3.0).

---

## Local Development Workflow

### 1. Prerequisites
- Python 3.11 or newer
- Go 1.20 or newer
- Git

### 2. Quickstart Environment Setup
Run the automated setup script to provision a virtual environment, install dependencies, and compile the Go connection broker:
```bash
./setup.sh
```

### 3. Pre-Commit Verification Gate
Before submitting any pull request or committing changes, all 5 gates in `./check-minibots.sh` must pass cleanly:
```bash
./check-minibots.sh
```
This script validates:
1. Python AST syntax across all modules.
2. Zero hardcoded secrets, private keys, or API tokens.
3. Strict AGPL-3.0-or-later SPDX header compliance.
4. Go toolchain checks (`go vet`, `go test`, and binary build).
5. 100% pass rate on hermetic Python unit tests.

---

## Coding Standards

- **Python:** PEP 8 style, explicit type annotations where practical, asynchronous I/O with `asyncio`.
- **Go:** Adherence to standard `go fmt` and `go vet` rules.
- **Security:** Never hardcode credentials, IP addresses, or secrets. Always ingest configuration via `config.json` or environment variables (`${VAR_NAME}`).
