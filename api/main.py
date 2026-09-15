from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr
from starlette.middleware.sessions import SessionMiddleware

from config import CONFIG
from pipeline.universe_builder import RunCancelled, RunResult, run_universe_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Bump this string with every meaningful pipeline/UI change so it's trivial
# to confirm from the running app (no git command needed) whether a given
# laptop is actually on the latest code after running update.bat -- printed
# loudly at startup and exposed via /api/health.
BUILD_VERSION = "2026-09-15-01-chromedriver-cache-and-200-floor"
logger.info("=" * 60)
logger.info("Market Universe Finder API starting -- BUILD_VERSION: %s", BUILD_VERSION)
logger.info("=" * 60)

app = FastAPI(title="Market Universe Finder API")

# Local-only tool (per UNDERSTANDING.txt §7): the Next.js dev server and this
# API both run on localhost, so CORS is restricted to that origin rather than
# left open.
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session cookie signing key -- generated fresh per process start if not set.
# Fine for a local single-user tool; restarting the API just logs everyone
# out, matching the old tool's 24h-session/no-persistence-required model.
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr


@app.post("/api/login")
def login(body: LoginRequest, request: Request) -> dict:
    email = body.email.lower().strip()
    request.session["email"] = email
    return {"email": email, "is_admin": email in CONFIG.admin_emails}


@app.post("/api/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@app.get("/api/session")
def get_session(request: Request) -> dict:
    email = request.session.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Not logged in")
    return {"email": email, "is_admin": email in CONFIG.admin_emails}


def _require_email(request: Request) -> str:
    email = request.session.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Not logged in")
    return email


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    run_id: str
    owner_email: str
    market: str
    geography: str
    category_prompt: str
    brief: str
    status: Literal["running", "done", "error", "cancelled"] = "running"
    progress_log: list[dict] = field(default_factory=list)
    result: RunResult | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)


# In-memory job store -- fine for a single-laptop local tool (see
# UNDERSTANDING.txt §7: this is never a shared server). Lost on restart.
RUNS: dict[str, RunState] = {}
_runs_lock = threading.Lock()


class StartRunRequest(BaseModel):
    market: str
    geography: str = "Global"
    category_prompt: str = ""
    brief: str = ""


def _execute_run(state: RunState) -> None:
    def progress_cb(stage: str, detail: str) -> None:
        with _runs_lock:
            state.progress_log.append({"stage": stage, "detail": detail, "ts": time.time()})

    try:
        result = run_universe_search(
            state.market, state.geography, state.category_prompt,
            brief=state.brief, progress_cb=progress_cb,
            cancel_event=state.cancel_event,
        )
        with _runs_lock:
            state.result = result
            state.status = "done"
    except RunCancelled:
        with _runs_lock:
            state.status = "cancelled"
    except Exception as e:
        logger.exception("Run %s failed", state.run_id)
        with _runs_lock:
            state.error = str(e)
            state.status = "error"


@app.post("/api/runs")
def start_run(body: StartRunRequest, request: Request) -> dict:
    email = _require_email(request)
    if not body.market.strip():
        raise HTTPException(status_code=400, detail="Market name is required")

    run_id = uuid.uuid4().hex
    state = RunState(
        run_id=run_id, owner_email=email, market=body.market.strip(),
        geography=body.geography.strip() or "Global",
        category_prompt=body.category_prompt.strip(), brief=body.brief.strip(),
    )
    with _runs_lock:
        RUNS[run_id] = state

    thread = threading.Thread(target=_execute_run, args=(state,), daemon=True)
    thread.start()
    return {"run_id": run_id}


def _run_summary(state: RunState) -> dict:
    summary = {
        "run_id": state.run_id,
        "market": state.market,
        "geography": state.geography,
        "category_prompt": state.category_prompt,
        "status": state.status,
        "started_at": state.started_at,
        "progress_log": state.progress_log[-50:],
    }
    if state.status == "done" and state.result:
        r = state.result
        summary["duration_seconds"] = r.duration_seconds
        summary["total_candidates_found"] = r.total_candidates_found
        summary["total_verified"] = r.total_verified
        summary["companies_count"] = len(r.companies)
        summary["companies_preview"] = [
            {
                "company_name": c.company_name,
                "brand_name": c.brand_name or c.company_name,
                "parent_or_independent": c.parent_or_independent or "Independent",
                "website": c.website,
                "functionality": c.subcategory or c.category,
                "geography": c.hq_country,
                "is_relevant": "yes" if c.is_relevant else "no",
                "category": c.category,
            }
            for c in r.companies[:200]
        ]
        summary["download_formats"] = list(r.output_paths.keys())
    elif state.status == "error":
        summary["error"] = state.error
    return summary


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str, request: Request) -> dict:
    email = _require_email(request)
    with _runs_lock:
        state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    if state.owner_email != email and email not in CONFIG.admin_emails:
        raise HTTPException(status_code=403, detail="Not your run")
    if state.status != "running":
        raise HTTPException(status_code=409, detail="Run is not currently running")
    # Cooperative cancellation only -- there's no way to forcibly kill a
    # thread mid-Selenium-session or mid-HTTP-call, so this signals the
    # pipeline to stop at its next checkpoint (between discovery rounds or
    # pipeline stages) rather than instantly.
    state.cancel_event.set()
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    email = _require_email(request)
    with _runs_lock:
        state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    if state.owner_email != email and email not in CONFIG.admin_emails:
        raise HTTPException(status_code=403, detail="Not your run")
    return _run_summary(state)


@app.get("/api/runs")
def list_runs(request: Request) -> list[dict]:
    email = _require_email(request)
    is_admin = email in CONFIG.admin_emails
    with _runs_lock:
        states = list(RUNS.values())
    visible = states if is_admin else [s for s in states if s.owner_email == email]
    visible.sort(key=lambda s: s.started_at, reverse=True)
    return [_run_summary(s) for s in visible]


_FORMAT_MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@app.get("/api/runs/{run_id}/download/{fmt}")
def download_run_file(run_id: str, fmt: str, request: Request) -> FileResponse:
    email = _require_email(request)
    with _runs_lock:
        state = RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    if state.owner_email != email and email not in CONFIG.admin_emails:
        raise HTTPException(status_code=403, detail="Not your run")
    if state.status != "done" or not state.result:
        raise HTTPException(status_code=409, detail="Run not finished yet")

    path: Path | None = state.result.output_paths.get(fmt)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail=f"No {fmt} output for this run")

    return FileResponse(
        path, media_type=_FORMAT_MEDIA_TYPES.get(fmt, "application/octet-stream"), filename=path.name,
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "build_version": BUILD_VERSION}
