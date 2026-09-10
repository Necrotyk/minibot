#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_live_model_switch.py — Live IRC Verification of Model Switching & Auto-Switch ChatOps.
Target: irc.libera.chat:6697 (SSL)
Channel: #test-bot ONLY
Execution Host: Local or Staging Test Runner
"""

import os
import socket
import ssl
import sys
import time
from typing import Any, Dict, List

HOST = os.environ.get("IRC_TEST_HOST", "irc.libera.chat")
PORT = int(os.environ.get("IRC_TEST_PORT", "6697"))
CHANNEL = os.environ.get("IRC_TEST_CHANNEL", "#test-bot")


def run_irc_session(nick: str, steps: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print(f"\n[*] Connecting to {HOST}:{PORT} (SSL) as '{nick}' targeting {CHANNEL}...")
    results: Dict[str, List[str]] = {s["id"]: [] for s in steps}

    with socket.create_connection((HOST, PORT), timeout=150.0) as sock:
        with ctx.wrap_socket(sock) as ssock:
            def send(line: str):
                ssock.sendall((line + "\r\n").encode("utf-8"))

            send(f"NICK {nick}")
            send(f"USER {nick} 0 * :DAI Model Switch Tester")

            ssock.settimeout(0.5)
            buffer = ""
            joined = False
            step_idx = -1
            step_start = 0.0
            overall_start = time.time()

            while time.time() - overall_start < 150.0:
                try:
                    data = ssock.recv(4096)
                    if not data:
                        break
                    buffer += data.decode("utf-8", errors="replace")
                    lines = buffer.split("\r\n")
                    buffer = lines.pop()
                except socket.timeout:
                    lines = []

                for line in lines:
                    if line.startswith("PING"):
                        send(f"PONG {line.split()[1]}")
                        continue

                    parts = line.split()
                    if len(parts) >= 2 and parts[1] in ("001", "376", "422") and not joined:
                        print(f"[+] IRC Registered as {nick}. Joining {CHANNEL}...")
                        send(f"JOIN {CHANNEL}")
                        joined = True
                        continue

                    if joined and step_idx == -1 and len(parts) >= 2 and parts[1] == "366":
                        print(f"[+] Joined {CHANNEL}. Starting test steps...")
                        time.sleep(1.5)
                        step_idx = 0
                        st = steps[step_idx]
                        print(f"\n[>>> Step {step_idx+1}/{len(steps)}] {st['id']}: {st['cmd']}")
                        send(f"PRIVMSG {CHANNEL} :{st['cmd']}")
                        step_start = time.time()
                        continue

                    if len(parts) >= 4 and parts[1] == "PRIVMSG":
                        sender = parts[0].split("!")[0].lstrip(":")
                        target = parts[2]
                        msg = " ".join(parts[3:]).lstrip(":")

                        if target.lower() == CHANNEL.lower() and sender.lower() == "toolz":
                            print(f"[{target}] <{sender}> {msg}")
                            if 0 <= step_idx < len(steps):
                                st = steps[step_idx]
                                results[st["id"]].append(msg)

                if 0 <= step_idx < len(steps):
                    st = steps[step_idx]
                    elapsed = time.time() - step_start
                    msgs = results[st["id"]]

                    done = False
                    if msgs:
                        if "expect" in st and any(st["expect"].lower() in m.lower() for m in msgs):
                            done = True
                        elif "expect" not in st and len(msgs) >= st.get("min_msgs", 1):
                            done = True

                    if done or elapsed >= st.get("timeout", 15.0):
                        if not done:
                            print(f"[-] Step {st['id']} timed out after {round(elapsed, 1)}s")
                        else:
                            print(f"[+] Step {st['id']} completed in {round(elapsed, 1)}s")

                        step_idx += 1
                        if step_idx < len(steps):
                            time.sleep(1.5)
                            st = steps[step_idx]
                            print(f"\n[>>> Step {step_idx+1}/{len(steps)}] {st['id']}: {st['cmd']}")
                            send(f"PRIVMSG {CHANNEL} :{st['cmd']}")
                            step_start = time.time()
                        else:
                            print("\n[+] All steps in session completed.")
                            send("QUIT :Finished tests")
                            return results

            send("QUIT :Timeout")
    return results


def main():
    print("================================================================")
    print("🚀 DAI Live Model Switching & Auto-Switch Verification Battery")
    print(f"Server: {HOST}:{PORT} (SSL) | Channel: {CHANNEL}")
    print("================================================================")

    # -------------------------------------------------------------
    # PHASE 1: Non-Admin Permissions & Model Hub Listing
    # -------------------------------------------------------------
    unauth_nick = f"testusr_{int(time.time()) % 10000}"
    phase1_steps = [
        {
            "id": "P1_LIST_MODELS",
            "cmd": ".models",
            "expect": "[DAI·Models]",
            "timeout": 12.0,
        },
        {
            "id": "P2_UNAUTH_SWITCH_BLOCKED",
            "cmd": ".model llama",
            "expect": "Permission denied",
            "timeout": 10.0,
        },
        {
            "id": "P3_UNAUTH_AUTO_BLOCKED",
            "cmd": ".model auto on",
            "expect": "Permission denied",
            "timeout": 10.0,
        },
        {
            "id": "P4_UNAUTH_AUTO_STATUS_OK",
            "cmd": ".model auto",
            "expect": "Auto-switch is currently",
            "timeout": 10.0,
        },
    ]

    p1_results = run_irc_session(unauth_nick, phase1_steps)
    time.sleep(2.0)

    # -------------------------------------------------------------
    # PHASE 2: Admin Model Switching & Dynamic Introspection
    # -------------------------------------------------------------
    admin_nick = os.environ.get("ADMIN_NICK", "admin")
    phase2_steps = [
        {
            "id": "P5_ADMIN_SWITCH_LLAMA",
            "cmd": ".model llama",
            "expect": "llama",
            "timeout": 25.0,
        },
        {
            "id": "P6_VERIFY_LLAMA_ACTIVE",
            "cmd": ".dai what model are you operating as",
            "expect": "llama",
            "timeout": 15.0,
        },
        {
            "id": "P7_ADMIN_SWITCH_LFM",
            "cmd": ".model lfm",
            "expect": "LFM",
            "timeout": 25.0,
        },
        {
            "id": "P8_VERIFY_LFM_ACTIVE",
            "cmd": ".dai what model are you operating as",
            "expect": "LFM",
            "timeout": 15.0,
        },
        {
            "id": "P9_ADMIN_AUTO_ENABLE",
            "cmd": ".model auto on",
            "expect": "Auto-switch enabled",
            "timeout": 10.0,
        },
        {
            "id": "P10_ADMIN_AUTO_DISABLE",
            "cmd": ".model auto off",
            "expect": "Auto-switch disabled",
            "timeout": 10.0,
        },
    ]

    p2_results = run_irc_session(admin_nick, phase2_steps)

    # -------------------------------------------------------------
    # VERIFICATION AUDIT
    # -------------------------------------------------------------
    print("\n================================================================")
    print("📊 LIVE VERIFICATION REPORT SUMMARY")
    print("================================================================")

    all_passed = True
    combined = {**p1_results, **p2_results}

    criteria = [
        ("P1_LIST_MODELS", lambda msgs: any("[DAI·Models]" in m for m in msgs)),
        ("P2_UNAUTH_SWITCH_BLOCKED", lambda msgs: any("Permission denied" in m for m in msgs)),
        ("P3_UNAUTH_AUTO_BLOCKED", lambda msgs: any("Permission denied" in m for m in msgs)),
        ("P4_UNAUTH_AUTO_STATUS_OK", lambda msgs: any("Auto-switch is currently" in m for m in msgs)),
        ("P5_ADMIN_SWITCH_LLAMA", lambda msgs: any("llama" in m.lower() for m in msgs)),
        ("P6_VERIFY_LLAMA_ACTIVE", lambda msgs: any("llama" in m.lower() for m in msgs)),
        ("P7_ADMIN_SWITCH_LFM", lambda msgs: any("lfm" in m.lower() for m in msgs)),
        ("P8_VERIFY_LFM_ACTIVE", lambda msgs: any("lfm" in m.lower() for m in msgs)),
        ("P9_ADMIN_AUTO_ENABLE", lambda msgs: any("Auto-switch enabled" in m for m in msgs)),
        ("P10_ADMIN_AUTO_DISABLE", lambda msgs: any("Auto-switch disabled" in m for m in msgs)),
    ]

    for cid, check_fn in criteria:
        msgs = combined.get(cid, [])
        passed = check_fn(msgs)
        status = "✅ PASS" if passed else "❌ FAIL"
        if not passed:
            all_passed = False
        print(f"{status} [{cid}] Messages: {msgs}")

    print("================================================================")
    if all_passed:
        print("🎉 ALL 10 LIVE IRC VERIFICATION TESTS PASSED SUCCESSFULLY!")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    main()
