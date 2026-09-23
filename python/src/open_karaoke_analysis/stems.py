"""Stem adapter, immutable content cache, and per-job artifact publication."""

import errno
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .adapters import AnalysisContext, atomic_json, resolve_artifact
from .contracts import ModelInfo, Role, ServiceError
from .stem_backend import MODEL_ID, WEIGHT_VERSION, UmxHqBackend, file_sha256

PIPELINE_VERSION = 1
STEM_FILES = ("vocals.wav", "accompaniment.wav")


class StemOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    device: Literal["cpu", "cuda"] = "cpu"
    chunk_seconds: int = Field(default=12, ge=2, le=30)
    overlap_seconds: int = Field(default=2, ge=1, le=5)
    niter: int = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def overlap_fits(self):
        if self.overlap_seconds >= self.chunk_seconds:
            raise ValueError("Overlap must be smaller than a chunk")
        return self


def copy_checked(source: Path, destination: Path, context: AnalysisContext) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        while block := input_file.read(1024 * 1024):
            context.checkpoint()
            digest.update(block)
            output_file.write(block)
        output_file.flush()
        os.fsync(output_file.fileno())
    context.checkpoint()
    return digest.hexdigest()


def cache_identity(input_hash: str, model: dict, options: dict) -> dict:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "input_sha256": input_hash,
        "model": model,
        "options": options,
    }


def cache_key(identity: dict) -> str:
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_cache(path: Path, identity: dict, context: AnalysisContext) -> dict | None:
    from .stem_audio import inspect_audio

    if not path.is_dir():
        return None
    try:
        manifest_path = resolve_artifact(
            context.root,
            str((path / "cache.json").relative_to(context.root).as_posix()),
            must_exist=True,
        )
        if manifest_path.stat().st_size > 32_768:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["identity"] != identity:
            return None
        for name in STEM_FILES:
            file = resolve_artifact(
                context.root, (path / name).relative_to(context.root).as_posix(), must_exist=True
            )
            if file_sha256(file, context) != manifest["sha256"][name]:
                return None
            info = inspect_audio(file)
            if info.frames != manifest["audio"]["duration_samples"] or info.subtype != "FLOAT":
                return None
        return manifest
    except ServiceError as error:
        if error.detail.code == "invalid_path":
            raise  # Never treat a symlink escape as an ordinary cache miss.
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None


class StemsAdapter:
    info = ModelInfo(
        id=MODEL_ID,
        version=WEIGHT_VERSION,
        role=Role.STEMS,
        capabilities=["vocals", "accompaniment", "48khz", "content-cache", "lazy-runtime"],
    )

    def __init__(self, backend_factory=UmxHqBackend):
        self.backend_factory = backend_factory

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return StemOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid stem separation options") from error

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        if context.input_path is None:
            raise ServiceError(
                "input_required", "Stem separation requires a normalized audio artifact"
            )
        context.checkpoint()
        # Heavy dependencies are imported only after a real separation request.
        try:
            from .stem_audio import inspect_audio, separate_audio
        except ImportError as error:
            raise ServiceError(
                "dependency_missing", "Install the analysis package with the stems extra", 503
            ) from error
        options = self.validate_options(options)
        inspect_audio(context.input_path)
        backend = self.backend_factory()
        model = backend.identity(options["device"])
        cache_root = resolve_artifact(context.root, "cache/stems/v1")
        cache_root.mkdir(parents=True, exist_ok=True)
        output = resolve_artifact(context.root, context.output_prefix)
        output.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".working-", dir=cache_root) as work:
                directory = Path(work)
                # Hash exactly the bytes used for inference, even if the original is later replaced.
                context.report(0.01, "snapshot_input")
                snapshot = directory / "input.wav"
                input_hash = copy_checked(context.input_path, snapshot, context)
                inspect_audio(snapshot)
                identity = cache_identity(input_hash, model, options)
                key = cache_key(identity)
                cached = resolve_artifact(context.root, f"cache/stems/v1/{key}")
                manifest = read_cache(cached, identity, context)
                hit = manifest is not None
                if hit:
                    context.report(0.85, "cache_hit")
                    source_directory = cached
                else:
                    backend.prepare(context, options["device"], options["niter"])
                    staging = directory / "result"
                    staging.mkdir()
                    audio = separate_audio(snapshot, staging, backend, context, options)
                    hashes = {name: file_sha256(staging / name, context) for name in STEM_FILES}
                    manifest = {"identity": identity, "audio": audio, "sha256": hashes}
                    atomic_json(staging / "cache.json", manifest)
                    context.checkpoint()
                    # A reader never sees half a cache entry; existing entries are immutable.
                    try:
                        os.rename(staging, cached)
                        source_directory = cached
                    except OSError as error:
                        if (
                            error.errno not in {errno.EEXIST, errno.ENOTEMPTY, errno.EACCES}
                            or not cached.is_dir()
                        ):
                            raise
                        # Another process won, or a corrupt entry exists. Do not overwrite it.
                        source_directory = staging
                artifacts = []
                for name in STEM_FILES:
                    context.checkpoint()
                    target = resolve_artifact(context.root, f"{context.output_prefix}/{name}")
                    digest = copy_checked(source_directory / name, target, context)
                    if digest != manifest["sha256"][name]:
                        raise ServiceError(
                            "cache_integrity", "Stem cache changed during copying", 500
                        )
                    artifacts.append(f"{context.output_prefix}/{name}")
                context.report(0.95, "publishing_stems")
                artifacts.append(
                    context.write_json(
                        "stems.json",
                        {
                            "format_version": 1,
                            "analysis_version": PIPELINE_VERSION,
                            "model": model,
                            "options": options,
                            "input_sha256": input_hash,
                            "cache_key": key,
                            "cache_hit": hit,
                            **manifest["audio"],
                            "files": dict(zip(("vocals", "accompaniment"), artifacts, strict=True)),
                            "output_sha256": manifest["sha256"],
                        },
                    )
                )
                context.checkpoint()
                return artifacts
        except Exception:
            # Direct CLI callers receive the same no-partial-output guarantee as JobManager.
            shutil.rmtree(output, ignore_errors=True)
            raise
        finally:
            backend.close()
