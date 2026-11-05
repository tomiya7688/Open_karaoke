"""Loopback analysis API with bounded workers, typed errors, and isolated artifacts."""

import asyncio
import copy
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
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from .adapters import (
    AdapterRegistry,
    AnalysisCancelled,
    AnalysisContext,
    MockAdapter,
    ModelAdapter,
    resolve_artifact,
)
from .alignment import AlignmentAdapter
from .contracts import PROTOCOL_VERSION, AnalysisRequest, ErrorDetail, Job, Role, ServiceError
from .lyrics import LyricsAdapter
from .model_packages import discover_packages
from .model_selection import SelectionChoice, read_selection, resolve_selection, set_selection
from .notes import NotesAdapter
from .pitch import PitchAdapter
from .song import STAGES, SongAdapter
from .stems import StemsAdapter
from .vocal_events import VocalEventAdapter

LOG = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}


def now() -> datetime:
    return datetime.now(UTC)


class ModelSelectionRequest(BaseModel):
    """
    {
      責務: [ModelSelectionRequest: POST /model-selection の本文。1 roleの選択か解除]
      フィールド: [role: 対象role, model_id: 選ぶモデルID。nullで解除]
    }
    """

    model_config = ConfigDict(extra="forbid")
    role: Role
    model_id: str | None = Field(default=None, min_length=1, max_length=128)


@dataclass
class Record:
    job: Job
    cancel: Event
    task: asyncio.Task[None] | None = None
    model_plan: dict | None = None


def pin_song_plan(
    options: dict, stage_choices: dict[str, SelectionChoice] | None
) -> tuple[dict, dict | None]:
    """
    {
      責務: [pin_song_plan: song Jobの各段のモデルを受付時に確定し、決まり方を記録する]
      処理: [
        1: options.modelsに明示指定がある段はsource=requestとして変えない,
        2: 無ければ保存済み選択が使える段はそのAdapter IDをmodelsへ書き込む(source=selection),
        3: 使えない選択はfallback_reason付きでsource=default_fallback、選択なしはdefault
      ]
      引数: [options: リクエストのoptions(変更しない), stage_choices: 段名 -> 解決済み選択]
      戻り値: [(pin済みoptionsのコピー, 段別プラン。optionsの形が不正ならプランなし)]
    }
    """
    models = options.get("models", {})
    if stage_choices is None or not isinstance(models, dict):
        return options, None
    pinned = dict(models)
    plan: dict[str, dict] = {}
    for stage, choice in stage_choices.items():
        if stage in models:
            plan[stage] = {"source": "request", "selected_model_id": None, "fallback_reason": None}
        elif choice.adapter_id is not None:
            pinned[stage] = choice.adapter_id
            plan[stage] = {
                "source": "selection",
                "selected_model_id": choice.stored_id,
                "fallback_reason": None,
            }
        elif choice.stored_id is not None:
            plan[stage] = {
                "source": "default_fallback",
                "selected_model_id": choice.stored_id,
                "fallback_reason": choice.fallback_reason,
            }
        else:
            plan[stage] = {"source": "default", "selected_model_id": None, "fallback_reason": None}
    plan["resolved_at"] = now().isoformat()
    return {**options, "models": pinned}, plan


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

    def submit(
        self,
        role: Role,
        request: AnalysisRequest,
        choice: SelectionChoice | None = None,
        stage_choices: dict[str, SelectionChoice] | None = None,
    ) -> Job:
        """
        {
          責務: [submit: Jobを受け付け、使うAdapterを決めて実行を始める]
          処理: [
            1: model_idの明示指定があればそれを最優先する(保存済み選択は見ない),
            2: 無ければ保存済み選択(choice)のAdapterを使う,
            3: 選択が無い・使えないときは従来の既定Adapterを使い、使えない場合は理由を記録する,
            4: song Jobは各段の選択(stage_choices)も受付時に確定し、以後の変更を反映しない
          ]
          引数: [role: Jobのrole, request: 解析リクエスト, choice: 事前に解決した保存済み選択,
            stage_choices: song各段の事前解決済み選択]
          戻り値: [model_id・model_source等を含むJobのスナップショット]
          エラー: [ServiceError: shutting_down, model_not_found, model_role_mismatch, queue_full等]
        }
        """
        if self.stopping:
            raise ServiceError("shutting_down", "Service is shutting down", 503, True)
        source, selected, reason = "default", None, None
        if request.model_id is not None:
            source = "request"
            adapter = self.registry.select(role, request.model_id)
        elif choice is not None and choice.adapter_id is not None:
            source, selected = "selection", choice.stored_id
            adapter = self.registry.select(role, choice.adapter_id)
        else:
            if choice is not None and choice.stored_id is not None:
                source, selected = "default_fallback", choice.stored_id
                reason = choice.fallback_reason
            adapter = self.registry.select(role, None)
        request_options, plan = request.options, None
        if role == Role.SONG:
            request_options, plan = pin_song_plan(request.options, stage_choices)
        options = adapter.validate_options(request_options)
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
            model_source=source,
            selected_model_id=selected,
            selection_fallback_reason=reason,
            model_plan=copy.deepcopy(plan),
            created_at=now(),
        )
        record = Record(job, Event(), model_plan=plan)
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
            record.model_plan,
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
    registry = AdapterRegistry(
        [
            MockAdapter(),
            StemsAdapter(),
            LyricsAdapter(),
            LyricsAdapter(model_name="large-v3-turbo"),
            AlignmentAdapter(),
            PitchAdapter(),
            VocalEventAdapter(),
            NotesAdapter(),
        ]
        if adapters is None
        else adapters
    )
    if adapters is None:
        registry.adapters[SongAdapter.info.id] = SongAdapter(registry)
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

    @app.get("/model-packages")
    async def model_packages():
        entries = await asyncio.to_thread(discover_packages, root, registry.adapters)
        return {"packages": [e.model_dump(mode="json") for e in entries]}

    @app.get("/model-selection")
    async def get_model_selection():
        selection = await asyncio.to_thread(read_selection, root, registry.adapters)
        return {"selection": selection}

    @app.post("/model-selection")
    async def post_model_selection(request: ModelSelectionRequest):
        selection = await asyncio.to_thread(
            set_selection, root, registry.adapters, request.role, request.model_id
        )
        return {"selection": selection}

    @app.post("/analysis/{role}", response_model=Job, status_code=202)
    async def analyze(role: Role, request: AnalysisRequest):
        # 明示指定が無いときだけ保存済み選択を読む(ディスクI/Oはスレッドで行う)。
        choice = None
        if request.model_id is None:
            choice = await asyncio.to_thread(resolve_selection, root, registry.adapters, role)
        stage_choices = None
        if role == Role.SONG:
            stage_choices = {
                stage: await asyncio.to_thread(
                    resolve_selection, root, registry.adapters, Role(stage)
                )
                for stage in STAGES
            }
        return app.state.jobs.submit(role, request, choice, stage_choices)

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
