"""List-based intelligence: disposable, free, role, spam-trap keywords, typo suggestions."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def _load(name: str) -> set[str]:
    out = set()
    p = DATA / name
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.split("\t")[0].strip().lower())
    return out


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    return frozenset(_load("disposable_domains.txt") - _load("disposable_allowlist.txt"))


@lru_cache(maxsize=1)
def disposable_mx_operators() -> frozenset[str]:
    return frozenset(_load("disposable_mx_operators.txt"))


@lru_cache(maxsize=1)
def allowlist() -> frozenset[str]:
    return frozenset(_load("disposable_allowlist.txt"))


@lru_cache(maxsize=1)
def free_domains() -> frozenset[str]:
    return frozenset((_load("free_domains.txt") | allowlist()) - disposable_domains())


def _parents(domain: str):
    """example.mail.foo.com -> example.mail.foo.com, mail.foo.com, foo.com"""
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        yield ".".join(parts[i:])


def is_disposable_domain(domain: str) -> bool:
    d = domain.lower()
    if d in allowlist():
        return False
    return any(p in disposable_domains() for p in _parents(d))


def disposable_mx_operator(mx_hosts: list[str]) -> str:
    """Return the disposable operator that runs these MX hosts, or ''."""
    ops = disposable_mx_operators()
    for h in mx_hosts:
        for p in _parents(h.lower().rstrip(".")):
            if p in ops:
                return p
    return ""


def is_free_domain(domain: str) -> bool:
    return domain.lower() in free_domains()


# --- role accounts ---------------------------------------------------------
ROLE_LOCALS = frozenset("""
abuse accounting accounts admin administration administrator admissions all billing
bookings booking careers ceo cfo compliance contact contactus contact-us customercare
customerservice customer.service customersupport cs dev developers devnull do-not-reply
donotreply editor editorial enquiries enquiry everyone events feedback finance founders
general hello help helpdesk hostmaster hr info information inquiries inquiry investor
investors it jobs legal list mail mailer-daemon management marketing media news
newsletter no-reply noreply notifications office operations ops orders partners
payments postmaster press privacy procurement purchasing reception recruiting
recruitment reservations root sales security service services staff subscribe
support sysadmin team tech technical unsubscribe webmaster welcome
""".split())


def local_base(local: str) -> str:
    return local.lower().split("+", 1)[0]


def is_role(local: str) -> bool:
    base = local_base(local)
    if base in ROLE_LOCALS:
        return True
    # "sales.uk", "support-team", "info_de": role word followed by a qualifier
    for sep in (".", "-", "_"):
        head = base.split(sep, 1)[0]
        if head != base and head in ROLE_LOCALS:
            return True
    return False


TRAP_LOCALS = frozenset({"spam", "spamtrap", "spam-trap", "spam_trap", "honeypot", "trap", "blackhole"})


def is_possible_trap(local: str, domain: str) -> bool:
    """Keyword heuristic only (same idea as ZeroBounce's possible_trap). Real trap data is proprietary."""
    return local_base(local) in TRAP_LOCALS or domain.split(".")[0] in {"spamtrap", "spam-trap", "honeypot"}


# --- typo suggestions --------------------------------------------------------
POPULAR = (
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "gmx.com", "gmx.de", "web.de", "mail.ru", "yandex.ru",
    "yandex.com", "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "bellsouth.net",
    "cox.net", "charter.net", "earthlink.net", "btinternet.com", "orange.fr", "free.fr",
    "laposte.net", "libero.it", "yahoo.co.uk", "hotmail.co.uk", "yahoo.fr", "hotmail.fr",
    "yahoo.co.in", "rediffmail.com", "qq.com", "163.com", "126.com", "zoho.com", "mail.com",
    "fastmail.com", "t-online.de", "shaw.ca", "rogers.com", "sky.com", "virginmedia.com",
)
_POPULAR_SET = frozenset(POPULAR)
# Real, independent domains that are one edit away from a popular domain.
REAL_LOOKALIKES = frozenset({
    "email.com", "mail.com", "gmx.net", "gmx.at", "gmx.ch", "aon.com", "aol.de", "aim.com",
    "hotmail.de", "hotmail.it", "hotmail.es", "live.co.uk", "live.fr", "me.org", "msn.cn",
    "yahoo.de", "yahoo.it", "yahoo.es", "yahoo.ca", "yahoo.com.au", "outlook.de", "outlook.fr",
    "web.com", "qq.cn", "mail.de", "live.de", "att.com", "cox.com", "sky.net", "verizon.com",
})
_TLD_TYPOS = {
    "con": "com", "cm": "com", "om": "com", "comm": "com", "cmo": "com", "vom": "com",
    "xom": "com", "co": "com", "ocm": "com", "coom": "com", "cpm": "com", "clm": "com",
    "nte": "net", "nt": "net", "ne": "net", "nett": "net", "met": "net",
}


def _dl_distance(a: str, b: str) -> int:
    """Optimal-string-alignment Damerau-Levenshtein distance."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def suggest_domain(domain: str) -> str:
    """Return a likely intended popular domain, or '' when the domain looks intentional."""
    d = domain.lower()
    if d in _POPULAR_SET or d in REAL_LOOKALIKES:
        return ""
    sld, _, tld = d.rpartition(".")
    # TLD typo on an exact popular second-level name: gmail.con -> gmail.com
    if tld in _TLD_TYPOS:
        cand = f"{sld}.{_TLD_TYPOS[tld]}"
        if cand in _POPULAR_SET:
            return cand
    best, best_dist = "", 99
    for p in POPULAR:
        p_sld = p.rsplit(".", 1)[0] if p.count(".") == 1 else p
        # Short names (aol, msn, me, qq) produce too many false matches by edit distance.
        if len(p_sld) < 5:
            continue
        dist = _dl_distance(d, p)
        # Distance 2 only for long names (googlemail, protonmail, btinternet...). Otherwise
        # real domains such as yopmail.com would be "corrected" to hotmail.com.
        limit = 2 if len(p_sld) >= 9 else 1
        if dist <= limit and dist < best_dist:
            best, best_dist = p, dist
    return best
