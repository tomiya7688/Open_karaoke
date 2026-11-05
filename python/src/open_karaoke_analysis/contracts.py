"""Versioned wire contracts; no model framework imports at service startup."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class Role(StrEnum):
    MOCK = "mock"
    STEMS = "stems"
    LYRICS = "lyrics"
    ALIGNMENT = "alignment"
    PITCH = "pitch"
    EVENTS = "events"
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


ModelSource = Literal["request", "selection", "default", "default_fallback"]


class Job(BaseModel):
    """
    {
      責務: [Job: 解析Jobのスナップショット。実際に動かしたモデルと、その決まり方も含む]
      フィールド: [
        model_id: 実際に実行した組み込みAdapterのID(パッケージ選択ならそのmanifestのbackend),
        model_source: request=リクエストの明示指定 / selection=保存済み選択 /
          default=選択なしの既定 / default_fallback=保存済み選択が使えず既定へ戻した,
        selected_model_id: 保存済み選択のID(selection・default_fallback時のみ。package IDあり),
        selection_fallback_reason: default_fallbackのときの短い理由。パスは含めない,
        model_plan: song Jobの受付時に確定した段別プラン(stage -> {source, selected_model_id,
          fallback_reason}と解決時刻resolved_at)。song以外はnull
      ]
    }
    """

    id: UUID
    instance_id: UUID
    kind: Role
    model_id: str
    model_source: ModelSource = "default"
    selected_model_id: str | None = None
    selection_fallback_reason: str | None = None
    model_plan: dict[str, Any] | None = None
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
