"""Commercial verification providers (ZeroBounce, MillionVerifier, Hunter).

Used only for addresses the local engine could not settle (typically UNKNOWN because
port 25 is blocked on this machine). These services run their own SMTP infrastructure,
so they can answer "does this mailbox exist?" where a home connection cannot.

Every provider answer is mapped to emailish statuses with its original status kept in
`sub_status` as "<provider>:<their status>" and in `verification_reason`, so it is always
clear that the evidence came from the provider, not from emailish.

Endpoints (documented, see research/competitor-analysis.md):
  ZeroBounce       GET https://api.zerobounce.net/v2/validate       credits: /v2/getcredits ({"Credits": n}, -1 = bad key)
  MillionVerifier  GET https://api.millionverifier.com/api/v3/      credits: /api/v3/credits
  Hunter           GET https://api.hunter.io/v2/email-verifier       account: /v2/account (202 = still verifying)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from .engine.models import Certainty, Status

C, U = Certainty.CONFIRMED, Certainty.UNCERTAIN


class ProviderFatal(Exception):
    """Bad key, no credits, account blocked: stop calling the provider for this job."""


@dataclass
class ProviderVerdict:
    status: Status
    sub_status: str
    certainty: Certainty
    confidence: int
    reason: str
    disposable: bool = False
    role: bool = False
    raw: dict = field(default_factory=dict)


def _v(status, sub, certainty, conf, reason, **kw) -> ProviderVerdict:
    return ProviderVerdict(status, sub, certainty, conf, reason, **kw)


class Provider:
    key_env = ""
    name = ""
    label = ""
    concurrency = 4
    min_interval = 0.0          # seconds between requests (rate limits)
    credits_per_check = 1.0

    def __init__(self, api_key: str, timeout: float = 45.0):
        self.api_key = api_key
        self.timeout = timeout
        self.transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def _pace(self):
        if not self.min_interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def credits(self, client: httpx.AsyncClient) -> float | None:
        raise NotImplementedError

    async def check(self, client: httpx.AsyncClient, email: str) -> ProviderVerdict:
        raise NotImplementedError


# --------------------------------------------------------------------------- ZeroBounce
def map_zerobounce(j: dict) -> ProviderVerdict:
    st = (j.get("status") or "").lower()
    sub = (j.get("sub_status") or "").lower()
    tag = f"zerobounce:{st}" + (f"/{sub}" if sub else "")
    why = f"ZeroBounce: {st}" + (f" ({sub})" if sub else "")
    if st == "valid":
        if sub == "role_based_accept_all":
            return _v(Status.ROLE_BASED, tag, U, 70, why + ": role address on a domain ZeroBounce lists as accept-all.", role=True)
        if sub == "accept_all":
            return _v(Status.VALID, tag, U, 75, why + ": domain accepts all mail; ZeroBounce rates it deliverable "
                                                      "from its own history, not from a mailbox check.")
        return _v(Status.VALID, tag, C, 88, why + ": mailbox confirmed by ZeroBounce.")
    if st == "invalid":
        return _v(Status.INVALID, tag, C, 92, why + ": address will bounce.")
    if st == "catch-all":
        return _v(Status.CATCH_ALL, tag, U, 75, why + ": domain accepts any address; mailbox cannot be confirmed.")
    if st == "do_not_mail":
        if sub == "disposable":
            return _v(Status.DISPOSABLE, tag, C, 90, why + ".", disposable=True)
        if sub in ("role_based", "role_based_catch_all"):
            return _v(Status.ROLE_BASED, tag, C if sub == "role_based" else U,
                      85 if sub == "role_based" else 60, why + ".", role=True)
        return _v(Status.RISKY, tag, U, 60, why + ": ZeroBounce advises not to mail.")
    if st in ("spamtrap", "abuse"):
        return _v(Status.RISKY, tag, U, 70, why + ": ZeroBounce advises not to mail.")
    return _v(Status.UNKNOWN, tag, U, 0, why + ": ZeroBounce could not verify (no credit charged).")


class ZeroBounce(Provider):
    key_env, name, label = "ZEROBOUNCE_API_KEY", "zerobounce", "ZeroBounce"
    concurrency = 5

    async def credits(self, client):
        r = await client.get("https://api.zerobounce.net/v2/getcredits", params={"api_key": self.api_key}, timeout=20)
        r.raise_for_status()
        c = float(r.json().get("Credits", -1))
        if c < 0:
            raise ProviderFatal("ZeroBounce rejected the API key")
        return c

    async def check(self, client, email):
        await self._pace()
        r = await client.get("https://api.zerobounce.net/v2/validate",
                             params={"api_key": self.api_key, "email": email, "ip_address": "", "timeout": 30},
                             timeout=self.timeout)
        if r.status_code in (401, 403):
            raise ProviderFatal(f"ZeroBounce HTTP {r.status_code}")
        r.raise_for_status()
        j = r.json()
        if "error" in j and "status" not in j:
            msg = str(j["error"])
            if "key" in msg.lower() or "credit" in msg.lower():
                raise ProviderFatal(f"ZeroBounce: {msg}")
            raise RuntimeError(f"ZeroBounce: {msg}")
        v = map_zerobounce(j)
        v.raw = j
        return v


# ---------------------------------------------------------------------- MillionVerifier
def map_millionverifier(j: dict) -> ProviderVerdict:
    res = (j.get("result") or "").lower()
    sub = (j.get("subresult") or "").lower()
    tag = f"millionverifier:{res}" + (f"/{sub}" if sub and sub != res else "")
    why = f"MillionVerifier: {res}" + (f" ({sub})" if sub and sub != res else "")
    if j.get("livemode") is False:
        why += " [TEST KEY: simulated result, not a real check]"
    role = bool(j.get("role"))
    if res == "ok":
        if role:
            return _v(Status.ROLE_BASED, tag, C, 85, why + ": role mailbox confirmed by MillionVerifier.", role=True)
        return _v(Status.VALID, tag, C, 88, why + ": mailbox confirmed by MillionVerifier.")
    if res == "invalid":
        return _v(Status.INVALID, tag, C, 92, why + ": address will bounce.", role=role)
    if res == "catch_all":
        return _v(Status.CATCH_ALL, tag, U, 75, why + ": domain accepts any address; mailbox cannot be confirmed.", role=role)
    if res == "disposable":
        return _v(Status.DISPOSABLE, tag, C, 90, why + ".", disposable=True, role=role)
    return _v(Status.UNKNOWN, tag, U, 0, why + ": MillionVerifier could not verify (no credit charged).", role=role)


class MillionVerifier(Provider):
    key_env, name, label = "MILLIONVERIFIER_API_KEY", "millionverifier", "MillionVerifier"
    concurrency = 5

    async def credits(self, client):
        r = await client.get("https://api.millionverifier.com/api/v3/credits", params={"api": self.api_key}, timeout=20)
        r.raise_for_status()
        j = r.json()
        if j.get("result") == "error":
            raise ProviderFatal(f"MillionVerifier: {j.get('error')}")
        return float(j.get("credits", 0))

    async def check(self, client, email):
        await self._pace()
        r = await client.get("https://api.millionverifier.com/api/v3/",
                             params={"api": self.api_key, "email": email, "timeout": 20}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("result") == "error":
            err = str(j.get("error") or "error")
            if any(k in err.lower() for k in ("apikey", "api key", "credit", "insufficient")):
                raise ProviderFatal(f"MillionVerifier: {err}")
            raise RuntimeError(f"MillionVerifier: {err}")
        v = map_millionverifier(j)
        v.raw = j
        return v


# ------------------------------------------------------------------------------- Hunter
def map_hunter(j: dict) -> ProviderVerdict:
    d = j.get("data", j)
    st = (d.get("status") or "").lower()
    res = (d.get("result") or "").lower()
    tag = f"hunter:{st}"
    why = f"Hunter: {st}" + (f" (score {d.get('score')})" if d.get("score") is not None else "")
    if st == "valid":
        return _v(Status.VALID, tag, C, 88, why + ": mailbox confirmed by Hunter.")
    if st == "invalid":
        return _v(Status.INVALID, tag, C, 92, why + ": address will bounce.")
    if st == "accept_all":
        return _v(Status.CATCH_ALL, tag, U, 75, why + ": domain accepts any address; mailbox cannot be confirmed.")
    if st == "disposable":
        return _v(Status.DISPOSABLE, tag, C, 90, why + ".", disposable=True)
    if st == "webmail":
        if res == "deliverable":
            return _v(Status.VALID, tag, C, 85, why + ": webmail address Hunter rates deliverable.")
        if res == "undeliverable":
            return _v(Status.INVALID, tag, C, 90, why + ": webmail address Hunter rates undeliverable.")
        return _v(Status.RISKY, tag, U, 50, why + ": webmail address Hunter rates risky.")
    return _v(Status.UNKNOWN, tag, U, 0, why + ": Hunter could not verify.")


class Hunter(Provider):
    key_env, name, label = "HUNTER_API_KEY", "hunter", "Hunter"
    concurrency = 3
    min_interval = 0.15          # documented limit: 10 requests/s, 300/min
    credits_per_check = 0.5

    async def credits(self, client):
        r = await client.get("https://api.hunter.io/v2/account", params={"api_key": self.api_key}, timeout=20)
        if r.status_code == 401:
            raise ProviderFatal("Hunter rejected the API key")
        r.raise_for_status()
        ver = (r.json().get("data", {}).get("requests", {}) or {}).get("verifications", {}) or {}
        if "available" in ver:
            return float(ver.get("available", 0)) - float(ver.get("used", 0))
        return None

    async def check(self, client, email):
        for attempt in range(8):
            await self._pace()
            r = await client.get("https://api.hunter.io/v2/email-verifier",
                                 params={"email": email, "api_key": self.api_key}, timeout=self.timeout)
            if r.status_code == 202:            # still verifying: poll again (counted once)
                await asyncio.sleep(4)
                continue
            if r.status_code == 429:            # rate limited: back off, never hammer
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (401, 403):
                raise ProviderFatal(f"Hunter HTTP {r.status_code}")
            if r.status_code == 451:
                return _v(Status.UNKNOWN, "hunter:451_opted_out", U, 0,
                          "Hunter: the address owner asked Hunter not to process it.")
            if r.status_code == 222:
                return _v(Status.UNKNOWN, "hunter:222_smtp_error", U, 0,
                          "Hunter: the mail server gave an unexpected response; try again later.")
            r.raise_for_status()
            j = r.json()
            v = map_hunter(j)
            v.raw = j
            return v
        return _v(Status.UNKNOWN, "hunter:still_verifying", U, 0, "Hunter did not finish in time.")


PROVIDERS = {p.name: p for p in (ZeroBounce, MillionVerifier, Hunter)}


def client_for(p: Provider) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=p.transport, headers={"User-Agent": "emailish"})


def provider_from_settings(s) -> Provider | None:
    """EMAILISH_PROVIDER = zerobounce | millionverifier | hunter | none | (empty = auto: the one key set)."""
    choice = (s.provider or "").strip().lower()
    if choice in ("none", "off", "false", "0"):
        return None
    keys = {name: s.provider_key(name) for name in PROVIDERS}
    if choice:
        if choice not in PROVIDERS:
            raise ValueError(f"unknown EMAILISH_PROVIDER {choice!r}; use zerobounce, millionverifier, hunter or none")
        return PROVIDERS[choice](keys[choice]) if keys[choice] else None
    configured = [n for n, k in keys.items() if k]
    if len(configured) == 1:
        return PROVIDERS[configured[0]](keys[configured[0]])
    return None   # none, or several keys without an explicit choice
