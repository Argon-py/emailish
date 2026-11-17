"""Verification orchestrator: syntax -> lists -> DNS -> disposable -> SMTP -> classify.

Every rule in classify() corresponds to a row of research/technical-architecture.md §5.
Hard invariants:
  * VALID requires an SMTP 250/251 for the target AND a rejected catch-all probe.
  * SMTP-based INVALID requires a recipient-specific rejection.
  * No conclusive evidence -> UNKNOWN / TIMEOUT / RISKY with confidence reflecting that.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..config import Settings
from ..providers import Provider, ProviderFatal, client_for, provider_from_settings
from . import lists, replies, smtp
from .dnslayer import DomainCache, DomainInfo, pick_backend
from .models import Certainty, SmtpStatus, Status, VerificationResult
from .syntax import SyntaxResult, check_syntax, is_blank
from .throttle import Throttle

ProgressCb = Callable[[int, int, str], Awaitable[None] | None]


@dataclass
class Prelim:
    raw: str
    syn: SyntaxResult
    role: bool = False
    free: bool = False
    trap: bool = False
    did_you_mean: str = ""
    disposable_list: bool = False


@dataclass
class SmtpOutcome:
    """What SMTP told us about one address."""
    status: str                    # 'reply' | 'session_failure' | 'unavailable' | 'disabled'
    mx_host: str = ""
    reply: smtp.Reply | None = None
    rclass: replies.ReplyClass | None = None
    failure_kind: str = ""
    failure_stage: str = ""
    failure_reply: smtp.Reply | None = None
    failure_detail: str = ""


@dataclass
class CatchAllVerdict:
    value: bool | None             # True catch-all, False not catch-all, None undetermined
    detail: str = ""
    expires: float = 0.0


@dataclass
class EngineStatus:
    dns_backend: str = ""
    smtp_available: bool | None = None
    smtp_note: str = ""
    helo_host: str = ""
    mail_from: str = ""
    provider: str = ""                 # label of the commercial provider in use, or ""
    provider_credits: float | None = None
    provider_note: str = ""
    warnings: list[str] = field(default_factory=list)


_AUTO = object()
ESCALATE = (Status.UNKNOWN, Status.TIMEOUT)


class Verifier:
    def __init__(self, settings: Settings | None = None, dns_backend=None, provider=_AUTO):
        self.s = settings or Settings()
        self.backend = dns_backend
        self.provider: Provider | None = provider_from_settings(self.s) if provider is _AUTO else provider
        self.last_provider_stats: dict = {}
        self.dcache = DomainCache(allow_private=self.s.allow_private_mx)
        self.catchall: dict[str, CatchAllVerdict] = {}
        self._probe_addr: dict[str, str] = {}
        self.status = EngineStatus(helo_host=self.s.helo_host, mail_from=self.s.mail_from)
        self._ready = False

    # ------------------------------------------------------------------ setup
    async def init(self, force_egress_check: bool = False) -> EngineStatus:
        if self.backend is None:
            self.backend, note = await pick_backend(self.s.dns_mode)
            self.status.dns_backend = note
            if note.startswith("WARNING"):
                self.status.warnings.append(note)
        else:
            self.status.dns_backend = getattr(self.backend, "name", "custom")
        if not self.s.smtp_enabled:
            self.status.smtp_available, self.status.smtp_note = False, "SMTP checks disabled by configuration"
        elif self.s.skip_egress_check:
            self.status.smtp_available, self.status.smtp_note = True, "egress check skipped by configuration"
        elif self.status.smtp_available is None or force_egress_check:
            await self.check_egress()
        await self._init_provider()
        if self.s.smtp_enabled and self.s.smtp_allowlist:
            self.status.warnings.append(
                "SMTP is limited to: " + ", ".join(self.s.smtp_allowlist) +
                " (EMAILISH_SMTP_ALLOWED_DOMAINS). Other domains are reported UNKNOWN / smtp_not_permitted.")
        if self.s.smtp_enabled and self.s.helo_host.startswith("localhost"):
            self.status.warnings.append(
                "HELO hostname is not a real FQDN; many mail servers will block probes. "
                "Set EMAILISH_HELO_HOST to a hostname with matching reverse DNS.")
        self._ready = True
        return self.status

    async def _init_provider(self) -> None:
        if not self.provider:
            several = [n for n in ("zerobounce", "millionverifier", "hunter") if self.s.provider_key(n)]
            if len(several) > 1 and not self.s.provider:
                self.status.warnings.append(
                    f"Several provider keys are set ({', '.join(several)}); choose one with EMAILISH_PROVIDER.")
            return
        p = self.provider
        self.status.provider = p.label
        try:
            async with client_for(p) as c:
                self.status.provider_credits = await p.credits(c)
            cr = self.status.provider_credits
            self.status.provider_note = (f"{p.label}: {cr:g} credits available" if cr is not None
                                         else f"{p.label}: key accepted")
        except ProviderFatal as e:
            self.status.warnings.append(f"{e}. Provider checks are switched off; fix the key in .env.")
            self.status.provider_note = str(e)
            self.provider = None
            self.status.provider = ""
        except Exception as e:
            self.status.provider_note = f"{p.label}: credit balance not available ({type(e).__name__}); checks will still be attempted"

    async def check_egress(self) -> None:
        host = self.s.egress_check_host
        if self.s.smtp_allowlist and host == "gmail-smtp-in.l.google.com":
            # Controlled testing: test egress against YOUR first allowed domain's mail server.
            mx = await self.backend.query(self.s.smtp_allowlist[0], "MX")
            hosts = [h for _, h in sorted(mx.records) if h not in ("", ".")] if mx.status == "ok" else []
            host = hosts[0] if hosts else self.s.smtp_allowlist[0]
        ans = await self.backend.query(host, "A")
        if not ans.records:
            self.status.smtp_available = False
            self.status.smtp_note = f"could not resolve egress test host {host}"
            return
        ok, detail = await smtp.tcp_port_open(ans.records[0], self.s.smtp_port, 6)
        self.status.smtp_available = ok
        self.status.smtp_note = (
            f"outbound TCP/{self.s.smtp_port} works ({host})" if ok else
            f"outbound TCP/{self.s.smtp_port} is blocked from this host ({detail}). "
            "Mailbox-level checks cannot run here; results will be UNKNOWN (smtp_unavailable) "
            "instead of guesses. Run emailish on a server with port 25 egress.")

    # ------------------------------------------------------------- public API
    async def verify_one(self, raw: str) -> VerificationResult:
        return (await self.verify_many([raw]))[raw]

    async def verify_many(self, raws: list[str], progress: ProgressCb | None = None,
                          retry_greylisted: bool = True) -> dict[str, VerificationResult]:
        if not self._ready:
            await self.init()
        total = len(raws)
        done = 0
        results: dict[str, VerificationResult] = {}

        async def tick(n: int, phase: str):
            nonlocal done
            done += n
            if progress:
                r = progress(min(done, total), total, phase)
                if asyncio.iscoroutine(r):
                    await r

        # 1. syntax + list intelligence; de-duplicate on the normalized address
        by_domain: dict[str, list[Prelim]] = {}
        aliases: dict[str, list[str]] = {}          # canonical raw -> other raws with same address
        canon_of: dict[str, str] = {}               # normalized key -> canonical raw
        for raw in dict.fromkeys(raws):
            p = self._prelim(raw)
            if not p.syn.ok:
                results[raw] = self._syntax_invalid(p)
                continue
            key = p.syn.normalized.lower()
            if key in canon_of:
                aliases[canon_of[key]].append(raw)
                continue
            canon_of[key] = raw
            aliases[raw] = []
            by_domain.setdefault(p.syn.domain, []).append(p)
        total = len(results) + len(canon_of)
        await tick(len(results), "syntax")

        # 2. per domain: DNS -> disposable -> SMTP -> classify
        pending_retry: list[Prelim] = []
        dns_sem = asyncio.Semaphore(20)

        async def run_domain(domain: str, plist: list[Prelim]):
            async with dns_sem:
                info = await self.dcache.get(self.backend, domain)
            outs = await self._domain_outcomes(domain, info, plist)
            for p in plist:
                res = self.classify(p, info, outs.get(p.raw))
                results[p.raw] = res
                o = outs.get(p.raw)
                if o and o.rclass and (o.rclass.kind in (replies.GREYLISTED, replies.TEMP)
                                       or res.sub_status == "catch_all_undetermined"):
                    pending_retry.append(p)
            await tick(len(plist), "verifying")

        await asyncio.gather(*(run_domain(d, pl) for d, pl in by_domain.items()))

        # 3. greylist retry pass
        if retry_greylisted and pending_retry and self.s.greylist_retry_delay > 0:
            if progress:
                r = progress(total, total, f"greylist retry in {int(self.s.greylist_retry_delay)}s "
                                           f"({len(pending_retry)} addresses)")
                if asyncio.iscoroutine(r):
                    await r
            await asyncio.sleep(self.s.greylist_retry_delay)
            regroup: dict[str, list[Prelim]] = {}
            for p in pending_retry:
                regroup.setdefault(p.syn.domain, []).append(p)
            for domain, plist in regroup.items():
                info = await self.dcache.get(self.backend, domain)
                outs = await self._domain_outcomes(domain, info, plist)
                for p in plist:
                    res = self.classify(p, info, outs.get(p.raw))
                    res.verification_reason += " (after greylist retry)"
                    results[p.raw] = res
        # 4. commercial provider for what the local engine could not settle
        self.last_provider_stats = {}
        if self.provider:
            await self._provider_pass(list(aliases), results, progress, total)
        for canon, others in aliases.items():
            for raw in others:
                results[raw] = results[canon]
        if progress:
            r = progress(total, total, "done")
            if asyncio.iscoroutine(r):
                await r
        return results

    # ------------------------------------------------------------ provider pass
    def _needs_provider(self, r: VerificationResult) -> bool:
        if r.email_status in ESCALATE or r.sub_status == "catch_all_undetermined":
            return True
        return self.s.provider_include_catch_all and (
            r.email_status == Status.CATCH_ALL or r.sub_status == "role_based_catch_all")

    async def _provider_pass(self, canon_raws: list[str], results: dict, progress: ProgressCb | None,
                             total: int) -> None:
        p = self.provider
        todo = [raw for raw in canon_raws if self._needs_provider(results[raw])]
        cap = max(0, self.s.provider_max_checks)
        if self.status.provider_credits is not None:
            cap = min(cap, int(self.status.provider_credits // p.credits_per_check))
        run, over = todo[:cap], todo[cap:]
        stats = {"provider": p.label, "candidates": len(todo), "checked": 0, "errors": 0,
                 "not_checked_cap": len(over), "credits_estimated": 0.0, "stopped": ""}
        for raw in over:
            results[raw].verification_reason += (f" Not sent to {p.label}: per-job limit "
                                                 f"(EMAILISH_PROVIDER_MAX_CHECKS) or credit balance reached.")
        sem = asyncio.Semaphore(p.concurrency)
        done = 0
        stop: list[str] = []

        async def one(raw: str):
            nonlocal done
            local = results[raw]
            async with sem:
                if stop:
                    local.verification_reason += f" Not sent to {p.label}: {stop[0]}."
                    return
                try:
                    v = await p.check(client, local.email)
                except ProviderFatal as e:
                    if not stop:
                        stop.append(str(e))
                    local.verification_reason += f" Not sent to {p.label}: {stop[0]}."
                    return
                except Exception as e:
                    stats["errors"] += 1
                    local.verification_reason += f" {p.label} check failed ({type(e).__name__}); local result kept."
                    return
            stats["checked"] += 1
            stats["credits_estimated"] += p.credits_per_check if v.status != Status.UNKNOWN else 0
            results[raw] = self._merge_provider(local, v, p)
            done += 1
            if progress:
                pr = progress(total, total, f"checking with {p.label}: {done} / {len(run)}")
                if asyncio.iscoroutine(pr):
                    await pr

        if run:
            if progress:
                pr = progress(total, total, f"checking with {p.label}: 0 / {len(run)}")
                if asyncio.iscoroutine(pr):
                    await pr
            async with client_for(p) as client:
                await asyncio.gather(*(one(raw) for raw in run))
        stats["stopped"] = stop[0] if stop else ""
        self.last_provider_stats = stats

    @staticmethod
    def _merge_provider(local: VerificationResult, v, p: Provider) -> VerificationResult:
        r = VerificationResult(**{k: getattr(local, k) for k in local.__dataclass_fields__})
        status = v.status
        if status == Status.VALID and local.role_based:
            status = Status.ROLE_BASED           # same rule as the local engine: role + confirmed mailbox
        r.email_status, r.sub_status, r.certainty, r.confidence = status, v.sub_status, v.certainty, v.confidence
        r.disposable = local.disposable or v.disposable
        r.role_based = local.role_based or v.role
        r.verified_by = p.name
        r.verification_reason = f"{v.reason} (emailish could not settle it locally: {local.sub_status}.)"
        return r

    # -------------------------------------------------------------- internals
    def _prelim(self, raw: str) -> Prelim:
        syn = check_syntax(raw)
        p = Prelim(raw, syn)
        if syn.ok:
            p.role = lists.is_role(syn.local)
            p.free = lists.is_free_domain(syn.domain)
            p.trap = lists.is_possible_trap(syn.local, syn.domain)
            p.disposable_list = lists.is_disposable_domain(syn.domain)
            sug = lists.suggest_domain(syn.domain)
            if sug:
                p.did_you_mean = f"{syn.local}@{sug}"
        return p

    def _syntax_invalid(self, p: Prelim) -> VerificationResult:
        empty = is_blank(p.raw)    # "empty" means the cell itself is blank, not e.g. "mailto:" or "<>"
        return VerificationResult(
            email=p.syn.normalized or p.raw,
            email_status=Status.INVALID,
            sub_status="empty" if empty else "failed_syntax_check",
            certainty=Certainty.CONFIRMED, confidence=99,
            verification_reason=p.syn.reason, syntax_valid=False,
            domain_valid=None, mx_found=None,
        )

    def _needs_smtp(self, info: DomainInfo, plist: list[Prelim]) -> bool:
        if info.status != "ok":
            return False
        if plist and plist[0].disposable_list:
            return False
        if lists.disposable_mx_operator(info.mx_names):
            return False
        return True

    async def _domain_outcomes(self, domain: str, info: DomainInfo, plist: list[Prelim]) -> dict[str, SmtpOutcome]:
        if not self._needs_smtp(info, plist):
            return {}
        if not self.s.smtp_enabled:
            return {p.raw: SmtpOutcome("disabled") for p in plist}
        if not self.s.smtp_permitted(domain):
            return {p.raw: SmtpOutcome("not_permitted") for p in plist}
        if not self.status.smtp_available:
            return {p.raw: SmtpOutcome("unavailable") for p in plist}

        provider = replies.identify_provider(info.mx_names)
        addr_of = {p.raw: f"{p.syn.local}@{domain}" for p in plist}
        out: dict[str, SmtpOutcome] = {}
        chunk_n = max(1, self.s.rcpt_per_session)
        chunks = [plist[i:i + chunk_n] for i in range(0, len(plist), chunk_n)]
        for chunk in chunks:
            verdict = self._catchall_get(domain)
            probe_addr = None
            if verdict is None or verdict.value is None:
                # Reuse one random address per domain: greylisting is keyed on
                # (sender, recipient), so a fresh address would be greylisted on every retry.
                probe_addr = self._probe_addr.setdefault(domain, f"zz{secrets.token_hex(9)}@{domain}")
            recips = [addr_of[p.raw] for p in chunk]
            sess = await self._session_with_failover(info, recips, probe_addr)
            if not sess.connected or sess.failed_stage in ("connect", "banner", "ehlo", "mail_from"):
                # Session-level failure: same outcome for every remaining address in this
                # domain. Do not keep hammering the server.
                fo = SmtpOutcome("session_failure", sess.mx_host, failure_kind=sess.failure_kind,
                                 failure_stage=sess.failed_stage, failure_reply=sess.failure_reply,
                                 failure_detail=sess.failure_detail)
                if sess.failure_reply:
                    fo.rclass = replies.classify_rcpt(sess.failure_reply.code, sess.failure_reply.text, provider)
                for p in plist:
                    out.setdefault(p.raw, fo)
                return out
            if probe_addr and sess.probe_reply is not None:
                pc = replies.classify_rcpt(sess.probe_reply.code, sess.probe_reply.text, provider)
                if pc.kind == replies.ACCEPTED:
                    self._catchall_set(domain, True, f"random address accepted ({sess.probe_reply.short()})")
                elif pc.kind in (replies.NOT_FOUND, replies.DISABLED):
                    self._catchall_set(domain, False, f"random address rejected ({sess.probe_reply.short()})")
                else:
                    self._catchall_set(domain, None, f"catch-all probe inconclusive ({sess.probe_reply.short()})")
            for p in chunk:
                rep = sess.rcpt.get(addr_of[p.raw])
                if rep is None:
                    # Session ended (timeout / disconnect / 421) before this recipient got a reply.
                    out[p.raw] = SmtpOutcome(
                        "session_failure", sess.mx_host, failure_kind=sess.failure_kind or smtp.RESET,
                        failure_stage="rcpt",
                        failure_detail=sess.failure_detail or "server closed the session before this recipient was tested")
                else:
                    out[p.raw] = SmtpOutcome("reply", sess.mx_host, reply=rep,
                                             rclass=replies.classify_rcpt(rep.code, rep.text, provider))
        return out

    async def _session_with_failover(self, info: DomainInfo, recips: list[str], probe: str | None) -> smtp.SessionResult:
        last = smtp.SessionResult()
        tried = 0
        for host in info.hosts:
            if not host.ips:
                continue
            ip = host.ips[0]
            tried += 1
            async with self.throttle.slot(host.host):
                sess = await smtp.probe_session(
                    host.host, ip, port=self.s.smtp_port, helo=self.s.helo_host,
                    mail_from=self.s.mail_from, recipients=recips, catchall_probe=probe,
                    connect_timeout=self.s.connect_timeout, command_timeout=self.s.command_timeout)
            last = sess
            # Fail over only when we never got an SMTP conversation going.
            if sess.connected and sess.failed_stage != "banner":
                return sess
            if sess.connected and sess.failure_kind != smtp.TIMEOUT and sess.failure_reply is not None:
                return sess   # banner refused with a code: a deliberate answer, do not re-ask
            if tried >= 2:
                break
        return last

    @property
    def throttle(self) -> Throttle:
        t = getattr(self, "_throttle", None)
        if t is None:
            t = self._throttle = Throttle(self.s.global_concurrency, self.s.per_host_concurrency,
                                          self.s.mx_min_interval)
        return t

    def _catchall_get(self, domain: str) -> CatchAllVerdict | None:
        v = self.catchall.get(domain)
        if v and v.expires > time.monotonic():
            return v
        return None

    def _catchall_set(self, domain: str, value: bool | None, detail: str):
        ttl = 86400 if value is not None else 300
        self.catchall[domain] = CatchAllVerdict(value, detail, time.monotonic() + ttl)

    # ---------------------------------------------------------- classification
    def classify(self, p: Prelim, info: DomainInfo, o: SmtpOutcome | None) -> VerificationResult:
        domain = p.syn.domain
        provider = replies.identify_provider(info.mx_names)
        r = VerificationResult(
            email=p.syn.normalized, email_status=Status.UNKNOWN, sub_status="", certainty=Certainty.UNCERTAIN,
            confidence=0, verification_reason="", syntax_valid=True,
            domain_valid={"ok": True, "dns_error": None}.get(info.status, False),
            mx_found=info.mx_found, disposable=False, free_email=p.free, role_based=p.role,
            did_you_mean=p.did_you_mean, mx_host=info.hosts[0].host if info.hosts else "",
            mail_provider=provider.label,
        )

        def done(status, sub, certainty, conf, reason):
            r.email_status, r.sub_status, r.certainty, r.confidence = status, sub, certainty, conf
            r.verification_reason = reason
            if r.did_you_mean and status in (Status.INVALID, Status.RISKY, Status.UNKNOWN, Status.DISPOSABLE):
                r.verification_reason += f" Did you mean {r.did_you_mean}?"
            return r

        C, U = Certainty.CONFIRMED, Certainty.UNCERTAIN
        # --- DNS verdicts ---
        dns_invalid = {
            "nxdomain": ("no_dns_entries", 97),
            "null_mx": ("does_not_accept_mail", 97),
            "no_mail_host": ("no_mail_host", 95),
            "mx_unresolvable": ("no_mail_host", 93),
            "unroutable": ("unroutable_ip_address", 92),
        }
        if info.status in dns_invalid:
            sub, conf = dns_invalid[info.status]
            r.disposable = p.disposable_list
            return done(Status.INVALID, sub, C, conf, info.detail + ".")
        if info.status == "dns_error":
            if p.disposable_list:
                r.disposable = True
                return done(Status.DISPOSABLE, "disposable", C, 95,
                            f"Domain is on the disposable-provider list (DNS lookup failed: {info.detail}).")
            return done(Status.UNKNOWN, "dns_error", U, 0, f"DNS lookup failed ({info.detail}); not treated as invalid.")

        # --- disposable ---
        op = lists.disposable_mx_operator(info.mx_names)
        if p.disposable_list:
            r.disposable = True
            return done(Status.DISPOSABLE, "disposable", C, 95, "Domain is on the disposable-provider list.")
        if op:
            r.disposable = True
            return done(Status.DISPOSABLE, "disposable_mx", C, 85,
                        f"Domain's mail is handled by disposable-mail infrastructure ({op}).")

        mx_desc = info.detail
        # --- SMTP not performed ---
        if o is None or o.status in ("unavailable", "disabled", "not_permitted"):
            r.smtp_status = SmtpStatus.UNAVAILABLE if (o and o.status == "unavailable") else SmtpStatus.NOT_CHECKED
            why = {"unavailable": "outbound SMTP (port 25) is unavailable from this host",
                   "not_permitted": "domain is not in EMAILISH_SMTP_ALLOWED_DOMAINS",
                   }.get(o.status if o else "", "SMTP checks are disabled")
            if p.did_you_mean:
                return done(Status.RISKY, "possible_typo", U, 60,
                            f"Domain looks like a misspelling of a popular provider; mailbox not checked ({why}).")
            if p.trap:
                return done(Status.RISKY, "possible_trap", U, 55,
                            f"Local part matches spam-trap keywords; mailbox not checked ({why}).")
            sub = {"unavailable": "smtp_unavailable", "not_permitted": "smtp_not_permitted"}.get(
                o.status if o else "", "smtp_disabled")
            return done(Status.UNKNOWN, sub, U, 0,
                        f"Domain accepts mail ({mx_desc}). Mailbox existence NOT verified: {why}.")

        r.mx_host = o.mx_host or r.mx_host
        # --- session-level failure ---
        if o.status == "session_failure" and (o.rclass is None or o.failure_stage == "connect"):
            kind = o.failure_kind
            stage = o.failure_stage or "connect"
            if kind == smtp.TIMEOUT:
                r.smtp_status = SmtpStatus.TIMEOUT
                return done(Status.TIMEOUT, "timeout_exceeded", U, 0,
                            f"Mail server {o.mx_host} timed out at the {stage} stage.")
            if kind == smtp.PROTOCOL:
                r.smtp_status = SmtpStatus.PROTOCOL_ERROR
                return done(Status.UNKNOWN, "unexpected_response", U, 0,
                            f"Mail server {o.mx_host} sent a non-SMTP response at the {stage} stage ({o.failure_detail}).")
            if kind == smtp.RESET and stage != "connect":
                r.smtp_status = SmtpStatus.CONNECTION_FAILED
                return done(Status.UNKNOWN, "forcible_disconnect", U, 0,
                            f"Mail server {o.mx_host} dropped the connection at the {stage} stage.")
            r.smtp_status = SmtpStatus.CONNECTION_FAILED
            return done(Status.UNKNOWN, "failed_smtp_connection", U, 0,
                        f"Could not open an SMTP connection to {o.mx_host or 'any MX host'} ({o.failure_detail or kind}).")

        rep = o.reply or o.failure_reply
        rc = o.rclass
        r.smtp_code = (f"{rep.code} " + ".".join(map(str, replies.enhanced(rep.text) or ()))).strip() if rep else ""
        evidence = f"SMTP {rep.short()} from {o.mx_host}" if rep else (o.failure_detail or "no reply")
        stage_note = f" at the {o.failure_stage} stage" if o.status == "session_failure" else ""

        # Session refused with a code (banner/EHLO/MAIL FROM): never a mailbox verdict.
        if o.status == "session_failure":
            if rc.kind == replies.GREYLISTED:
                r.smtp_status = SmtpStatus.GREYLISTED
                return done(Status.UNKNOWN, "greylisted", U, 0, f"Greylisted{stage_note}: {evidence}.")
            if rc.kind in (replies.TEMP,):
                r.smtp_status = SmtpStatus.TEMP_ERROR
                return done(Status.UNKNOWN, "mail_server_temporary_error", U, 0, f"Temporary error{stage_note}: {evidence}.")
            r.smtp_status = SmtpStatus.BLOCKED
            return done(Status.UNKNOWN, "antispam_system", U, 0,
                        f"Mail server refused the verification session{stage_note}: {evidence}.")

        k = rc.kind
        if k in (replies.NOT_FOUND, replies.DISABLED):
            r.smtp_status = SmtpStatus.REJECTED
            ca = self._catchall_get(domain)
            r.catch_all = ca.value if ca else None
            if k == replies.DISABLED:
                return done(Status.INVALID, "mailbox_disabled", C, 90, f"Mailbox disabled: {evidence} ({rc.why}).")
            conf = 88 if "5.4.1" in rc.why else 92
            return done(Status.INVALID, "mailbox_not_found", C, conf, f"Mailbox does not exist: {evidence} ({rc.why}).")

        if k == replies.FULL:
            r.smtp_status = SmtpStatus.MAILBOX_FULL
            return done(Status.RISKY, "mailbox_quota_exceeded", U, 70, f"Mailbox exists but is full: {evidence}.")
        if k == replies.GREYLISTED:
            r.smtp_status = SmtpStatus.GREYLISTED
            return done(Status.UNKNOWN, "greylisted", U, 0, f"Greylisted, mailbox not confirmed: {evidence}.")
        if k == replies.TEMP:
            r.smtp_status = SmtpStatus.TEMP_ERROR
            return done(Status.UNKNOWN, "mail_server_temporary_error", U, 0, f"Temporary error: {evidence}.")
        if k == replies.BLOCKED:
            r.smtp_status = SmtpStatus.BLOCKED
            return done(Status.UNKNOWN, "antispam_system", U, 0,
                        f"Verification blocked by the receiving server (says nothing about the mailbox): {evidence}.")
        if k in (replies.AMBIGUOUS, replies.CANNOT_VERIFY):
            r.smtp_status = SmtpStatus.PROTOCOL_ERROR if k == replies.AMBIGUOUS else SmtpStatus.TEMP_ERROR
            sub = "ambiguous_rejection" if k == replies.AMBIGUOUS else "cannot_verify"
            return done(Status.UNKNOWN, sub, U, 0, f"Inconclusive server response: {evidence} ({rc.why}).")

        # --- accepted ---
        assert k == replies.ACCEPTED
        ca = self._catchall_get(domain)
        r.catch_all = ca.value if ca else None
        ca_note = ca.detail if ca else "catch-all probe not completed"
        if p.did_you_mean:
            r.smtp_status = SmtpStatus.CATCH_ALL if r.catch_all else SmtpStatus.ACCEPTED
            return done(Status.RISKY, "possible_typo", U, 60,
                        f"Server accepted the address, but the domain looks like a misspelling of a popular "
                        f"provider (typo domains often accept everything). {evidence}.")
        if r.catch_all is True:
            r.smtp_status = SmtpStatus.CATCH_ALL
            if p.role:
                return done(Status.ROLE_BASED, "role_based_catch_all", U, 60,
                            f"Role address on a catch-all domain; mailbox cannot be confirmed ({ca_note}).")
            return done(Status.CATCH_ALL, "catch_all", U, 80,
                        f"Server accepts any address ({ca_note}); this mailbox cannot be confirmed. {evidence}.")
        r.smtp_status = SmtpStatus.ACCEPTED
        if r.catch_all is None:
            return done(Status.RISKY, "catch_all_undetermined", U, 50,
                        f"Recipient accepted but catch-all status could not be determined ({ca_note}). {evidence}.")
        # accepted + catch-all probe rejected
        if p.trap:
            return done(Status.RISKY, "possible_trap", U, 55, f"Mailbox accepted, but local part matches spam-trap keywords. {evidence}.")
        conf = 85 if (provider.seg or provider.accept_all_reported) else 90
        extra = f" Note: {provider.label} is known to sometimes accept and bounce later." if conf == 85 else ""
        if p.role:
            return done(Status.ROLE_BASED, "role_based", C, conf,
                        f"Mailbox exists (role/group address). {evidence}; {ca_note}.{extra}")
        return done(Status.VALID, "accepted", C, conf, f"Mailbox exists. {evidence}; {ca_note}.{extra}")
