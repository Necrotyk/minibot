# SPDX-License-Identifier: AGPL-3.0-or-later
"""
lertz_threat_intel.py — Dual-Tiered IBM Granite Threat Intelligence Sideload.
Connects the local Raspberry Pi 5 IBM Granite engine with native PostgreSQL Lertz telemetry
(ids_events, ids_ip_reputation, ids_active_bans, lertz_alerts) for deep actor profiling,
campaign detection, MITRE ATT&CK mapping, and rapid cybernetic triage.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    from sopel import module
except ImportError:
    try:
        from adapters.sopel_shim import inject_sopel_shim
    except ImportError:
        from minibots.adapters.sopel_shim import inject_sopel_shim
    inject_sopel_shim()
    from sopel import module

logger = logging.getLogger("LertzThreatIntel")

# Styling constants for cybernetic mIRC formatting
C_TEAL = "\x0310"
C_CYAN = "\x0311"
C_GREY = "\x0314"
C_RED = "\x0304"
C_GREEN = "\x0303"
C_YELLOW = "\x0308"
C_BOLD = "\x02"
C_RESET = "\x0f"

# LLM Endpoints for CTI Analyst Worker
CTI_ANALYST_URL = os.environ.get("CTI_ANALYST_URL", os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8080")).rstrip("/")
PI5_AGENT_URL = CTI_ANALYST_URL
PI5_SECOPS_URL = CTI_ANALYST_URL

# Per-node inference serialization semaphore to prevent Cortex-A76 CPU/RAM contention
GRANITE_SEMAPHORE = asyncio.Semaphore(1)

# Anti-spam cooldown cache: (scope, nick) -> last_request_monotonic
_USER_COOLDOWN: Dict[Tuple[str, str], float] = {}
COOLDOWN_SECONDS = 5.0


def _is_cooldown_active(scope: str, nick: str) -> bool:
    now = time.monotonic()
    key = (scope.lower(), nick.lower())
    last = _USER_COOLDOWN.get(key, 0.0)
    if now - last < COOLDOWN_SECONDS:
        return True
    _USER_COOLDOWN[key] = now
    return False


class LertzDBReader:
    """Zero-trust, engine-enforced read-only PostgreSQL reader for Lertz telemetry."""

    def __init__(self, dsn: str = "dbname=lertz user=minibots_reader sslmode=disable"):
        self.dsn = dsn

    def _get_connection(self):
        if not psycopg:
            raise RuntimeError("psycopg is not installed. Run 'sudo apt-get install python3-psycopg'.")
        return psycopg.connect(self.dsn, connect_timeout=5)

    def get_ip_timeline(self, ip: str, limit: int = 40) -> Dict[str, Any]:
        """Retrieves and aggregates forensic trajectory for a specific IP address."""
        clean_ip = ip.strip()
        data: Dict[str, Any] = {
            "ip": clean_ip,
            "events_count": 0,
            "first_seen": None,
            "last_seen": None,
            "country": "",
            "asn": "",
            "event_types": {},
            "targeted_paths": [],
            "summaries": [],
            "reputation": {},
            "active_ban": None,
            "threat_score": 0.0,
        }

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    # 1. Fetch recent events
                    cur.execute("""
                        SELECT timestamp, event_type, source, country, asn, threat_score, summary, enriched_json
                        FROM ids_events
                        WHERE ip_address = %s
                        ORDER BY timestamp DESC
                        LIMIT %s;
                    """, (clean_ip, limit))
                    rows = cur.fetchall()

                    if rows:
                        data["events_count"] = len(rows)
                        data["last_seen"] = rows[0][0]
                        data["first_seen"] = rows[-1][0]
                        data["country"] = rows[0][3] or ""
                        data["asn"] = rows[0][4] or ""
                        data["threat_score"] = float(rows[0][5] or 0.0)

                        type_counts: Dict[str, int] = {}
                        paths: List[str] = []
                        summaries: List[str] = []

                        for r in rows:
                            ev_type = r[1] or r[2] or "unknown"
                            type_counts[ev_type] = type_counts.get(ev_type, 0) + 1
                            summ = r[6] or ""
                            if summ and summ not in summaries and len(summaries) < 6:
                                summaries.append(summ)

                            raw_json = r[7] or ""
                            if raw_json:
                                try:
                                    j = json.loads(raw_json)
                                    path = j.get("path") or j.get("request_uri") or j.get("url")
                                    if path and path not in paths and len(paths) < 10:
                                        paths.append(str(path))
                                except Exception:
                                    pass

                        data["event_types"] = type_counts
                        data["targeted_paths"] = paths
                        data["summaries"] = summaries

                    # 2. Check IP reputation table
                    cur.execute("""
                        SELECT country, asn, is_vpn, is_tor, threat_score, shodan_summary, event_count, last_seen
                        FROM ids_ip_reputation
                        WHERE ip = %s
                        LIMIT 1;
                    """, (clean_ip,))
                    rep = cur.fetchone()
                    if rep:
                        data["reputation"] = {
                            "country": rep[0],
                            "asn": rep[1],
                            "is_vpn": bool(rep[2]),
                            "is_tor": bool(rep[3]),
                            "threat_score": float(rep[4] or 0.0),
                            "shodan_summary": rep[5] or "",
                            "total_events": rep[6],
                            "last_seen": rep[7],
                        }
                        if not data["country"] and rep[0]:
                            data["country"] = rep[0]
                        if not data["asn"] and rep[1]:
                            data["asn"] = rep[1]

                    # 3. Check active bans
                    cur.execute("""
                        SELECT scenarios, created_at, expires_at, origin
                        FROM ids_active_bans
                        WHERE ip = %s
                        LIMIT 1;
                    """, (clean_ip,))
                    ban = cur.fetchone()
                    if ban:
                        data["active_ban"] = {
                            "reason": ban[0] or "active ban",
                            "banned_at": ban[1],
                            "expires_at": ban[2],
                            "source": ban[3] or "ids",
                        }
        except Exception as e:
            logger.error(f"Error querying IP timeline for {clean_ip}: {e}")

        return data

    def get_campaign_clusters(self, hours: float = 24.0, limit: int = 150) -> Dict[str, Any]:
        """Aggregates multi-IP scan clusters, common targets, and synchronized activity."""
        data: Dict[str, Any] = {
            "hours": hours,
            "total_events": 0,
            "unique_ips": 0,
            "subnet_clusters": {},
            "top_asns": {},
            "top_paths": {},
            "active_ban_count": 0,
        }

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    # 1. Total events in window
                    cur.execute("""
                        SELECT ip_address, asn, summary, enriched_json
                        FROM ids_events
                        WHERE timestamp >= (EXTRACT(EPOCH FROM NOW()) - (%s * 3600))
                          AND ip_address IS NOT NULL AND ip_address != ''
                        ORDER BY timestamp DESC
                        LIMIT %s;
                    """, (hours, limit))
                    rows = cur.fetchall()

                    data["total_events"] = len(rows)
                    ip_set = set()
                    subnets: Dict[str, int] = {}
                    asns: Dict[str, int] = {}
                    paths: Dict[str, int] = {}

                    for r in rows:
                        ip_str = r[0]
                        asn_str = r[1] or "Unknown ASN"
                        ip_set.add(ip_str)

                        try:
                            ip_obj = ipaddress.ip_address(ip_str)
                            if ip_obj.version == 4:
                                net = str(ipaddress.ip_network(f"{ip_str}/24", strict=False))
                            else:
                                net = str(ipaddress.ip_network(f"{ip_str}/48", strict=False))
                            subnets[net] = subnets.get(net, 0) + 1
                        except Exception:
                            pass

                        if asn_str:
                            asns[asn_str] = asns.get(asn_str, 0) + 1

                        raw_json = r[3] or ""
                        if raw_json:
                            try:
                                j = json.loads(raw_json)
                                p = j.get("path") or j.get("request_uri") or j.get("url")
                                if p:
                                    paths[str(p)] = paths.get(str(p), 0) + 1
                            except Exception:
                                pass

                    data["unique_ips"] = len(ip_set)
                    data["subnet_clusters"] = dict(sorted(subnets.items(), key=lambda x: x[1], reverse=True)[:5])
                    data["top_asns"] = dict(sorted(asns.items(), key=lambda x: x[1], reverse=True)[:5])
                    data["top_paths"] = dict(sorted(paths.items(), key=lambda x: x[1], reverse=True)[:5])

                    # 2. Count active bans
                    cur.execute("SELECT count(*) FROM ids_active_bans;")
                    ban_row = cur.fetchone()
                    if ban_row:
                        data["active_ban_count"] = ban_row[0]

        except Exception as e:
            logger.error(f"Error querying campaign clusters: {e}")

        return data

    def get_cve_details(self, cve_id: str) -> Optional[Dict[str, Any]]:
        """Cross-references a CVE identifier against local alerts and probe signatures."""
        clean_id = cve_id.strip().upper()
        res: Optional[Dict[str, Any]] = None

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT cve_id, title, cvss_score, epss_score, vendor, product, affected_versions, description
                        FROM lertz_alerts
                        WHERE UPPER(cve_id) = %s
                        ORDER BY id DESC
                        LIMIT 1;
                    """, (clean_id,))
                    row = cur.fetchone()

                    if row:
                        res = {
                            "cve_id": row[0],
                            "title": row[1],
                            "cvss_score": float(row[2] or 0.0),
                            "epss_score": float(row[3] or 0.0) if row[3] is not None else None,
                            "vendor": row[4] or "",
                            "product": row[5] or "",
                            "affected_versions": row[6] or "",
                            "description": row[7] or "",
                            "local_probes_detected": 0,
                        }

                        # Check if any attack signatures or events reference this CVE
                        cur.execute("""
                            SELECT count(*) FROM ids_events
                            WHERE summary ILIKE %s OR enriched_json ILIKE %s;
                        """, (f"%{clean_id}%", f"%{clean_id}%"))
                        ev_count = cur.fetchone()
                        if ev_count:
                            res["local_probes_detected"] = ev_count[0]
        except Exception as e:
            logger.error(f"Error querying CVE details for {clean_id}: {e}")

        return res

    def get_24h_telemetry_digest(self, hours: float = 24.0) -> Dict[str, Any]:
        """Provides high-level technical metrics for natural language and brief generation."""
        return self.get_campaign_clusters(hours=hours, limit=200)


# Instantiate default DB reader
DB_READER = LertzDBReader()


def build_ip_context(timeline: Dict[str, Any]) -> str:
    """Pre-aggregates raw IP data into a dense structured summary (~120 tokens)."""
    ip = timeline["ip"]
    cnt = timeline["events_count"]
    country = timeline.get("country") or "Unknown"
    asn = timeline.get("asn") or "Unknown"
    score = timeline.get("threat_score", 0.0)

    ev_types = ", ".join(f"{k}:{v}" for k, v in timeline.get("event_types", {}).items()) or "none"
    paths = ", ".join(timeline.get("targeted_paths", [])[:6]) or "none"
    summaries = " | ".join(timeline.get("summaries", [])[:3]) or "none"

    ban_info = "None"
    if timeline.get("active_ban"):
        b = timeline["active_ban"]
        ban_info = f"ACTIVE ({b.get('reason', 'banned')})"

    rep_info = "None"
    if timeline.get("reputation"):
        r = timeline["reputation"]
        rep_info = f"Score:{r.get('threat_score')} Tor:{r.get('is_tor')} VPN:{r.get('is_vpn')}"

    return (
        f"Target IP: {ip}\n"
        f"Location: {country} | ASN: {asn} | Threat Score: {score}\n"
        f"Events ({cnt} hits): {ev_types}\n"
        f"Probed Paths: {paths}\n"
        f"Sample Log Traces: {summaries}\n"
        f"Reputation: {rep_info} | Active Ban: {ban_info}"
    )


def build_campaign_context(camp: Dict[str, Any]) -> str:
    """Pre-aggregates multi-IP campaign activity (~140 tokens)."""
    hrs = camp["hours"]
    tot = camp["total_events"]
    uips = camp["unique_ips"]
    bans = camp["active_ban_count"]

    subnets = ", ".join(f"{k} ({v} hits)" for k, v in camp.get("subnet_clusters", {}).items()) or "none"
    asns = ", ".join(f"{k} ({v})" for k, v in camp.get("top_asns", {}).items()) or "none"
    paths = ", ".join(f"{k} ({v})" for k, v in camp.get("top_paths", {}).items()) or "none"

    return (
        f"Window: Past {int(hrs)} Hours\n"
        f"Volume: {tot} events across {uips} unique IPs | Active Bans: {bans}\n"
        f"Concentrated Subnets: {subnets}\n"
        f"Top Hostile ASNs: {asns}\n"
        f"Top Probed Endpoints: {paths}"
    )


async def execute_granite_inference(
    prompt: str,
    system_role: str = "general",
    use_8b: bool = False,
    timeout: float = 40.0
) -> Tuple[bool, str]:
    """Sends structured prompt to IBM Granite on Pi 5 ARM CPU, handling fallback and timeouts."""
    if not httpx:
        return False, "httpx library is missing on server."

    model_alias = "granite-8b" if use_8b else "granite"
    expected_timeout = 120.0 if use_8b else timeout

    system_prompts = {
        "actor": (
            "You are the Dystopi Cybernetic Threat Analyst powered by IBM Granite. "
            "Analyze the pre-aggregated telemetry facts for the suspect IP. "
            "Provide an executive evaluation with: 1) Threat Actor Tier (e.g. Commodity Scanner, Automated Botnet, Targeted Hunter), "
            "2) Tool Fingerprint (e.g. Nuclei, ZGrab, sqlmap, custom script), 3) Attack Objective, 4) Defensive Status. "
            "Strictly concise: maximum 2 to 3 lines. Avoid filler text. Output factual technical security analysis."
        ),
        "campaign": (
            "You are the Dystopi Cybernetic Threat Analyst powered by IBM Granite. "
            "Analyze the multi-IP cluster telemetry facts. "
            "Identify: 1) Coordinated scan campaigns, 2) Common TTPs or exploit targets across subnets, "
            "3) Defensive containment status. Strictly concise: maximum 2 to 3 lines."
        ),
        "mitre": (
            "You are the Dystopi Cybernetic Threat Analyst powered by IBM Granite. "
            "Map the observed IP probe trajectory to official MITRE ATT&CK Tactics & Techniques (e.g. T1190, T1595, T1078). "
            "List specific techniques and brief defense advice. Strictly concise: maximum 2 lines."
        ),
        "cve": (
            "You are the Dystopi Cybernetic Threat Analyst powered by IBM Granite. "
            "Assess the CVE impact against local Dystopi systems (Debian Linux, Nginx L7, pure nftables, OpenSSH on wg0 only). "
            "State local exposure level, observed exploit probes, and mitigation status. Strictly concise: maximum 2 lines."
        ),
        "ask": (
            "You are the Dystopi Cybernetic Threat Analyst powered by IBM Granite. "
            "Answer the operator's question using the provided 24h telemetry facts. "
            "Answer factually and concisely in 1 to 2 lines for an IRC SecOps team."
        )
    }

    sys_text = system_prompts.get(system_role, system_prompts["actor"])

    messages = [
        {"role": "system", "content": sys_text},
        {"role": "user", "content": prompt}
    ]

    payload = {
        "model": model_alias,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 220,
        "stream": False
    }

    urls = [f"{PI5_AGENT_URL}/v1/chat/completions", f"{PI5_SECOPS_URL}/v1/chat/completions"]

    async with GRANITE_SEMAPHORE:
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(expected_timeout, connect=2.5)) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            content = choices[0].get("message", {}).get("content", "").strip()
                            # Strip thinking tags if model outputs <think>...</think>
                            clean = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                            return True, clean or content
            except Exception as e:
                logger.debug(f"Granite inference error on {url}: {e}")

    return False, "Failed connecting to Pi 5 Granite worker over WireGuard mesh."


def format_irc_output(header_badge: str, text: str, engine_tag: str = "3B") -> List[str]:
    """Wraps lines to 380 bytes and applies cybernetic color formatting (strictly max 4 lines)."""
    tag = f"{C_TEAL}{C_BOLD}[{header_badge}·{engine_tag}]{C_RESET}"
    lines = text.strip().splitlines()
    clean_lines = [l.strip() for l in lines if l.strip()]

    if not clean_lines:
        return [f"{tag} No analysis content generated."]

    out: List[str] = []
    first = True
    for l in clean_lines:
        prefix = f"{tag} " if first else f"  {C_GREY}↳{C_RESET} "
        first = False
        avail = 370 - len(prefix)
        if len(l) > avail:
            chunk = l[:avail]
            out.append(f"{prefix}{chunk}")
        else:
            out.append(f"{prefix}{l}")
        if len(out) >= 4:
            break

    return out[:4]


async def handle_granite_command(
    send_msg_cb: Any,
    target: str,
    sender: str,
    args: List[str],
    is_admin: bool = False
) -> None:
    """Core router for !granite and !gr subcommands."""
    if _is_cooldown_active(target, sender) and not is_admin:
        send_msg_cb(target, f"{C_RED}{C_BOLD}[GRANITE]{C_RESET} Cooldown active (5s limit). Please wait.")
        return

    if not args:
        send_msg_cb(
            target,
            f"🛡️ {C_TEAL}{C_BOLD}[GRANITE·CTI]{C_RESET} Usage: !gr <actor|campaign|mitre|triage|cve|brief|ask|status|switch> [args] [--8b]"
        )
        return

    sub = args[0].lower().strip()
    sub_args = args[1:]
    use_8b = "--8b" in sub_args or "--deep" in sub_args
    sub_args = [a for a in sub_args if a not in ("--8b", "--deep")]

    engine_label = "8B" if use_8b else "3B"

    # --- 1. Status Command ---
    if sub in ("status", "st", "ping", "info"):
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE·Status]{C_RESET} Checking Pi 5 worker connectivity...")
        try:
            from sideload.dai import get_active_model_info, PI5_BASE_URL
            alias, name = await get_active_model_info()
            send_msg_cb(
                target,
                f"🧠 {C_TEAL}{C_BOLD}[GRANITE·Status]{C_RESET} Endpoint: {C_BOLD}{CTI_ANALYST_URL}{C_RESET} | "
                f"Active Model: {C_CYAN}{name}{C_RESET} ({alias}) | Ready for CTI analysis."
            )
        except Exception as e:
            send_msg_cb(target, f"🧠 {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} Worker check failed: {e}")
        return

    # --- 2. Model Switch Command ---
    if sub in ("switch", "swap", "model"):
        target_engine = sub_args[0].lower() if sub_args else ("8b" if not use_8b else "3b")
        canon = "granite-8b" if target_engine in ("8b", "8", "deep") else "granite"
        # If 8b requested, check if dai has granite or granite-8b
        model_name = "Granite 4.1 8B" if target_engine in ("8b", "8", "deep") else "Granite 4.1 3B"
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE·Model]{C_RESET} Switching Pi 5 worker to {model_name}... please wait.")
        try:
            from sideload.dai import switch_pi_model
            switch_key = canon
            ok, msg = await switch_pi_model(switch_key)
            if ok:
                send_msg_cb(target, f"🧠 {C_GREEN}{C_BOLD}[GRANITE·Model]{C_RESET} {msg}")
            else:
                send_msg_cb(target, f"🧠 {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {msg}")
        except Exception as e:
            send_msg_cb(target, f"🧠 {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} Model switch failed: {e}")
        return

    # --- 3. Actor Profiling ---
    if sub in ("actor", "profile", "investigate"):
        if not sub_args:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Usage: !gr actor <IP> [--8b]")
            return
        ip = sub_args[0].strip()
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Analyzing IP {ip} against Lertz telemetry ({engine_label})...")

        tl = DB_READER.get_ip_timeline(ip)
        if tl["events_count"] == 0 and not tl.get("reputation") and not tl.get("active_ban"):
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE·Actor]{C_RESET} No historical telemetry or attack signatures recorded for {ip}.")
            return

        ctx = build_ip_context(tl)
        ok, res = await execute_granite_inference(ctx, system_role="actor", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:Actor", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    # --- 4. Triage Decision ---
    if sub in ("triage", "verdict", "check"):
        if not sub_args:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Usage: !gr triage <IP> [--8b]")
            return
        ip = sub_args[0].strip()
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Running triage evaluation for {ip} ({engine_label})...")

        tl = DB_READER.get_ip_timeline(ip)
        if tl["events_count"] == 0 and not tl.get("reputation") and not tl.get("active_ban"):
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE·Triage]{C_RESET} {ip}: ZERO_TRAFFIC (No events in database).")
            return

        ctx = f"Perform rapid triage verdict on this IP:\n{build_ip_context(tl)}"
        ok, res = await execute_granite_inference(ctx, system_role="actor", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:Triage", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    # --- 5. MITRE ATT&CK Mapping ---
    if sub in ("mitre", "attack", "ttps"):
        if not sub_args:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Usage: !gr mitre <IP> [--8b]")
            return
        ip = sub_args[0].strip()
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Mapping {ip} probes to MITRE ATT&CK matrix ({engine_label})...")

        tl = DB_READER.get_ip_timeline(ip)
        if tl["events_count"] == 0:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE·MITRE]{C_RESET} No probes recorded for {ip}.")
            return

        ctx = f"Map observed traces to MITRE ATT&CK Tactics & Techniques:\n{build_ip_context(tl)}"
        ok, res = await execute_granite_inference(ctx, system_role="mitre", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:MITRE", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    # --- 6. Campaign Clustering ---
    if sub in ("campaign", "clusters", "cluster"):
        hours = 24.0
        if sub_args:
            try:
                hours = float(sub_args[0])
            except ValueError:
                pass
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Analyzing {int(hours)}h telemetry for campaign clusters ({engine_label})...")

        camp = DB_READER.get_campaign_clusters(hours=hours)
        if camp["total_events"] == 0:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE·Campaign]{C_RESET} Zero telemetry events recorded in the past {int(hours)}h.")
            return

        ctx = build_campaign_context(camp)
        ok, res = await execute_granite_inference(ctx, system_role="campaign", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:Campaign", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    # --- 7. CVE Cross-Reference ---
    if sub in ("cve", "vuln"):
        if not sub_args:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Usage: !gr cve <CVE-ID> [--8b]")
            return
        cve_id = sub_args[0].strip().upper()
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Evaluating impact of {cve_id} against local posture ({engine_label})...")

        cve = DB_READER.get_cve_details(cve_id)
        if not cve:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE·CVE]{C_RESET} No advisory found for {cve_id} in Lertz alerts database.")
            return

        ctx = (
            f"Vulnerability: {cve['cve_id']} ({cve['title']})\n"
            f"Severity CVSS: {cve['cvss_score']} | EPSS: {cve.get('epss_score') or 'N/A'}\n"
            f"Target Software: {cve.get('vendor')}:{cve.get('product')} (Affects: {cve.get('affected_versions') or 'all'})\n"
            f"Description: {cve.get('description', '')[:220]}\n"
            f"Inbound Exploit Probes Detected: {cve.get('local_probes_detected', 0)}"
        )
        ok, res = await execute_granite_inference(ctx, system_role="cve", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:CVE", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    # --- 8. Executive Brief ---
    if sub in ("brief", "digest", "summary"):
        hours = 24.0
        if sub_args:
            try:
                hours = float(sub_args[0])
            except ValueError:
                pass
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Synthesizing {int(hours)}h executive threat brief ({engine_label})...")

        camp = DB_READER.get_campaign_clusters(hours=hours)
        ctx = f"Synthesize executive threat brief for past {int(hours)} hours:\n{build_campaign_context(camp)}"
        ok, res = await execute_granite_inference(ctx, system_role="campaign", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:Brief", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    # --- 9. Natural Language Query ---
    if sub in ("ask", "q", "query"):
        if not sub_args:
            send_msg_cb(target, f"🛡️ {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Usage: !gr ask <technical question> [--8b]")
            return
        query = " ".join(sub_args).strip()
        send_msg_cb(target, f"🧠 {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Reasoning across Lertz telemetry for query ({engine_label})...")

        camp = DB_READER.get_campaign_clusters(hours=24.0)
        ctx = f"Telemetry facts for the past 24 hours:\n{build_campaign_context(camp)}\n\nOperator Question: {query}"
        ok, res = await execute_granite_inference(ctx, system_role="ask", use_8b=use_8b)
        if ok:
            for l in format_irc_output("GRANITE:Ask", res, engine_label):
                send_msg_cb(target, l)
        else:
            send_msg_cb(target, f"🛡️ {C_RED}{C_BOLD}[GRANITE·Error]{C_RESET} {res}")
        return

    send_msg_cb(
        target,
        f"❓ {C_TEAL}{C_BOLD}[GRANITE]{C_RESET} Unknown subcommand '{sub}'. Available: actor, campaign, mitre, triage, cve, brief, ask, status, switch."
    )


# --- Sopel Sideload Plugin Hooks for Spawned 'gr_dystopi' Bot ---

@module.commands("granite", "gr")
def sopel_granite(bot: Any, trigger: Any) -> None:
    """Sopel entrypoint for !granite and !gr commands on spawned bots."""
    args = (trigger.group(2) or "").split()
    target = trigger.sender
    sender = trigger.nick

    def send_cb(tgt: str, msg: str) -> None:
        if hasattr(bot, "say"):
            bot.say(msg, recipient=tgt)
        elif hasattr(bot, "send_msg"):
            bot.send_msg(tgt, msg)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        asyncio.create_task(handle_granite_command(send_cb, target, sender, args))
    else:
        loop.run_until_complete(handle_granite_command(send_cb, target, sender, args))


@module.commands("actor", "campaign", "mitre", "triage", "cve")
def sopel_direct_cti(bot: Any, trigger: Any) -> None:
    """Direct shorthand entrypoint when addressed directly to gr_dystopi."""
    cmd = trigger.group(1).lower()
    raw_args = (trigger.group(2) or "").split()
    full_args = [cmd] + raw_args
    target = trigger.sender
    sender = trigger.nick

    def send_cb(tgt: str, msg: str) -> None:
        if hasattr(bot, "say"):
            bot.say(msg, recipient=tgt)
        elif hasattr(bot, "send_msg"):
            bot.send_msg(tgt, msg)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        asyncio.create_task(handle_granite_command(send_cb, target, sender, full_args))
    else:
        loop.run_until_complete(handle_granite_command(send_cb, target, sender, full_args))
