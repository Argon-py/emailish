"""`python -m app.cli diagnose`: can THIS machine run SMTP mailbox diagnostics?

What it does (and does not do):
  * resolves DNS with the same backend the engine uses;
  * opens a TCP connection to port 25 of a target: your own domain's mail server
    (--domain), a host you name (--target), or by default the neutral port-test service
    portquiz.net, which answers on every port and is not a mail server;
  * for an SMTP target, reads the 220 banner, sends EHLO and QUIT. It NEVER sends MAIL
    FROM, RCPT TO or DATA, so no mailbox is queried and no email is sent;
  * looks up this machine's public IP (api.ipify.org; skip with --no-public-ip) and checks
    that its reverse DNS (PTR) matches EMAILISH_HELO_HOST (forward-confirmed).
"""
from __future__ import annotations

import asyncio
import ipaddress
import time

import httpx

from .config import Settings
from .engine import smtp
from .engine.dnslayer import pick_backend

NEUTRAL_TARGET = "portquiz.net"


def _line(ok: bool | None, text: str) -> str:
    mark = {True: "OK  ", False: "FAIL", None: "INFO"}[ok]
    return f"[{mark}] {text}"


async def _banner_and_ehlo(ip: str, port: int, helo: str, timeout: float) -> tuple[bool, str]:
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except Exception as e:
        return False, f"connect failed: {type(e).__name__}: {e}"
    try:
        banner = await smtp._read_reply(r, timeout)
        if banner.code != 220:
            return False, f"banner {banner.short()}"
        ehlo = await smtp._cmd(r, w, f"EHLO {helo}", timeout)
        w.write(b"QUIT\r\n")
        await w.drain()
        return True, f"banner '{banner.short()}', EHLO -> {ehlo.code}"
    except Exception as e:
        return False, f"no valid SMTP banner ({type(e).__name__}: {e}). Something on the path (ISP, " \
                      "antivirus mail shield, firewall) may be intercepting port 25."
    finally:
        w.close()


async def diagnose(domain: str | None = None, target: str | None = None, public_ip: bool = True) -> int:
    s = Settings()
    out: list[str] = []
    problems: list[str] = []
    print("emailish SMTP readiness check (no MAIL/RCPT/DATA is sent)\n")

    out.append(_line(None, f"EMAILISH_SMTP_ENABLED = {s.smtp_enabled}"))
    out.append(_line(None, f"EMAILISH_SMTP_ALLOWED_DOMAINS = {', '.join(s.smtp_allowlist) or '(not set: all domains)'}"))
    out.append(_line(None, f"HELO host = {s.helo_host}   MAIL FROM = {s.mail_from}"))
    if not s.smtp_enabled:
        problems.append("SMTP is switched off (EMAILISH_SMTP_ENABLED=false / run.bat --no-smtp).")

    backend, note = await pick_backend(s.dns_mode)
    ok_dns = not note.startswith("WARNING")
    out.append(_line(ok_dns, f"DNS: {note}"))
    if not ok_dns:
        problems.append("DNS resolver cannot return MX records reliably.")

    # --- port 25 egress
    smtp_target = False
    if domain:
        mx = await backend.query(domain, "MX")
        hosts = [h for _, h in sorted(mx.records) if h not in ("", ".")] if mx.status == "ok" else []
        tgt = hosts[0] if hosts else domain
        smtp_target = True
        out.append(_line(None, f"target: MX of your domain {domain} -> {tgt}"))
    elif target:
        tgt, smtp_target = target, True
        out.append(_line(None, f"target: {tgt}"))
    else:
        tgt = NEUTRAL_TARGET
        out.append(_line(None, f"target: {tgt} (neutral port-test service, not a mail server)"))
    try:
        ipaddress.ip_address(tgt)
        from .engine.dnslayer import DnsAnswer
        a = DnsAnswer("ok", [tgt])          # an IP literal needs no lookup
    except ValueError:
        a = await backend.query(tgt, "A")
    if not a.records:
        out.append(_line(False, f"could not resolve {tgt}: {a.status} {a.error}"))
        problems.append(f"target {tgt} does not resolve.")
    else:
        ip = a.records[0]
        t0 = time.monotonic()
        ok, detail = await smtp.tcp_port_open(ip, s.smtp_port, 8)
        ms = int((time.monotonic() - t0) * 1000)
        out.append(_line(ok, f"outbound TCP/{s.smtp_port} to {tgt} [{ip}]: {detail} ({ms} ms)"))
        if not ok:
            hint = ("connection refused quickly: typically an ISP/cloud provider or local firewall rejecting port 25"
                    if "Refused" in detail else
                    "no answer: typically port 25 silently dropped by an ISP/cloud provider or firewall")
            out.append(_line(None, hint))
            problems.append(f"outbound port {s.smtp_port} is blocked from this network.")
        elif smtp_target:
            ok2, d2 = await _banner_and_ehlo(ip, s.smtp_port, s.helo_host, 15)
            out.append(_line(ok2, f"SMTP greeting: {d2}"))
            if not ok2:
                problems.append("port 25 opens but no genuine SMTP server answers (interception).")

    # --- sender identity
    if s.helo_host.startswith("localhost") or "." not in s.helo_host:
        out.append(_line(False, "HELO host is not a fully-qualified domain name"))
        problems.append("set EMAILISH_HELO_HOST to a real hostname you control.")
    if public_ip:
        try:
            pip = httpx.get("https://api.ipify.org", timeout=8).text.strip()
            ipaddress.ip_address(pip)
        except Exception as e:
            pip = ""
            out.append(_line(None, f"public IP lookup skipped ({type(e).__name__})"))
        if pip:
            rev = ".".join(reversed(pip.split("."))) + ".in-addr.arpa" if ":" not in pip else ""
            ptr = await backend.query(rev, "PTR") if rev else None
            names = [n.rstrip(".").lower() for n in (ptr.records if ptr and ptr.records else [])]
            out.append(_line(None, f"public IP {pip}, reverse DNS (PTR): {', '.join(names) or 'none'}"))
            fwd_ok = False
            for n in names:
                fa = await backend.query(n, "A")
                fwd_ok |= pip in fa.records
            match = s.helo_host.lower() in names
            out.append(_line(match and fwd_ok, "PTR matches HELO host and resolves back to this IP"
                             if match and fwd_ok else
                             f"PTR does not match HELO host '{s.helo_host}' (or does not resolve back)"))
            if not (match and fwd_ok):
                problems.append("reverse DNS (PTR) of the public IP must be set to EMAILISH_HELO_HOST, "
                                "and that name must resolve to the IP. Many mail servers block probes otherwise.")
    print("\n".join(out))
    print()
    if problems:
        print("NOT READY for SMTP diagnostics:")
        for p in problems:
            print(f"  - {p}")
        print("\nUntil this is fixed, emailish reports mailbox-level results as UNKNOWN (never guessed).")
        print("See research/SMTP_ENABLEMENT_REPORT.md for the infrastructure needed.")
        return 1
    print("READY: this machine can run SMTP diagnostics. Start with EMAILISH_SMTP_ALLOWED_DOMAINS set to "
          "domains you own and test with research/REAL_WORLD_TESTING_GUIDE.md.")
    return 0
