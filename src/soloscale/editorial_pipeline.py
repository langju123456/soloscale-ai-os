"""Small, opt-in editorial helpers; no provider is contacted by default."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib import request

from pydantic import Field, model_validator

from soloscale.editorial_models import (
    EditorialProvenance,
    EditorialRole,
    ProviderIdentity,
    ProviderKind,
    RunStatus,
)
from soloscale.models import ContractModel


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_provenance(
    *,
    role: EditorialRole,
    provider: ProviderIdentity,
    prompt_version: str,
    input_artifacts: dict[str, str],
    output_artifacts: dict[str, str] | None = None,
    reasoning: str | None = None,
    network_used: bool = False,
    token_usage: dict[str, int] | None = None,
    cost_usd: float | None = None,
    errors: list[str] | None = None,
    fresh_context: bool = True,
) -> EditorialProvenance:
    """Build a receipt without retaining the potentially private prompt or output body."""

    now = datetime.now(UTC)
    failure = errors or []
    status = RunStatus.FAILED if failure else RunStatus.SUCCEEDED
    return EditorialProvenance(
        role=role,
        provider=provider,
        exact_model=provider.model or "UNKNOWN",
        reasoning=reasoning,
        prompt_version=prompt_version,
        input_artifact_hashes={name: sha256_text(value) for name, value in input_artifacts.items()},
        output_artifact_hashes={
            name: sha256_text(value) for name, value in (output_artifacts or {}).items()
        },
        started_at=now,
        completed_at=now,
        network_used=network_used,
        token_usage=token_usage,
        cost_usd=cost_usd,
        status=status,
        errors=failure,
        fresh_context=fresh_context,
    )


class HuggingFaceOpenAIConfig(ContractModel):
    """Explicit configuration for an optional OpenAI-compatible HF endpoint."""

    token: str | None = Field(default=None, min_length=1)
    base_url: str = "https://router.huggingface.co/v1"
    model: str = ""
    provider: str = "huggingface"
    allow_model_list_inspection: bool = False

    @model_validator(mode="after")
    def protect_provider_token(self) -> HuggingFaceOpenAIConfig:
        if not self.base_url.startswith("https://"):
            raise ValueError("Hugging Face provider URLs must use HTTPS")
        return self

    @classmethod
    def from_environment(cls) -> HuggingFaceOpenAIConfig:
        return cls(
            token=os.getenv("HF_TOKEN") or None,
            base_url=os.getenv("SOLOSCALE_HF_BASE_URL", "https://router.huggingface.co/v1"),
            model=os.getenv("SOLOSCALE_HF_MODEL", ""),
            provider=os.getenv("SOLOSCALE_HF_PROVIDER", "huggingface"),
            allow_model_list_inspection=(
                os.getenv("SOLOSCALE_HF_ALLOW_MODEL_LIST", "").lower() in {"1", "true", "yes"}
            ),
        )


class HuggingFaceOpenAIAdapter:
    """Lazy stdlib-only adapter. Calls occur only through explicit methods."""

    def __init__(self, config: HuggingFaceOpenAIConfig | None = None) -> None:
        self.config = config or HuggingFaceOpenAIConfig.from_environment()

    @property
    def configured(self) -> bool:
        return bool(self.config.token and self.config.model)

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind=ProviderKind.HUGGINGFACE,
            provider=self.config.provider,
            model=self.config.model or None,
            base_url=self.config.base_url,
        )

    def list_models(self) -> list[dict[str, Any]]:
        """Inspect models only when both token and inspection opt-in are configured."""

        if not self.config.token or not self.config.allow_model_list_inspection:
            return []
        response = self._json_request("GET", "/models")
        if isinstance(response, dict) and isinstance(response.get("data"), list):
            return [item for item in response["data"] if isinstance(item, dict)]
        return []

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Submit one explicit OpenAI-compatible chat request when configured."""

        if not self.configured:
            raise ValueError("Hugging Face editorial provider is not configured")
        payload = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        response = self._json_request("POST", "/chat/completions", payload)
        if not isinstance(response, dict):
            raise ValueError("Hugging Face returned an invalid chat-completion response")
        return response

    def _json_request(
        self, method: str, suffix: str, payload: dict[str, Any] | None = None
    ) -> object:
        if not self.config.token:
            raise ValueError("Hugging Face editorial provider is not configured")
        url = self.config.base_url.rstrip("/") + suffix
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url,
            method=method,
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(http_request, timeout=10) as response:  # noqa: S310 - explicit opt-in URL
            return cast(object, json.loads(response.read().decode("utf-8")))


class PrivateWriteError(ValueError):
    """Raised when a private artifact cannot be safely written once."""


def write_private_once(path: Path, content: str | bytes) -> str:
    """Create one 0600 artifact without following an existing file or overwriting it."""

    absolute_parent = path.parent.expanduser().absolute()
    if any(candidate.is_symlink() for candidate in (absolute_parent, *absolute_parent.parents)):
        raise PrivateWriteError("private artifact ancestry cannot contain a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise PrivateWriteError("private artifact already exists")
    data = content.encode("utf-8") if isinstance(content, str) else content
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PrivateWriteError("private artifact already exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return hashlib.sha256(data).hexdigest()
