"""
BNI 爬蟲 Web UI - FastAPI 後端

提供：
- GET  /                         首頁（國家勾選 UI）
- GET  /api/countries            44 個國家清單（id, name, region 數）
- POST /api/scrape               啟動爬取任務，回傳 job_id
- GET  /api/jobs/{job_id}        查詢 job 狀態與進度
- GET  /api/download/{job_id}    下載產出 CSV
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiohttp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse the existing scraper internals — no duplication of scraping logic.
from bni_scraper_async import (
    COUNTRIES,
    DEFAULT_CONCURRENCY,
    HEADERS,
    scrape_country,
)
from bni_core import write_members_csv

logger = logging.getLogger("bni-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(title="BNI Scraper Web UI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"

# Completed CSVs are persisted to Google Cloud Storage so they survive Cloud
# Run instance recycling (scale-to-zero). The in-memory JOBS dict only tracks
# live progress; once a scrape finishes, the file lives in GCS keyed by job_id.
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")  # e.g. "bni-scrw-exports"

# In-memory job store — fine for live progress on a single instance. The
# durable artefact is the CSV in GCS, not this dict.
JOBS: dict[str, dict[str, Any]] = {}

# Lazy GCS client (only imported/initialised when a bucket is configured).
_gcs_client = None


def _get_bucket():
    """Return the configured GCS bucket handle, or None if GCS is disabled."""
    global _gcs_client
    if not GCS_BUCKET:
        return None
    if _gcs_client is None:
        from google.cloud import storage  # lazy import — only on Cloud Run
        _gcs_client = storage.Client()
    return _gcs_client.bucket(GCS_BUCKET)


def _upload_csv_to_gcs(job_id: str, local_path: Path, download_name: str) -> bool:
    """Upload the finished CSV to gs://<bucket>/exports/<job_id>.csv.

    The friendly download filename is stored in object metadata so the
    download endpoint can set Content-Disposition even on a cold instance.
    Returns True on success.
    """
    bucket = _get_bucket()
    if bucket is None:
        return False
    try:
        blob = bucket.blob(f"exports/{job_id}.csv")
        blob.metadata = {"download_name": download_name}
        blob.upload_from_filename(str(local_path), content_type="text/csv")
        return True
    except Exception:
        logger.exception("Failed to upload %s to GCS", job_id)
        return False


class ScrapeRequest(BaseModel):
    country_ids: list[int]
    concurrency: int = DEFAULT_CONCURRENCY


# Cloud Run 只有一台 1 vCPU 機器：同時跑多個爬取任務會把 CPU 吃滿（查進度、
# 下載被 Cloud Run 回 429 Rate exceeded），也會開太多連線被 BNI 斷線。
# 所以一次只跑一個任務，其他的排隊。
_SCRAPE_LOCK = asyncio.Lock()
_BACKGROUND_TASKS: set = set()
MAX_CONCURRENCY = 50


# ── Scrape worker ────────────────────────────────────────────────────────────


def _job_progress(job: dict[str, Any]):
    """把 scrape_country() 的進度回呼寫進 job dict；``log`` 會累加成清單。"""
    def report(**fields: Any) -> None:
        line = fields.pop("log", None)
        if line:
            job["log"].append(line)
        job.update(fields)
    return report


async def _run_job(job_id: str, country_ids: list[int], concurrency: int) -> None:
    job = JOBS[job_id]
    if _SCRAPE_LOCK.locked():
        job["log"].append("前面還有其他爬取任務，排隊中⋯")
    async with _SCRAPE_LOCK:
        await _run_job_now(job_id, country_ids, concurrency)


async def _run_job_now(job_id: str, country_ids: list[int], concurrency: int) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = time.time()

    try:
        connector = aiohttp.TCPConnector(
            limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300,
        )
        async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
            all_members: list = []
            for country_id in country_ids:
                members = await scrape_country(
                    session, country_id, concurrency, progress=_job_progress(job),
                )
                all_members.extend(members)
                job["total_members"] = len(all_members)

        # Write combined CSV
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if len(country_ids) == 1:
            slug = COUNTRIES.get(country_ids[0], ("unknown", 0))[0].lower().replace(" ", "_")
            csv_path = OUTPUT_DIR / f"bni_members_{slug}_{timestamp}.csv"
        else:
            csv_path = OUTPUT_DIR / f"bni_members_multi_{timestamp}.csv"
        write_members_csv(csv_path, all_members)

        job["csv_path"] = str(csv_path)
        job["csv_filename"] = csv_path.name

        # Persist to GCS so the download survives instance recycling.
        loop = asyncio.get_event_loop()
        uploaded = await loop.run_in_executor(
            None, _upload_csv_to_gcs, job_id, csv_path, csv_path.name,
        )
        job["gcs_persisted"] = uploaded

        job["status"] = "done"
        job["stage"] = "done"
        job["finished_at"] = time.time()
        job["elapsed"] = job["finished_at"] - job["started_at"]
    except Exception as exc:  # surface to UI rather than swallow
        logger.exception("Job %s failed", job_id)
        job["status"] = "error"
        job["stage"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["finished_at"] = time.time()


# ── API endpoints ────────────────────────────────────────────────────────────


@app.get("/api/countries")
async def list_countries() -> dict:
    """44 個國家清單，依字母排序，方便前端直接渲染。"""
    items = [
        {"id": cid, "name": name, "region_count": regions}
        for cid, (name, regions) in sorted(COUNTRIES.items(), key=lambda x: x[1][0])
    ]
    return {"countries": items, "default_id": 7699}  # 7699 = Taiwan


@app.post("/api/scrape")
async def start_scrape(
    req: ScrapeRequest, background: BackgroundTasks,
) -> dict:
    if not req.country_ids:
        raise HTTPException(400, "至少選一個國家")
    invalid = [cid for cid in req.country_ids if cid not in COUNTRIES]
    if invalid:
        raise HTTPException(400, f"未知 country_id: {invalid}")

    concurrency = max(1, min(req.concurrency, MAX_CONCURRENCY))
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "created_at": time.time(),
        "country_ids": req.country_ids,
        "country_names": [COUNTRIES[c][0] for c in req.country_ids],
        "concurrency": concurrency,
        "stage": "queued",
        "log": [],
        "members_count": 0,
        "total_members": 0,
        "chapters_done": 0,
        "chapters_total": 0,
        "details_done": 0,
        "details_total": 0,
        "regions_total": 0,
        "csv_path": None,
        "csv_filename": None,
        "started_at": None,
        "finished_at": None,
        "elapsed": None,
        "error": None,
    }

    # Schedule the coroutine on the running loop so we don't block the response.
    # 保留 task 參照，避免背景任務被垃圾回收中途消失。
    task = asyncio.create_task(_run_job(job_id, req.country_ids, concurrency))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, "job 不存在")
    job = JOBS[job_id]
    # Don't leak the absolute file path — only expose a download URL when ready.
    public = {k: v for k, v in job.items() if k != "csv_path"}
    if job.get("status") == "queued":
        public["queue_ahead"] = sum(
            1 for other in JOBS.values()
            if other["status"] == "running"
            or (other["status"] == "queued" and other["created_at"] < job["created_at"])
        )
    if job.get("status") == "done":
        public["download_url"] = f"/api/download/{job_id}"
    return public


def _download_name_for(job_id: str) -> str:
    """Best-effort friendly filename for a download (warm dict → fallback)."""
    job = JOBS.get(job_id)
    if job and job.get("csv_filename"):
        return job["csv_filename"]
    return f"bni_members_{job_id}.csv"


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    # 1. Warm path: the local file is still on this instance.
    job = JOBS.get(job_id)
    if job and job.get("status") == "done" and job.get("csv_path"):
        local = Path(job["csv_path"])
        if local.exists():
            return FileResponse(
                str(local), media_type="text/csv", filename=job["csv_filename"],
            )

    # 2. Cold path: instance recycled (or different instance) — stream from GCS.
    bucket = _get_bucket()
    if bucket is not None:
        blob = bucket.blob(f"exports/{job_id}.csv")
        # Run the blocking GCS calls off the event loop.
        loop = asyncio.get_event_loop()
        exists = await loop.run_in_executor(None, blob.exists)
        if exists:
            await loop.run_in_executor(None, blob.reload)  # fetch metadata
            download_name = (blob.metadata or {}).get(
                "download_name", _download_name_for(job_id),
            )
            data = await loop.run_in_executor(None, blob.download_as_bytes)
            headers = {
                "Content-Disposition": f'attachment; filename="{download_name}"'
            }
            return StreamingResponse(
                iter([data]), media_type="text/csv", headers=headers,
            )

    # 3. Genuinely gone.
    raise HTTPException(
        404,
        "找不到此檔案。可能是任務已過期（檔案保留 7 天），請重新爬取。",
    )


# ── Static frontend ──────────────────────────────────────────────────────────


# Mount static last so /api routes win priority.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import os
    import uvicorn
    # Cloud Run sets PORT; locally default to 8000.
    port = int(os.environ.get("PORT", "8000"))
    # Bind to 0.0.0.0 in container, 127.0.0.1 locally.
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    uvicorn.run("server:app", host=host, port=port, reload=False)
