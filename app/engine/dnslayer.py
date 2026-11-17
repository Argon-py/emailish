"""DNS layer: resolver backends, self-test, and mail-domain analysis.

Rules implemented:
  * NXDOMAIN                         -> domain does not exist
  * MX "0 ." (RFC 7505 Null MX)      -> domain accepts no mail
  * no MX but A/AAAA (RFC 5321 5.1)  -> implicit MX (domain host itself)
  * no MX and no A/AAAA              -> no mail host
  * every mail host resolves only to private/reserved IPs -> unroutable
  * SERVFAIL / REFUSED / timeout     -> dns_error (never treated as "missing")
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass, field

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx

OK, NXDOMAIN, NODATA, ERROR = "ok", "nxdomain", "nodata", "error"
_TYPE_NUM = {"A": 1, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28}


@dataclass
class DnsAnswer:
    status: str                      # ok | nxdomain | nodata | error
    records: list = field(default_factory=list)   # MX: [(pref, host)], A/AAAA: [ip]
    error: str = ""


class SystemBackend:
    name = "system"

    def __init__(self, timeout: float = 5.0, nameservers: list[str] | None = None):
        self.r = dns.asyncresolver.Resolver()
        if nameservers:
            self.r.nameservers = nameservers
        self.r.lifetime = timeout

    async def query(self, name: str, rtype: str) -> DnsAnswer:
        try:
            ans = await self.r.resolve(name, rtype)
        except dns.resolver.NXDOMAIN:
            return DnsAnswer(NXDOMAIN)
        except dns.resolver.NoAnswer:
            return DnsAnswer(NODATA)
        except (dns.resolver.NoNameservers, dns.exception.Timeout, dns.resolver.LifetimeTimeout) as e:
            return DnsAnswer(ERROR, error=type(e).__name__)
        except Exception as e:  # pragma: no cover - defensive
            return DnsAnswer(ERROR, error=f"{type(e).__name__}: {e}")
        if rtype == "MX":
            recs = [(int(r.preference), str(r.exchange).rstrip(".").lower()) for r in ans]
        else:
            recs = [r.to_text() for r in ans]
        return DnsAnswer(OK, recs)


class DohBackend:
    """DNS-over-HTTPS (JSON API). Used when the local resolver is unreliable."""

    name = "doh"
    ENDPOINTS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._loop = None

    def _c(self) -> httpx.AsyncClient:
        # An AsyncClient is bound to the event loop it was created on; the CLI and tests
        # may call us from several loops, so keep one client per running loop.
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self.timeout, headers={"accept": "application/dns-json"})
            self._loop = loop
        return self._client

    async def query(self, name: str, rtype: str) -> DnsAnswer:
        last = ""
        for url in self.ENDPOINTS:
            try:
                r = await self._c().get(url, params={"name": name, "type": rtype})
                r.raise_for_status()
                j = r.json()
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                continue
            st = j.get("Status")
            if st == 3:
                return DnsAnswer(NXDOMAIN)
            if st != 0:
                last = f"rcode {st}"
                continue  # SERVFAIL/REFUSED: try the next provider
            want = _TYPE_NUM[rtype]
            recs = []
            for a in j.get("Answer", []) or []:
                if a.get("type") != want:
                    continue
                data = a.get("data", "").strip()
                if rtype == "MX":
                    pref, _, host = data.partition(" ")
                    recs.append((int(pref), host.strip().rstrip(".").lower()))
                else:
                    recs.append(data)
            return DnsAnswer(OK if recs else NODATA, recs)
        return DnsAnswer(ERROR, error=last or "all DoH endpoints failed")


class FakeBackend:
    """Deterministic backend for tests: {(name, type): DnsAnswer}."""

    name = "fake"

    def __init__(self, table: dict[tuple[str, str], DnsAnswer]):
        self.table = {(k[0].lower(), k[1]): v for k, v in table.items()}

    async def query(self, name: str, rtype: str) -> DnsAnswer:
        return self.table.get((name.lower(), rtype), DnsAnswer(NXDOMAIN))


async def self_test(backend) -> tuple[bool, str]:
    """A resolver is usable for mail checks only if it returns real MX data."""
    a = await backend.query("gmail.com", "MX")
    if a.status == OK and any("google" in h for _, h in a.records):
        return True, "MX lookup for gmail.com returned Google MX hosts"
    return False, f"MX lookup for gmail.com returned {a.status} {a.records or a.error}"


async def pick_backend(mode: str = "auto"):
    """Return (backend, note). auto = system resolver if it passes the self-test, else DoH."""
    if mode == "system":
        sysb = SystemBackend()
        ok, why = await self_test(sysb)
        if ok:
            return sysb, "system resolver (forced)"
        return sysb, ("WARNING: system resolver (forced by EMAILISH_DNS_MODE=system) failed the MX self-test "
                      f"({why}). MX-based results (mx_found, Null MX, implicit MX) will be wrong. "
                      "Use EMAILISH_DNS_MODE=auto or doh.")
    if mode == "doh":
        return DohBackend(), "DNS-over-HTTPS (forced)"
    sysb = SystemBackend()
    ok, why = await self_test(sysb)
    if ok:
        return sysb, f"system resolver ({why})"
    doh = DohBackend()
    ok2, why2 = await self_test(doh)
    if ok2:
        return doh, f"DNS-over-HTTPS: system resolver failed self-test ({why})"
    return sysb, f"WARNING: no resolver passed the MX self-test (system: {why}; DoH: {why2})"


# --- domain analysis --------------------------------------------------------
@dataclass
class MailHost:
    host: str
    pref: int
    ips: list[str]


@dataclass
class DomainInfo:
    domain: str
    status: str                 # ok | nxdomain | null_mx | no_mail_host | unroutable | mx_unresolvable | dns_error
    mx_found: bool | None
    implicit_mx: bool = False
    hosts: list[MailHost] = field(default_factory=list)
    detail: str = ""

    @property
    def mx_names(self) -> list[str]:
        return [h.host for h in self.hosts]


def _routable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global


async def _host_ips(backend, host: str) -> tuple[list[str], bool]:
    """(ips, had_error)."""
    a4, a6 = await asyncio.gather(backend.query(host, "A"), backend.query(host, "AAAA"))
    ips = list(a4.records if a4.status == OK else []) + list(a6.records if a6.status == OK else [])
    return ips, (a4.status == ERROR or a6.status == ERROR) and not ips


async def analyze_domain(backend, domain: str, max_hosts: int = 3, allow_private: bool = False) -> DomainInfo:
    routable = (lambda ip: True) if allow_private else _routable
    mx = await backend.query(domain, "MX")
    if mx.status == NXDOMAIN:
        return DomainInfo(domain, "nxdomain", False, detail="Domain does not exist (NXDOMAIN)")
    if mx.status == ERROR:
        return DomainInfo(domain, "dns_error", None, detail=f"MX lookup failed: {mx.error}")

    if mx.status == OK:
        recs = sorted(mx.records)
        if all(h in ("", ".") for _, h in recs):
            return DomainInfo(domain, "null_mx", True, detail="Domain publishes a Null MX (RFC 7505): it accepts no mail")
        recs = [(p, h) for p, h in recs if h not in ("", ".")]
        hosts, errors = [], 0
        for pref, h in recs[:max_hosts]:
            ips, err = await _host_ips(backend, h)
            errors += err
            hosts.append(MailHost(h, pref, ips))
        resolved = [h for h in hosts if h.ips]
        if not resolved:
            if errors:
                return DomainInfo(domain, "dns_error", True, hosts=hosts, detail="MX host address lookup failed")
            return DomainInfo(domain, "mx_unresolvable", True, hosts=hosts,
                              detail="MX hosts have no A/AAAA records: mail cannot be delivered")
        if not any(routable(ip) for h in resolved for ip in h.ips):
            return DomainInfo(domain, "unroutable", True, hosts=hosts,
                              detail="All MX hosts resolve to private/reserved IP addresses")
        return DomainInfo(domain, "ok", True, hosts=hosts,
                          detail=f"MX: {', '.join(h.host for h in hosts)}")

    # NODATA for MX: RFC 5321 implicit MX via A/AAAA.
    ips, err = await _host_ips(backend, domain)
    if ips:
        if not any(routable(ip) for ip in ips):
            return DomainInfo(domain, "unroutable", False, implicit_mx=True,
                              hosts=[MailHost(domain, 0, ips)], detail="No MX; A/AAAA records are private/reserved")
        return DomainInfo(domain, "ok", False, implicit_mx=True, hosts=[MailHost(domain, 0, ips)],
                          detail="No MX record; using the domain's A/AAAA record as implicit MX (RFC 5321)")
    if err:
        return DomainInfo(domain, "dns_error", False, detail="No MX and A/AAAA lookup failed")
    return DomainInfo(domain, "no_mail_host", False, detail="Domain exists but has no MX and no A/AAAA record")


class DomainCache:
    def __init__(self, ttl: float = 3600, allow_private: bool = False):
        self.ttl = ttl
        self.allow_private = allow_private  # tests only: lets MX point at 127.0.0.1
        self._d: dict[str, tuple[float, DomainInfo]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, backend, domain: str) -> DomainInfo:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            hit = self._d.get(domain)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            info = await analyze_domain(backend, domain, allow_private=self.allow_private)
            # Do not cache transient failures for long.
            ttl = 60 if info.status == "dns_error" else self.ttl
            self._d[domain] = (time.monotonic() + ttl, info)
            return info
