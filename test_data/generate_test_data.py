"""Generate the synthetic QA list (1,000 records) and its answer key.

Expected results are NOT produced by the emailish engine. They come from:
  1. explicit rules written here (syntax cases, role words, typo targets, duplicates), and
  2. an independent DNS oracle (a small DNS-over-HTTPS client in this file that shares no
     code with app/engine), frozen into dns_evidence.json with a timestamp,
  3. the vendored datasets (app/data/*.txt), parsed independently.
The expected status for DNS-dependent records follows the classification spec in
research/technical-architecture.md §5 with SMTP DISABLED.

Every record carries an expectation_type:
  DETERMINISTIC_OFFLINE   no network needed (syntax, empty cells, list flags, typo hint)
  DETERMINISTIC_DNS       fixed by public DNS facts captured in dns_evidence.json
  REQUIRES_SMTP_EVIDENCE  mailbox existence unknowable without SMTP; with SMTP disabled the
                          only correct answer is UNKNOWN (smtp_disabled), never VALID

No local part here is a real inbox: free-provider addresses use an obviously synthetic
'emailish.qa.*' local part or a '+emailish-qa' tag, and nothing is ever sent to them.

  python test_data/generate_test_data.py            # uses cached dns_evidence.json if present
  python test_data/generate_test_data.py --refresh-dns
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import ipaddress
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import httpx
from openpyxl import Workbook

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "app" / "data"
SEED = 20261117
N = 1000

FREE_PROVIDERS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com",
                  "protonmail.com", "gmx.com", "zoho.com", "yandex.com", "mail.com", "live.com"]
NULL_MX_RESERVED = ["example.com", "example.org", "example.net"]          # RFC 2606 + RFC 7505 Null MX
RESERVED_TLD_SYNTAX = ["test", "invalid", "localhost"]                    # RFC 6761 special-use: rejected by syntax
RESERVED_TLD_DNS = ["example"]                                            # RFC 2606: never delegated -> NXDOMAIN
ROLE_WORDS = ["info", "sales", "support", "admin", "contact", "billing", "hr", "marketing", "office",
              "noreply", "no-reply", "postmaster", "webmaster", "help", "jobs", "press", "team", "accounts"]
NOT_ROLE = ["infomaniac", "john.sales", "salesforce.fan", "adminton", "helpful.hannah", "teamwork.qa"]
TYPOS = {  # typo domain -> intended domain (independently written, not taken from the engine)
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gmail.con": "gmail.com",
    "gmaill.com": "gmail.com", "gnail.com": "gmail.com", "hotmial.com": "hotmail.com",
    "hotmal.com": "hotmail.com", "hotmail.co": "hotmail.com", "yaho.com": "yahoo.com",
    "yahooo.com": "yahoo.com", "yahoo.con": "yahoo.com", "outlok.com": "outlook.com",
    "outllok.com": "outlook.com", "icloud.co": "icloud.com", "protonmial.com": "protonmail.com",
    "googlemial.com": "googlemail.com", "aol.con": "aol.com",
}
# (raw value, why it is invalid) -- RFC 5321/5322 based, written by hand
INVALID_SYNTAX = [
    ("plainaddress", "no @"), ("emailish.qa.noat.example.com", "no @"), ("@example.com", "empty local part"),
    ("emailish.qa@", "empty domain"), ("emailish.qa@@gmail.com", "two @"), ("a@b@gmail.com", "two @"),
    ("emailish..qa@gmail.com", "consecutive dots in local part"), (".emailish.qa@gmail.com", "leading dot"),
    ("emailish.qa.@gmail.com", "trailing dot in local part"), ("emailish qa@gmail.com", "space in local part"),
    ("emailish.qa@gmail", "domain without TLD"), ("emailish.qa@gmail..com", "consecutive dots in domain"),
    ("emailish.qa@.gmail.com", "domain starts with dot"), ("emailish.qa@gmail.com.", "trailing dot in domain"),
    ("emailish.qa@-gmail.com", "domain label starts with hyphen"), ("emailish.qa@gmail-.com", "label ends with hyphen"),
    ("emailish.qa@gm ail.com", "space in domain"), ("emailish,qa@gmail.com", "comma in local part"),
    ("emailish.qa@gmail,com", "comma in domain"), ("emailish(qa)@gmail.com", "parentheses"),
    ("emailish[qa]@gmail.com", "brackets"), ('"emailish qa"@gmail.com', "quoted local part (rejected by policy)"),
    ("emailish.qa@[192.168.1.1]", "IP literal (rejected by policy)"), ("x" * 65 + "@gmail.com", "local part > 64"),
    ("emailish.qa@" + "a" * 64 + ".com", "domain label > 63"), ("emailish.qa@@", "only @ signs"),
    ("emailish.qa@123.123.123.123", "bare IP as domain"), ("emailish.qa@gmail_com", "underscore, no dot"),
    ("emailish.qa@sub_domain.gmail.com", "underscore in domain label"), ("mailto:", "mailto with nothing"),
    ("<>", "empty angle brackets"), ("emailish.qa@gmail.com>extra", "trailing junk"),
    ("emailish.qa@gmail.com,other@gmail.com", "two addresses in one cell"),
    ("emailish.qa@gmail.com other@gmail.com", "two addresses separated by space"),
    ("N/A", "placeholder text"), ("none", "placeholder text"), ("-", "placeholder text"),
    ("emailish.qa(at)gmail.com", "obfuscated @"), ("emailish.qa at gmail dot com", "obfuscated"),
    ("emailish.qa@gmail.com\nsecond", "embedded newline"),
]
# QA round 1 correction: a one-letter TLD is legal SYNTAX (RFC 1035 labels may be 1 char);
# such addresses fail on DNS (NXDOMAIN), so they are tested as nonexistent domains, not syntax.
NONEXISTENT_DOMAINS = ["gmail.c", "emailish-qa-nonexistent.zzqq", "emailish-qa.notarealtld"]
# QA round 1 correction: listed domains that are themselves one edit away from a popular
# provider. The documented typo rule (edit distance 1) applies to them as to any domain.
KNOWN_LOOKALIKES = {"amail.com": "gmail.com"}
WHITESPACE_WRAPPERS = [(" ", ""), ("", " "), ("  ", "  "), ("\t", ""), ("", "\t"), (" ", ""),
                       ("", " "), (" \t ", " "), ("", "​"), ("​", "")]
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn"]
LAST = ["Testwell", "Sampleton", "Fakely", "Mockford", "Dummer", "Placeholder", "Synthwood", "Example"]
COMPANIES = ["Acme QA Ltd", "Globex Test Co", "Initech Sample", "Umbrella Synthetic", "Hooli Mock"]
NOTES = ["", "", "", "=SUM(1,2)", "+44 20 7946 0000", "-follow up", "@mention", "=HYPERLINK(\"http://x\")",
         "line1; line2", "comma, inside", 'quote "inside"', "ünïcödé ✓"]


# --------------------------------------------------------------- independent DNS oracle
def _load(name: str) -> set[str]:
    out = set()
    for line in (DATA / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.split("\t")[0].lower())
    return out


async def _doh(client, name: str, rtype: str) -> dict:
    for url in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
        try:
            r = await client.get(url, params={"name": name, "type": rtype},
                                 headers={"accept": "application/dns-json"}, timeout=10)
            j = r.json()
            if j.get("Status") in (0, 3):
                want = {"MX": 15, "A": 1, "AAAA": 28}[rtype]
                return {"rcode": j["Status"], "answers": [a["data"] for a in j.get("Answer", []) if a.get("type") == want]}
        except Exception:
            continue
    return {"rcode": "error", "answers": []}


async def oracle_domain(client, sem, domain: str) -> dict:
    """Facts only: what DNS says about this domain's mail route."""
    async with sem:
        mx = await _doh(client, domain, "MX")
        ev = {"domain": domain, "mx_rcode": mx["rcode"], "mx": mx["answers"]}
        if mx["rcode"] == "error":
            ev["fact"] = "dns_error"
            return ev
        if mx["rcode"] == 3:
            ev["fact"] = "nxdomain"
            return ev
        hosts = []
        for d in mx["answers"]:
            pref, _, host = d.partition(" ")
            hosts.append((int(pref), host.rstrip(".").lower()))
        hosts.sort()
        if hosts and all(h in ("", ".") for _, h in hosts):
            ev["fact"] = "null_mx"
            return ev
        targets = [h for _, h in hosts if h not in ("", ".")][:3] if hosts else [domain]
        ev["implicit_mx"] = not hosts
        ips = {}
        for h in targets:
            a, aaaa = await _doh(client, h, "A"), await _doh(client, h, "AAAA")
            ips[h] = a["answers"] + aaaa["answers"]
        ev["mx_ips"] = ips
        all_ips = [ip for v in ips.values() for ip in v]
        if not all_ips:
            ev["fact"] = "no_mail_host"
            return ev
        def glob(ip):
            try:
                return ipaddress.ip_address(ip).is_global
            except ValueError:
                return False
        ev["fact"] = "ok" if any(glob(ip) for ip in all_ips) else "unroutable"
        ev["mx_hosts"] = [h for _, h in hosts]
        return ev


async def run_oracle(domains: list[str]) -> dict[str, dict]:
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(*(oracle_domain(c, sem, d) for d in domains))
    return {r["domain"]: r for r in res}


# --------------------------------------------------------------- expectation rules
class Spec:
    """Expected outcome per research/technical-architecture.md §5, SMTP disabled."""

    def __init__(self, evidence: dict[str, dict]):
        allow = _load("disposable_allowlist.txt")
        self.disposable = _load("disposable_domains.txt") - allow
        self.allow = allow
        self.free = (_load("free_domains.txt") | allow) - self.disposable
        self.disp_mx = _load("disposable_mx_operators.txt")
        self.ev = evidence

    def is_disposable(self, domain: str) -> bool:
        if domain in self.allow:
            return False
        parts = domain.split(".")
        return any(".".join(parts[i:]) in self.disposable for i in range(len(parts) - 1))

    def disposable_mx(self, hosts: list[str]) -> bool:
        for h in hosts:
            parts = h.split(".")
            if any(".".join(parts[i:]) in self.disp_mx for i in range(len(parts) - 1)):
                return True
        return False

    def expect(self, domain: str, *, role: bool, typo_target: str = "") -> dict:
        ev = self.ev[domain]
        fact = ev["fact"]
        e = {"expected_syntax_valid": "Yes", "expected_disposable": "Yes" if self.is_disposable(domain) else "No",
             "expected_free_email": "Yes" if domain in self.free else "No",
             "expected_role_based": "Yes" if role else "No", "dns_fact": fact}
        e["expected_mx_found"] = {"nxdomain": "No", "null_mx": "Yes", "dns_error": "Unknown"}.get(
            fact, "No" if ev.get("implicit_mx") else "Yes")
        e["expected_domain_valid"] = {"ok": "Yes", "dns_error": "Unknown"}.get(fact, "No")
        if fact in ("nxdomain", "null_mx", "no_mail_host", "unroutable"):
            sub = {"nxdomain": "no_dns_entries", "null_mx": "does_not_accept_mail",
                   "no_mail_host": "no_mail_host", "unroutable": "unroutable_ip_address"}[fact]
            e.update(expected_status="INVALID", expected_sub_status=sub)
        elif fact == "dns_error":
            e.update(expected_status="DISPOSABLE" if e["expected_disposable"] == "Yes" else "UNKNOWN",
                     expected_sub_status="disposable" if e["expected_disposable"] == "Yes" else "dns_error")
        elif e["expected_disposable"] == "Yes":
            e.update(expected_status="DISPOSABLE", expected_sub_status="disposable")
        elif self.disposable_mx(ev.get("mx_hosts", [])):
            e.update(expected_status="DISPOSABLE", expected_sub_status="disposable_mx", expected_disposable="Yes")
        elif typo_target:
            e.update(expected_status="RISKY", expected_sub_status="possible_typo")
        else:
            e.update(expected_status="UNKNOWN", expected_sub_status="smtp_disabled")
        return e


INVALID_ROW = {"expected_status": "INVALID", "expected_sub_status": "failed_syntax_check",
               "expected_syntax_valid": "No", "expected_disposable": "No", "expected_free_email": "No",
               "expected_role_based": "No", "expected_mx_found": "Unknown", "expected_domain_valid": "Unknown",
               "dns_fact": ""}


# --------------------------------------------------------------- generation
def build(evidence: dict[str, dict], rng: random.Random, disposable_sample: list[str]) -> list[dict]:
    spec = Spec(evidence)
    recs: list[dict] = []

    def add(email, category, scenario, etype, exp, *, typo="", dup_of="", note=""):
        recs.append({"email": email, "category": category, "scenario": scenario, "expectation_type": etype,
                     "expected_did_you_mean": typo, "duplicate_of": dup_of, "rationale": note, **exp})

    def synth(i: int) -> str:
        return f"emailish.qa.{i:04d}.{rng.randrange(16**4):04x}"

    # 1. valid syntax at free providers (200): mailbox unknowable without SMTP
    for i in range(200):
        d = FREE_PROVIDERS[i % len(FREE_PROVIDERS)]
        local = synth(i)
        if i % 10 == 3:
            local += "+tag"
        if i % 10 == 7:
            local = local.replace(".", "_")
        add(f"{local}@{d}", "valid_syntax_free_provider", "well-formed synthetic address at a free provider",
            "REQUIRES_SMTP_EVIDENCE", spec.expect(d, role=False),
            note="mailbox existence needs SMTP; with SMTP disabled the spec answer is UNKNOWN/smtp_disabled")
    # 2. other valid syntax shapes (37) + syntactically valid but nonexistent domains (3)
    shapes = ["{s}@GMAIL.COM", "{s}@Yahoo.Com", "first.last.{n}@outlook.com", "{s}+news@gmail.com",
              "o'connor.{n}@gmail.com", "{s}@bücher.de", "{s}@gmx.com", "a.b.c.{n}@icloud.com"]
    for i, d in enumerate(NONEXISTENT_DOMAINS):
        add(f"{synth(1900 + i)}@{d}", "nonexistent_domain", "valid syntax, domain does not exist",
            "DETERMINISTIC_DNS", spec.expect(d, role=False))
    for i in range(37):
        s = synth(1000 + i)
        raw = shapes[i % len(shapes)].format(s=s, n=f"qa{i:03d}")
        domain = raw.rsplit("@", 1)[1].lower()
        dom_ascii = domain.encode("idna").decode() if domain == "bücher.de" else domain
        add(raw, "valid_syntax_other", "uppercase / plus-tag / apostrophe / IDN",
            "REQUIRES_SMTP_EVIDENCE", spec.expect(dom_ascii, role=False))
    # 3. Null-MX reserved domains (60)
    for i in range(60):
        d = NULL_MX_RESERVED[i % 3]
        add(f"{synth(2000 + i)}@{d}", "reserved_domain_null_mx", "RFC 2606 domain publishing RFC 7505 Null MX",
            "DETERMINISTIC_DNS", spec.expect(d, role=False))
    # 4. reserved special-use TLDs (80)
    for i in range(80):
        if i < 60:
            tld = RESERVED_TLD_SYNTAX[i % 3]
            add(f"{synth(3000 + i)}@qa-{i:02d}.{tld}", "reserved_tld", f".{tld} special-use name (RFC 6761)",
                "DETERMINISTIC_OFFLINE", dict(INVALID_ROW, expected_sub_status="failed_syntax_check"),
                note="email-validator rejects special-use names; can never receive mail")
        else:
            d = f"qa-{i:02d}.example"
            add(f"{synth(3000 + i)}@{d}", "reserved_tld", ".example TLD (RFC 2606): never delegated",
                "DETERMINISTIC_DNS", spec.expect(d, role=False))
    # 5. invalid syntax (120 = 40 patterns x 3)
    for i in range(120):
        raw, why = INVALID_SYNTAX[i % len(INVALID_SYNTAX)]
        add(raw, "invalid_syntax", why, "DETERMINISTIC_OFFLINE", dict(INVALID_ROW))
    # 6. role-based (90): 54 at reserved Null-MX domains, 24 tagged at free providers, 12 non-role look-alikes
    for i in range(54):
        w, d = ROLE_WORDS[i % len(ROLE_WORDS)], NULL_MX_RESERVED[i % 3]
        add(f"{w}@{d}", "role_based", f"role word '{w}' at a Null-MX domain", "DETERMINISTIC_DNS",
            spec.expect(d, role=True), note="role flag Yes; status INVALID because domain accepts no mail")
    for i in range(24):
        w, d = ROLE_WORDS[i % len(ROLE_WORDS)], FREE_PROVIDERS[i % len(FREE_PROVIDERS)]
        add(f"{w}+emailish-qa{i:02d}@{d}", "role_based", f"role word '{w}' (+tag) at a free provider",
            "REQUIRES_SMTP_EVIDENCE", spec.expect(d, role=True),
            note="role flag Yes; status stays UNKNOWN: ROLE_BASED is only given after the mailbox is confirmed")
    for i in range(12):
        w, d = NOT_ROLE[i % len(NOT_ROLE)], FREE_PROVIDERS[(i + 3) % len(FREE_PROVIDERS)]
        add(f"{w}.{i:02d}@{d}", "role_based", "look-alike that is NOT a role word", "REQUIRES_SMTP_EVIDENCE",
            spec.expect(d, role=False))
    # 7. typo domains (90 = 18 typos x 5)
    typo_items = list(TYPOS.items())
    for i in range(90):
        d, target = typo_items[i % len(typo_items)]
        local = synth(4000 + i)
        add(f"{local}@{d}", "typo_domain", f"{d} -> {target}", "DETERMINISTIC_DNS",
            spec.expect(d, role=False, typo_target=target), typo=f"{local}@{target}",
            note="did_you_mean is offline-deterministic; status depends on whether the typo domain resolves")
    # 8. disposable domains from the dataset (120)
    for i in range(120):
        d = disposable_sample[i]
        local = synth(5000 + i)
        add(f"{local}@{d}", "disposable", "domain taken from app/data/disposable_domains.txt",
            "DETERMINISTIC_DNS", spec.expect(d, role=False),
            typo=f"{local}@{KNOWN_LOOKALIKES[d]}" if d in KNOWN_LOOKALIKES else "",
            note="disposable flag is offline-deterministic; dead disposable domains are INVALID (no mail route)")
    # 9. leading/trailing whitespace (60)
    for i in range(60):
        pre, post = WHITESPACE_WRAPPERS[i % len(WHITESPACE_WRAPPERS)]
        d = FREE_PROVIDERS[i % len(FREE_PROVIDERS)] if i % 2 == 0 else NULL_MX_RESERVED[i % 3]
        core = f"{synth(6000 + i)}@{d}"
        exp = spec.expect(d, role=False)
        add(pre + core + post, "whitespace", f"wrapped in {pre!r} / {post!r}",
            "REQUIRES_SMTP_EVIDENCE" if d in FREE_PROVIDERS else "DETERMINISTIC_DNS", exp,
            note="must be trimmed and classified exactly like the bare address")
    # 10. empty cells (40)
    for i in range(40):
        raw = ["", " ", "   ", "\t"][i % 4]
        add(raw, "empty", f"empty cell {raw!r}", "DETERMINISTIC_OFFLINE",
            dict(INVALID_ROW, expected_sub_status="empty"))
    # 11. duplicates (100) of earlier deduplicable addresses, with variations
    pool = [r for r in recs if r["category"] in ("valid_syntax_free_provider", "reserved_domain_null_mx", "disposable")]
    variants = [lambda e: e, lambda e: e.upper(), lambda e: f"  {e} ", lambda e: f"<{e}>",
                lambda e: f"mailto:{e}", lambda e: e.split("@")[0] + "@" + e.split("@")[1].upper()]
    for i in range(100):
        src = pool[rng.randrange(len(pool))]
        v = variants[i % len(variants)](src["email"])
        exp = {k: src[k] for k in src if k.startswith("expected_") or k == "dns_fact"}
        add(v, "duplicate", "exact" if i % len(variants) == 0 else "case/whitespace/bracket/mailto variant",
            src["expectation_type"], exp, dup_of=src["email"],
            note="must get the same result as the original and must not be re-verified")
    assert len(recs) == N, len(recs)
    rng.shuffle(recs)
    for i, r in enumerate(recs, 1):
        r["record_id"] = f"QA{i:04d}"
    return recs


def user_columns(rng: random.Random, recs: list[dict]) -> list[dict]:
    """The 'customer file' the user would upload: realistic columns incl. leading zeros."""
    rows = []
    for r in recs:
        rows.append({
            "Record ID": r["record_id"],
            "First Name": rng.choice(FIRST), "Last Name": rng.choice(LAST),
            "Company": rng.choice(COMPANIES),
            "Email Address": r["email"],
            "Zip": f"{rng.randrange(0, 1000):05d}" if rng.random() < 0.5 else f"{rng.randrange(1000, 99999):05d}",
            "Customer No": f"{rng.randrange(0, 99999):08d}",
            "Phone": f"+1 555 01{rng.randrange(0, 100):02d}",
            "Notes": rng.choice(NOTES),
        })
    return rows


def write_outputs(recs, rows, evidence):
    cols = list(rows[0])
    with (HERE / "synthetic_1000.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "contacts"
    ws.append(cols)
    for r in rows:
        ws.append([r[c] for c in cols])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.data_type = "s"          # keep leading zeros and formula-looking notes as text
    wb.save(HERE / "synthetic_1000.xlsx")
    # manual column selection file: two email columns, the second one is the one to verify
    with (HERE / "two_email_columns.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Record ID", "Email", "Backup Email"])
        for i in range(30):
            w.writerow([f"TWO{i:03d}", f"emailish.qa.two.{i:03d}@gmail.com", f"emailish.qa.backup.{i:03d}@example.com"])
    key_cols = ["record_id", "category", "scenario", "expectation_type", "email", "expected_status",
                "expected_sub_status", "expected_syntax_valid", "expected_domain_valid", "expected_mx_found",
                "expected_disposable", "expected_free_email", "expected_role_based", "expected_did_you_mean",
                "duplicate_of", "dns_fact", "rationale"]
    with (HERE / "synthetic_1000_expected.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=key_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(recs, key=lambda r: r["record_id"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-dns", action="store_true")
    a = ap.parse_args()
    rng = random.Random(SEED)
    disp = sorted(_load("disposable_domains.txt") - _load("disposable_allowlist.txt"))
    well_known = ["mailinator.com", "yopmail.com", "guerrillamail.com", "10minutemail.com", "trashmail.com",
                  "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc", "tempmail.plus"]
    sample = [d for d in well_known if d in set(disp)] + rng.sample(disp, 120)
    sample = list(dict.fromkeys(sample))[:120]
    domains = sorted(set(FREE_PROVIDERS + NULL_MX_RESERVED + list(TYPOS) + sample
                         + [f"qa-{i:02d}.example" for i in range(60, 80)] + ["xn--bcher-kva.de"]
                         + NONEXISTENT_DOMAINS))
    ev_path = HERE / "dns_evidence.json"
    if ev_path.exists() and not a.refresh_dns:
        evidence = json.loads(ev_path.read_text())["domains"]
        missing = [d for d in domains if d not in evidence]
        if missing:
            evidence.update(asyncio.run(run_oracle(missing)))
    else:
        evidence = asyncio.run(run_oracle(domains))
    ev_path.write_text(json.dumps({"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "source": "DNS-over-HTTPS (cloudflare-dns.com, fallback dns.google)",
                                   "domains": evidence}, indent=1, sort_keys=True))
    recs = build(evidence, rng, sample)
    rows = user_columns(rng, recs)
    write_outputs(recs, rows, evidence)
    from collections import Counter
    print("records:", len(recs))
    print("by category:", dict(Counter(r["category"] for r in recs)))
    print("by expectation_type:", dict(Counter(r["expectation_type"] for r in recs)))
    print("by expected_status:", dict(Counter(r["expected_status"] for r in recs)))
    print("dns facts:", dict(Counter(v["fact"] for v in evidence.values())))


if __name__ == "__main__":
    main()
