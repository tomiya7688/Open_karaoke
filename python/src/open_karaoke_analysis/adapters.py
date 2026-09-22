"""Trusted in-process adapters run off the HTTP event loop and cooperate with cancellation."""

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ModelInfo, Role, ServiceError


class AnalysisCancelled(Exception):
    pass


def resolve_artifact(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    """Reject traversal, Windows drives/ADS, and symlinks on every platform."""
    parts = relative.split("/")
    if not relative or any(p in {"", ".", ".."} for p in parts):
        raise ServiceError("invalid_path", "Artifact must be a normalized relative path")
    if any(c in relative for c in ("\\", ":", "\x00")):
        raise ServiceError("invalid_path", "Artifact path contains a forbidden character")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ServiceError("invalid_path", "Artifact symlinks are not permitted")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ServiceError("invalid_path", "Artifact path escapes the configured root")
    if must_exist and not path.is_file():
        raise ServiceError("input_not_found", "Input artifact is not a regular file", 404)
    return path


def atomic_json(path: Path, value: Any) -> None:
    """Publish complete JSON or leave the previous file unchanged."""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass
class AnalysisContext:
    root: Path
    output_prefix: str
    input_path: Path | None
    cancelled: Event
    report: Callable[[float, str], None]

    def checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise AnalysisCancelled

    def write_json(self, name: str, value: Any) -> str:
        self.checkpoint()
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", name):
            raise ServiceError("invalid_path", "Adapter output must be a simple JSON filename")
        relative = f"{self.output_prefix}/{name}"
        atomic_json(resolve_artifact(self.root, relative), value)
        self.checkpoint()
        return relative


class ModelAdapter(Protocol):
    info: ModelInfo

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]: ...

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]: ...


class MockOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    steps: int = Field(default=5, ge=1, le=100)
    delay_ms: int = Field(default=10, ge=0, le=100)
    fail: bool = False


class MockAdapter:
    """Infrastructure fixture only; never advertises ASR/stems/pitch capabilities."""

    info = ModelInfo(id="mock-v1", version="1.0.0", role=Role.MOCK, capabilities=["fixture"])

    def validate_options(self, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return MockOptions.model_validate(options).model_dump()
        except ValidationError as error:
            raise ServiceError("invalid_options", "Invalid mock options") from error

    def analyze(self, context: AnalysisContext, options: dict[str, Any]) -> list[str]:
        for step in range(1, options["steps"] + 1):
            context.cancelled.wait(options["delay_ms"] / 1000)
            context.checkpoint()
            context.report(step / options["steps"], f"mock_step_{step}")
        if options["fail"]:
            raise ServiceError("mock_failure", "Requested fixture failure", 500)
        return [
            context.write_json(
                "result.json", {"format_version": 1, "mock": True, "steps": options["steps"]}
            )
        ]


class AdapterRegistry:
    def __init__(self, adapters: list[ModelAdapter]):
        self.adapters: dict[str, ModelAdapter] = {}
        for adapter in adapters:
            if adapter.info.id in self.adapters:
                raise ValueError(f"Duplicate model ID: {adapter.info.id}")
            self.adapters[adapter.info.id] = adapter

    def select(self, role: Role, model_id: str | None) -> ModelAdapter:
        if model_id is not None:
            adapter = self.adapters.get(model_id)
            if adapter is None:
                raise ServiceError("model_not_found", "Requested model is not registered", 404)
            if adapter.info.role != role:
                raise ServiceError("model_role_mismatch", "Model does not support this operation")
            return adapter
        for adapter in self.adapters.values():
            if adapter.info.role == role:
                return adapter
        raise ServiceError("not_implemented", "No adapter is installed for this operation", 501)
