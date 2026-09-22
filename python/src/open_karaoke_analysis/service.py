"""Loopback analysis API with bounded workers, typed errors, and isolated artifacts."""

import asyncio
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .adapters import (
    AdapterRegistry,
    AnalysisCancelled,
    AnalysisContext,
    MockAdapter,
    ModelAdapter,
    resolve_artifact,
)
from .contracts import PROTOCOL_VERSION, AnalysisRequest, ErrorDetail, Job, Role, ServiceError
from .stems import StemsAdapter

LOG = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Record:
    job: Job
    cancel: Event
    task: asyncio.Task[None] | None = None


class JobManager:
    def __init__(self, root: Path, registry: AdapterRegistry, instance_id: UUID, capacity: int):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.instance_id = instance_id
        self.capacity = capacity
        self.records: dict[UUID, Record] = {}
        # One inference worker by default: model/GPU adapters need not be reentrant.
        self.semaphore = asyncio.Semaphore(1)
        self.stopping = False

    def get(self, job_id: UUID) -> Record:
        try:
            return self.records[job_id]
        except KeyError as error:
            raise ServiceError(
                "job_not_found", "Job is not present in this service instance", 404
            ) from error

    def submit(self, role: Role, request: AnalysisRequest) -> Job:
        if self.stopping:
            raise ServiceError("shutting_down", "Service is shutting down", 503, True)
        adapter = self.registry.select(role, request.model_id)
        options = adapter.validate_options(request.options)
        input_path = None
        if request.input_artifact is not None:
            input_path = resolve_artifact(self.root, request.input_artifact, must_exist=True)
        # Never evict running jobs. Bound memory while keeping recent terminal snapshots.
        if len(self.records) >= self.capacity:
            expired = next(
                (
                    key
                    for key, rec in self.records.items()
                    if rec.job.status in TERMINAL and rec.task is not None and rec.task.done()
                ),
                None,
            )
            if expired is None:
                raise ServiceError("queue_full", "Analysis queue is full", 429, True)
            del self.records[expired]
        job = Job(
            id=uuid4(),
            instance_id=self.instance_id,
            kind=role,
            model_id=adapter.info.id,
            created_at=now(),
        )
        record = Record(job, Event())
        self.records[job.id] = record
        record.task = asyncio.create_task(self.run(record, adapter, options, input_path))
        return job.model_copy(deep=True)

    def cancel(self, job_id: UUID) -> Job:
        record = self.get(job_id)
        if record.job.status not in TERMINAL:
            record.cancel.set()
            # Terminal acknowledgement follows worker cleanup, not just task cancellation.
            record.job.stage = "cancelling"
        return record.job.model_copy(deep=True)

    def report(self, record: Record, progress: float, stage: str) -> None:
        if not record.cancel.is_set() and record.job.status == "running":
            record.job.progress = max(record.job.progress, min(1.0, max(0.0, progress)))
            record.job.stage = stage

    async def run(
        self, record: Record, adapter: ModelAdapter, options: dict, input_path: Path | None
    ) -> None:
        job = record.job
        prefix = f"jobs/{self.instance_id}/{job.id}"
        loop = asyncio.get_running_loop()
        context = AnalysisContext(
            self.root,
            prefix,
            input_path,
            record.cancel,
            lambda p, s: loop.call_soon_threadsafe(self.report, record, p, s),
        )
        terminal = "failed"
        try:
            async with self.semaphore:
                context.checkpoint()
                job.status, job.stage, job.started_at = "running", "running", now()
                artifacts = await asyncio.to_thread(adapter.analyze, context, options)
                context.checkpoint()
                for artifact in artifacts:
                    if not artifact.startswith(prefix + "/"):
                        raise ServiceError(
                            "invalid_artifact", "Adapter returned an unowned artifact", 500
                        )
                    resolve_artifact(self.root, artifact, must_exist=True)
                job.artifacts = artifacts
                terminal = "completed"
                job.progress = 1.0
        except AnalysisCancelled:
            terminal = "cancelled"
        except ServiceError as error:
            job.error = error.detail
        except Exception:
            LOG.exception("Adapter failed for job %s", job.id)
            job.error = ErrorDetail(code="adapter_failure", message="Analysis adapter failed")
        finally:
            if terminal != "completed":
                job.artifacts = []
                # Only this job's private directory is removed.
                try:
                    directory = resolve_artifact(self.root, prefix)
                    if directory.exists():
                        await asyncio.to_thread(shutil.rmtree, directory)
                except (OSError, ServiceError):
                    LOG.exception("Could not clean artifacts for job %s", job.id)
            job.status = job.stage = terminal
            job.finished_at = now()

    async def shutdown(self) -> None:
        self.stopping = True
        for record in self.records.values():
            if record.job.status not in TERMINAL:
                record.cancel.set()
        await asyncio.gather(*(r.task for r in self.records.values() if r.task is not None))


def create_app(
    root: Path, token: str, adapters: list[ModelAdapter] | None = None, *, capacity: int = 256
) -> FastAPI:
    if len(token) < 32 or capacity < 1:
        raise ValueError("A 32-character session token and positive capacity are required")
    registry = AdapterRegistry([MockAdapter(), StemsAdapter()] if adapters is None else adapters)
    instance_id = uuid4()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.jobs = JobManager(root, registry, instance_id, capacity)
        app.state.stop_requested = asyncio.Event()
        try:
            yield
        finally:
            await app.state.jobs.shutdown()

    app = FastAPI(
        title="Open Karaoke Analysis",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.instance_id = instance_id

    def error_response(error: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"error": error.detail.model_dump()})

    @app.middleware("http")
    async def session_guard(request: Request, call_next):
        # No browser-facing API. Bearer token is passed privately by the parent Core process.
        if request.headers.get("origin") is not None:
            return error_response(
                ServiceError("forbidden_origin", "Browser requests are not accepted", 403)
            )
        supplied = request.headers.get("authorization", "").encode()
        if not secrets.compare_digest(supplied, f"Bearer {token}".encode()):
            return error_response(
                ServiceError("unauthorized", "A valid session token is required", 401)
            )
        if request.method == "POST":
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65_536:
                    return error_response(
                        ServiceError("request_too_large", "Request exceeds 64 KiB", 413)
                    )
                chunks.append(chunk)
            # Cache the bounded body for FastAPI's downstream parser.
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, error: ServiceError):
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError):
        return error_response(
            ServiceError("invalid_request", "Request does not match the API schema", 422)
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        return error_response(ServiceError("http_error", str(error.detail), error.status_code))

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, _error: Exception):
        LOG.exception("Unhandled service error")
        return error_response(ServiceError("internal_error", "Internal service error", 500))

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "open-karaoke-analysis",
            "protocol_version": PROTOCOL_VERSION,
            "instance_id": str(instance_id),
            "pid": os.getpid(),
        }

    @app.get("/models")
    async def models():
        return {"models": [a.info.model_dump() for a in registry.adapters.values()]}

    @app.post("/analysis/{role}", response_model=Job, status_code=202)
    async def analyze(role: Role, request: AnalysisRequest):
        return app.state.jobs.submit(role, request)

    @app.get("/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: UUID):
        return app.state.jobs.get(job_id).job.model_copy(deep=True)

    @app.post("/jobs/{job_id}/cancel", response_model=Job)
    async def cancel_job(job_id: UUID):
        return app.state.jobs.cancel(job_id)

    @app.post("/shutdown")
    async def shutdown():
        app.state.jobs.stopping = True
        app.state.stop_requested.set()
        return {"status": "stopping"}

    return app
