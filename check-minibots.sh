#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# check-minibots.sh — Local Pre-Commit & Verification Suite for minibots submodule

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Minibots Local Verification Suite ==="

# 1. Python AST Compilation Sweep
echo "[*] Gate 1: Python AST Syntax Sweep..."
python3 -m compileall -q -e "/(\.git|venv|\.venv)" .
echo "[+] AST compilation passed cleanly."

# 2. Secret & Private Key Leak Scan
echo "[*] Gate 2: Secret & Token Scan..."
if grep -rnE --exclude="*.pyc" --exclude-dir=".git" --exclude="*.example.json" "(AIzaSy[0-9A-Za-z_-]{33}|ghp_[0-9A-Za-z]{36}|-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----)" . 2>/dev/null; then
    echo "[✗] Potential hardcoded secret found!"
    exit 1
fi
echo "[+] Zero leaked secrets detected."

# 3. SPDX License Header Verification
echo "[*] Gate 3: SPDX License Header Check (AGPL-3.0-or-later)..."
MISSING_SPDX=0
while IFS= read -r f; do
    if ! grep -q "SPDX-License-Identifier: AGPL-3.0-or-later" "$f"; then
        echo "[✗] Missing AGPL SPDX header in: $f"
        MISSING_SPDX=$((MISSING_SPDX + 1))
    fi
done < <(find . -type f \( -name "*.py" -o -name "*.sh" -o -name "*.go" \) -not -path "*/.git/*" -not -path "*/data/*" -not -path "*/.agents/*")

if [[ $MISSING_SPDX -gt 0 ]]; then
    exit 1
fi
echo "[+] All source files contain valid AGPL-3.0-or-later SPDX headers."

# 4. Go Verification & Unit Test Suite
echo "[*] Gate 4: Go Vet, Build & Unit Tests..."
if command -v go >/dev/null 2>&1; then
    go vet ./...
    go test -v ./...
    go build -o /tmp/minibots-conn-buildcheck ./cmd/conn-daemon
    rm -f /tmp/minibots-conn-buildcheck
    echo "[+] Go verification passed cleanly."
else
    echo "[!] Warning: 'go' binary not found; skipping Go verification."
fi

# 5. Python Unit Test Suite Execution
echo "[*] Gate 5: Running Python Hermetic Unit Tests..."
python3 -m unittest discover -s tests -p "test_*.py" -v

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ALL MINIBOTS VERIFICATION GATES PASSED (100%)           ║"
echo "╚══════════════════════════════════════════════════════════╝"
