"""QA run: drive the real emailish application over HTTP with SMTP DISABLED and compare
every output row with the answer key in synthetic_1000_expected.csv.

  python test_data/run_qa.py                 # starts its own server on a free port
  python test_data/run_qa.py --url http://127.0.0.1:8000   # use a running server (must have SMTP disabled)

Writes test_data/qa_results/latest/{summary.json, comparison.csv, failures.csv}.
No SMTP connection is made: the server is started with EMAILISH_SMTP_ENABLED=false.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "qa_results" / "latest"
RESULT_COLS = ["email_status", "syntax_valid", "domain_valid", "mx_found", "disposable", "free_email",
               "role_based", "smtp_status", "catch_all", "verification_reason", "confidence", "verified_at",
               "sub_status", "certainty", "did_you_mean", "mx_host", "mail_provider", "smtp_code"]
COMPARED = [("expected_status", "email_status"), ("expected_sub_status", "sub_status"),
            ("expected_syntax_valid", "syntax_valid"), ("expected_domain_valid", "domain_valid"),
            ("expected_mx_found", "mx_found"), ("expected_disposable", "disposable"),
            ("expected_free_email", "free_email"), ("expected_role_based", "role_based"),
            ("expected_did_you_mean", "did_you_mean")]
WS = " \t\r\n ​﻿"
checks: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def read_csv_exact(data: bytes | str) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return rows[0], rows[1:]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(data_dir: Path) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = dict(os.environ, EMAILISH_SMTP_ENABLED="false", EMAILISH_DATA_DIR=str(data_dir))
    log = open(data_dir / "server.log", "w")
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                          "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                return p, url
        except Exception:
            time.sleep(0.25)
    p.kill()
    raise SystemExit("server did not start; see " + str(data_dir / "server.log"))


def run_job(c: httpx.Client, path: Path, column: str | None, label: str) -> dict:
    t0 = time.monotonic()
    up = c.post("/api/upload", files={"file": (path.name, path.read_bytes())}).json()
    t_up = time.monotonic() - t0
    col = column or up["detected_column"]
    t1 = time.monotonic()
    r = c.post(f"/api/jobs/{up['job_id']}/start", json={"email_column": col})
    assert r.status_code == 200, r.text
    snaps = []
    while True:
        j = c.get(f"/api/jobs/{up['job_id']}").json()
        snaps.append((round(time.monotonic() - t1, 3), j["status"], j["phase"], j["done"], j["unique_emails"]))
        if j["status"] in ("done", "failed", "interrupted"):
            break
        time.sleep(0.05)
    t_run = time.monotonic() - t1
    out = {"label": label, "upload": up, "column": col, "job": j, "snapshots": snaps,
           "upload_seconds": round(t_up, 3), "verify_seconds": round(t_run, 3)}
    if j["status"] == "done":
        t2 = time.monotonic()
        out["csv"] = c.get(f"/api/jobs/{up['job_id']}/download?format=csv")
        out["xlsx"] = c.get(f"/api/jobs/{up['job_id']}/download?format=xlsx")
        out["download_seconds"] = round(time.monotonic() - t2, 3)
    return out


def canon(email: str) -> str:
    s = email.strip(WS).strip("<>\"' ;,")
    if s.lower().startswith("mailto:"):
        s = s[7:]
    return s.strip(WS).strip("<>").lower()


def validate_exports(job: dict, input_header: list[str], input_rows: list[list[str]], name: str) -> dict:
    res = {}
    hdr, rows = read_csv_exact(job["csv"].content)
    check(f"{name}: CSV export downloads (HTTP 200, text/csv)",
          job["csv"].status_code == 200 and "text/csv" in job["csv"].headers.get("content-type", ""))
    check(f"{name}: CSV header = original columns (same order) + 18 result columns",
          hdr == input_header + RESULT_COLS, f"got {hdr[:3]}...{hdr[-3:]}")
    check(f"{name}: CSV row count equals input", len(rows) == len(input_rows), f"{len(rows)} vs {len(input_rows)}")
    n = len(input_header)
    diffs = [(i, k) for i, (a, b) in enumerate(zip(input_rows, rows)) for k in range(n) if a[k] != b[k]]
    check(f"{name}: CSV preserves every original cell exactly", not diffs,
          f"{len(diffs)} differing cells" + (f", first {diffs[:3]}" if diffs else ""))
    missing = [i for i, r in enumerate(rows) if not r[hdr.index('email_status')]]
    check(f"{name}: every CSV row has an email_status", not missing, f"{len(missing)} missing")
    res["csv_rows"] = rows
    res["csv_header"] = hdr
    # XLSX
    check(f"{name}: XLSX export downloads (HTTP 200)", job["xlsx"].status_code == 200)
    wb = load_workbook(io.BytesIO(job["xlsx"].content))
    ws = wb.active
    xrows = [[("" if c.value is None else str(c.value)) for c in r] for r in ws.iter_rows()]
    types = Counter(c.data_type for r in ws.iter_rows(min_row=2) for c in r if c.value is not None)
    check(f"{name}: XLSX header identical to CSV header", xrows[0] == hdr)
    check(f"{name}: XLSX row count equals input", len(xrows) - 1 == len(input_rows), f"{len(xrows) - 1}")
    xd = [(i, k) for i, (a, b) in enumerate(zip(rows, xrows[1:])) for k in range(len(hdr))
          if a[k] != b[k] and not (a[k].strip(WS) == "" and b[k] == "")]
    check(f"{name}: XLSX content identical to CSV content", not xd,
          f"{len(xd)} differing cells" + (f", first {xd[:3]}" if xd else ""))
    check(f"{name}: XLSX stores all values as text (no formulas/numbers)", set(types) <= {"s"}, str(dict(types)))
    for col in ("Zip", "Customer No"):
        if col in hdr:
            k = hdr.index(col)
            lz_in = sum(1 for r in input_rows if r[k].startswith("0"))
            lz_csv = sum(1 for r in rows if r[k].startswith("0"))
            lz_x = sum(1 for r in xrows[1:] if r[k].startswith("0"))
            check(f"{name}: leading zeros preserved in '{col}' (CSV and XLSX)", lz_in == lz_csv == lz_x,
                  f"input {lz_in}, csv {lz_csv}, xlsx {lz_x}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="emailish-qa-"))
    proc = None
    if a.url:
        url = a.url
    else:
        proc, url = start_server(tmp)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = {r["record_id"]: r for r in csv.DictReader((HERE / "synthetic_1000_expected.csv").open(encoding="utf-8"))}
    in_hdr, in_rows = read_csv_exact((HERE / "synthetic_1000.csv").read_text(encoding="utf-8"))
    summary: dict = {"started_at": started}
    try:
        with httpx.Client(base_url=url, timeout=120) as c:
            h = c.get("/api/health").json()
            summary["health"] = h
            check("server: SMTP checks disabled for this run", h["smtp_available"] is False
                  and "disabled" in h["smtp_note"], h["smtp_note"])

            # ---- Job A: CSV, automatic column detection
            A = run_job(c, HERE / "synthetic_1000.csv", None, "csv")
            check("CSV upload accepted, 1000 rows", A["upload"]["rows"] == 1000, str(A["upload"]["rows"]))
            check("automatic email column detection (CSV) -> 'Email Address'",
                  A["upload"]["detected_column"] == "Email Address", str(A["upload"]["detected_column"]))
            check("CSV job finished", A["job"]["status"] == "done", A["job"].get("error") or "")
            snaps = A["snapshots"]
            dones = [s[3] for s in snaps]
            check("progress: 'done' counter never decreases", all(x <= y for x, y in zip(dones, dones[1:])))
            check("progress: final done == unique_emails", dones[-1] == A["job"]["unique_emails"],
                  f"{dones[-1]} / {A['job']['unique_emails']}")
            check("progress: intermediate states observed (queued/running before done)",
                  any(s[1] in ("queued", "running") for s in snaps), f"{len(snaps)} polls, phases "
                  f"{sorted(set(s[2] for s in snaps))}")
            ex = validate_exports(A, in_hdr, in_rows, "CSV job")
            hdr, rows = ex["csv_header"], ex["csv_rows"]
            ix = {c_: hdr.index(c_) for c_ in hdr}

            # ---- per-record comparison with the answer key
            comp, fails = [], []
            got_by_id = {r[ix["Record ID"]]: r for r in rows}
            stat = Counter()
            for rid, k in key.items():
                g = got_by_id.get(rid)
                row = {"record_id": rid, "category": k["category"], "expectation_type": k["expectation_type"],
                       "email": k["email"]}
                if g is None or not g[ix["email_status"]]:
                    row["outcome"] = "MISSING"
                    stat["missing"] += 1
                    comp.append(row)
                    fails.append(row)
                    continue
                mism = []
                for ek, gk in COMPARED:
                    row[ek] = k[ek]
                    row["got_" + gk] = g[ix[gk]]
                    if k[ek] != g[ix[gk]]:
                        mism.append(gk)
                row["got_reason"] = g[ix["verification_reason"]]
                row["mismatched_fields"] = ";".join(mism)
                if not mism:
                    row["outcome"] = "PASS"
                    stat["pass"] += 1
                elif "email_status" in mism:
                    row["outcome"] = "FAIL_CLASSIFICATION"
                    stat["fail"] += 1
                    stat["incorrect_classification"] += 1
                else:
                    row["outcome"] = "FAIL_FIELD"
                    stat["fail"] += 1
                if g[ix["email_status"]] == "UNKNOWN":
                    stat["unknown_results"] += 1
                comp.append(row)
                if row["outcome"] != "PASS":
                    fails.append(row)
            summary["records"] = dict(stat)
            summary["by_category"] = {}
            for cat in sorted({r["category"] for r in comp}):
                cs = [r for r in comp if r["category"] == cat]
                summary["by_category"][cat] = dict(Counter(r["outcome"] for r in cs), total=len(cs))
            summary["by_expectation_type"] = {}
            for et in sorted({r["expectation_type"] for r in comp}):
                cs = [r for r in comp if r["expectation_type"] == et]
                summary["by_expectation_type"][et] = dict(Counter(r["outcome"] for r in cs), total=len(cs))
            summary["got_status_counts"] = dict(Counter(r[ix["email_status"]] for r in rows))
            summary["expected_status_counts"] = dict(Counter(k["expected_status"] for k in key.values()))

            # ---- safety invariants (SMTP disabled)
            check("invariant: no VALID result without SMTP evidence",
                  not any(r[ix["email_status"]] == "VALID" for r in rows))
            check("invariant: no ROLE_BASED / CATCH_ALL without SMTP evidence",
                  not any(r[ix["email_status"]] in ("ROLE_BASED", "CATCH_ALL") for r in rows))
            check("invariant: smtp_status is not_checked for every row",
                  {r[ix["smtp_status"]] for r in rows} == {"not_checked"}, str({r[ix["smtp_status"]] for r in rows}))
            check("invariant: every UNKNOWN has confidence 0 and certainty UNCERTAIN",
                  all(r[ix["confidence"]] == "0" and r[ix["certainty"]] == "UNCERTAIN"
                      for r in rows if r[ix["email_status"]] == "UNKNOWN"))
            check("invariant: every row has a verification_reason and verified_at",
                  all(r[ix["verification_reason"]] and r[ix["verified_at"]] for r in rows))

            # ---- duplicates
            by_email = {}
            for k in key.values():
                by_email.setdefault(k["email"], k["record_id"])
            dup_bad = []
            for k in key.values():
                if k["category"] != "duplicate":
                    continue
                src = got_by_id[by_email[k["duplicate_of"]]]
                me = got_by_id[k["record_id"]]
                same = all(src[ix[f]] == me[ix[f]] for f in RESULT_COLS if f != "verified_at")
                if not same:
                    dup_bad.append(k["record_id"])
            check("duplicates: each variant got exactly the original's result", not dup_bad,
                  f"{len(dup_bad)} differ: {dup_bad[:5]}")
            keys = set()
            for k in key.values():
                if k["category"] == "duplicate":
                    keys.add(("v", canon(k["duplicate_of"])))
                elif k["expected_syntax_valid"] == "Yes":
                    keys.add(("v", canon(k["email"])))
                else:
                    keys.add(("raw", k["email"]))
            check("duplicates: unique addresses verified once (job.unique_emails == independent count)",
                  A["job"]["unique_emails"] == len(keys), f"job {A['job']['unique_emails']}, expected {len(keys)}")

            # ---- Job B: XLSX, same data
            B = run_job(c, HERE / "synthetic_1000.xlsx", None, "xlsx")
            check("XLSX upload accepted, 1000 rows", B["upload"]["rows"] == 1000, str(B["upload"]["rows"]))
            check("automatic email column detection (XLSX) -> 'Email Address'",
                  B["upload"]["detected_column"] == "Email Address", str(B["upload"]["detected_column"]))
            check("XLSX job finished", B["job"]["status"] == "done", B["job"].get("error") or "")
            exb = validate_exports(B, in_hdr, in_rows, "XLSX job")
            ib = {c_: exb["csv_header"].index(c_) for c_ in exb["csv_header"]}
            diff = [r[0] for r, rb in zip(rows, exb["csv_rows"])
                    if any(r[ix[f]] != rb[ib[f]] for f in RESULT_COLS if f != "verified_at")]
            check("XLSX input produces the same results as CSV input", not diff, f"{len(diff)} rows differ {diff[:5]}")

            # ---- Job C: manual column selection
            C_up = c.post("/api/upload", files={"file": ("two_email_columns.csv",
                                                          (HERE / "two_email_columns.csv").read_bytes())}).json()
            check("manual selection: rejecting a column that does not exist (HTTP 400)",
                  c.post(f"/api/jobs/{C_up['job_id']}/start", json={"email_column": "Nope"}).status_code == 400)
            C = run_job(c, HERE / "two_email_columns.csv", "Backup Email", "manual")
            check("manual selection: auto-detect proposed 'Email', user chose 'Backup Email'",
                  C["upload"]["detected_column"] == "Email" and C["column"] == "Backup Email",
                  f"detected {C['upload']['detected_column']}")
            ch, cr = read_csv_exact(C["csv"].content)
            st = Counter((r[ch.index("email_status")], r[ch.index("sub_status")]) for r in cr)
            check("manual selection: the chosen column was verified (all example.com -> INVALID Null MX)",
                  st == Counter({("INVALID", "does_not_accept_mail"): 30}), str(dict(st)))

            # ---- history
            hist = c.get("/api/jobs").json()
            ids = {A["job"]["id"], B["job"]["id"], C["job"]["id"]}
            done_jobs = {j["id"]: j for j in hist if j["id"] in ids}
            check("history: all three jobs listed as done", len(done_jobs) == 3 and
                  all(j["status"] == "done" for j in done_jobs.values()))
            counts = Counter(r[ix["email_status"]] for r in rows)
            check("history: stored summary matches the exported file",
                  done_jobs[A["job"]["id"]]["summary"]["by_status"] == dict(counts),
                  str(done_jobs[A["job"]["id"]]["summary"]["by_status"]))

            summary["timing"] = {j_["label"]: {"rows": j_["upload"]["rows"], "unique": j_["job"]["unique_emails"],
                                               "upload_s": j_["upload_seconds"], "verify_s": j_["verify_seconds"],
                                               "download_s": j_.get("download_seconds"), "polls": len(j_["snapshots"])}
                                 for j_ in (A, B, C)}
            summary["progress_samples_csv_job"] = A["snapshots"][:: max(1, len(A["snapshots"]) // 12)]
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    summary["checks"] = checks
    summary["checks_passed"] = sum(c_["result"] == "PASS" for c_ in checks)
    summary["checks_failed"] = sum(c_["result"] == "FAIL" for c_ in checks)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    fields = sorted({k for r in comp for k in r}, key=lambda x: (x != "record_id", x))
    for name, data in (("comparison.csv", comp), ("failures.csv", fails)):
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(data)
    print("\nrecords:", summary["records"])
    print("checks:", summary["checks_passed"], "passed,", summary["checks_failed"], "failed")
    print("timing:", json.dumps(summary["timing"]))
    return 0 if not fails and summary["checks_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
