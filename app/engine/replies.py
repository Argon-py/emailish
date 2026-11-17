"""SMTP reply interpretation (RFC 5321 basic codes + RFC 3463 enhanced codes) and
mail-provider profiles identified from MX hostnames.

Design rule: a permanent (5xx) failure is classified as "mailbox does not exist"
ONLY when the enhanced code or the text identifies the recipient. A 5xx caused by
policy, reputation or blocking says nothing about the mailbox and becomes
'blocked' (UNKNOWN), and an unrecognized 5xx becomes 'ambiguous_reject' (UNKNOWN).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- provider profiles ---------------------------------------------------------
@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    seg: bool = False                  # secure email gateway in front of the real server
    accept_all_reported: bool = False  # widely reported to accept any RCPT and bounce later
    dbeb_541: bool = False             # Exchange Online Directory-Based Edge Blocking


_PROVIDERS: list[tuple[re.Pattern, Provider]] = [
    (re.compile(r"(^|\.)(google\.com|googlemail\.com|smtp\.google\.com)$"), Provider("google", "Google")),
    (re.compile(r"\.olc\.protection\.outlook\.com$"), Provider("outlook_consumer", "Outlook.com / Hotmail")),
    (re.compile(r"\.mail\.protection\.outlook\.com$"), Provider("microsoft365", "Microsoft 365", dbeb_541=True)),
    (re.compile(r"(^|\.)yahoodns\.net$"), Provider("yahoo", "Yahoo / AOL", accept_all_reported=True)),
    (re.compile(r"(^|\.)mail\.icloud\.com$"), Provider("icloud", "Apple iCloud")),
    (re.compile(r"(^|\.)(zoho\.com|zoho\.eu|zohomail\.com)$"), Provider("zoho", "Zoho")),
    (re.compile(r"(^|\.)protonmail\.ch$"), Provider("proton", "Proton")),
    (re.compile(r"(^|\.)messagingengine\.com$"), Provider("fastmail", "Fastmail")),
    (re.compile(r"(^|\.)(yandex\.net|yandex\.ru)$"), Provider("yandex", "Yandex")),
    (re.compile(r"(^|\.)mail\.ru$"), Provider("mailru", "Mail.ru")),
    (re.compile(r"(^|\.)(gmx\.net|web\.de)$"), Provider("gmx", "GMX / WEB.DE")),
    (re.compile(r"(^|\.)secureserver\.net$"), Provider("godaddy", "GoDaddy")),
    (re.compile(r"(^|\.)mx\.cloudflare\.net$"), Provider("cloudflare", "Cloudflare Email Routing")),
    (re.compile(r"(^|\.)mimecast\.com$"), Provider("mimecast", "Mimecast (SEG)", seg=True)),
    (re.compile(r"(^|\.)(pphosted\.com|ppe-hosted\.com)$"), Provider("proofpoint", "Proofpoint (SEG)", seg=True)),
    (re.compile(r"(^|\.)barracudanetworks\.com$"), Provider("barracuda", "Barracuda (SEG)", seg=True)),
    (re.compile(r"(^|\.)iphmx\.com$"), Provider("cisco", "Cisco Secure Email (SEG)", seg=True)),
]
GENERIC = Provider("other", "")


def identify_provider(mx_hosts: list[str]) -> Provider:
    for h in mx_hosts:
        h = h.lower().rstrip(".")
        for pat, prov in _PROVIDERS:
            if pat.search(h):
                return prov
    return GENERIC


# --- reply classification ------------------------------------------------------
ACCEPTED = "accepted"
NOT_FOUND = "mailbox_not_found"
DISABLED = "mailbox_disabled"
FULL = "mailbox_full"
BLOCKED = "blocked"
GREYLISTED = "greylisted"
TEMP = "temp_error"
AMBIGUOUS = "ambiguous_reject"
CANNOT_VERIFY = "cannot_verify"


@dataclass(frozen=True)
class ReplyClass:
    kind: str
    why: str


_ENH = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")

_NOT_FOUND_TXT = re.compile(
    r"user unknown|unknown user|no such (user|mailbox|recipient|account|address)|"
    r"does ?n[o']t exist|not exist|recipient (unknown|not found|invalid)|unknown recipient|"
    r"invalid (recipient|mailbox|address)|mailbox (not found|unavailable|does not exist|unknown)|"
    r"user not found|no mailbox|not a valid mailbox|unrouteable address|undeliverable address|"
    r"address (not found|unknown)|unknown (local part|address|mailbox)|account (not found|does not exist)|"
    r"illegal alias|user doesn't have|no account|recipient rejected: user",
    re.I,
)
_DISABLED_TXT = re.compile(r"disabled|deactivated|suspended|inactive|no longer (active|available|in use)|closed", re.I)
_FULL_TXT = re.compile(r"quota|mailbox (is )?full|over ?quota|insufficient (system )?storage|exceeded storage", re.I)
_GREY_TXT = re.compile(r"grey ?list|gray ?list|greylisted|try again later|please (try again|retry)|temporar(il)?y (deferred|rejected)|deferred", re.I)
_BLOCK_TXT = re.compile(
    r"spamhaus|spamcop|sorbs|barracudacentral|\brbl\b|dnsbl|block ?list|black ?list|blocked|"
    r"listed (at|on|in)|reputation|banned|denied by policy|policy (violation|reasons|rejection)|"
    r"\bpolicy\b|not allowed|not permitted|client host|unauthori[sz]ed|unauthenticated|"
    r"reverse (dns|lookup)|\bptr\b|helo|rate limit|too many|unusual rate|unsolicited|spam|"
    r"relay(ing)? (access )?denied|access denied|refused|abuse",
    re.I,
)


def enhanced(text: str) -> tuple[int, int, int] | None:
    m = _ENH.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def classify_rcpt(code: int, text: str, provider: Provider = GENERIC) -> ReplyClass:
    text = text or ""
    if code in (250, 251):
        return ReplyClass(ACCEPTED, "recipient accepted")
    if code == 252:
        return ReplyClass(CANNOT_VERIFY, "252: server cannot verify the user but would attempt delivery")

    enh = enhanced(text)
    nf, dis, full = bool(_NOT_FOUND_TXT.search(text)), bool(_DISABLED_TXT.search(text)), bool(_FULL_TXT.search(text))
    grey, block = bool(_GREY_TXT.search(text)), bool(_BLOCK_TXT.search(text))

    if 400 <= code < 500:
        if enh and enh[1:] == (2, 2) or full:
            return ReplyClass(FULL, "temporary: mailbox over quota")
        if grey:
            return ReplyClass(GREYLISTED, "temporary deferral (greylisting)")
        if block or (enh and enh[1] == 7 and enh[2] != 1):
            return ReplyClass(BLOCKED, "temporary block / rate limit of the verifying host")
        if enh and enh[1:] == (7, 1):
            return ReplyClass(GREYLISTED, "4.7.1 temporary rejection (typically greylisting)")
        return ReplyClass(TEMP, "temporary failure")

    if not 500 <= code < 600:
        return ReplyClass(AMBIGUOUS, f"unexpected reply code {code}")

    # --- permanent failures ---
    if enh:
        _, subj, det = enh
        if subj == 1 and det in (0, 1, 6, 10):
            return ReplyClass(NOT_FOUND, f"5.1.{det}: recipient address rejected")
        if subj == 1 and det == 2:
            return ReplyClass(NOT_FOUND, "5.1.2: destination domain not handled by this server")
        if subj == 1 and det in (7, 8):
            return ReplyClass(BLOCKED, f"5.1.{det}: our probe's sender address was rejected")
        if subj == 2 and det == 1:
            return ReplyClass(DISABLED, "5.2.1: mailbox disabled")
        if subj == 2 and det == 2:
            return ReplyClass(FULL, "5.2.2: mailbox full")
        if subj == 4 and det == 1 and provider.dbeb_541:
            return ReplyClass(NOT_FOUND, "5.4.1 from Microsoft 365: recipient not in directory (Directory-Based Edge Blocking)")
        if subj == 7:
            if nf and not block:
                return ReplyClass(NOT_FOUND, "5.7.x with explicit unknown-recipient text")
            return ReplyClass(BLOCKED, f"5.7.{det}: policy/security rejection of the verifying host")

    if full or code == 552:
        return ReplyClass(FULL, "mailbox full / storage exceeded")
    if dis and not block:
        return ReplyClass(DISABLED, "mailbox disabled or inactive")
    if nf and not block:
        return ReplyClass(NOT_FOUND, "recipient explicitly rejected as unknown")
    if block and not nf:
        return ReplyClass(BLOCKED, "permanent rejection of the verifying host (policy/blocklist)")
    return ReplyClass(AMBIGUOUS, "permanent rejection without a recognizable recipient-specific reason")
