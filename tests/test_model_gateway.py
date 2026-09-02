import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest
from pydantic import BaseModel, Field

from soloscale.evidence_agent import OllamaCallProfile
from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayErrorCategory,
    GatewayTransportScope,
    MockHostedGatewayTransport,
    ModelGatewayInvalidResponse,
    ModelGatewayNotConfigured,
    ModelGatewayTransportError,
    ModelProviderId,
    OllamaModelGateway,
    hosted_gateway_runtime_config,
    model_gateway_for,
)


class _Reply(BaseModel):
    value: str


class _StrictSchemaProbe(BaseModel):
    labels: list[str] = Field(json_schema_extra={"uniqueItems": True})
    optional_note: str | None = None


class _ScriptedReasoner:
    model = "test-model"
    last_call_profile: OllamaCallProfile | None = None

    def complete(
        self,
        schema: type[_Reply],
        *,
        system: str,
        user: str,
    ) -> _Reply:
        del system, user
        return schema(value="grounded")


def test_unconfigured_external_providers_fail_closed_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOLOSCALE_HOSTED_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    for provider in (
        ModelProviderId.SOLOSCALE_HOSTED,
        ModelProviderId.OPENAI_COMPATIBLE,
    ):
        gateway = model_gateway_for(provider)
        assert gateway.descriptor.configuration_state is GatewayConfigurationState.NOT_CONFIGURED
        assert gateway.descriptor.transport_scope is GatewayTransportScope.EXTERNAL
        with pytest.raises(ModelGatewayNotConfigured):
            gateway.complete(_Reply, system="system", user="user")


def test_optional_ollama_gateway_delegates_only_to_the_supplied_reasoner() -> None:
    reasoner = _ScriptedReasoner()
    reasoner.last_call_profile = OllamaCallProfile(
        model="qwen3:8b",
        system_chars=6,
        user_chars=4,
        schema_chars=100,
        max_output_tokens=2048,
        prompt_eval_tokens=10,
        output_tokens=5,
        wall_ms=12,
        response_chars=20,
        thinking_chars=0,
    )
    gateway = model_gateway_for(
        ModelProviderId.OLLAMA,
        model="qwen3:8b",
        reasoner=reasoner,  # type: ignore[arg-type]
    )
    assert isinstance(gateway, OllamaModelGateway)
    assert gateway.descriptor.provider is ModelProviderId.OLLAMA
    assert gateway.descriptor.configuration_state is GatewayConfigurationState.CONFIGURED
    assert gateway.descriptor.transport_scope is GatewayTransportScope.LOOPBACK
    assert gateway.complete(_Reply, system="system", user="user") == _Reply(
        value="grounded"
    )
    assert gateway.last_call_profile is not None
    assert gateway.last_call_profile.prompt_eval_tokens == 10
    assert gateway.last_call_profile.output_tokens == 5

    with pytest.raises(ValueError):
        model_gateway_for("unknown-provider")


def test_openai_compatible_gateway_requires_explicit_in_memory_configuration() -> None:
    unconfigured = model_gateway_for(
        ModelProviderId.OPENAI_COMPATIBLE,
        model="gpt-5-mini",
        openai_endpoint="https://api.openai.com/v1/chat/completions",
    )
    assert unconfigured.descriptor.configuration_state is GatewayConfigurationState.NOT_CONFIGURED
    assert unconfigured.descriptor.model == "gpt-5-mini"
    assert (
        unconfigured.descriptor.base_url == "https://api.openai.com/v1/chat/completions"
    )
    with pytest.raises(ModelGatewayNotConfigured):
        unconfigured.complete(_Reply, system="system", user="user")

    with pytest.raises(ValueError, match="HTTPS URL"):
        model_gateway_for(
            ModelProviderId.OPENAI_COMPATIBLE,
            model="gpt-5-mini",
            openai_api_key="in-memory-test-secret",
            openai_endpoint="http://127.0.0.1/v1/chat/completions",
        )


def test_openai_compatible_transport_posts_structured_schema_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    secret = "in-memory-test-secret"

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, maximum: int) -> bytes:
            captured["read_maximum"] = maximum
            return b'{"choices":[{"message":{"content":"{\\\"value\\\":\\\"grounded\\\"}"}}]}'

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> Response:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        assert isinstance(request.data, bytes)
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    gateway = model_gateway_for(
        ModelProviderId.OPENAI_COMPATIBLE,
        model="gpt-5-mini",
        openai_api_key=secret,
        openai_endpoint="https://api.openai.com/v1/chat/completions",
    )

    assert gateway.descriptor.configuration_state is GatewayConfigurationState.CONFIGURED
    assert gateway.complete(
        _Reply,
        system="grounded-system",
        user="synthetic-user",
        reasoning_effort="none",
    ) == _Reply(value="grounded")
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["authorization"] == f"Bearer {secret}"
    assert captured["timeout"] == 105
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "gpt-5-mini"
    assert body["messages"] == [
        {"role": "system", "content": "grounded-system"},
        {"role": "user", "content": "synthetic-user"},
    ]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["title"] == "_Reply"
    assert body["reasoning_effort"] == "none"
    assert body["max_completion_tokens"] == 8_192
    assert "max_tokens" not in body
    assert secret not in gateway.descriptor.model_dump_json()
    assert secret not in json.dumps(body)
    profile = getattr(gateway, "last_call_profile", None)
    assert profile is not None
    assert profile.provider is ModelProviderId.OPENAI_COMPATIBLE
    assert profile.prompt_eval_tokens is None
    assert profile.output_tokens is None
    assert profile.response_chars > 0
    assert profile.thinking_enabled is False


def test_openai_wire_schema_is_projected_without_weakening_domain_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, maximum: int) -> bytes:
            del maximum
            return (
                b'{"choices":[{"message":{"content":"{\\\"labels\\\":[\\\"one\\\"],'
                b'\\\"optional_note\\\":null}"}}]}'
            )

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> Response:
        del timeout
        assert isinstance(request.data, bytes)
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    gateway = model_gateway_for(
        ModelProviderId.OPENAI_COMPATIBLE,
        model="gpt-5-mini",
        openai_api_key="in-memory-test-secret",
        openai_endpoint="https://api.openai.com/v1/chat/completions",
    )
    canonical_schema = _StrictSchemaProbe.model_json_schema()

    result = gateway.complete(
        _StrictSchemaProbe,
        system="synthetic-system",
        user="synthetic-user",
    )

    assert result == _StrictSchemaProbe(labels=["one"], optional_note=None)
    assert canonical_schema["properties"]["labels"]["uniqueItems"] is True
    assert canonical_schema["required"] == ["labels"]
    body = captured["body"]
    assert isinstance(body, dict)
    wire_schema = body["response_format"]["json_schema"]["schema"]
    assert "uniqueItems" not in wire_schema["properties"]["labels"]
    assert wire_schema["required"] == ["labels", "optional_note"]
    assert wire_schema["additionalProperties"] is False


def test_openai_compatible_transport_failure_redacts_in_memory_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "in-memory-test-secret"

    def rejected_request(request: urllib.request.Request, timeout: int) -> None:
        del request, timeout
        raise RuntimeError(secret)

    monkeypatch.setattr("urllib.request.urlopen", rejected_request)
    gateway = model_gateway_for(
        ModelProviderId.OPENAI_COMPATIBLE,
        model="gpt-5-mini",
        openai_api_key=secret,
        openai_endpoint="https://api.openai.com/v1/chat/completions",
    )
    with pytest.raises(ModelGatewayTransportError) as failure:
        gateway.complete(_Reply, system="system", user="synthetic-user")

    assert secret not in str(failure.value)
    event_text = capsys.readouterr().out.strip()
    assert secret not in event_text


def test_hosted_gateway_is_feature_flagged_and_injected_transport_is_schema_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_calls = 0

    def forbidden_network(*args: object, **kwargs: object) -> None:
        nonlocal network_calls
        del args, kwargs
        network_calls += 1
        raise AssertionError("hosted mock validation must not access the network")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    disabled = {
        "SOLOSCALE_HOSTED_GATEWAY_ENABLED": "false",
        "AI_GATEWAY_API_KEY": "fake-test-secret",
    }
    scripted = MockHostedGatewayTransport('{"value":"grounded"}')
    unavailable = model_gateway_for(
        ModelProviderId.SOLOSCALE_HOSTED,
        hosted_transport=scripted,
        environment=disabled,
    )
    assert unavailable.descriptor.configuration_state is GatewayConfigurationState.NOT_CONFIGURED
    with pytest.raises(ModelGatewayNotConfigured):
        unavailable.complete(_Reply, system="system", user="user")
    assert scripted.requests == []

    enabled = dict(disabled, SOLOSCALE_HOSTED_GATEWAY_ENABLED="true")
    gateway = model_gateway_for(
        ModelProviderId.SOLOSCALE_HOSTED,
        hosted_transport=scripted,
        environment=enabled,
    )
    assert gateway.complete(_Reply, system="system", user="typed-payload") == _Reply(
        value="grounded"
    )
    profile = getattr(gateway, "last_call_profile", None)
    assert profile is not None
    assert profile.provider is ModelProviderId.SOLOSCALE_HOSTED
    assert profile.prompt_eval_tokens is None
    assert profile.output_tokens is None
    request = scripted.requests[0]
    assert request.endpoint == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert request.model == "zai/glm-5.2"
    assert request.privacy.zero_data_retention is True
    assert request.privacy.disallow_prompt_training is True
    assert request.privacy.provider_allowlist == ["zai"]
    assert request.max_retries == 0
    assert request.timeout_seconds == 105
    assert request.reasoning_effort == "low"
    assert request.max_output_tokens == 8_192
    serialized = request.model_dump_json()
    assert "fake-test-secret" not in serialized
    assert request.response_json_schema["title"] == "_Reply"

    assert gateway.complete(
        _Reply,
        system="system",
        user="typed-payload",
        reasoning_effort="none",
    ) == _Reply(value="grounded")
    assert scripted.requests[1].reasoning_effort == "none"

    malformed = model_gateway_for(
        ModelProviderId.SOLOSCALE_HOSTED,
        hosted_transport=MockHostedGatewayTransport('{"wrong":"shape"}'),
        environment=enabled,
    )
    with pytest.raises(ModelGatewayInvalidResponse) as failure:
        malformed.complete(_Reply, system="system", user="typed-payload")
    assert "fake-test-secret" not in str(failure.value)
    assert failure.value.__cause__ is None

    class SecretFailureTransport:
        def send(self, request: object) -> str:
            del request
            raise RuntimeError("fake-test-secret")

    failed = model_gateway_for(
        ModelProviderId.SOLOSCALE_HOSTED,
        hosted_transport=SecretFailureTransport(),
        environment=enabled,
    )
    with pytest.raises(ModelGatewayTransportError) as transport_failure:
        failed.complete(_Reply, system="system", user="typed-payload")
    assert "fake-test-secret" not in str(transport_failure.value)
    assert transport_failure.value.__cause__ is None

    config = hosted_gateway_runtime_config(enabled)
    assert config.credential_source == "ai_gateway_api_key"
    assert "fake-test-secret" not in config.model_dump_json()
    assert network_calls == 0


def test_real_hosted_transport_uses_vercel_gateway_and_api_key_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self, maximum: int) -> bytes:
            captured["read_maximum"] = maximum
            return json.dumps(
                {"choices": [{"message": {"content": '{"value":"live"}'}}]}
            ).encode()

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> Response:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        assert isinstance(request.data, bytes)
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    environment = {
        "SOLOSCALE_HOSTED_GATEWAY_ENABLED": "true",
        "AI_GATEWAY_API_KEY": "preferred-api-key",
        "VERCEL_OIDC_TOKEN": "secondary-oidc-token",
        "RESUME_HOSTED_MODEL": "zai/glm-5.2",
    }
    gateway = model_gateway_for(ModelProviderId.SOLOSCALE_HOSTED, environment=environment)
    assert gateway.complete(_Reply, system="grounded-system", user="synthetic-user") == _Reply(
        value="live"
    )
    assert captured["url"] == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert captured["authorization"] == "Bearer preferred-api-key"
    assert captured["timeout"] == 105
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "zai/glm-5.2"
    assert body["stream"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_tokens"] == 8_192
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "providerOptions" not in body
    assert "zeroDataRetention" not in body
    assert "disallowPromptTraining" not in body
    assert "preferred-api-key" not in json.dumps(body)


def test_hosted_timeout_is_not_retried_inside_one_vercel_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def timed_out_request(
        request: urllib.request.Request,
        timeout: int,
    ) -> None:
        nonlocal calls
        del request
        calls += 1
        assert timeout == 105
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr("urllib.request.urlopen", timed_out_request)
    gateway = model_gateway_for(
        ModelProviderId.SOLOSCALE_HOSTED,
        environment={
            "SOLOSCALE_HOSTED_GATEWAY_ENABLED": "true",
            "AI_GATEWAY_API_KEY": "diagnostic-test-key",
            "RESUME_HOSTED_MODEL": "zai/glm-5.2",
        },
    )

    with pytest.raises(ModelGatewayTransportError) as failure:
        gateway.complete(_Reply, system="system", user="synthetic-user")

    assert calls == 1
    assert failure.value.details is not None
    assert failure.value.details.category is GatewayErrorCategory.TIMEOUT


def test_real_hosted_transport_classifies_http_failure_without_logging_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0
    private_message = "private resume content must never appear in logs"

    def rejected_request(
        request: urllib.request.Request,
        timeout: int,
    ) -> None:
        nonlocal calls
        del timeout
        calls += 1
        headers = Message()
        headers["x-request-id"] = "req_header_123"
        body = json.dumps(
            {
                "error": {
                    "message": private_message,
                    "type": "invalid_request_error",
                    "code": "invalid_json_schema",
                    "statusCode": 400,
                    "requestId": "req_gateway_123",
                    "provider": "zai",
                }
            }
        ).encode()
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            headers,
            io.BytesIO(body),
        )

    monkeypatch.setattr("urllib.request.urlopen", rejected_request)
    gateway = model_gateway_for(
        ModelProviderId.SOLOSCALE_HOSTED,
        environment={
            "SOLOSCALE_HOSTED_GATEWAY_ENABLED": "true",
            "AI_GATEWAY_API_KEY": "diagnostic-test-key",
            "RESUME_HOSTED_MODEL": "zai/glm-5.2",
        },
    )
    with pytest.raises(ModelGatewayTransportError) as failure:
        gateway.complete(_Reply, system="system", user=private_message)

    assert calls == 1
    details = failure.value.details
    assert details is not None
    assert details.category is GatewayErrorCategory.SCHEMA
    assert details.upstream_http_status == 400
    assert details.gateway_error_type == "invalid_request_error"
    assert details.gateway_error_code == "invalid_json_schema"
    assert details.request_id == "req_gateway_123"
    event_text = capsys.readouterr().out.strip()
    event = json.loads(event_text)
    assert event["event_type"] == "gateway_request_failed"
    assert event["category"] == "gateway_schema_error"
    assert event["request_id"] == "req_gateway_123"
    assert private_message not in event_text
    assert "diagnostic-test-key" not in event_text
