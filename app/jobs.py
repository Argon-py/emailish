"""Background verification jobs (asyncio tasks inside the server process)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from pathlib import Path

from . import files
from .db import DB, now
from .engine.verifier import Verifier

log = logging.getLogger("emailish.jobs")


def input_path(job_dir: Path, filename: str) -> Path:
    """Uploads are stored under a fixed name; only the extension of the user's filename is kept."""
    return job_dir / ("input" + Path(filename).suffix.lower())


class JobManager:
    def __init__(self, db: DB, verifier: Verifier, data_dir: Path):
        self.db = db
        self.verifier = verifier
        self.data_dir = data_dir
        self.tasks: dict[str, asyncio.Task] = {}
        self._run_lock = asyncio.Lock()   # one job at a time keeps SMTP load predictable

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / job_id

    def start(self, job_id: str, email_column: str) -> None:
        self.db.update_job(job_id, status="queued", email_column=email_column, phase="queued", error=None, done=0)
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, email_column))

    async def _run(self, job_id: str, email_column: str) -> None:
        async with self._run_lock:
            jd = self.job_dir(job_id)
            job = self.db.get_job(job_id)
            try:
                self.db.update_job(job_id, status="running", phase="reading file")
                df = await asyncio.to_thread(files.read_table, input_path(jd, job["filename"]))
                if email_column not in df.columns:
                    raise files.FileError(f"Column {email_column!r} not found")
                raws = [str(v) for v in df[email_column].tolist()]
                unique = list(dict.fromkeys(raws))
                # Re-check egress at the start of each job (network conditions change).
                if self.verifier.s.smtp_enabled and not self.verifier.s.skip_egress_check:
                    await self.verifier.check_egress()
                st = self.verifier.status
                self.db.update_job(job_id, total_rows=len(df), unique_emails=len(unique), phase="verifying",
                                   engine_json=json.dumps({
                                       "dns_backend": st.dns_backend, "smtp_available": st.smtp_available,
                                       "smtp_note": st.smtp_note, "helo_host": st.helo_host,
                                       "mail_from": st.mail_from, "warnings": st.warnings,
                                       "provider": st.provider, "provider_note": st.provider_note}))
                last = [0.0]

                def progress(done: int, total: int, phase: str):
                    t = time.monotonic()
                    if t - last[0] > 0.5 or done == total:
                        last[0] = t
                        self.db.update_job(job_id, done=done, phase=phase, unique_emails=total)

                results = await self.verifier.verify_many(unique, progress=progress)
                self.db.update_job(job_id, phase="writing results")
                out = files.build_output(df, email_column, results)
                status_col = [c for c in out.columns if c.endswith("email_status")][-1]
                await asyncio.to_thread(files.write_csv, out, jd / "results.csv")
                xlsx_ok = await asyncio.to_thread(files.write_xlsx, out, jd / "results.xlsx", status_col)
                by_status = Counter(r.email_status.value for r in (results[v] for v in raws))
                certainty = Counter(r.certainty.value for r in (results[v] for v in raws))
                summary = {"by_status": dict(by_status), "by_certainty": dict(certainty), "xlsx": xlsx_ok,
                           "verified_by": dict(Counter(results[v].verified_by for v in raws)),
                           "provider": self.verifier.last_provider_stats or None}
                self.db.update_job(job_id, status="done", phase="done", finished_at=now(),
                                   summary_json=json.dumps(summary))
            except Exception as e:
                log.exception("job %s failed", job_id)
                self.db.update_job(job_id, status="failed", phase="failed", error=str(e)[:500], finished_at=now())
