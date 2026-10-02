"""Bounded domain service for cloud tasks; log only task IDs and state."""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass

from soloscale.model_gateway import (
    GatewayConfigurationState,
    ModelGateway,
    ModelProviderId,
    model_gateway_for,
)
from soloscale.resume_api import ResumeInput as LocalResumeInput
from soloscale.resume_api import ResumeRequest as LocalResumeRequest
from soloscale.resume_api import _decode_resume, _prepare
from soloscale.resume_cloud_models import CreateTaskRequest, TaskMode
from soloscale.resume_cloud_repository import CloudTask, ResumeCloudRepository
from soloscale.resume_docx import (
    ResumeTemplateError,
    tailor_resume_docx,
    tailor_resume_docx_with_gateway,
)


@dataclass(frozen=True)
class CloudSettings:
    database_url: str
    bearer_token: str
    input_ttl_hours: int = 24
    output_ttl_hours: int = 24
    lease_seconds: int = 300

    @classmethod
    def from_environment(cls, *, require_bearer: bool = True) -> CloudSettings:
        database_url = os.environ.get("RESUME_CLOUD_DATABASE_URL", "")
        bearer_token = os.environ.get("RESUME_CLOUD_BEARER_TOKEN", "")
        token_file = os.environ.get("RESUME_CLOUD_BEARER_TOKEN_FILE", "")
        if token_file and not bearer_token:
            with open(token_file, encoding="utf-8") as handle:
                bearer_token = handle.read().strip()
        if not database_url or (require_bearer and len(bearer_token) < 32):
            raise RuntimeError("RESUME_CLOUD_DATABASE_URL and a 32+ byte bearer token are required")
        return cls(database_url=database_url, bearer_token=bearer_token)


class _LeaseHeartbeat:
    def __init__(
        self, repository: ResumeCloudRepository, task: CloudTask, lease_seconds: int
    ) -> None:
        self.repository = repository
        self.task = task
        self.lease_seconds = lease_seconds
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _LeaseHeartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        assert self.task.worker_token is not None
        while not self.stop.wait(max(1, self.lease_seconds // 3)):
            if not self.repository.renew_lease(
                self.task.id, self.task.worker_token, self.lease_seconds
            ):
                self.lost.set()
                return


def prepare_cloud_input(payload: CreateTaskRequest) -> tuple[bytes, str, str]:
    """Use the canonical bounded parser and normalize non-DOCX uploads before storage."""
    local = LocalResumeRequest(
        resume=LocalResumeInput(
            filename=payload.resume.filename, content_base64=payload.resume.content_base64
        ),
        job_description=payload.job_description,
    )
    raw = _decode_resume(local.resume)
    _, template = _prepare(local)
    jd = payload.job_description.strip()
    fields = (
        raw,
        payload.resume.filename.encode("utf-8"),
        jd.encode("utf-8"),
        payload.mode.value.encode("ascii"),
        str(payload.approve_external_model_call).encode("ascii"),
    )
    semantic = b"".join(len(item).to_bytes(8, "big") + item for item in fields)
    return template, jd, hashlib.sha256(semantic).hexdigest()


class ResumeCloudService:
    def __init__(self, repository: ResumeCloudRepository, settings: CloudSettings) -> None:
        self.repository = repository
        self.settings = settings

    def create(self, payload: CreateTaskRequest, idempotency_key: str) -> tuple[CloudTask, bool]:
        template, jd, payload_hash = prepare_cloud_input(payload)
        return self.repository.create_or_replay(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            resume_docx=template,
            job_description=jd,
            mode=payload.mode,
            approved=payload.approve_external_model_call,
            input_ttl_hours=self.settings.input_ttl_hours,
        )

    @staticmethod
    def configured_gateway() -> ModelGateway | None:
        provider = os.environ.get("RESUME_CLOUD_MODEL_PROVIDER", "").strip()
        model = os.environ.get("RESUME_CLOUD_MODEL", "").strip()
        if not provider or not model:
            return None
        try:
            selected = ModelProviderId(provider)
        except ValueError:
            return None
        if selected not in {ModelProviderId.OPENAI_COMPATIBLE, ModelProviderId.DEEPSEEK}:
            return None
        gateway = model_gateway_for(
            selected,
            model=model,
            openai_api_key=os.environ.get("RESUME_CLOUD_OPENAI_API_KEY"),
            openai_endpoint=os.environ.get("RESUME_CLOUD_OPENAI_ENDPOINT"),
            deepseek_api_key=os.environ.get("RESUME_CLOUD_DEEPSEEK_API_KEY"),
        )
        if gateway.descriptor.configuration_state is not GatewayConfigurationState.CONFIGURED:
            return None
        return gateway

    def process_one(self, gateway: ModelGateway | None = None) -> CloudTask | None:
        task = self.repository.claim(self.settings.lease_seconds)
        if task is None:
            return None
        assert (
            task.resume_docx is not None
            and task.job_description is not None
            and task.worker_token is not None
        )
        try:
            if task.mode is TaskMode.TEMPLATE:
                output = tailor_resume_docx(task.resume_docx, task.job_description).content
            else:
                if not task.approve_external_model_call:
                    raise RuntimeError("MODEL_APPROVAL_REQUIRED")
                selected_gateway = gateway or self.configured_gateway()
                if selected_gateway is None:
                    raise RuntimeError("MODEL_NOT_CONFIGURED")
                if not self.repository.mark_model_call(task.id, task.worker_token):
                    raise RuntimeError("TASK_LEASE_LOST")
                with _LeaseHeartbeat(
                    self.repository, task, self.settings.lease_seconds
                ) as heartbeat:
                    output = tailor_resume_docx_with_gateway(
                        task.resume_docx, task.job_description, gateway=selected_gateway
                    ).content
                    if heartbeat.lost.is_set():
                        raise RuntimeError("TASK_LEASE_LOST")
            self.repository.complete(
                task.id, task.worker_token, output, self.settings.output_ttl_hours
            )
        except (ResumeTemplateError, RuntimeError):
            self.repository.fail(task.id, task.worker_token, "PROCESSING_REJECTED")
        return self.repository.get(task.id)
