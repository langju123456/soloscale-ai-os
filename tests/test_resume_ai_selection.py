"""Focused coverage for canonical request-scoped Resume AI selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from soloscale.local_ui import (
    OllamaReadiness,
    _apply_resume_ai_selection,
    _creator_generation_mode,
    _load_ai_provider_preference,
    _record_ai_provider_validation,
    _resolve_resume_ai_selection,
    _resume_gateway_from_selection,
    _save_ai_provider_preference,
    _user_page,
)
from soloscale.model_gateway import ModelProviderId


def _stable_credentials(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deepseek: bool = False,
    openai: bool = False,
    local: bool = False,
) -> None:
    monkeypatch.setattr(
        "soloscale.local_ui._ollama_readiness",
        lambda preference: OllamaReadiness(local, local, local),
    )
    monkeypatch.setattr(
        "soloscale.local_ui.deepseek_api_key_is_configured", lambda: deepseek
    )
    monkeypatch.setattr(
        "soloscale.local_ui.deepseek_api_key",
        lambda: "synthetic-deepseek-key" if deepseek else None,
    )
    monkeypatch.setattr(
        "soloscale.local_ui.openai_api_key_is_configured", lambda: openai
    )
    monkeypatch.setattr(
        "soloscale.local_ui.openai_api_key",
        lambda: "synthetic-openai-key" if openai else None,
    )


def _ready_default(
    data_root: Path,
    *,
    provider: ModelProviderId,
    model: str,
    reasoning_effort: str = "low",
) -> None:
    def save(*, set_default: bool) -> None:
        if provider is ModelProviderId.DEEPSEEK:
            _save_ai_provider_preference(
                data_root,
                provider=provider.value,
                deepseek_model=model,
                deepseek_reasoning_effort=reasoning_effort,
                set_default=set_default,
            )
        elif provider is ModelProviderId.OPENAI_COMPATIBLE:
            _save_ai_provider_preference(
                data_root,
                provider=provider.value,
                openai_model=model,
                set_default=set_default,
            )
        else:
            _save_ai_provider_preference(
                data_root,
                provider=provider.value,
                set_default=set_default,
            )

    save(set_default=False)
    _record_ai_provider_validation(
        data_root,
        provider=provider,
        status="ready",
        detail="ready",
    )
    save(set_default=True)


def test_global_deepseek_default_is_ready_and_visible_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_credentials(monkeypatch, deepseek=True)
    _ready_default(
        tmp_path,
        provider=ModelProviderId.DEEPSEEK,
        model="deepseek-v4-pro",
        reasoning_effort="high",
    )

    page = _user_page(None, tmp_path, {}, "en")

    assert "AI service for this run" in page
    assert "Use global default — DeepSeek · DeepSeek V4 Pro · High — READY" in page
    assert 'name="resume_ai_selection"' in page
    assert "SoloScale Hosted AI · Recommended" not in page
    assert "GPT-5.6 Sol Expert Review" not in page
    assert "using my OpenAI API account" not in page


def test_openai_authorization_copy_only_appears_for_selected_openai_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_credentials(monkeypatch, openai=True)
    _ready_default(
        tmp_path,
        provider=ModelProviderId.OPENAI_COMPATIBLE,
        model="gpt-5.6-sol",
    )
    choice = f"{ModelProviderId.OPENAI_COMPATIBLE.value}:gpt-5.6-sol"

    page = _user_page(
        None,
        tmp_path,
        {
            "expert_review_mode": "ai",
            "expert_ai_selection": choice,
        },
        "en",
    )

    assert "Use global default — OpenAI API · GPT-5.6 Sol — READY" in page
    assert "This review uses your OpenAI API account." in page
    assert "GPT-5.6 Sol Expert Review" not in page


def test_run_override_is_provider_owned_and_does_not_mutate_global_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_credentials(monkeypatch, deepseek=True, local=True)
    _ready_default(
        tmp_path,
        provider=ModelProviderId.DEEPSEEK,
        model="deepseek-v4-pro",
        reasoning_effort="high",
    )
    original = _load_ai_provider_preference(tmp_path)

    override = _resolve_resume_ai_selection(tmp_path, "ollama:qwen3:8b")
    form: dict[str, str] = {}
    _apply_resume_ai_selection(form, override)
    gateway = _resume_gateway_from_selection(override, tmp_path)

    assert override.ready is True
    assert form == {
        "generation_mode": "ollama",
        "provider_model": "qwen3:8b",
        "provider_reasoning_effort": "none",
    }
    assert gateway.descriptor.provider is ModelProviderId.OLLAMA
    assert gateway.descriptor.model == "qwen3:8b"
    assert _load_ai_provider_preference(tmp_path) == original


def test_unready_or_cross_provider_selection_fails_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_credentials(monkeypatch)
    _save_ai_provider_preference(
        tmp_path,
        provider=ModelProviderId.DEEPSEEK.value,
        deepseek_model="deepseek-v4-pro",
    )

    with pytest.raises(ValueError, match="not available"):
        _resolve_resume_ai_selection(tmp_path, "deepseek:gpt-5.6-sol")

    selected = _resolve_resume_ai_selection(tmp_path, "default")
    assert selected.provider is ModelProviderId.DEEPSEEK
    assert selected.model == "deepseek-v4-pro"
    assert selected.readiness == "NOT_CONFIGURED"
    with pytest.raises(ValueError, match="NOT_CONFIGURED"):
        _resume_gateway_from_selection(selected, tmp_path)


def test_unvalidated_deepseek_model_override_is_not_mislabeled_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_credentials(monkeypatch, deepseek=True)
    _ready_default(
        tmp_path,
        provider=ModelProviderId.DEEPSEEK,
        model="deepseek-v4-pro",
        reasoning_effort="high",
    )

    alternate = _resolve_resume_ai_selection(
        tmp_path, "deepseek:deepseek-v4-flash"
    )

    assert alternate.readiness == "CONFIGURED_NOT_TESTED"
    with pytest.raises(ValueError, match="CONFIGURED_NOT_TESTED"):
        _resume_gateway_from_selection(alternate, tmp_path)


def test_creator_uses_global_provider_without_silent_template_fallback(
    tmp_path: Path,
) -> None:
    preference = _save_ai_provider_preference(
        tmp_path,
        provider=ModelProviderId.DEEPSEEK.value,
        deepseek_model="deepseek-v4-pro",
    )

    assert _creator_generation_mode(preference, {}, "STORY") == "deepseek"
    assert (
        _creator_generation_mode(
            preference, {"generation_mode": "soloscale_hosted"}, "CREATE"
        )
        == "deepseek"
    )
    assert (
        _creator_generation_mode(
            preference, {"generation_mode": "template"}, "CREATE"
        )
        == "template"
    )
