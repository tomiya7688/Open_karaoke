"""Versioned wire contracts; no model framework imports at service startup."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class Role(str, Enum):
    MOCK = "mock"
    STEMS = "stems"
    LYRICS = "lyrics"
    ALIGNMENT = "alignment"
    PITCH = "pitch"
    NOTES = "notes"
    SONG = "song"


class ModelInfo(BaseModel):
    id: str
    version: str
    role: Role
    capabilities: list[str] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_artifact: str | None = Field(default=None, max_length=1024)
    options: dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False


class Job(BaseModel):
    id: UUID
    instance_id: UUID
    kind: Role
    model_id: str
    status: str = "queued"
    progress: float = 0.0
    stage: str = "queued"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: ErrorDetail | None = None
    artifacts: list[str] = Field(default_factory=list)


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.detail = ErrorDetail(code=code, message=message, retryable=retryable)
