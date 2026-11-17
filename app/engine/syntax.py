"""Normalization and RFC syntax validation (no network access)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email

_MAILTO = re.compile(r"^mailto:", re.I)
_WRAPPERS = "<>\"' \t\r\n;,"
# Invisible format characters that copy-paste from web pages / Excel leaves around a value.
# str.strip() does not remove them (they are not Unicode whitespace). They are only trimmed
# at the edges; inside an address they remain an error.
_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff"


@dataclass
class SyntaxResult:
    ok: bool
    normalized: str          # lowercased-domain address, or the cleaned input
    local: str = ""
    domain: str = ""         # ASCII (IDNA) form, used for DNS/SMTP
    reason: str = ""


def _trim(s: str, chars: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = s.strip().strip(chars)
    return s


def is_blank(raw: str) -> bool:
    """True for an empty cell: nothing but whitespace / invisible characters."""
    return not _trim(raw or "", _INVISIBLE)


def clean(raw: str) -> str:
    """Strip the wrappers people paste into spreadsheets: spaces, invisible characters,
    <>, quotes, mailto:."""
    s = _trim(raw or "", _WRAPPERS + _INVISIBLE)
    s = _trim(_MAILTO.sub("", s), _WRAPPERS + _INVISIBLE)
    return s


def check_syntax(raw: str) -> SyntaxResult:
    s = clean(raw)
    if not s:
        if is_blank(raw):
            return SyntaxResult(False, "", reason="No email address in this cell")
        return SyntaxResult(False, "", reason="No address found: the cell only contains wrapper "
                                              f"characters ({(raw or '').strip()[:40]!r})")
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
