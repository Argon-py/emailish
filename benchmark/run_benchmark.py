"""Benchmark emailish against commercial verifiers on a labelled list of controlled addresses.

Usage
-----
  # 1. adapter self-test with the vendors' documented test keys (no account, no credits):
  python -m benchmark.run_benchmark --selftest

  # 2. dry run: shows which vendors are configured and the credits a run would use
  python -m benchmark.run_benchmark benchmark/controlled_addresses.csv

  # 3. real run (spends vendor credits; needs your API keys in the environment)
  export ZEROBOUNCE_API_KEY=...  MILLIONVERIFIER_API_KEY=...  HUNTER_API_KEY=...
  python -m benchmark.run_benchmark benchmark/controlled_addresses.csv --yes

Input CSV columns: email, expected (optional: VALID/INVALID/CATCH_ALL/DISPOSABLE/ROLE_BASED/RISKY), note
Output: benchmark/results/<timestamp>/{results.csv, report.md}

Nothing in this script fabricates vendor results: a vendor that is not configured or
errors is reported as such and excluded from its comparison.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402
from app.engine.verifier import Verifier  # noqa: E402

CATS = ["VALID", "INVALID", "CATCH_ALL", "DISPOSABLE", "ROLE_BASED", "RISKY", "UNKNOWN", "TIMEOUT"]


# --- vendor taxonomy -> emailish categories ------------------------------------
# One source of truth: the same mapping the application uses (app/providers.py).
from app import providers as _providers  # noqa: E402


def map_zerobounce(j: dict) -> str:
    return _providers.map_zerobounce(j).status.value


def map_millionverifier(j: dict) -> str:
    return _providers.map_millionverifier(j).status.value


def map_hunter(j: dict) -> str:
    return _providers.map_hunter(j).status.value


# --- vendor clients ----------------------------------------------------------------
class Vendor:
    name = ""
    env = ""
    credits_per_email = 1.0
    min_interval = 1.0          # conservative pacing, well under every documented limit

    def __init__(self, key: str | None = None):
        self.key = key or os.environ.get(self.env, "")

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def raw(self, c: httpx.AsyncClient, email: str) -> dict:
        raise NotImplementedError

    def map(self, j: dict) -> str:
        raise NotImplementedError


class ZeroBounce(Vendor):
    name, env = "zerobounce", "ZEROBOUNCE_API_KEY"

    async def raw(self, c, email):
        r = await c.get("https://api.zerobounce.net/v2/validate",
                        params={"api_key": self.key, "email": email, "ip_address": "", "timeout": 30}, timeout=40)
        r.raise_for_status()
        return r.json()

    def map(self, j):
        return map_zerobounce(j)


class MillionVerifier(Vendor):
    name, env = "millionverifier", "MILLIONVERIFIER_API_KEY"

    async def raw(self, c, email):
        r = await c.get("https://api.millionverifier.com/api/v3/",
                        params={"api": self.key, "email": email, "timeout": 20}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("result") == "error":
            raise RuntimeError(j.get("error") or "millionverifier error")
        return j

    def map(self, j):
        return map_millionverifier(j)


class Hunter(Vendor):
    name, env = "hunter", "HUNTER_API_KEY"
    credits_per_email = 0.5

    async def raw(self, c, email):
        for _ in range(6):    # 202 = still verifying; documented to poll again (counted once)
            r = await c.get("https://api.hunter.io/v2/email-verifier",
                            params={"email": email, "api_key": self.key}, timeout=30)
            if r.status_code == 202:
                await asyncio.sleep(5)
                continue
            if r.status_code == 451:
                return {"data": {"status": "unknown", "_note": "451: data subject opted out"}}
            if r.status_code == 222:
                return {"data": {"status": "unknown", "_note": "222: unexpected SMTP response"}}
            r.raise_for_status()
            return r.json()
        return {"data": {"status": "unknown", "_note": "still 202 after polling"}}

    def map(self, j):
        return map_hunter(j)


VENDORS = [ZeroBounce, MillionVerifier, Hunter]


async def run_vendor(v: Vendor, emails: list[str]) -> dict[str, tuple[str, dict | str]]:
    out = {}
    async with httpx.AsyncClient() as c:
        for e in emails:
            t0 = time.monotonic()
            try:
                j = await v.raw(c, e)
                out[e] = (v.map(j), j)
            except Exception as ex:
                out[e] = ("ERROR", f"{type(ex).__name__}: {ex}")
            await asyncio.sleep(max(0.0, v.min_interval - (time.monotonic() - t0)))
    return out


# --- adapter self-test (documented public test keys, no credits) -------------------
async def selftest() -> int:
    ok = True
    mv_cases = {"API_KEY_FOR_OK": "VALID", "API_KEY_FOR_CATCH_ALL": "CATCH_ALL", "API_KEY_FOR_INVALID": "INVALID",
                "API_KEY_FOR_UNKOWN": "UNKNOWN", "API_KEY_FOR_DISPOSABLE": "DISPOSABLE"}
    async with httpx.AsyncClient() as c:
        for key, want in mv_cases.items():
            j = await MillionVerifier(key).raw(c, "john.doe@example.com")
            got = map_millionverifier(j)
            print(f"millionverifier {key:24} -> {got:10} (raw result={j.get('result')}, livemode={j.get('livemode')})"
                  f" {'OK' if got == want else 'MISMATCH'}")
            ok &= got == want
        j = await Hunter("test-api-key").raw(c, "patrick@stripe.com")
        got = map_hunter(j)
        print(f"hunter          test-api-key             -> {got:10} (raw status={j['data']['status']}) "
              f"{'OK' if got == 'VALID' else 'MISMATCH'}")
        ok &= got == "VALID"
    zb = {"valid": "VALID", "invalid": "INVALID", "catch-all": "CATCH_ALL", "unknown": "UNKNOWN",
          "spamtrap": "RISKY", "abuse": "RISKY"}
    for st, want in zb.items():
        ok &= map_zerobounce({"status": st, "sub_status": ""}) == want
    ok &= map_zerobounce({"status": "do_not_mail", "sub_status": "disposable"}) == "DISPOSABLE"
    ok &= map_zerobounce({"status": "do_not_mail", "sub_status": "role_based"}) == "ROLE_BASED"
    print("zerobounce      mapping table (offline; sandbox needs your key: --zerobounce-sandbox)",
          "OK" if ok else "MISMATCH")
    return 0 if ok else 1


ZB_SANDBOX = {"valid@example.com": "VALID", "invalid@example.com": "INVALID", "catch_all@example.com": "CATCH_ALL",
              "unknown@example.com": "UNKNOWN", "disposable@example.com": "DISPOSABLE",
              "spamtrap@example.com": "RISKY", "abuse@example.com": "RISKY"}


async def zb_sandbox() -> int:
    v = ZeroBounce()
    if not v.configured:
        print("ZEROBOUNCE_API_KEY not set")
        return 2
    res = await run_vendor(v, list(ZB_SANDBOX))
    bad = 0
    for e, want in ZB_SANDBOX.items():
        got = res[e][0]
        print(f"{e:28} -> {got:10} {'OK' if got == want else 'MISMATCH'}  {json.dumps(res[e][1])[:120]}")
        bad += got != want
    return 1 if bad else 0


# --- benchmark ----------------------------------------------------------------------
def load_input(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("email") or "").strip() and not r["email"].startswith("#")]
    for r in rows:
        r["expected"] = (r.get("expected") or "").strip().upper()
    return rows


def deliverability(cat: str) -> str:
    """Collapse to the question that matters most: will mail to this address bounce?"""
    return {"VALID": "deliverable", "ROLE_BASED": "deliverable", "INVALID": "undeliverable",
            "DISPOSABLE": "do-not-send"}.get(cat, "uncertain")


async def benchmark(path: Path, yes: bool, vendors_filter: list[str] | None) -> int:
    rows = load_input(path)
    emails = list(dict.fromkeys(r["email"].strip() for r in rows))
    vendors = [V() for V in VENDORS if not vendors_filter or V.name in vendors_filter]
    print(f"{len(emails)} unique addresses from {path}")
    for v in vendors:
        print(f"  {v.name:16} {'configured' if v.configured else 'NOT configured (' + v.env + ' unset)'}"
              + (f", ~{len(emails) * v.credits_per_email:g} credits" if v.configured else ""))
    active = [v for v in vendors if v.configured]
    if active and not yes:
        print("\nDry run. Re-run with --yes to call the configured vendors (spends credits).")
        active = []

    ver = Verifier(Settings(greylist_retry_delay=float(os.environ.get("EMAILISH_GREYLIST_RETRY_DELAY", 90))))
    st = await ver.init()
    print(f"\nemailish DNS: {st.dns_backend}\nemailish SMTP: {st.smtp_note}")
    ours = await ver.verify_many(emails)
    results = {v.name: await run_vendor(v, emails) for v in active}

    outdir = Path(__file__).resolve().parent / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir.mkdir(parents=True, exist_ok=True)
    exp = {r["email"].strip(): r["expected"] for r in rows}
    notes = {r["email"].strip(): r.get("note", "") for r in rows}
    with (outdir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "expected", "note", "emailish", "emailish_sub_status", "emailish_reason"]
                   + [f"{n}{s}" for n in results for s in ("", "_raw")])
        for e in emails:
            o = ours[e]
            w.writerow([e, exp.get(e, ""), notes.get(e, ""), o.email_status.value, o.sub_status, o.verification_reason]
                       + [x for n in results for x in (results[n][e][0], json.dumps(results[n][e][1])[:2000])])

    lines = [f"# emailish benchmark — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             f"- Input: `{path.name}` ({len(emails)} unique addresses)",
             f"- emailish DNS backend: {st.dns_backend}", f"- emailish SMTP: {st.smtp_note}",
             f"- Vendors called: {', '.join(results) or 'none (dry run or no keys)'}", ""]
    engines = {"emailish": {e: ours[e].email_status.value for e in emails}}
    engines.update({n: {e: r[e][0] for e in emails} for n, r in results.items()})

    labelled = [e for e in emails if exp.get(e)]
    if labelled:
        lines += ["## Accuracy against your ground truth", "",
                  "| engine | exact category | deliverability verdict | uncertain/unknown | errors |", "|---|---|---|---|---|"]
        for n, m in engines.items():
            exact = sum(m[e] == exp[e] for e in labelled)
            deliv = sum(deliverability(m[e]) == deliverability(exp[e]) for e in labelled)
            unc = sum(deliverability(m[e]) == "uncertain" for e in labelled)
            err = sum(m[e] == "ERROR" for e in labelled)
            lines.append(f"| {n} | {exact}/{len(labelled)} | {deliv}/{len(labelled)} | {unc} | {err} |")
        lines.append("")
    for n in results:
        agree = sum(engines[n][e] == engines["emailish"][e] for e in emails)
        conf = defaultdict(Counter)
        for e in emails:
            conf[engines["emailish"][e]][engines[n][e]] += 1
        cols = sorted({c for row in conf.values() for c in row})
        lines += [f"## emailish vs {n}: {agree}/{len(emails)} identical", "",
                  "| emailish ↓ / " + n + " → | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
        for k in sorted(conf):
            lines.append(f"| {k} | " + " | ".join(str(conf[k][c]) for c in cols) + " |")
        lines += ["", "### Disagreements", "", "| email | expected | emailish | reason | " + n + " |", "|---|---|---|---|---|"]
        for e in emails:
            if engines[n][e] != engines["emailish"][e]:
                lines.append(f"| {e} | {exp.get(e, '')} | {engines['emailish'][e]} | "
                             f"{ours[e].verification_reason[:120].replace('|', '/')} | {engines[n][e]} |")
        lines.append("")
    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwrote {outdir}/results.csv and report.md")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", type=Path)
    ap.add_argument("--yes", action="store_true", help="actually call vendor APIs (spends credits)")
    ap.add_argument("--vendors", help="comma list: zerobounce,millionverifier,hunter")
    ap.add_argument("--selftest", action="store_true", help="check vendor adapters with public test keys")
    ap.add_argument("--zerobounce-sandbox", action="store_true", help="check ZB adapter with sandbox emails (free)")
    a = ap.parse_args()
    if a.selftest:
        return asyncio.run(selftest())
    if a.zerobounce_sandbox:
        return asyncio.run(zb_sandbox())
    if not a.input:
        ap.error("input CSV required")
    return asyncio.run(benchmark(a.input, a.yes, a.vendors.split(",") if a.vendors else None))


if __name__ == "__main__":
    sys.exit(main())
