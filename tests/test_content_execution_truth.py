from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from soloscale.content_models import (
    ClaimStatus,
    ContentBrief,
    ContentClaim,
    ContentDrafts,
    StoryboardScene,
)
from soloscale.content_workspace import (
    run_content_workspace,
    run_content_workspace_with_gateway,
)
from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayDescriptor,
    GatewayTransportScope,
    ModelCallProfile,
    ModelProviderId,
)


def _brief() -> ContentBrief:
    return ContentBrief(
        topic="Evidence-first product integration",
        audience="AI engineers and solo builders",
        language="English",
        call_to_action="Follow the next measured iteration.",
        source_label="https://github.com/example/solo-scale/pull/8",
        claims=[
            ContentClaim(
                id="CLAIM-01",
                text="Python 3.11 and 3.12 CI checks passed.",
                status=ClaimStatus.VERIFIED,
                receipt="https://github.com/example/solo-scale/actions/runs/8",
                limits="This does not prove production readiness.",
            )
        ],
    )


def _model_drafts(brief: ContentBrief) -> ContentDrafts:
    claim_lines = [f"{claim.status.value} · {claim.id} — {claim.text}" for claim in brief.claims]
    x_posts = [
        f"{index}/{len(claim_lines) + 1} {line}"
        for index, line in enumerate(claim_lines, start=1)
    ]
    x_posts.append(f"{len(x_posts) + 1}/{len(claim_lines) + 1} {brief.call_to_action}")
    scenes = [
        StoryboardScene(
            id=f"SCENE-{index:02d}",
            start_second=(index - 1) * 6,
            end_second=index * 6,
            purpose=f"{claim.status.value} · {claim.id}",
            visual="Evidence card",
            voiceover=claim.text,
            on_screen_text=f"{claim.status.value} · {claim.id}",
            claim_ids=[claim.id],
        )
        for index, claim in enumerate(brief.claims, start=1)
    ]
    scenes.append(
        StoryboardScene(
            id=f"SCENE-{len(scenes) + 1:02d}",
            start_second=len(scenes) * 6,
            end_second=(len(scenes) + 1) * 6,
            purpose="CTA",
            visual="Action card",
            voiceover=brief.call_to_action,
            on_screen_text=brief.call_to_action,
            claim_ids=[],
        )
    )
    body = "\n\n".join(claim_lines)
    return ContentDrafts(
        canonical_story=f"Canonical\n\n{body}\n\n{brief.call_to_action}",
        linkedin=f"LinkedIn\n\n{body}\n\n{brief.call_to_action}",
        x_thread=x_posts,
        x_post=x_posts[0],
        blog=f"Blog\n\n{body}\n\n{brief.call_to_action}",
        youtube_script=f"YouTube\n\n{body}\n\n{brief.call_to_action}",
        video_script=f"Video\n\n{body}\n\n{brief.call_to_action}",
        storyboard=scenes,
    )


class _FakeGateway:
    def __init__(
        self,
        drafts: ContentDrafts,
        *,
        profile: ModelCallProfile | None,
    ) -> None:
        self.drafts = drafts
        self.last_call_profile = profile
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local Ollama",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="qwen3:8b",
            base_url="http://127.0.0.1:11434",
        )

    def complete(
        self,
        schema: object,
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ContentDrafts:
        del schema, system, user, reasoning_effort
        return self.drafts


def _profile() -> ModelCallProfile:
    return ModelCallProfile(
        provider=ModelProviderId.OLLAMA,
        model="qwen3:8b",
        system_chars=100,
        user_chars=500,
        schema_chars=300,
        max_output_tokens=4096,
        thinking_enabled=False,
        prompt_eval_tokens=10,
        output_tokens=5,
        wall_ms=123,
        response_chars=400,
        thinking_chars=0,
    )


def _run_payload(data_root: Path, run_id: str) -> dict[str, object]:
    path = data_root / "content-runs" / run_id / "run.json"
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_deterministic_run_records_ai_not_executed(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())

    assert run.execution_state == "AI_NOT_EXECUTED"
    assert run.model_calls == 0
    assert run.token_usage is None
    assert run.latency_ms is None
    assert run.cost_usd is None
    assert run.fallback_used is False
    payload = _run_payload(data_root, run.run_id)
    assert payload["execution_state"] == "AI_NOT_EXECUTED"
    assert payload["model_calls"] == 0
    verification = json.loads(
        (data_root / "content-runs" / run.run_id / "08_verification.json").read_text(
            encoding="utf-8"
        )
    )
    assert verification["execution_state"] == "AI_NOT_EXECUTED"
    assert verification["model_calls"] == 0


def test_gateway_run_persists_full_execution_truth(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    brief = _brief()
    gateway = _FakeGateway(_model_drafts(brief), profile=_profile())
    run = run_content_workspace_with_gateway(
        data_root=data_root, brief=brief, gateway=gateway  # type: ignore[arg-type]
    )

    assert run.execution_state == "AI_EXECUTED"
    assert run.model_calls == 1
    assert run.token_usage == {"prompt_eval_tokens": 10, "output_tokens": 5}
    assert run.latency_ms == 123
    assert run.cost_usd is None
    assert run.fallback_used is False
    assert run.editorial_provenance[0].token_usage == {
        "prompt_eval_tokens": 10,
        "output_tokens": 5,
    }
    payload = _run_payload(data_root, run.run_id)
    assert payload["execution_state"] == "AI_EXECUTED"
    assert payload["model_calls"] == 1
    assert payload["token_usage"] == {"prompt_eval_tokens": 10, "output_tokens": 5}
    assert payload["latency_ms"] == 123


def test_gateway_run_without_profile_stays_truthful(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    brief = _brief()
    gateway = _FakeGateway(_model_drafts(brief), profile=None)
    run = run_content_workspace_with_gateway(
        data_root=data_root, brief=brief, gateway=gateway  # type: ignore[arg-type]
    )

    assert run.execution_state == "AI_EXECUTED"
    assert run.model_calls == 1
    assert run.token_usage is None
    assert run.latency_ms is None
    assert run.cost_usd is None
