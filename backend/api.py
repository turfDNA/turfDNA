from __future__ import annotations

import shutil
from pathlib import Path
import json
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import ADMIN_PASSWORD, OUTPUTS_DIR, RACECARDS_INBOX, RESULTS_INBOX, ROOT, HISTORICAL_RESULTS_INBOX, DAILY_RESULTS_INBOX, DAILY_RACECARDS_INBOX
from pipeline.run_daily_pipeline import run_daily_pipeline, rebuild_database_from_folders
from pipeline.horse_profiles import build_horse_profile
from pipeline.diagnostics import build_diagnostics

app = FastAPI(title="Race Intelligence Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def check_password(password: str) -> None:
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect admin password")


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def home():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/horse.html", response_class=HTMLResponse)
def horse_page():
    return FileResponse(FRONTEND_DIR / "horse.html")


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
@app.get("/admin.html", response_class=HTMLResponse)
def admin_page():
    return FileResponse(FRONTEND_DIR / "admin.html")


@app.get("/api/results")
def latest_results():
    path = OUTPUTS_DIR / "latest_results_review.json"
    if not path.exists():
        return {"date": None, "courses": []}
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/racecards")
def latest_racecards():
    path = OUTPUTS_DIR / "latest_racecards.json"
    if not path.exists():
        return {"date": None, "courses": []}
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/horse")
def horse_profile(horse_id: str | None = None, horse: str | None = None):
    return JSONResponse(build_horse_profile(horse_id=horse_id, horse=horse))


@app.get("/api/admin/status")
def admin_status(password: str):
    check_password(password)
    path = OUTPUTS_DIR / "admin_run_log.json"
    if not path.exists():
        return {"latest_run": None, "imported_totals": []}
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.post("/api/admin/upload")
def upload_file(
    password: Annotated[str, Form()],
    file_type: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    check_password(password)
    if file_type not in {"historical_results", "daily_results", "results", "racecards", "daily_racecards"}:
        raise HTTPException(status_code=400, detail="file_type must be historical_results, daily_results or racecards")
    if not file.filename or not file.filename.lower().endswith((".csv", ".zip")):
        raise HTTPException(status_code=400, detail="Please upload a CSV or ZIP file")

    if file_type == "historical_results":
        target_dir = HISTORICAL_RESULTS_INBOX
    elif file_type in {"daily_results", "results"}:
        target_dir = DAILY_RESULTS_INBOX
    else:
        target_dir = DAILY_RACECARDS_INBOX
    target = target_dir / Path(file.filename).name
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"status": "uploaded", "file_type": file_type, "file_name": target.name, "saved_to": str(target_dir)}


@app.post("/api/admin/run")
def run_update(password: Annotated[str, Form()]):
    check_password(password)
    return run_daily_pipeline()


@app.get("/api/admin/folders")
def admin_folders(password: str):
    check_password(password)
    return {
        "historical_results": str(HISTORICAL_RESULTS_INBOX),
        "daily_results": str(DAILY_RESULTS_INBOX),
        "daily_racecards": str(DAILY_RACECARDS_INBOX),
        "database": str(ROOT / "database" / "racing.db"),
    }


@app.post("/api/admin/rebuild")
def rebuild_all(password: Annotated[str, Form()]):
    check_password(password)
    return rebuild_database_from_folders()


@app.get("/api/admin/diagnostics")
def diagnostics(password: str):
    check_password(password)
    return JSONResponse(build_diagnostics())
