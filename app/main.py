"""FastAPI application: upload -> choose column -> verify (background) -> download."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import files
from .config import Settings
from .db import DB, now
from .engine.verifier import Verifier
from .jobs import JobManager, input_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 50 * 1024 * 1024


class StartReq(BaseModel):
    email_column: str


class VerifyReq(BaseModel):
    email: str


def create_app(settings: Settings | None = None, verifier: Verifier | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = DB(settings.data_dir / "emailish.sqlite3")
        v = verifier or Verifier(settings)
        await v.init()
        app.state.db, app.state.verifier = db, v
        app.state.jobs = JobManager(db, v, settings.data_dir)
        logging.getLogger("emailish").info("DNS: %s | SMTP: %s", v.status.dns_backend, v.status.smtp_note)
        yield

    app = FastAPI(title="emailish", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    async def health():
        st = app.state.verifier.status
        return {"dns_backend": st.dns_backend, "smtp_available": st.smtp_available, "smtp_note": st.smtp_note,
                "helo_host": st.helo_host, "mail_from": st.mail_from, "warnings": st.warnings}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)):
        name = Path(file.filename or "upload.csv").name
        ext = Path(name).suffix.lower()
        if ext not in files.SUPPORTED:
            raise HTTPException(400, f"Unsupported file type {ext!r}. Use CSV, TXT or XLSX.")
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File larger than 50 MB.")
        job_id = uuid.uuid4().hex[:12]
        jd = app.state.jobs.job_dir(job_id)
        jd.mkdir(parents=True, exist_ok=True)
        p = input_path(jd, name)
        p.write_bytes(data)
        try:
            df = files.read_table(p)
        except files.FileError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"Could not read file: {e}")
        best, scores = files.detect_email_column(df)
        cols = [str(c) for c in df.columns]
        app.state.db.insert_job(id=job_id, filename=name, created_at=now(), status="uploaded",
                                total_rows=len(df), email_column=best, columns_json=json.dumps(cols))
        preview = df.head(5).to_dict(orient="records")
        return {"job_id": job_id, "filename": name, "rows": len(df), "columns": cols,
                "detected_column": best, "column_scores": scores, "preview": preview}

    @app.post("/api/jobs/{job_id}/start")
    async def start(job_id: str, req: StartReq):
        job = app.state.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "job already running")
        if req.email_column not in (job["columns"] or []):
            raise HTTPException(400, f"column {req.email_column!r} not in file")
        app.state.jobs.start(job_id, req.email_column)
        return app.state.db.get_job(job_id)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        job = app.state.db.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job

    @app.get("/api/jobs")
    async def list_jobs():
        return app.state.db.list_jobs()

    @app.get("/api/jobs/{job_id}/download")
    async def download(job_id: str, format: str = "csv"):
        job = app.state.db.get_job(job_id)
        if not job or job["status"] != "done":
            raise HTTPException(404, "results not ready")
        if format not in ("csv", "xlsx"):
            raise HTTPException(400, "format must be csv or xlsx")
        p = app.state.jobs.job_dir(job_id) / f"results.{format}"
        if not p.exists():
            raise HTTPException(404, f"{format} file not available")
        stem = Path(job["filename"]).stem
        media = "text/csv" if format == "csv" else \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return FileResponse(p, media_type=media, filename=f"{stem}_verified.{format}")

    @app.post("/api/verify")
    async def verify(req: VerifyReq):
        r = await app.state.verifier.verify_one(req.email)
        return r.to_json()

    return app


app = create_app()
