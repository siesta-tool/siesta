"""HTTP API that runs the fundinfo pipeline remotely.

    uvicorn api.app:app --host 0.0.0.0 --port 8000

Runs execute one at a time, because every run shares output/ with its caches and the Siesta log.
Each run keeps its upload, log and results in output/runs/<run id>/.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
RUNS = Path(os.environ.get("RUNS_DIR", OUT / "runs"))
PIPELINE = [sys.executable, str(ROOT / "run_pipeline.py")]

# Server-wide endpoints and names. A run cannot override them.

SETTINGS = {flag: os.environ[name] for flag, name in [
    ("--siesta-api", "SIESTA_API"), ("--log-name", "SIESTA_LOG_NAME"),
    ("--llm-url", "LLM_URL"), ("--llm-model", "LLM_MODEL"),
] if os.environ.get(name)}

# Results copied out of the shared output folder when a run ends.

ARTIFACTS = ["report.html", "validation/issues.csv", "validation/rows.csv", "validation/summary.json",
             "rules.csv", "rules_base.csv", "violations.json", "verdicts.json", "explanations.json",
             "trace.summary.json"]
STEP = re.compile(r"== Step (\d+): (.+)")
FINISHED = {"succeeded", "failed", "cancelled"}

lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def run_dir(run_id):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", run_id):
        raise HTTPException(404, "Unknown run")
    return RUNS / run_id


def load(run_id):
    path = run_dir(run_id) / "run.json"
    if not path.exists():
        raise HTTPException(404, "Unknown run")
    return json.loads(path.read_text(encoding="utf-8"))


def save(run):
    path = RUNS / run["id"] / "run.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(run, indent=2), encoding="utf-8")
    temporary.replace(path)


def update(run_id, **fields):
    with lock:
        run = load(run_id)
        run.update(fields)
        save(run)
        return run


class Runner:
    """Executes queued runs one after another on a background thread."""

    def __init__(self):
        self.queue = queue.Queue()
        self.process = None
        self.current = None
        self.cancelled = False

    def start(self):
        RUNS.mkdir(parents=True, exist_ok=True)

        # A run that was running when the server stopped cannot resume.

        pending = []
        for path in sorted(RUNS.glob("*/run.json")):
            run = json.loads(path.read_text(encoding="utf-8"))
            if run["status"] == "running":
                update(run["id"], status="failed", finished_at=now(), error="The server stopped during the run")
            elif run["status"] == "queued":
                pending.append(run["id"])
        for run_id in pending:
            self.queue.put(run_id)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            run_id = self.queue.get()
            if load(run_id)["status"] == "queued":
                try:
                    self.execute(run_id)
                except Exception as exc:  # The worker must survive any single run.
                    update(run_id, status="failed", finished_at=now(), error=f"Runner error: {exc}")

    def execute(self, run_id):
        run = update(run_id, status="running", started_at=now())
        folder = RUNS / run_id
        started = time.time()
        self.current, self.cancelled = run_id, False
        with open(folder / "log.txt", "w", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                [*PIPELINE, *command_args(run)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
            for line in self.process.stdout:
                log.write(line)
                log.flush()
                match = STEP.match(line)
                if match:
                    update(run_id, step=int(match[1]), step_title=match[2].strip())
            code = self.process.wait()
        self.process, self.current = None, None

        out = OUT / "added" / Path(run["workbook"]).stem if run["mode"] == "add" else OUT
        artifacts = []
        for name in ARTIFACTS:
            source = out / name
            if source.exists() and source.stat().st_mtime >= started:
                target = folder / "results" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                artifacts.append(name)

        if self.cancelled: status, error = "cancelled", None
        elif code == 0: status, error = "succeeded", None
        else: status, error = "failed", last_line(folder / "log.txt") or f"Exit code {code}"
        update(run_id, status=status, exit_code=code, error=error, finished_at=now(), artifacts=artifacts)

    def cancel(self, run_id):
        with lock:
            run = load(run_id)
            if run["status"] == "queued":
                run.update(status="cancelled", finished_at=now())
                save(run)
                return run
        if run["status"] == "running" and self.current == run_id and self.process:
            self.cancelled = True

            # The pipeline starts one child process per step, so the whole group is stopped.

            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return run
        raise HTTPException(409, f"Run is {run['status']}")


def command_args(run):
    workbook = str(RUNS / run["id"] / run["workbook"])
    args = ["--add", workbook] if run["mode"] == "add" else [workbook]
    args += ["--from", str(run["first"]), "--to", str(run["last"])]
    if run["llm_limit"] is not None: args += ["--llm-limit", str(run["llm_limit"])]
    if run["rescrape"]: args.append("--rescrape")
    for flag, value in SETTINGS.items(): args += [flag, value]
    return args


def last_line(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines[-1] if lines else ""


runner = Runner()


@asynccontextmanager
async def lifespan(_app):
    runner.start()
    yield


router = APIRouter()


@router.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/docs")


@router.get("/health")
def health():
    return {"status": "ok", "running": runner.current, "queued": runner.queue.qsize()}


@router.post("/runs", status_code=202)
def create_run(workbook: UploadFile = File(...),
               mode: Literal["base", "add"] = Form("base"),
               first: int = Form(1, ge=1, le=9),
               last: int = Form(9, ge=1, le=9),
               llm_limit: int | None = Form(None, ge=0),
               rescrape: bool = Form(False)):
    """Queues a pipeline run on an uploaded workbook. `mode=add` adds its rows to the finished base run."""
    name = safe_name(Path(workbook.filename or "").name)
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(422, "The workbook must be an .xlsx or .xlsm file")
    if first > last:
        raise HTTPException(422, "first must not be after last")
    if mode == "add" and not (OUT / "trace.summary.json").exists():
        raise HTTPException(409, "mode=add needs a finished base run")
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    folder = RUNS / run_id
    folder.mkdir(parents=True)
    with open(folder / name, "wb") as target:
        shutil.copyfileobj(workbook.file, target)
    run = {"id": run_id, "status": "queued", "workbook": name, "mode": mode, "first": first, "last": last,
           "llm_limit": llm_limit, "rescrape": rescrape, "created_at": now(), "started_at": None,
           "finished_at": None, "step": None, "step_title": None, "exit_code": None, "error": None, "artifacts": []}
    with lock:
        save(run)
    runner.queue.put(run_id)
    return run


@router.get("/runs")
def list_runs():
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in RUNS.glob("*/run.json")]
    return sorted(runs, key=lambda run: run["id"], reverse=True)


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    return load(run_id)


@router.delete("/runs/{run_id}")
def cancel_run(run_id: str):
    """Cancels a queued or running run."""
    return runner.cancel(run_id)


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def get_log(run_id: str, tail: int | None = Query(None, ge=1)):
    load(run_id)
    path = run_dir(run_id) / "log.txt"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return "".join(text.splitlines(keepends=True)[-tail:]) if tail else text


@router.get("/runs/{run_id}/report")
def get_report(run_id: str):
    return get_file(run_id, "report.html")


@router.get("/runs/{run_id}/files/{name:path}")
def get_file(run_id: str, name: str):
    """Downloads one result, such as validation/issues.csv. GET /runs/{id} lists them under artifacts."""
    if name not in load(run_id)["artifacts"]:
        raise HTTPException(404, "This run has no such result")
    path = run_dir(run_id) / "results" / name
    return FileResponse(path, media_type="text/html" if name.endswith(".html") else None,
                        filename=None if name.endswith(".html") else Path(name).name)


# Siesta's API includes `router` directly. This app serves it standalone.

app = FastAPI(title="Fundinfo pipeline", lifespan=lifespan)
app.include_router(router)
