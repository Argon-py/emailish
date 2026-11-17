"""Normalization and RFC syntax validation (no network access)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email

_MAILTO = re.compile(r"^mailto:", re.I)
_WRAPPERS = "<>\"' \t\r\n;,"


@dataclass
class SyntaxResult:
    ok: bool
    normalized: str          # lowercased-domain address, or the cleaned input
    local: str = ""
    domain: str = ""         # ASCII (IDNA) form, used for DNS/SMTP
    reason: str = ""


def clean(raw: str) -> str:
    """Strip the wrappers people paste into spreadsheets: spaces, <>, quotes, mailto:."""
    s = (raw or "").strip().strip(_WRAPPERS)
    s = _MAILTO.sub("", s).strip(_WRAPPERS)
    return s


def check_syntax(raw: str) -> SyntaxResult:
    s = clean(raw)
    if not s:
        return SyntaxResult(False, "", reason="No email address in this cell")
    if s.count("@") != 1:
        return SyntaxResult(False, s, reason="Address must contain exactly one '@'")
    local, _, domain = s.rpartition("@")
    if local.startswith('"'):
        return SyntaxResult(False, s, reason="Quoted local parts are not accepted for mailing lists")
    if domain.startswith("["):
        return SyntaxResult(False, s, reason="IP-literal domains are not accepted for mailing lists")
    try:
        v = validate_email(
            s,
            check_deliverability=False,   # DNS is done separately so we control the outcome
            allow_smtputf8=True,
            allow_quoted_local=False,
            allow_domain_literal=False,
        )
    except EmailNotValidError as e:
        return SyntaxResult(False, s, reason=f"Fails RFC syntax: {e}")
    ascii_domain = (v.ascii_domain or v.domain).lower()
    # Keep the local part's case (RFC 5321: local part may be case-sensitive), lowercase domain.
    normalized = f"{v.local_part}@{v.domain.lower()}"
    if len(v.local_part.encode()) > 64:
        return SyntaxResult(False, s, reason="Local part longer than 64 octets (RFC 5321)")
    if len(normalized.encode()) > 254:
        return SyntaxResult(False, s, reason="Address longer than 254 octets (RFC 5321)")
    return SyntaxResult(True, normalized, v.local_part, ascii_domain)
