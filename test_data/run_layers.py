"""Test the three non-SMTP layers SEPARATELY against the answer key.

  syntax   app.engine.syntax.check_syntax          vs expected_syntax_valid        (offline)
  dataset  app.engine.lists (disposable/free/role/typo) vs expected flags          (offline)
  dns      app.engine.dnslayer.analyze_domain       vs the independent oracle facts (live DoH)

  python test_data/run_layers.py
Writes test_data/qa_results/latest/layers.json
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from app.engine import lists  # noqa: E402
from app.engine.dnslayer import DohBackend, analyze_domain  # noqa: E402
from app.engine.syntax import check_syntax  # noqa: E402

key = list(csv.DictReader((HERE / "synthetic_1000_expected.csv").open(encoding="utf-8")))
evidence = json.loads((HERE / "dns_evidence.json").read_text())["domains"]
report: dict = {}


def layer(name, rows):
    c = Counter(ok for ok, _ in rows)
    fails = [d for ok, d in rows if not ok]
    report[name] = {"tested": len(rows), "pass": c[True], "fail": c[False], "failures": fails[:20]}
    print(f"{name:28} tested {len(rows):4}  pass {c[True]:4}  fail {c[False]:3}" + (f"  e.g. {fails[:3]}" if fails else ""))


# ---- 1. syntax layer (offline)
t = time.monotonic()
rows = []
for k in key:
    got = "Yes" if check_syntax(k["email"]).ok else "No"
    rows.append((got == k["expected_syntax_valid"], (k["record_id"], k["email"], got)))
layer("syntax", rows)
report["syntax"]["seconds"] = round(time.monotonic() - t, 3)

# ---- 2. dataset layer (offline): only syntactically valid records
valid = [(k, check_syntax(k["email"])) for k in key if k["expected_syntax_valid"] == "Yes"]
layer("dataset: disposable (list)", [
    (("Yes" if lists.is_disposable_domain(s.domain) else "No") == k["expected_disposable"]
     or k["expected_sub_status"] == "disposable_mx", (k["record_id"], s.domain)) for k, s in valid])
layer("dataset: free provider", [
    (("Yes" if lists.is_free_domain(s.domain) else "No") == k["expected_free_email"], (k["record_id"], s.domain))
    for k, s in valid])
layer("dataset: role-based", [
    (("Yes" if lists.is_role(s.local) else "No") == k["expected_role_based"], (k["record_id"], s.local))
    for k, s in valid])
layer("dataset: typo suggestion", [
    ((f"{s.local}@{lists.suggest_domain(s.domain)}" if lists.suggest_domain(s.domain) else "")
     == k["expected_did_you_mean"], (k["record_id"], s.domain)) for k, s in valid])

# ---- 3. DNS layer (live DoH) vs the independent oracle
FACT_TO_STATUS = {"nxdomain": "nxdomain", "null_mx": "null_mx", "no_mail_host": {"no_mail_host", "mx_unresolvable"},
                  "unroutable": "unroutable", "ok": "ok", "dns_error": "dns_error"}


async def dns_layer():
    b = DohBackend()
    domains = sorted({s.domain for _, s in valid})
    t0 = time.monotonic()
    infos = await asyncio.gather(*(analyze_domain(b, d) for d in domains))
    rows = []
    for d, info in zip(domains, infos):
        want = FACT_TO_STATUS[evidence[d]["fact"]]
        ok = info.status in want if isinstance(want, set) else info.status == want
        rows.append((ok, (d, evidence[d]["fact"], info.status)))
    layer("dns: domain status vs oracle", rows)
    report["dns: domain status vs oracle"]["seconds"] = round(time.monotonic() - t0, 2)
    mx_rows = []
    for d, info in zip(domains, infos):
        if evidence[d]["fact"] == "ok":
            # the engine keeps the 3 most-preferred MX hosts (max_hosts=3), in preference order
            want = (evidence[d].get("mx_hosts") or ([d] if evidence[d].get("implicit_mx") else []))[:3]
            mx_rows.append((info.mx_names == want, (d, info.mx_names, want)))
    layer("dns: top-3 MX hosts vs oracle", mx_rows)


asyncio.run(dns_layer())
out = HERE / "qa_results" / "latest"
out.mkdir(parents=True, exist_ok=True)
(out / "layers.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
sys.exit(0 if all(v["fail"] == 0 for v in report.values()) else 1)
