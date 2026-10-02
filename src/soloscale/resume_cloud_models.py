"""Public-safe contracts for the dedicated Resume cloud worker."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_JD_CHARS = 50_000
MAX_IDEMPOTENCY_KEY_CHARS = 200


class TaskState(StrEnum):
    PREPARED = "PREPARED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class TaskMode(StrEnum):
    TEMPLATE = "template"
    AI = "ai"


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeInput(RequestModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class CreateTaskRequest(RequestModel):
    resume: ResumeInput
    job_description: str = Field(min_length=1, max_length=MAX_JD_CHARS)
    mode: TaskMode = TaskMode.TEMPLATE
    approve_external_model_call: bool = False


class TaskView(RequestModel):
    id: str
    state: TaskState
    mode: TaskMode
    model_calls: int
    output_ready: bool
    failure_code: str | None = None
