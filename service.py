from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import queue
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("AIO_SERVICE_DATA_DIR", "/tmp/aio-service"))
JOBS_ROOT = DATA_ROOT / "jobs"
MAX_QUEUE = int(os.getenv("AIO_MAX_QUEUE", "16"))
JOB_RETENTION_SECONDS = int(os.getenv("AIO_JOB_RETENTION_SECONDS", str(24 * 60 * 60)))
DOWNLOAD_TIMEOUT = (30, 600)
SETUP_TIMEOUT_SECONDS = int(os.getenv("AIO_SETUP_TIMEOUT_SECONDS", "300"))
PREDICTION_TIMEOUT_SECONDS = int(os.getenv("AIO_PREDICTION_TIMEOUT_SECONDS", str(60 * 60)))
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_prediction.py"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def sanitize_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


@dataclass
class JobRecord:
    id: str
    input_payload: dict[str, Any]
    status: str = "starting"
    error: Optional[str] = None
    logs: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    output_files: Optional[dict[str, Any]] = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    @property
    def job_dir(self) -> Path:
        return JOBS_ROOT / self.id

    @property
    def artifacts_dir(self) -> Path:
        return self.job_dir / "artifacts"

    @property
    def workspace_dir(self) -> Path:
        return self.job_dir / "workspace"


class CreatePredictionRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class PredictionRunnerError(RuntimeError):
    def __init__(self, message: str, logs: str = "") -> None:
        super().__init__(message)
        self.logs = logs


class ServiceState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.queue: queue.Queue[Optional[str]] = queue.Queue()
        self.jobs: dict[str, JobRecord] = {}
        self.active_job_id: Optional[str] = None
        self.runner_process: Optional[subprocess.Popen[str]] = None
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._worker_loop, name="prediction-worker", daemon=True)
        self.setup_info: dict[str, Any] = {
            "started_at": None,
            "completed_at": None,
            "status": "not_started",
            "logs": "",
        }

    def startup(self) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        self.setup_info["started_at"] = utc_now_iso()
        self.setup_info["status"] = "running"

        try:
            setup_logs = self._start_runner()
        except Exception:
            self.setup_info["completed_at"] = utc_now_iso()
            self.setup_info["status"] = "failed"
            self.setup_info["logs"] = traceback.format_exc()
            raise

        self.setup_info["completed_at"] = utc_now_iso()
        self.setup_info["status"] = "succeeded"
        self.setup_info["logs"] = setup_logs
        self.worker.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.queue.put(None)
        if self.worker.is_alive():
            self.worker.join(timeout=2)
        self._stop_runner()

    def create_job(self, payload: dict[str, Any]) -> JobRecord:
        if self.setup_info["status"] != "succeeded":
            raise RuntimeError("Service setup has not completed.")
        if self.queue.qsize() >= MAX_QUEUE:
            raise RuntimeError("Prediction queue is full.")

        job = JobRecord(id=f"pred_{uuid.uuid4().hex[:16]}", input_payload=dict(payload))
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job.id)
        self._prune_jobs()
        return job

    def get_job(self, job_id: str) -> JobRecord:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job

    def cancel_job(self, job_id: str) -> JobRecord:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.status == "starting":
                job.status = "canceled"
                job.completed_at = utc_now_iso()
                job.error = "Prediction canceled before execution."
                return job
            if job.status in TERMINAL_STATUSES:
                return job
        raise RuntimeError("Canceling an in-flight prediction is not supported.")

    def health_payload(self) -> dict[str, Any]:
        with self.lock:
            busy = self.active_job_id is not None
            queued = sum(1 for job in self.jobs.values() if job.status == "starting")
            active_job_id = self.active_job_id

        if self.setup_info["status"] != "succeeded":
            status = "SETUP_FAILED" if self.setup_info["status"] == "failed" else "STARTING"
        else:
            status = "BUSY" if busy else "READY"

        return {
            "status": status,
            "setup": self.setup_info,
            "queue_depth": queued,
            "active_prediction_id": active_job_id,
            "version": {
                "service": "async-wrapper",
            },
        }

    def serialize_job(self, job: JobRecord, request: Request) -> dict[str, Any]:
        output = self._build_output_urls(job, request) if job.output_files is not None else None
        return {
            "id": job.id,
            "status": job.status,
            "input": job.input_payload,
            "output": output,
            "error": job.error,
            "logs": job.logs,
            "metrics": job.metrics,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "urls": {
                "get": str(request.url_for("get_prediction", prediction_id=job.id)),
                "cancel": str(request.url_for("cancel_prediction", prediction_id=job.id)),
            },
        }

    def _build_output_urls(self, job: JobRecord, request: Request) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, str):
                return str(request.url_for("get_artifact", prediction_id=job.id, artifact_path=value))
            return value

        return {key: convert(value) for key, value in job.output_files.items()}

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            job_id = self.queue.get()
            if job_id is None:
                self.queue.task_done()
                return

            with self.lock:
                job = self.jobs.get(job_id)
                if not job or job.status == "canceled":
                    self.queue.task_done()
                    continue
                job.status = "processing"
                job.started_at = utc_now_iso()
                self.active_job_id = job.id

            total_started = time.perf_counter()
            try:
                output_files, logs, predict_time = self._run_prediction(job)
            except PredictionRunnerError as exc:
                with self.lock:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.logs = exc.logs
                    job.completed_at = utc_now_iso()
                    job.metrics = {
                        "predict_time": round(time.perf_counter() - total_started, 6),
                        "total_time": round(time.perf_counter() - total_started, 6),
                    }
                    self.active_job_id = None
                self.queue.task_done()
                self._prune_jobs()
                continue
            except Exception as exc:
                with self.lock:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.logs = traceback.format_exc()
                    job.completed_at = utc_now_iso()
                    job.metrics = {
                        "predict_time": round(time.perf_counter() - total_started, 6),
                        "total_time": round(time.perf_counter() - total_started, 6),
                    }
                    self.active_job_id = None
                self.queue.task_done()
                self._prune_jobs()
                continue

            with self.lock:
                job.status = "succeeded"
                job.output_files = output_files
                job.logs = logs
                job.completed_at = utc_now_iso()
                job.metrics = {
                    "predict_time": round(predict_time, 6),
                    "total_time": round(time.perf_counter() - total_started, 6),
                }
                self.active_job_id = None

            self.queue.task_done()
            self._prune_jobs()

    def _run_prediction(self, job: JobRecord) -> tuple[dict[str, Any], str, float]:
        job.job_dir.mkdir(parents=True, exist_ok=True)
        workspace = job.workspace_dir
        artifacts = job.artifacts_dir
        if workspace.exists():
            shutil.rmtree(workspace)
        if artifacts.exists():
            shutil.rmtree(artifacts)
        workspace.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)

        input_path = self._materialize_input(job.input_payload.get("music_input"), workspace / "inputs")
        predictor_payload = {
            "music_input": input_path,
            "visualize": bool(job.input_payload.get("visualize", False)),
            "sonify": bool(job.input_payload.get("sonify", False)),
            "model": job.input_payload.get("model", "harmonix-all"),
            "include_activations": bool(job.input_payload.get("include_activations", False)),
            "include_embeddings": bool(job.input_payload.get("include_embeddings", False)),
            "audioSeparator": bool(job.input_payload.get("audioSeparator", False)),
            "audioSeparatorModel": job.input_payload.get("audioSeparatorModel", "Kim_Vocal_2.onnx"),
            "includeMdxOutputs": bool(job.input_payload.get("includeMdxOutputs", False)),
        }

        log_path = job.job_dir / "runner.log"
        request = {
            "workspace": str(workspace),
            "payload": {**predictor_payload, "music_input": str(input_path)},
        }

        started = time.perf_counter()
        try:
            response = self._send_runner_request(request, PREDICTION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self._restart_runner()
            logs = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise PredictionRunnerError("Prediction timed out.", logs=logs) from exc
        predict_time = time.perf_counter() - started

        logs = str(response.get("logs") or "")
        log_path.write_text(logs, encoding="utf-8")
        if not response.get("ok"):
            raise PredictionRunnerError(str(response.get("error") or "Prediction failed."), logs=logs)

        output_dict = response.get("output")
        if not isinstance(output_dict, dict):
            raise PredictionRunnerError("Prediction runner returned an invalid output manifest.", logs=logs)
        serialized_output = self._copy_output_files(output_dict, workspace, artifacts)
        shutil.rmtree(workspace, ignore_errors=True)
        return serialized_output, logs, predict_time

    def _materialize_input(self, music_input: Any, target_dir: Path) -> Path:
        if not music_input:
            raise ValueError("Must provide `music_input`.")

        target_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(music_input, os.PathLike):
            local_path = Path(music_input)
            if not local_path.exists():
                raise ValueError(f"Input path does not exist: {local_path}")
            return local_path.resolve()

        music_input = str(music_input)
        if music_input.startswith(("http://", "https://")):
            return self._download_input(music_input, target_dir)

        local_path = Path(music_input).expanduser()
        if not local_path.exists():
            raise ValueError(f"Input path does not exist: {local_path}")
        return local_path.resolve()

    def _download_input(self, url: str, target_dir: Path) -> Path:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()

            parsed = urlparse(url)
            source_name = Path(parsed.path).name
            if not source_name:
                extension = mimetypes.guess_extension(response.headers.get("content-type", "").split(";", 1)[0].strip()) or ".bin"
                source_name = f"input{extension}"

            destination = target_dir / sanitize_filename(source_name)
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        return destination

    def _copy_output_files(self, output_dict: dict[str, Any], workspace: Path, artifacts_dir: Path) -> dict[str, Any]:
        def copy_value(key: str, value: Any, index: Optional[int] = None) -> Any:
            if isinstance(value, list):
                return [copy_value(key, item, idx) for idx, item in enumerate(value)]
            if value is None:
                return None

            source = Path(str(value))
            if not source.is_absolute():
                source = workspace / source
            if not source.exists():
                return None

            prefix = f"{key}-{index}" if index is not None else key
            destination_name = sanitize_filename(f"{prefix}-{source.name}")
            destination = artifacts_dir / destination_name
            shutil.copy2(source, destination)
            return destination_name

        return {key: copy_value(key, value) for key, value in output_dict.items()}

    def _prune_jobs(self) -> None:
        cutoff = utc_now() - timedelta(seconds=JOB_RETENTION_SECONDS)
        stale_jobs: list[JobRecord] = []
        with self.lock:
            for job_id, job in list(self.jobs.items()):
                if job.status not in TERMINAL_STATUSES:
                    continue
                completed_at = parse_iso(job.completed_at)
                if completed_at and completed_at < cutoff:
                    stale_jobs.append(job)
                    del self.jobs[job_id]

        for job in stale_jobs:
            shutil.rmtree(job.job_dir, ignore_errors=True)

    def _start_runner(self) -> str:
        self._stop_runner()
        self.runner_process = subprocess.Popen(
            [sys.executable, str(RUNNER_SCRIPT), "--server"],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        message, diagnostics = self._read_runner_message(SETUP_TIMEOUT_SECONDS)
        logs = diagnostics + str(message.get("logs") or "")
        if not message.get("ok"):
            self._stop_runner()
            raise RuntimeError(str(message.get("error") or "Prediction runner setup failed.") + f"\n{logs}")
        return logs

    def _stop_runner(self) -> None:
        process = self.runner_process
        self.runner_process = None
        if process is None:
            return
        with contextlib.suppress(Exception):
            if process.stdin:
                process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _restart_runner(self) -> None:
        if self.stop_event.is_set():
            self._stop_runner()
            return
        self._start_runner()

    def _send_runner_request(self, request: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        if self.runner_process is None or self.runner_process.poll() is not None:
            self._start_runner()

        process = self.runner_process
        assert process is not None
        if process.stdin is None:
            raise PredictionRunnerError("Prediction runner stdin is unavailable.")

        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        except BrokenPipeError as exc:
            self._restart_runner()
            raise PredictionRunnerError("Prediction runner pipe broke before request dispatch.") from exc

        message, diagnostics = self._read_runner_message(timeout_seconds)
        message["logs"] = diagnostics + str(message.get("logs") or "")
        return message

    def _read_runner_message(self, timeout_seconds: int) -> tuple[dict[str, Any], str]:
        process = self.runner_process
        if process is None or process.stdout is None:
            raise PredictionRunnerError("Prediction runner is not available.")

        diagnostics: list[str] = []
        deadline = time.monotonic() + timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the prediction runner.")

            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError("Timed out waiting for the prediction runner.")

            line = process.stdout.readline()
            if not line:
                raise PredictionRunnerError(
                    f"Prediction runner exited unexpectedly with code {process.poll()}.",
                    logs="".join(diagnostics),
                )

            stripped = line.strip()
            if not stripped:
                continue

            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                diagnostics.append(line)
                continue

            if not isinstance(payload, dict):
                diagnostics.append(line)
                continue

            return payload, "".join(diagnostics)


state = ServiceState()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    state.startup()
    try:
        yield
    finally:
        state.shutdown()


app = FastAPI(
    title="All-in-One Audio API",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "docs_url": "/docs",
        "healthcheck_url": "/health-check",
        "openapi_url": "/openapi.json",
        "predictions_url": "/predictions",
        "predictions_idempotent_url": "/predictions/{prediction_id}",
        "predictions_cancel_url": "/predictions/{prediction_id}/cancel",
    }


@app.get("/health-check")
async def health_check():
    payload = state.health_payload()
    status_code = 200 if payload["status"] != "SETUP_FAILED" else 503
    return JSONResponse(payload, status_code=status_code)


@app.post("/predictions")
async def create_prediction(request: Request, body: CreatePredictionRequest):
    try:
        job = state.create_job(body.input)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return state.serialize_job(job, request)


@app.get("/predictions/{prediction_id}", name="get_prediction")
async def get_prediction(prediction_id: str, request: Request):
    try:
        job = state.get_job(prediction_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Prediction not found.") from exc
    return state.serialize_job(job, request)


@app.post("/predictions/{prediction_id}/cancel", name="cancel_prediction")
async def cancel_prediction(prediction_id: str, request: Request):
    try:
        job = state.cancel_job(prediction_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Prediction not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return state.serialize_job(job, request)


@app.get("/artifacts/{prediction_id}/{artifact_path:path}", name="get_artifact")
async def get_artifact(prediction_id: str, artifact_path: str):
    try:
        job = state.get_job(prediction_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Prediction not found.") from exc

    artifacts_dir = job.artifacts_dir.resolve()
    artifact = (artifacts_dir / artifact_path).resolve()
    if artifacts_dir not in artifact.parents or not artifact.exists() or not artifact.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return FileResponse(artifact)
