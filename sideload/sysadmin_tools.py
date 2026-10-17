# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
sysadmin_tools.py — Network Diagnostics & Sysadmin Module for Dystopi Minibots.
Supports DNS dig, SSL certificate inspection, TCP port probe, and WHOIS/RDAP.
"""

import datetime
import json
import logging
import socket
import ssl
import urllib.parse
import urllib.request
from typing import Optional

from sopel import module

logger = logging.getLogger("SysadminTools")

# SSRF Guard
try:
    from core.guard import is_safe_url
except ImportError:
    def is_safe_url(url: str):
        return True, "127.0.0.1"


@module.commands("dig", "dns")
def dns_lookup(bot, trigger):
    """Resolves DNS records for a host."""
    args = (trigger.group(2) or "").split()
    if not args:
        bot.reply("Usage: !dig <hostname> [A|AAAA]")
        return

    host = args[0].strip()
    qtype = args[1].upper() if len(args) > 1 else "A"

    try:
        family = socket.AF_INET6 if qtype == "AAAA" else socket.AF_INET
        results = socket.getaddrinfo(host, None, family=family)
        ips = sorted(list({r[4][0] for r in results}))
        if ips:
            bot.say(f"🔍 [DNS {qtype}] {host} -> {', '.join(ips[:6])}")
        else:
            bot.reply(f"❌ [DNS] No {qtype} records found for {host}")
    except socket.gaierror as e:
        bot.reply(f"❌ [DNS] Resolution error for {host}: {e}")
    except Exception as e:
        bot.reply(f"❌ [DNS] Query error: {e}")


@module.commands("ssl", "cert")
def ssl_inspect(bot, trigger):
    """Inspects SSL certificate for a domain."""
    domain = trigger.group(2)
    if not domain:
        bot.reply("Usage: !ssl <domain[:port]>")
        return

    clean = domain.strip().split()[0]
    port = 443
    if ":" in clean:
        clean, p_str = clean.split(":", 1)
        if p_str.isdigit():
            port = int(p_str)

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((clean, port), timeout=4.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=clean) as ssock:
                cert = ssock.getpeercert()

        subject = dict(x[0] for x in cert.get("subject", []))
        issuer = dict(x[0] for x in cert.get("issuer", []))
        cn = subject.get("commonName", clean)
        issuer_org = issuer.get("organizationName") or issuer.get("commonName", "Unknown Issuer")
        not_after = cert.get("notAfter")

        days_left = "?"
        if not_after:
            exp_date = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
            delta = exp_date - datetime.datetime.now(datetime.timezone.utc)
            days_left = f"{delta.days} days"

        bot.say(f"🔒 [SSL] CN: {cn} | Issuer: {issuer_org} | Expires: {not_after} ({days_left} remaining)"[:380])
    except Exception as e:
        bot.reply(f"❌ [SSL] Handshake/Inspection failed for {clean}:{port} — {e}")


@module.commands("port", "probe")
def port_probe(bot, trigger):
    """Checks whether a TCP port is open."""
    args = (trigger.group(2) or "").split()
    if len(args) < 2:
        bot.reply("Usage: !port <host> <port>")
        return

    host, p_str = args[0].strip(), args[1].strip()
    if not p_str.isdigit() or not (1 <= int(p_str) <= 65535):
        bot.reply("❌ Invalid port number (1-65535).")
        return
    port = int(p_str)

    # Validate against SSRF
    safe, resolved_ip = is_safe_url(f"http://{host}:{port}")
    if not safe:
        bot.reply("🛡️ [SSRF Guard] Probing internal/private network addresses is blocked.")
        return

    try:
        with socket.create_connection((host, port), timeout=3.0):
            bot.say(f"🟢 [PORT] {host}:{port} ({resolved_ip}) is OPEN.")
    except socket.timeout:
        bot.say(f"⏳ [PORT] {host}:{port} timed out (FILTERED).")
    except ConnectionRefusedError:
        bot.say(f"🔴 [PORT] {host}:{port} is CLOSED (Connection refused).")
    except Exception as e:
        bot.say(f"🔴 [PORT] {host}:{port} connection failed: {e}")


@module.commands("whois", "rdap")
def rdap_whois(bot, trigger):
    """Fetches domain registration data via RDAP."""
    domain = trigger.group(2)
    if not domain:
        bot.reply("Usage: !whois <domain>")
        return

    clean_domain = domain.strip().split()[0].lower()
    url = f"https://rdap.org/domain/{urllib.parse.quote(clean_domain)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MinibotsRDAP/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        events = {ev.get("eventAction"): ev.get("eventDate") for ev in data.get("events", [])}
        created = events.get("registration", "Unknown")[:10]
        expires = events.get("expiration", "Unknown")[:10]
        entities = data.get("entities", [])
        registrar = "Unknown"
        for ent in entities:
            if "registrar" in ent.get("roles", []):
                registrar = ent.get("vcardArray", [[], []])[1]
                for v in registrar:
                    if v[0] == "fn":
                        registrar = v[3]
                        break

        status = ", ".join(data.get("status", [])[:2]) or "active"
        bot.say(f"📋 [WHOIS/RDAP] {clean_domain} | Registrar: {registrar} | Registered: {created} | Expires: {expires} | Status: {status}"[:380])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            bot.reply(f"❌ [WHOIS] Domain '{clean_domain}' not found in RDAP.")
        else:
            bot.reply(f"❌ [WHOIS] RDAP error {e.code}.")
    except Exception as e:
        logger.debug(f"RDAP lookup failed: {e}")
        bot.reply("❌ [WHOIS] Query failed or timed out.")
