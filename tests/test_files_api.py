"""File parsing/export and the full HTTP workflow (upload -> start -> progress -> download)."""
import io
import time
from pathlib import Path

import pandas as pd
import pytest
from conftest import fake_dns, make_settings
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import files
from app.engine.models import VerificationResult
from app.engine.verifier import Verifier
from app.main import create_app

RESULT_COLS = list(VerificationResult.OUTPUT_COLUMNS)


# --- reading & detection -----------------------------------------------------
def test_csv_with_header_detects_email_column(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("Name,Contact,Zip\nAnn,ann@valid.mock,01234\nBo,bo@valid.mock,00501\n")
    df = files.read_table(p)
    assert list(df.columns) == ["Name", "Contact", "Zip"]
    assert df["Zip"].tolist() == ["01234", "00501"]            # leading zeros preserved
    assert files.detect_email_column(df)[0] == "Contact"


def test_semicolon_latin1_csv(tmp_path):
    p = tmp_path / "b.csv"
    p.write_bytes("Prénom;E-mail\nJosé;jose@valid.mock\nRené;rene@valid.mock\n".encode("cp1252"))
    df = files.read_table(p)
    assert list(df.columns) == ["Prénom", "E-mail"]
    assert files.detect_email_column(df)[0] == "E-mail"


def test_headerless_csv(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("ann@valid.mock,Ann\nbo@valid.mock,Bo\n")
    df = files.read_table(p)
    assert len(df) == 2 and files.detect_email_column(df)[0] == "column_1"


def test_txt_one_per_line_with_noise(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("email\nann@valid.mock\n\nBo Smith <bo@valid.mock>\nnot an email\n")
    df = files.read_table(p)
    assert df["email"].tolist() == ["ann@valid.mock", "bo@valid.mock", "not an email"]


def test_xlsx_and_misleading_header(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["mail_status", "who", "id"])
    ws.append(["active", "ann@valid.mock", "0007"])
    ws.append(["active", "bo@valid.mock", "0008"])
    p = tmp_path / "e.xlsx"
    wb.save(p)
    df = files.read_table(p)
    assert files.detect_email_column(df)[0] == "who"           # content beats a misleading header
    assert df["id"].tolist() == ["0007", "0008"]


def test_no_email_column(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("a,b\n1,2\n3,4\n")
    assert files.detect_email_column(files.read_table(p))[0] is None


def test_unsupported_extension(tmp_path):
    p = tmp_path / "g.pdf"
    p.write_bytes(b"%PDF")
    with pytest.raises(files.FileError):
        files.read_table(p)


# --- full HTTP workflow --------------------------------------------------------
@pytest.fixture
def client(cluster, tmp_path):
    s = make_settings(cluster, data_dir=tmp_path / "data")
    v = Verifier(s, dns_backend=fake_dns())
    app = create_app(s, verifier=v)
    with TestClient(app) as c:
        yield c


def _wait(client, job_id, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "interrupted"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


CSV = (
    "Email,Company,Zip,Note\n"
    "alice@valid.mock,ABC,01234,=HYPERLINK(\"http://x\")\n"
    "nobody@valid.mock,XYZ,00000,+1 555\n"
    "info@valid.mock,ABC,12345,-dash\n"
    "anyone@catchall.mock,DEF,22222,@at\n"
    "x@mailinator.com,GHI,33333,\n"
    "a@doesnotexist.mock,JKL,44444,\n"
    "test@invalid-domain.example,XYZ,55555,\n"
    "alice@valid.mock,DUP,66666,duplicate row\n"
    ",EMPTY,77777,\n"
)


def test_upload_verify_download_csv_and_xlsx(client):
    r = client.post("/api/upload", files={"file": ("contacts.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    up = r.json()
    assert up["detected_column"] == "Email" and up["rows"] == 9
    job = up["job_id"]
    assert client.post(f"/api/jobs/{job}/start", json={"email_column": "Email"}).status_code == 200
    j = _wait(client, job)
    assert j["status"] == "done", j
    assert j["done"] == j["unique_emails"]
    assert sum(j["summary"]["by_status"].values()) == 9

    # CSV
    rc = client.get(f"/api/jobs/{job}/download?format=csv")
    assert rc.status_code == 200
    assert 'contacts_verified.csv' in rc.headers["content-disposition"]
    out = pd.read_csv(io.BytesIO(rc.content), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert list(out.columns) == ["Email", "Company", "Zip", "Note"] + RESULT_COLS
    assert out["Zip"].tolist()[:2] == ["01234", "00000"]
    status = dict(zip(out["Email"] + "|" + out["Company"], out["email_status"]))
    assert status["alice@valid.mock|ABC"] == "VALID"
    assert status["alice@valid.mock|DUP"] == "VALID"
    assert status["nobody@valid.mock|XYZ"] == "INVALID"
    assert status["info@valid.mock|ABC"] == "ROLE_BASED"
    assert status["anyone@catchall.mock|DEF"] == "CATCH_ALL"
    assert status["x@mailinator.com|GHI"] == "DISPOSABLE"
    assert status["a@doesnotexist.mock|JKL"] == "INVALID"
    assert status["test@invalid-domain.example|XYZ"] == "INVALID"
    assert status["|EMPTY"] == "INVALID"
    row = out[out["Email"] == "alice@valid.mock"].iloc[0]
    assert row["mx_found"] == "Yes" and row["disposable"] == "No" and row["smtp_status"] == "accepted"
    assert row["catch_all"] == "No" and row["certainty"] == "CONFIRMED" and row["verified_at"]

    # XLSX: same content, and formula-looking cells stay text
    rx = client.get(f"/api/jobs/{job}/download?format=xlsx")
    assert rx.status_code == 200
    wb = load_workbook(io.BytesIO(rx.content))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == ["Email", "Company", "Zip", "Note"] + RESULT_COLS
    note = ws.cell(row=2, column=4)
    assert note.value == '=HYPERLINK("http://x")' and note.data_type == "s"
    assert ws.cell(row=2, column=3).value == "01234"

    # history
    hist = client.get("/api/jobs").json()
    assert hist[0]["id"] == job and hist[0]["status"] == "done"


def test_existing_result_column_names_not_clobbered(client):
    csv = "email,email_status\nalice@valid.mock,customer-said-ok\n"
    up = client.post("/api/upload", files={"file": ("x.csv", csv.encode(), "text/csv")}).json()
    client.post(f"/api/jobs/{up['job_id']}/start", json={"email_column": "email"})
    _wait(client, up["job_id"])
    out = pd.read_csv(io.BytesIO(client.get(f"/api/jobs/{up['job_id']}/download?format=csv").content), dtype=str)
    assert out["email_status"].tolist() == ["customer-said-ok"]
    assert out["verify_email_status"].tolist() == ["VALID"]


def test_api_errors(client):
    assert client.post("/api/upload", files={"file": ("x.pdf", b"%PDF", "application/pdf")}).status_code == 400
    up = client.post("/api/upload", files={"file": ("x.csv", b"email\na@valid.mock\n", "text/csv")}).json()
    assert client.post(f"/api/jobs/{up['job_id']}/start", json={"email_column": "nope"}).status_code == 400
    assert client.get(f"/api/jobs/{up['job_id']}/download?format=csv").status_code == 404
    assert client.get("/api/jobs/doesnotexist").status_code == 404


def test_single_verify_and_health(client):
    r = client.post("/api/verify", json={"email": "alice@valid.mock"}).json()
    assert r["email_status"] == "VALID"
    h = client.get("/api/health").json()
    assert h["smtp_available"] is True and h["dns_backend"] == "fake"
    assert client.get("/").status_code == 200
