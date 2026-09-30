"""Truthful structured-model provider boundary for SoloScale product features."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypeVar, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError

from soloscale.evidence_agent import (
    OllamaCallProfile,
    OllamaReasoner,
    Reasoner,
    ReasonerInvalidResponseError,
    ReasonerTimeoutError,
    ReasonerTransportError,
)
from soloscale.models import ContractModel

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)
_OLLAMA_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_HOSTED_MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}/[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_HOSTED_ENDPOINT = "https://ai-gateway.vercel.sh/v1/chat/completions"
_HOSTED_DEFAULT_MODEL = "zai/glm-5.2"
_HOSTED_REQUEST_TIMEOUT_SECONDS = 105
_HOSTED_MAX_RETRIES = 0
_HOSTED_MAX_OUTPUT_TOKENS = 8_192
_DEEPSEEK_MAX_OUTPUT_TOKENS = 16_384
_HOSTED_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_HOSTED_MAX_ERROR_BYTES = 64 * 1024
_TRANSIENT_HTTP_STATUS = {408, 409, 429, 500, 502, 503, 504}
_SAFE_GATEWAY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_OPENAI_COMPATIBLE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_RESPONSES_URL = f"{DEEPSEEK_BASE_URL}/responses"
DEEPSEEK_MODELS_URL = f"{DEEPSEEK_BASE_URL}/models"
DEEPSEEK_MODEL_IDS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEEPSEEK_REASONING_EFFORTS = ("none", "low", "high", "max")


class ModelProviderId(StrEnum):
    """Stable product-facing provider identities."""

    SOLOSCALE_HOSTED = "soloscale_hosted"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    DEEPSEEK = "deepseek"


@dataclass(frozen=True)
class ModelProviderCatalogEntry:
    """One product-facing provider identity and its stable settings route."""

    provider: ModelProviderId
    display_name: str
    settings_path: str
    default_model: str


MODEL_PROVIDER_CATALOG = (
    ModelProviderCatalogEntry(
        provider=ModelProviderId.OLLAMA,
        display_name="Local AI",
        settings_path="/settings/ai/local",
        default_model="qwen3:8b",
    ),
    ModelProviderCatalogEntry(
        provider=ModelProviderId.SOLOSCALE_HOSTED,
        display_name="SoloScale Hosted AI",
        settings_path="/settings/ai/hosted",
        default_model=_HOSTED_DEFAULT_MODEL,
    ),
    ModelProviderCatalogEntry(
        provider=ModelProviderId.OPENAI_COMPATIBLE,
        display_name="OpenAI API",
        settings_path="/settings/ai/openai",
        default_model="gpt-5",
    ),
    ModelProviderCatalogEntry(
        provider=ModelProviderId.DEEPSEEK,
        display_name="DeepSeek",
        settings_path="/settings/ai/deepseek",
        default_model=DEEPSEEK_MODEL_IDS[0],
    ),
)


def model_provider_catalog_entry(
    provider: ModelProviderId | str,
) -> ModelProviderCatalogEntry:
    selected = ModelProviderId(provider)
    for entry in MODEL_PROVIDER_CATALOG:
        if entry.provider is selected:
            return entry
    raise AssertionError(f"provider catalog is incomplete for {selected.value}")


class GatewayConfigurationState(StrEnum):
    CONFIGURED = "configured"
    NOT_CONFIGURED = "not_configured"


class GatewayTransportScope(StrEnum):
    LOOPBACK = "loopback"
    EXTERNAL = "external"


class GatewayErrorCategory(StrEnum):
    AUTH = "gateway_auth_error"
    MODEL = "gateway_model_error"
    SCHEMA = "gateway_schema_error"
    PROVIDER = "gateway_provider_error"
    RATE_LIMIT = "gateway_rate_limit"
    TIMEOUT = "gateway_timeout"
    UPSTREAM = "gateway_upstream_error"


class GatewayFailureDetails(BaseModel):
    """Non-content diagnostics safe for runtime logs and public correlation."""

    correlation_id: str = Field(pattern=r"^gateway-[a-f0-9]{24}$")
    model: str
    category: GatewayErrorCategory
    upstream_http_status: int | None = Field(default=None, ge=100, le=599)
    gateway_error_type: str | None = Field(default=None, max_length=200)
    gateway_error_code: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=200)
    duration_ms: int = Field(ge=0)
    retryable: bool


class GatewayDescriptor(ContractModel):
    """Non-secret provider metadata safe to show in settings and receipts."""

    provider: ModelProviderId
    display_name: str = Field(min_length=1, max_length=120)
    configuration_state: GatewayConfigurationState
    transport_scope: GatewayTransportScope
    model: str | None = Field(default=None, min_length=1, max_length=240)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)


class ModelCallProfile(ContractModel):
    """Non-content model performance metadata safe for local run receipts."""

    provider: ModelProviderId
    model: str
    system_chars: int = Field(ge=0)
    user_chars: int = Field(ge=0)
    schema_chars: int = Field(ge=0)
    max_output_tokens: int = Field(ge=1)
    requested_context_tokens: int | None = Field(default=None, ge=1)
    thinking_enabled: bool
    reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    prompt_eval_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    wall_ms: int = Field(ge=0)
    total_duration_ms: int | None = Field(default=None, ge=0)
    load_duration_ms: int | None = Field(default=None, ge=0)
    prompt_eval_duration_ms: int | None = Field(default=None, ge=0)
    eval_duration_ms: int | None = Field(default=None, ge=0)
    done_reason: str | None = Field(default=None, max_length=80)
    response_chars: int = Field(ge=0)
    thinking_chars: int = Field(ge=0)


class ModelGatewayError(RuntimeError):
    """Base class for sanitized model-gateway failures."""

    def __init__(
        self,
        message: str,
        *,
        details: GatewayFailureDetails | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details


class ModelGatewayNotConfigured(ModelGatewayError):
    """The selected provider has no usable configuration in this build."""


class ModelGatewayTransportError(ModelGatewayError):
    """The configured provider could not be reached safely."""


class ModelGatewayTimeoutError(ModelGatewayTransportError):
    """The configured provider exceeded its processing deadline."""


class ModelGatewayInvalidResponse(ModelGatewayError):
    """The provider returned output outside the required schema."""


class ModelGateway(Protocol):
    """Minimal structured-output dependency used by product features."""

    descriptor: GatewayDescriptor

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT: ...


class HostedGatewayPrivacyOptions(ContractModel):
    """Mandatory privacy options for every future hosted request."""

    zero_data_retention: Literal[True] = True
    disallow_prompt_training: Literal[True] = True
    provider_allowlist: list[str] = Field(min_length=1, max_length=8)


class HostedGatewayRequest(ContractModel):
    """Ephemeral structured request passed to a real or injected hosted transport."""

    correlation_id: str = Field(pattern=r"^gateway-[a-f0-9]{24}$")
    endpoint: str = Field(pattern=r"^https://ai-gateway\.vercel\.sh/v1/chat/completions$")
    model: str
    system: str
    user: str
    response_json_schema: dict[str, object]
    privacy: HostedGatewayPrivacyOptions
    reasoning_effort: Literal["none", "low"] = "low"
    max_output_tokens: int = Field(default=_HOSTED_MAX_OUTPUT_TOKENS, ge=1, le=128_000)
    timeout_seconds: int = Field(ge=1, le=120)
    max_retries: int = Field(ge=0, le=2)


class HostedGatewayTransport(Protocol):
    """Narrow transport seam for the hosted structured-output request."""

    def send(self, request: HostedGatewayRequest) -> str: ...


class OpenAICompatibleGatewayRequest(ContractModel):
    """Ephemeral request for an explicitly configured OpenAI-compatible API."""

    correlation_id: str = Field(pattern=r"^gateway-[a-f0-9]{24}$")
    endpoint: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=240)
    system: str
    user: str
    response_json_schema: dict[str, object]
    reasoning_effort: Literal["none", "low"] = "low"
    max_output_tokens: int = Field(default=_HOSTED_MAX_OUTPUT_TOKENS, ge=1, le=128_000)
    timeout_seconds: int = Field(ge=1, le=120)
    max_retries: int = Field(ge=0, le=2)


class OpenAICompatibleGatewayTransport(Protocol):
    """Narrow transport seam for a user-supplied compatible endpoint."""

    def send(self, request: OpenAICompatibleGatewayRequest) -> str: ...


class DeepSeekResponsesRequest(ContractModel):
    """Ephemeral structured-output request for DeepSeek's Responses API."""

    correlation_id: str = Field(pattern=r"^gateway-[a-f0-9]{24}$")
    endpoint: Literal["https://api.deepseek.com/responses"] = (
        "https://api.deepseek.com/responses"
    )
    model: Literal["deepseek-v4-flash", "deepseek-v4-pro"]
    system: str
    user: str
    response_json_schema: dict[str, object]
    reasoning_effort: Literal["none", "low", "high", "max"] = "low"
    max_output_tokens: int = Field(
        default=_DEEPSEEK_MAX_OUTPUT_TOKENS, ge=1, le=64_000
    )
    timeout_seconds: int = Field(ge=1, le=120)


class DeepSeekResponsesTransport(Protocol):
    """Narrow transport seam for DeepSeek Responses requests."""

    def send(self, request: DeepSeekResponsesRequest) -> str: ...


def _external_model_call_profile(
    *,
    provider: ModelProviderId,
    model: str,
    system: str,
    user: str,
    response_json_schema: dict[str, object],
    max_output_tokens: int,
    reasoning_effort: Literal["none", "low", "high", "max"],
    wall_ms: int,
    response_chars: int,
) -> ModelCallProfile:
    """Capture available metadata without inventing provider token usage."""

    return ModelCallProfile(
        provider=provider,
        model=model,
        system_chars=len(system),
        user_chars=len(user),
        schema_chars=len(
            json.dumps(
                response_json_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        max_output_tokens=max_output_tokens,
        thinking_enabled=reasoning_effort != "none",
        reasoning_effort=reasoning_effort,
        prompt_eval_tokens=None,
        output_tokens=None,
        wall_ms=wall_ms,
        response_chars=response_chars,
        thinking_chars=0,
    )


class MockHostedGatewayTransport:
    """In-memory scripted transport for focused tests and local contract demos."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[HostedGatewayRequest] = []

    def send(self, request: HostedGatewayRequest) -> str:
        self.requests.append(request)
        return self.response


def _safe_gateway_value(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    selected = str(value).strip()
    return selected if _SAFE_GATEWAY_VALUE.fullmatch(selected) else None


def _deepseek_structured_json(response: str) -> object:
    """Decode raw JSON or one complete provider-added JSON code fence."""

    candidate = response.strip().lstrip("\ufeff")
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().casefold() in {"```", "```json"}
            and lines[-1].strip() == "```"
        ):
            candidate = "\n".join(lines[1:-1]).strip()
    return json.loads(candidate)


def _first_safe_gateway_value(
    scopes: list[Mapping[str, object]],
    names: tuple[str, ...],
) -> str | None:
    for scope in scopes:
        for name in names:
            selected = _safe_gateway_value(scope.get(name))
            if selected is not None:
                return selected
    return None


def _first_gateway_message(scopes: list[Mapping[str, object]]) -> str | None:
    for scope in scopes:
        for name in ("message", "error_description", "error"):
            value = scope.get(name)
            if isinstance(value, str):
                return value[:500]
    return None


def _gateway_error_category(
    *,
    status: int | None,
    error_type: str | None,
    error_code: str | None,
    message: str | None = None,
    timed_out: bool = False,
) -> GatewayErrorCategory:
    signal = " ".join(
        value.casefold()
        for value in (error_type, error_code, message)
        if isinstance(value, str)
    )
    if timed_out or status == 408 or "timeout" in signal:
        return GatewayErrorCategory.TIMEOUT
    if status in {401, 403} or any(
        marker in signal
        for marker in ("auth", "api_key", "unauthorized", "forbidden")
    ):
        return GatewayErrorCategory.AUTH
    if status == 429 or "rate_limit" in signal:
        return GatewayErrorCategory.RATE_LIMIT
    if any(
        marker in signal
        for marker in ("schema", "response_format", "structured_output", "json_schema")
    ):
        return GatewayErrorCategory.SCHEMA
    if "model" in signal:
        return GatewayErrorCategory.MODEL
    if "provider" in signal:
        return GatewayErrorCategory.PROVIDER
    return GatewayErrorCategory.UPSTREAM


def _log_gateway_failure(details: GatewayFailureDetails) -> None:
    print(
        json.dumps(
            {
                "schema_version": "1.0",
                "event_type": "gateway_request_failed",
                **details.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


class VercelAIGatewayHTTPTransport:
    """Bounded OpenAI-compatible transport for Vercel AI Gateway."""

    def __init__(self, credential: str) -> None:
        selected = credential.strip()
        if not selected:
            raise ValueError("hosted gateway credential must not be empty")
        self._credential = selected

    @staticmethod
    def _schema_name(schema: dict[str, object]) -> str:
        raw = str(schema.get("title", "soloscale_response"))
        normalized = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")
        return (normalized or "soloscale_response")[:64]

    @staticmethod
    def _extract_content(raw: bytes) -> str:
        try:
            envelope = json.loads(raw.decode("utf-8"))
            choices = envelope["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise ModelGatewayInvalidResponse(
                "hosted model returned an invalid response envelope"
            ) from None
        if not isinstance(content, str) or not content.strip():
            raise ModelGatewayInvalidResponse(
                "hosted model returned an empty structured response"
            )
        return content

    def _send_once(self, request: HostedGatewayRequest) -> str:
        body = json.dumps(
            {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "stream": False,
                "reasoning": {"effort": request.reasoning_effort},
                "max_tokens": request.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": self._schema_name(request.response_json_schema),
                        "strict": True,
                        "schema": request.response_json_schema,
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        outbound = urllib.request.Request(
            request.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._credential}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "SoloScale-Resume-MVP/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(outbound, timeout=request.timeout_seconds) as response:
            raw = response.read(_HOSTED_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _HOSTED_MAX_RESPONSE_BYTES:
            raise ModelGatewayInvalidResponse("hosted model response exceeded the size limit")
        return self._extract_content(raw)

    @staticmethod
    def _error_scopes(raw: bytes) -> list[Mapping[str, object]]:
        if len(raw) > _HOSTED_MAX_ERROR_BYTES:
            return []
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(decoded, dict):
            return []
        scopes: list[Mapping[str, object]] = []
        nested = decoded.get("error")
        if isinstance(nested, dict):
            scopes.append(cast("Mapping[str, object]", nested))
        scopes.append(cast("Mapping[str, object]", decoded))
        return scopes

    @classmethod
    def _http_failure_details(
        cls,
        *,
        request: HostedGatewayRequest,
        failure: urllib.error.HTTPError,
        started: float,
        retryable: bool,
    ) -> GatewayFailureDetails:
        try:
            raw = failure.read(_HOSTED_MAX_ERROR_BYTES + 1)
        except OSError:
            raw = b""
        scopes = cls._error_scopes(raw)
        error_type = _first_safe_gateway_value(
            scopes, ("type", "error_type", "errorType")
        )
        error_code = _first_safe_gateway_value(
            scopes, ("code", "error_code", "errorCode", "error")
        )
        request_id = _first_safe_gateway_value(
            scopes,
            ("request_id", "requestId", "generation_id", "generationId", "id"),
        )
        provider = _first_safe_gateway_value(scopes, ("provider", "provider_id"))
        if request_id is None and failure.headers is not None:
            request_id = _first_safe_gateway_value(
                [cast("Mapping[str, object]", failure.headers)],
                ("x-request-id", "x-vercel-id", "x-ai-gateway-request-id"),
            )
        message = _first_gateway_message(scopes)
        return GatewayFailureDetails(
            correlation_id=request.correlation_id,
            model=request.model,
            category=_gateway_error_category(
                status=failure.code,
                error_type=error_type,
                error_code=error_code,
                message=message,
            ),
            upstream_http_status=failure.code,
            gateway_error_type=error_type,
            gateway_error_code=error_code,
            request_id=request_id,
            provider=provider or request.model.split("/", maxsplit=1)[0],
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            retryable=retryable,
        )

    def send(self, request: HostedGatewayRequest) -> str:
        started = time.monotonic()
        attempts = request.max_retries + 1
        for attempt in range(attempts):
            try:
                return self._send_once(request)
            except ModelGatewayInvalidResponse as exc:
                details = GatewayFailureDetails(
                    correlation_id=request.correlation_id,
                    model=request.model,
                    category=GatewayErrorCategory.SCHEMA,
                    upstream_http_status=200,
                    gateway_error_type="invalid_response_envelope",
                    gateway_error_code="response_envelope_validation_failed",
                    provider=request.model.split("/", maxsplit=1)[0],
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    retryable=False,
                )
                _log_gateway_failure(details)
                raise ModelGatewayInvalidResponse(str(exc), details=details) from None
            except ModelGatewayError:
                raise
            except urllib.error.HTTPError as exc:
                retryable = exc.code in _TRANSIENT_HTTP_STATUS
                details = self._http_failure_details(
                    request=request,
                    failure=exc,
                    started=started,
                    retryable=retryable,
                )
                exc.close()
                if retryable and attempt + 1 < attempts:
                    continue
                _log_gateway_failure(details)
                raise ModelGatewayTransportError(
                    "hosted model request failed", details=details
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                timed_out = isinstance(exc, TimeoutError) or isinstance(
                    getattr(exc, "reason", None), TimeoutError
                )
                if attempt + 1 < attempts:
                    continue
                category = _gateway_error_category(
                    status=None,
                    error_type=None,
                    error_code=None,
                    timed_out=timed_out,
                )
                details = GatewayFailureDetails(
                    correlation_id=request.correlation_id,
                    model=request.model,
                    category=category,
                    gateway_error_type=(
                        "timeout" if timed_out else "transport_connection_error"
                    ),
                    gateway_error_code=(
                        "request_timeout" if timed_out else "connection_failed"
                    ),
                    provider=request.model.split("/", maxsplit=1)[0],
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    retryable=True,
                )
                _log_gateway_failure(details)
                raise ModelGatewayTransportError(
                    "hosted model request failed", details=details
                ) from None
        raise AssertionError("hosted gateway retry loop exhausted without a result")


class OpenAICompatibleHTTPTransport:
    """OpenAI Chat Completions transport without hosted-provider routing fields."""

    def __init__(self, credential: str) -> None:
        selected = credential.strip()
        if not selected:
            raise ValueError("custom gateway credential must not be empty")
        self._credential = selected

    _schema_name = staticmethod(VercelAIGatewayHTTPTransport._schema_name)
    _extract_content = staticmethod(VercelAIGatewayHTTPTransport._extract_content)

    @staticmethod
    def _provider_schema(schema: dict[str, object]) -> dict[str, object]:
        """Project the domain schema onto OpenAI's strict supported subset."""

        def project(value: object) -> object:
            if isinstance(value, list):
                return [project(item) for item in value]
            if not isinstance(value, dict):
                return value
            projected = {
                key: project(item)
                for key, item in value.items()
                if key != "uniqueItems"
            }
            properties = projected.get("properties")
            if isinstance(properties, dict):
                projected["required"] = list(properties)
                projected["additionalProperties"] = False
            return projected

        projected = project(schema)
        if not isinstance(projected, dict):
            raise TypeError("OpenAI provider schema must remain a JSON object")
        return projected

    def _send_once(self, request: OpenAICompatibleGatewayRequest) -> str:
        provider_schema = self._provider_schema(request.response_json_schema)
        body = json.dumps(
            {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "stream": False,
                "reasoning_effort": request.reasoning_effort,
                "max_completion_tokens": request.max_output_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": self._schema_name(request.response_json_schema),
                        "strict": True,
                        "schema": provider_schema,
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        outbound = urllib.request.Request(
            request.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._credential}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "SoloScale-Resume-MVP/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(outbound, timeout=request.timeout_seconds) as response:
            raw = response.read(_HOSTED_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _HOSTED_MAX_RESPONSE_BYTES:
            raise ModelGatewayInvalidResponse("custom model response exceeded the size limit")
        return self._extract_content(raw)

    def send(self, request: OpenAICompatibleGatewayRequest) -> str:
        return self._send_once(request)


def _validated_openai_compatible_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    selected = endpoint.strip()
    if not selected:
        return None
    parsed = urlsplit(selected)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OpenAI-compatible endpoint must be an HTTPS URL without credentials")
    return selected


class OpenAICompatibleModelGateway:
    """Explicit, in-memory OpenAI-compatible structured-output gateway."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        transport: OpenAICompatibleGatewayTransport,
    ) -> None:
        self._endpoint = endpoint
        self._model = model
        self._transport = transport
        self.last_call_profile: ModelCallProfile | None = None
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.OPENAI_COMPATIBLE,
            display_name="OpenAI-compatible provider",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.EXTERNAL,
            model=model,
            base_url=endpoint,
        )

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        self.last_call_profile = None
        correlation_id = f"gateway-{secrets.token_hex(12)}"
        started = time.monotonic()
        request = OpenAICompatibleGatewayRequest(
            correlation_id=correlation_id,
            endpoint=self._endpoint,
            model=self._model,
            system=system,
            user=user,
            response_json_schema=schema.model_json_schema(),
            reasoning_effort=reasoning_effort,
            timeout_seconds=_HOSTED_REQUEST_TIMEOUT_SECONDS,
            max_retries=_HOSTED_MAX_RETRIES,
        )
        try:
            response = self._transport.send(request)
        except ModelGatewayError:
            self.last_call_profile = _external_model_call_profile(
                provider=ModelProviderId.OPENAI_COMPATIBLE,
                model=self._model,
                system=system,
                user=user,
                response_json_schema=request.response_json_schema,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=reasoning_effort,
                wall_ms=max(0, int((time.monotonic() - started) * 1000)),
                response_chars=0,
            )
            raise
        except Exception:
            wall_ms = max(0, int((time.monotonic() - started) * 1000))
            self.last_call_profile = _external_model_call_profile(
                provider=ModelProviderId.OPENAI_COMPATIBLE,
                model=self._model,
                system=system,
                user=user,
                response_json_schema=request.response_json_schema,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=reasoning_effort,
                wall_ms=wall_ms,
                response_chars=0,
            )
            details = GatewayFailureDetails(
                correlation_id=correlation_id,
                model=self._model,
                category=GatewayErrorCategory.UPSTREAM,
                gateway_error_type="transport_exception",
                gateway_error_code="transport_failed",
                duration_ms=wall_ms,
                retryable=False,
            )
            _log_gateway_failure(details)
            raise ModelGatewayTransportError(
                "custom model request failed", details=details
            ) from None
        wall_ms = max(0, int((time.monotonic() - started) * 1000))
        self.last_call_profile = _external_model_call_profile(
            provider=ModelProviderId.OPENAI_COMPATIBLE,
            model=self._model,
            system=system,
            user=user,
            response_json_schema=request.response_json_schema,
            max_output_tokens=request.max_output_tokens,
            reasoning_effort=reasoning_effort,
            wall_ms=wall_ms,
            response_chars=len(response),
        )
        try:
            decoded = json.loads(response)
            return schema.model_validate(decoded)
        except (json.JSONDecodeError, TypeError, ValidationError):
            details = GatewayFailureDetails(
                correlation_id=correlation_id,
                model=self._model,
                category=GatewayErrorCategory.SCHEMA,
                upstream_http_status=200,
                gateway_error_type="invalid_structured_response",
                gateway_error_code="response_validation_failed",
                duration_ms=wall_ms,
                retryable=False,
            )
            _log_gateway_failure(details)
            raise ModelGatewayInvalidResponse(
                "custom model returned an invalid structured response",
                details=details,
            ) from None


class DeepSeekResponsesHTTPTransport:
    """Bounded DeepSeek Responses transport that never logs its credential."""

    def __init__(self, credential: str) -> None:
        selected = credential.strip()
        if not selected:
            raise ValueError("DeepSeek credential must not be empty")
        self._credential = selected

    @staticmethod
    def _provider_schema(schema: dict[str, object]) -> dict[str, object]:
        """Add explicit types required by DeepSeek's structured-output parser."""

        projected = OpenAICompatibleHTTPTransport._provider_schema(schema)
        definitions = projected.get("$defs")

        def resolved_schema(value: object) -> dict[str, object] | None:
            if not isinstance(value, dict):
                return None
            reference = value.get("$ref")
            if (
                isinstance(reference, str)
                and reference.startswith("#/$defs/")
                and isinstance(definitions, dict)
            ):
                target = definitions.get(reference.removeprefix("#/$defs/"))
                return target if isinstance(target, dict) else None
            return value

        def declared_types(value: object) -> list[str]:
            resolved = resolved_schema(value)
            if resolved is None:
                return []
            raw_type = resolved.get("type")
            if isinstance(raw_type, str):
                return [raw_type]
            if isinstance(raw_type, list) and all(
                isinstance(item, str) for item in raw_type
            ):
                return list(raw_type)
            return []

        def add_explicit_types(value: object) -> None:
            if isinstance(value, list):
                for item in value:
                    add_explicit_types(item)
                return
            if not isinstance(value, dict):
                return
            variants = value.get("anyOf")
            if "type" not in value and isinstance(variants, list):
                inferred = list(
                    dict.fromkeys(
                        item_type
                        for variant in variants
                        for item_type in declared_types(variant)
                    )
                )
                composite_types = [
                    item_type
                    for item_type in inferred
                    if item_type in {"object", "array"}
                ]
                composite_variants = [
                    resolved
                    for variant in variants
                    if (resolved := resolved_schema(variant)) is not None
                    and resolved.get("type") in {"object", "array"}
                ]
                if len(composite_variants) == 1 and set(inferred) <= {
                    composite_types[0],
                    "null",
                }:
                    preserved = {
                        key: item for key, item in value.items() if key != "anyOf"
                    }
                    value.clear()
                    value.update(deepcopy(composite_variants[0]))
                    value.update(preserved)
                elif inferred:
                    value["type"] = inferred[0] if len(inferred) == 1 else inferred
            for item in value.values():
                add_explicit_types(item)

        add_explicit_types(projected)
        return projected

    @staticmethod
    def _extract_content(raw: bytes) -> str:
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelGatewayInvalidResponse(
                "DeepSeek returned an invalid response envelope"
            ) from None
        if not isinstance(envelope, dict):
            raise ModelGatewayInvalidResponse(
                "DeepSeek returned an invalid response envelope"
            )
        direct = envelope.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        output = envelope.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        text = str(part["text"]).strip()
                        if text:
                            return text
        raise ModelGatewayInvalidResponse("DeepSeek returned an empty response")

    def send(self, request: DeepSeekResponsesRequest) -> str:
        provider_schema = self._provider_schema(request.response_json_schema)
        body = json.dumps(
            {
                "model": request.model,
                "input": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "stream": False,
                "reasoning": {"effort": request.reasoning_effort},
                "max_output_tokens": request.max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": VercelAIGatewayHTTPTransport._schema_name(
                            request.response_json_schema
                        ),
                        "strict": True,
                        "schema": provider_schema,
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        outbound = urllib.request.Request(
            request.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._credential}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "SoloScale/1.0",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                outbound, timeout=request.timeout_seconds
            ) as response:
                raw = response.read(_HOSTED_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            details = GatewayFailureDetails(
                correlation_id=request.correlation_id,
                model=request.model,
                category=_gateway_error_category(
                    status=exc.code,
                    error_type=None,
                    error_code=None,
                ),
                upstream_http_status=exc.code,
                gateway_error_type="deepseek_http_error",
                gateway_error_code=f"http_{exc.code}",
                provider=ModelProviderId.DEEPSEEK.value,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                retryable=exc.code in _TRANSIENT_HTTP_STATUS,
            )
            exc.close()
            _log_gateway_failure(details)
            raise ModelGatewayTransportError(
                "DeepSeek request failed", details=details
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError
            )
            details = GatewayFailureDetails(
                correlation_id=request.correlation_id,
                model=request.model,
                category=(
                    GatewayErrorCategory.TIMEOUT
                    if timed_out
                    else GatewayErrorCategory.UPSTREAM
                ),
                gateway_error_type=(
                    "timeout" if timed_out else "transport_connection_error"
                ),
                gateway_error_code=(
                    "request_timeout" if timed_out else "connection_failed"
                ),
                provider=ModelProviderId.DEEPSEEK.value,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                retryable=True,
            )
            _log_gateway_failure(details)
            raise ModelGatewayTransportError(
                "DeepSeek request failed", details=details
            ) from None
        if len(raw) > _HOSTED_MAX_RESPONSE_BYTES:
            raise ModelGatewayInvalidResponse(
                "DeepSeek response exceeded the size limit"
            )
        return self._extract_content(raw)


class DeepSeekModelGateway:
    """First-class DeepSeek adapter inside the canonical ModelGateway boundary."""

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: str,
        transport: DeepSeekResponsesTransport,
    ) -> None:
        if model not in DEEPSEEK_MODEL_IDS:
            raise ValueError("DeepSeek model is invalid")
        if reasoning_effort not in DEEPSEEK_REASONING_EFFORTS:
            raise ValueError("DeepSeek reasoning effort is invalid")
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._transport = transport
        self.last_call_profile: ModelCallProfile | None = None
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.DEEPSEEK,
            display_name="DeepSeek",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.EXTERNAL,
            model=model,
            base_url=DEEPSEEK_RESPONSES_URL,
        )

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        # "none" is a per-call safety override. Otherwise the tested provider
        # configuration is authoritative, including high/max.
        selected_effort = (
            "none" if reasoning_effort == "none" else self._reasoning_effort
        )
        correlation_id = f"gateway-{secrets.token_hex(12)}"
        started = time.monotonic()
        request = DeepSeekResponsesRequest(
            correlation_id=correlation_id,
            model=cast(
                'Literal["deepseek-v4-flash", "deepseek-v4-pro"]', self._model
            ),
            system=system,
            user=user,
            response_json_schema=schema.model_json_schema(),
            reasoning_effort=cast(
                'Literal["none", "low", "high", "max"]', selected_effort
            ),
            timeout_seconds=_HOSTED_REQUEST_TIMEOUT_SECONDS,
        )
        self.last_call_profile = None
        try:
            response = self._transport.send(request)
        except ModelGatewayError:
            self.last_call_profile = _external_model_call_profile(
                provider=ModelProviderId.DEEPSEEK,
                model=self._model,
                system=system,
                user=user,
                response_json_schema=request.response_json_schema,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=cast(
                    'Literal["none", "low", "high", "max"]', selected_effort
                ),
                wall_ms=max(0, int((time.monotonic() - started) * 1000)),
                response_chars=0,
            )
            raise
        except Exception:
            wall_ms = max(0, int((time.monotonic() - started) * 1000))
            self.last_call_profile = _external_model_call_profile(
                provider=ModelProviderId.DEEPSEEK,
                model=self._model,
                system=system,
                user=user,
                response_json_schema=request.response_json_schema,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=cast(
                    'Literal["none", "low", "high", "max"]', selected_effort
                ),
                wall_ms=wall_ms,
                response_chars=0,
            )
            details = GatewayFailureDetails(
                correlation_id=correlation_id,
                model=self._model,
                category=GatewayErrorCategory.UPSTREAM,
                gateway_error_type="transport_exception",
                gateway_error_code="transport_failed",
                provider=ModelProviderId.DEEPSEEK.value,
                duration_ms=wall_ms,
                retryable=False,
            )
            _log_gateway_failure(details)
            raise ModelGatewayTransportError(
                "DeepSeek request failed", details=details
            ) from None
        wall_ms = max(0, int((time.monotonic() - started) * 1000))
        self.last_call_profile = _external_model_call_profile(
            provider=ModelProviderId.DEEPSEEK,
            model=self._model,
            system=system,
            user=user,
            response_json_schema=request.response_json_schema,
            max_output_tokens=request.max_output_tokens,
            reasoning_effort=cast(
                'Literal["none", "low", "high", "max"]', selected_effort
            ),
            wall_ms=wall_ms,
            response_chars=len(response),
        )
        try:
            return schema.model_validate(_deepseek_structured_json(response))
        except (json.JSONDecodeError, TypeError, ValidationError):
            raise ModelGatewayInvalidResponse(
                "DeepSeek returned an invalid structured response"
            ) from None


class HostedGatewayRuntimeConfig(ContractModel):
    """Non-secret hosted configuration derived from environment variables."""

    enabled: bool
    endpoint: str = _HOSTED_ENDPOINT
    model: str = _HOSTED_DEFAULT_MODEL
    credential_source: str | None = None
    provider_allowlist: list[str] = Field(min_length=1, max_length=8)
    timeout_seconds: int = Field(
        default=_HOSTED_REQUEST_TIMEOUT_SECONDS,
        ge=1,
        le=120,
    )
    max_retries: int = Field(default=_HOSTED_MAX_RETRIES, ge=0, le=2)


def hosted_gateway_runtime_config(
    environment: Mapping[str, str] | None = None,
) -> HostedGatewayRuntimeConfig:
    """Read only configuration presence; credential values are never retained."""

    values = os.environ if environment is None else environment
    enabled = values.get("SOLOSCALE_HOSTED_GATEWAY_ENABLED", "false").strip().casefold()
    if enabled not in {"true", "false"}:
        raise ValueError("SOLOSCALE_HOSTED_GATEWAY_ENABLED must be true or false")
    model = values.get("RESUME_HOSTED_MODEL", _HOSTED_DEFAULT_MODEL).strip()
    if _HOSTED_MODEL.fullmatch(model) is None:
        raise ValueError("RESUME_HOSTED_MODEL is invalid")
    provider = model.split("/", maxsplit=1)[0]
    credential_source: str | None = None
    if values.get("AI_GATEWAY_API_KEY", "").strip():
        credential_source = "ai_gateway_api_key"
    elif values.get("VERCEL_OIDC_TOKEN", "").strip():
        credential_source = "vercel_oidc"
    return HostedGatewayRuntimeConfig(
        enabled=enabled == "true",
        model=model,
        credential_source=credential_source,
        provider_allowlist=[provider],
    )


class SoloScaleHostedModelGateway:
    """Hosted gateway with strict structured validation and no implicit fallback."""

    def __init__(
        self,
        *,
        config: HostedGatewayRuntimeConfig,
        transport: HostedGatewayTransport,
    ) -> None:
        if not config.enabled:
            raise ValueError("hosted gateway feature flag must be enabled")
        if config.credential_source is None:
            raise ValueError("hosted gateway credential is not configured")
        self._config = config
        self._transport = transport
        self.last_call_profile: ModelCallProfile | None = None
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.SOLOSCALE_HOSTED,
            display_name="SoloScale Hosted AI",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.EXTERNAL,
            model=config.model,
            base_url=config.endpoint,
        )

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        self.last_call_profile = None
        correlation_id = f"gateway-{secrets.token_hex(12)}"
        started = time.monotonic()
        request = HostedGatewayRequest(
            correlation_id=correlation_id,
            endpoint=self._config.endpoint,
            model=self._config.model,
            system=system,
            user=user,
            response_json_schema=schema.model_json_schema(),
            privacy=HostedGatewayPrivacyOptions(
                provider_allowlist=self._config.provider_allowlist
            ),
            reasoning_effort=reasoning_effort,
            timeout_seconds=self._config.timeout_seconds,
            max_retries=self._config.max_retries,
        )
        try:
            response = self._transport.send(request)
        except ModelGatewayError:
            self.last_call_profile = _external_model_call_profile(
                provider=ModelProviderId.SOLOSCALE_HOSTED,
                model=self._config.model,
                system=system,
                user=user,
                response_json_schema=request.response_json_schema,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=reasoning_effort,
                wall_ms=max(0, int((time.monotonic() - started) * 1000)),
                response_chars=0,
            )
            raise
        except Exception:
            wall_ms = max(0, int((time.monotonic() - started) * 1000))
            self.last_call_profile = _external_model_call_profile(
                provider=ModelProviderId.SOLOSCALE_HOSTED,
                model=self._config.model,
                system=system,
                user=user,
                response_json_schema=request.response_json_schema,
                max_output_tokens=request.max_output_tokens,
                reasoning_effort=reasoning_effort,
                wall_ms=wall_ms,
                response_chars=0,
            )
            details = GatewayFailureDetails(
                correlation_id=correlation_id,
                model=self._config.model,
                category=GatewayErrorCategory.UPSTREAM,
                gateway_error_type="transport_exception",
                gateway_error_code="transport_failed",
                provider=self._config.model.split("/", maxsplit=1)[0],
                duration_ms=wall_ms,
                retryable=False,
            )
            _log_gateway_failure(details)
            raise ModelGatewayTransportError(
                "hosted model request failed", details=details
            ) from None
        wall_ms = max(0, int((time.monotonic() - started) * 1000))
        self.last_call_profile = _external_model_call_profile(
            provider=ModelProviderId.SOLOSCALE_HOSTED,
            model=self._config.model,
            system=system,
            user=user,
            response_json_schema=request.response_json_schema,
            max_output_tokens=request.max_output_tokens,
            reasoning_effort=reasoning_effort,
            wall_ms=wall_ms,
            response_chars=len(response),
        )
        try:
            decoded = json.loads(response)
            return schema.model_validate(decoded)
        except (json.JSONDecodeError, TypeError, ValidationError):
            details = GatewayFailureDetails(
                correlation_id=correlation_id,
                model=self._config.model,
                category=GatewayErrorCategory.SCHEMA,
                upstream_http_status=200,
                gateway_error_type="invalid_structured_response",
                gateway_error_code="response_validation_failed",
                provider=self._config.model.split("/", maxsplit=1)[0],
                duration_ms=wall_ms,
                retryable=False,
            )
            _log_gateway_failure(details)
            raise ModelGatewayInvalidResponse(
                "hosted model returned an invalid structured response",
                details=details,
            ) from None


class UnconfiguredModelGateway:
    """Fail-closed gateway for product providers that are not connected yet."""

    def __init__(self, descriptor: GatewayDescriptor) -> None:
        if descriptor.configuration_state is not GatewayConfigurationState.NOT_CONFIGURED:
            raise ValueError("unconfigured gateway requires a not-configured descriptor")
        self.descriptor = descriptor

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        del schema, system, user, reasoning_effort
        raise ModelGatewayNotConfigured(
            f"{self.descriptor.display_name} is not configured in this build"
        )


class OllamaModelGateway:
    """Optional loopback-only adapter over the existing Ollama reasoner."""

    def __init__(
        self,
        *,
        model: str = "qwen3:8b",
        endpoint: str = "http://127.0.0.1:11434",
        reasoner: Reasoner | None = None,
        context_tokens: int | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        selected_model = model.strip()
        if _OLLAMA_MODEL.fullmatch(selected_model) is None:
            raise ValueError("Ollama model name is invalid")
        if timeout_seconds <= 0:
            raise ValueError("Ollama timeout must be positive")
        selected_reasoner = reasoner or OllamaReasoner(
            endpoint=endpoint,
            model=selected_model,
            timeout=timeout_seconds,
            max_tokens=4096,
            context_tokens=context_tokens,
        )
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local Ollama",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model=selected_model,
            base_url=endpoint,
        )
        self._reasoner = selected_reasoner
        self.last_call_profile: ModelCallProfile | None = None

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        del reasoning_effort
        self.last_call_profile = None
        try:
            result = self._reasoner.complete(schema, system=system, user=user)
            profile = getattr(self._reasoner, "last_call_profile", None)
            if isinstance(profile, OllamaCallProfile):
                self.last_call_profile = ModelCallProfile(
                    provider=ModelProviderId.OLLAMA,
                    reasoning_effort=(
                        "low" if profile.thinking_enabled else "none"
                    ),
                    **profile.model_dump(mode="python", exclude={"schema_version"}),
                )
            return result
        except ReasonerTimeoutError as exc:
            raise ModelGatewayTimeoutError(
                "local model processing deadline exceeded"
            ) from exc
        except ReasonerTransportError as exc:
            raise ModelGatewayTransportError("local model request failed") from exc
        except ReasonerInvalidResponseError as exc:
            raise ModelGatewayInvalidResponse(
                "local model returned an invalid structured response"
            ) from exc


def model_gateway_for(
    provider: ModelProviderId | str,
    *,
    model: str | None = None,
    reasoner: Reasoner | None = None,
    hosted_transport: HostedGatewayTransport | None = None,
    openai_api_key: str | None = None,
    openai_endpoint: str | None = None,
    openai_transport: OpenAICompatibleGatewayTransport | None = None,
    deepseek_api_key: str | None = None,
    deepseek_reasoning_effort: str = "low",
    deepseek_transport: DeepSeekResponsesTransport | None = None,
    ollama_endpoint: str = "http://127.0.0.1:11434",
    ollama_context_tokens: int | None = None,
    ollama_timeout_seconds: int | None = None,
    environment: Mapping[str, str] | None = None,
) -> ModelGateway:
    """Create one explicit provider adapter without implicit fallback."""

    selected = ModelProviderId(provider)
    if selected is ModelProviderId.SOLOSCALE_HOSTED:
        config = hosted_gateway_runtime_config(environment)
        values = os.environ if environment is None else environment
        credential = (
            values.get("AI_GATEWAY_API_KEY", "").strip()
            or values.get("VERCEL_OIDC_TOKEN", "").strip()
        )
        if config.enabled and config.credential_source is not None:
            transport = hosted_transport or VercelAIGatewayHTTPTransport(credential)
            return SoloScaleHostedModelGateway(config=config, transport=transport)
        return UnconfiguredModelGateway(
            GatewayDescriptor(
                provider=selected,
                display_name="SoloScale Hosted AI",
                configuration_state=GatewayConfigurationState.NOT_CONFIGURED,
                transport_scope=GatewayTransportScope.EXTERNAL,
                model=config.model,
                base_url=config.endpoint,
            )
        )
    if selected is ModelProviderId.OPENAI_COMPATIBLE:
        endpoint = _validated_openai_compatible_endpoint(openai_endpoint)
        credential = openai_api_key.strip() if openai_api_key is not None else ""
        selected_model = model.strip() if model is not None else ""
        if (
            credential
            and endpoint is not None
            and _OPENAI_COMPATIBLE_MODEL.fullmatch(selected_model) is not None
        ):
            openai_selected_transport: OpenAICompatibleGatewayTransport = (
                openai_transport or OpenAICompatibleHTTPTransport(credential)
            )
            return OpenAICompatibleModelGateway(
                endpoint=endpoint,
                model=selected_model,
                transport=openai_selected_transport,
            )
        return UnconfiguredModelGateway(
            GatewayDescriptor(
                provider=selected,
                display_name="OpenAI-compatible provider",
                configuration_state=GatewayConfigurationState.NOT_CONFIGURED,
                transport_scope=GatewayTransportScope.EXTERNAL,
                model=selected_model or None,
                base_url=endpoint,
            )
        )
    if selected is ModelProviderId.DEEPSEEK:
        selected_model = model or DEEPSEEK_MODEL_IDS[0]
        if selected_model not in DEEPSEEK_MODEL_IDS:
            raise ValueError("DeepSeek model is invalid")
        if deepseek_reasoning_effort not in DEEPSEEK_REASONING_EFFORTS:
            raise ValueError("DeepSeek reasoning effort is invalid")
        credential = (
            deepseek_api_key.strip() if deepseek_api_key is not None else ""
        )
        if credential:
            selected_transport = deepseek_transport or DeepSeekResponsesHTTPTransport(
                credential
            )
            return DeepSeekModelGateway(
                model=selected_model,
                reasoning_effort=deepseek_reasoning_effort,
                transport=selected_transport,
            )
        return UnconfiguredModelGateway(
            GatewayDescriptor(
                provider=selected,
                display_name="DeepSeek",
                configuration_state=GatewayConfigurationState.NOT_CONFIGURED,
                transport_scope=GatewayTransportScope.EXTERNAL,
                model=selected_model,
                base_url=DEEPSEEK_RESPONSES_URL,
            )
        )
    if selected is ModelProviderId.OLLAMA:
        return OllamaModelGateway(
            model=model or "qwen3:8b",
            endpoint=ollama_endpoint,
            reasoner=reasoner,
            context_tokens=ollama_context_tokens,
            timeout_seconds=(
                ollama_timeout_seconds if ollama_timeout_seconds is not None else 180
            ),
        )
    raise AssertionError(f"provider factory is incomplete for {selected.value}")
