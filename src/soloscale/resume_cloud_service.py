"""Bounded domain service for cloud tasks; log only task IDs and state."""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from soloscale.model_gateway import (
    GatewayConfigurationState,
    ModelGateway,
    ModelProviderId,
    model_gateway_for,
)
from soloscale.resume_api import ResumeInput as LocalResumeInput
from soloscale.resume_api import ResumeRequest as LocalResumeRequest
from soloscale.resume_api import _decode_resume, _prepare
from soloscale.resume_cloud_cache import (
    ResultCacheKey,
    gateway_cache_identity,
    generation_pipeline_fingerprint,
)
from soloscale.resume_cloud_models import CreateTaskRequest, TaskMode
from soloscale.resume_cloud_observability import Telemetry
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
    rate_limit_capacity: int = 20
    rate_limit_refill_per_second: float = 1.0
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    max_model_concurrency: int = 1

    def __post_init__(self) -> None:
        if (
            self.rate_limit_capacity < 1
            or not math.isfinite(self.rate_limit_refill_per_second)
            or self.rate_limit_refill_per_second <= 0
        ):
            raise ValueError("rate limit settings must be finite and positive")
        if self.max_model_concurrency < 1:
            raise ValueError("max_model_concurrency must be positive")

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
        capacity = int(os.environ.get("RESUME_CLOUD_RATE_LIMIT_CAPACITY", "20"))
        refill = float(os.environ.get("RESUME_CLOUD_RATE_LIMIT_REFILL_PER_SECOND", "1"))
        model_concurrency = int(os.environ.get("RESUME_CLOUD_MODEL_CONCURRENCY", "1"))
        if capacity < 1 or not math.isfinite(refill) or refill <= 0:
            raise RuntimeError("Resume cloud rate limit settings must be finite and positive")
        return cls(
            database_url=database_url,
            bearer_token=bearer_token,
            rate_limit_capacity=capacity,
            rate_limit_refill_per_second=refill,
            max_model_concurrency=model_concurrency,
        )


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


def prepare_cloud_input(payload: CreateTaskRequest) -> tuple[bytes, str, str, str, str]:
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
    return (
        template,
        jd,
        hashlib.sha256(semantic).hexdigest(),
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(jd.encode("utf-8")).hexdigest(),
    )


class ResumeCloudService:
    def __init__(
        self,
        repository: ResumeCloudRepository,
        settings: CloudSettings,
        pipeline_fingerprint: Callable[[], str] = generation_pipeline_fingerprint,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.pipeline_fingerprint = pipeline_fingerprint
        self.telemetry = telemetry or Telemetry()
        self._model_slots = threading.BoundedSemaphore(settings.max_model_concurrency)

    def create(
        self,
        payload: CreateTaskRequest,
        idempotency_key: str,
        client_id: str = "default",
        traceparent: str | None = None,
    ) -> tuple[CloudTask, bool]:
        template, jd, payload_hash, raw_resume_sha256, job_description_sha256 = prepare_cloud_input(
            payload
        )
        return self.repository.create_or_replay(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            resume_docx=template,
            job_description=jd,
            mode=payload.mode,
            approved=payload.approve_external_model_call,
            raw_resume_sha256=raw_resume_sha256,
            job_description_sha256=job_description_sha256,
            input_ttl_hours=self.settings.input_ttl_hours,
            client_id=client_id,
            rate_limit_capacity=self.settings.rate_limit_capacity,
            rate_limit_refill_per_second=self.settings.rate_limit_refill_per_second,
            rate_limit_now=self.settings.clock(),
            traceparent=traceparent,
        )

    def consume_client_rate_limit(self, client_id: str) -> tuple[bool, float]:
        return self.repository.consume_rate_limit(
            client_id,
            capacity=self.settings.rate_limit_capacity,
            refill_per_second=self.settings.rate_limit_refill_per_second,
            now=self.settings.clock(),
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

    def _cache_key(self, task: CloudTask, gateway: ModelGateway | None) -> ResultCacheKey | None:
        if (
            task.client_key is None
            or task.raw_resume_sha256 is None
            or task.job_description_sha256 is None
        ):
            return None
        provider, model, base_url_sha256 = gateway_cache_identity(gateway)
        return ResultCacheKey(
            client_key=task.client_key,
            raw_resume_sha256=task.raw_resume_sha256,
            job_description_sha256=task.job_description_sha256,
            mode=task.mode.value,
            provider=provider,
            model=model,
            base_url_sha256=base_url_sha256,
            pipeline_sha256=self.pipeline_fingerprint(),
        )

    def process_one(self, gateway: ModelGateway | None = None) -> CloudTask | None:
        # A worker process owns this semaphore. Do not claim a task if all pipeline
        # slots are occupied: it remains QUEUED for a later worker loop.
        if not self._model_slots.acquire(blocking=False):
            return None
        task: CloudTask | None = None
        execute_traceparent: str | None = None
        try:
            started = time.monotonic()
            task = self.repository.claim(self.settings.lease_seconds)
            if task is None:
                return None
            assert (
                task.resume_docx is not None
                and task.job_description is not None
                and task.worker_token is not None
            )
            with self.telemetry.span(
                "worker.claim",
                {
                    "task.id": task.id,
                    "duration.ms": max(0, int((time.monotonic() - started) * 1000)),
                },
                task.traceparent,
            ):
                with (
                    self.telemetry.span("worker.execute", {"task.id": task.id}),
                    self.telemetry.activate(),
                ):
                    execute_traceparent = self.telemetry.traceparent()
                    if task.mode is TaskMode.TEMPLATE:
                        cache_key = self._cache_key(task, None)
                        if cache_key is not None and self.repository.complete_from_cache(
                            task.id, task.worker_token, cache_key
                        ):
                            return self.repository.get(task.id)
                        output = tailor_resume_docx(task.resume_docx, task.job_description).content
                    else:
                        if not task.approve_external_model_call:
                            raise RuntimeError("MODEL_APPROVAL_REQUIRED")
                        selected_gateway = gateway or self.configured_gateway()
                        if selected_gateway is None:
                            raise RuntimeError("MODEL_NOT_CONFIGURED")
                        selected_gateway = self.telemetry.wrap_gateway(selected_gateway)
                        cache_key = self._cache_key(task, selected_gateway)
                        if cache_key is not None and self.repository.complete_from_cache(
                            task.id, task.worker_token, cache_key
                        ):
                            return self.repository.get(task.id)
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
                    completed = self.repository.complete(
                        task.id,
                        task.worker_token,
                        output,
                        self.settings.output_ttl_hours,
                        cache_key,
                    )
                    with self.telemetry.span(
                        "worker.terminal",
                        {"terminal.state": "SUCCEEDED" if completed else "FENCE_LOST"},
                    ):
                        pass
        except (ResumeTemplateError, RuntimeError):
            if task is not None and task.worker_token is not None:
                failed = self.repository.fail(task.id, task.worker_token, "PROCESSING_REJECTED")
                with self.telemetry.span(
                    "worker.terminal",
                    {"terminal.state": "FAILED" if failed else "FENCE_LOST"},
                    execute_traceparent,
                ):
                    pass
        finally:
            self._model_slots.release()
        return self.repository.get(task.id) if task is not None else None
