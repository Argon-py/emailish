"""End-to-end SMTP run of the REAL application against the Postfix lab:
UPLOAD (xlsx) -> VERIFY (SMTP enabled, allowlist = lab domains) -> DOWNLOAD (csv + xlsx).

  sudo bash lab/setup_lab_mta.sh
  python lab/run_lab_e2e.py [--screenshot]

Writes lab/results/{lab_contacts.xlsx, lab_contacts_verified.csv/.xlsx, e2e_summary.json, ui_smtp_enabled.png}
"""
from __future__ import annotations

import csv
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import uvicorn
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.engine.verifier import Verifier  # noqa: E402
from app.main import create_app  # noqa: E402
from lab.lab_env import LAB_DOMAINS, lab_dns, lab_settings  # noqa: E402

OUT = Path(__file__).resolve().parent / "results"
# (email, expected status, expected sub_status, what the lab server does)
CASES = [
    ("alice@lab-valid.mock", "VALID", "accepted", "real mailbox"),
    ("Bob@Lab-Valid.mock", "VALID", "accepted", "real mailbox, mixed case"),
    ("  bob@lab-valid.mock ", "VALID", "accepted", "duplicate of Bob with spaces"),
    ("info@lab-valid.mock", "ROLE_BASED", "role_based", "real role mailbox"),
    ("nobody@lab-valid.mock", "INVALID", "mailbox_not_found", "Postfix: User unknown in virtual mailbox table"),
    ("ghost.user@lab-valid.mock", "INVALID", "mailbox_not_found", "Postfix: User unknown"),
    ("disabled@lab-valid.mock", "INVALID", "mailbox_disabled", "access map 550 5.2.1"),
    ("full@lab-valid.mock", "RISKY", "mailbox_quota_exceeded", "access map 552 5.2.2"),
    ("anyone@lab-catchall.mock", "CATCH_ALL", "catch_all", "virtual alias catch-all"),
    ("sales@lab-catchall.mock", "ROLE_BASED", "role_based_catch_all", "role address on catch-all"),
    ("carol@lab-grey.mock", "VALID", "accepted", "real postgrey: greylisted first, accepted on retry"),
    ("dave@lab-temp.mock", "UNKNOWN", "mail_server_temporary_error", "451 4.3.0 temporary failure"),
    ("erin@lab-blocked.mock", "UNKNOWN", "antispam_system", "554 5.7.1 blocklist rejection"),
    ("frank@lab-down.mock", "UNKNOWN", "failed_smtp_connection", "mail server down (connection refused)"),
    ("someone@not-allowed.example.org", "UNKNOWN", "smtp_not_permitted", "outside EMAILISH_SMTP_ALLOWED_DOMAINS"),
    ("x@mailinator.com", "DISPOSABLE", "disposable", "disposable list, no SMTP needed"),
    ("not-an-email", "INVALID", "failed_syntax_check", "syntax"),
]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    OUT.mkdir(exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.append(["Customer ID", "Name", "E-mail", "Zip"])
    for i, (e, *_rest) in enumerate(CASES):
        ws.append([f"{i:06d}", f"Lab Contact {i}", e, f"{i:05d}"])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.data_type = "s"
    src = OUT / "lab_contacts.xlsx"
    wb.save(src)

    # the not-allowed and disposable domains must resolve too; give them DNS in the lab table
    dns = lab_dns()
    from app.engine.dnslayer import NODATA, OK, DnsAnswer
    dns.table[("not-allowed.example.org", "MX")] = DnsAnswer(OK, [(10, "mx.lab.mock")])
    dns.table[("mailinator.com", "MX")] = DnsAnswer(OK, [(10, "mx.lab.mock")])
    settings = lab_settings(smtp_allowed_domains=",".join(LAB_DOMAINS + ["lab-down.mock"]),
                            data_dir=Path(tempfile.mkdtemp(prefix="emailish-lab-")))
    app = create_app(settings, verifier=Verifier(settings, dns_backend=dns))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    summary: dict = {}
    try:
        with httpx.Client(base_url=url, timeout=60) as c:
            h = c.get("/api/health").json()
            summary["health"] = h
            print("health:", {k: h[k] for k in ("smtp_enabled", "smtp_available", "smtp_note")})
            up = c.post("/api/upload", files={"file": (src.name, src.read_bytes())}).json()
            print("detected column:", up["detected_column"])
            c.post(f"/api/jobs/{up['job_id']}/start", json={"email_column": up["detected_column"]})
            t0 = time.monotonic()
            phases = []
            while True:
                j = c.get(f"/api/jobs/{up['job_id']}").json()
                if not phases or phases[-1] != j["phase"]:
                    phases.append(j["phase"])
                if j["status"] in ("done", "failed"):
                    break
                time.sleep(0.2)
            took = time.monotonic() - t0
            print("phases:", " -> ".join(phases), f"({took:.1f} s)")
            rc = c.get(f"/api/jobs/{up['job_id']}/download?format=csv")
            rx = c.get(f"/api/jobs/{up['job_id']}/download?format=xlsx")
            (OUT / "lab_contacts_verified.csv").write_bytes(rc.content)
            (OUT / "lab_contacts_verified.xlsx").write_bytes(rx.content)
            if "--screenshot" in sys.argv:
                subprocess.run(["node", str(Path(__file__).with_name("screenshot.js")), url, src,
                                OUT / "ui_smtp_enabled.png"], check=False)
    finally:
        server.should_exit = True
        th.join(5)

    rows = list(csv.DictReader(io.StringIO(rc.content.decode("utf-8-sig"))))
    xl = load_workbook(io.BytesIO(rx.content)).active
    results, ok_all = [], True
    for (email, st, sub, what), r in zip(CASES, rows):
        ok = r["email_status"] == st and r["sub_status"] == sub and r["E-mail"] == email
        ok_all &= ok
        results.append({"email": email, "lab_behaviour": what, "expected": f"{st}/{sub}",
                        "got": f"{r['email_status']}/{r['sub_status']}", "smtp_status": r["smtp_status"],
                        "smtp_code": r["smtp_code"], "confidence": r["confidence"], "pass": ok})
        print(f"{'PASS' if ok else 'FAIL'}  {email!r:36} {r['email_status']:10} {r['sub_status']:28} "
              f"{r['smtp_code']:10} {what}")
    zips_ok = [r["Zip"] for r in rows] == [f"{i:05d}" for i in range(len(CASES))]
    xl_rows = xl.max_row - 1
    summary.update(phases=phases, seconds=round(took, 1), unique_emails=j["unique_emails"],
                   rows=len(rows), results=results, all_pass=ok_all, leading_zeros_ok=zips_ok,
                   xlsx_rows=xl_rows, by_status=dict(Counter(r["email_status"] for r in rows)))
    (OUT / "e2e_summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} rows as expected; leading zeros kept: {zips_ok}; "
          f"xlsx rows: {xl_rows}; unique verified: {j['unique_emails']}")
    return 0 if ok_all and zips_ok else 1


if __name__ == "__main__":
    sys.exit(main())
