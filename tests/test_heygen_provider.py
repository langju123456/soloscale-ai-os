from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from soloscale.heygen_provider import (
    AvatarAspectRatio,
    HeyGenProviderError,
    HeyGenSubmission,
    HeyGenUpload,
    generate_segment,
    preview_avatar_segment_request,
    save_avatar_submission_receipt,
)
from soloscale.media_cost import (
    BillingUnit,
    BudgetPolicy,
    PaidOperationAuthorization,
    PricingCatalog,
    PricingRate,
    PricingStatus,
    authorize_paid_operation,
    estimate_avatar_seconds,
    evaluate_budget,
)


class _Transport:
    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str]] = []
        self.payloads: list[dict[str, object]] = []

    def upload_audio(
        self, *, credential: str, content: bytes, content_type: str
    ) -> HeyGenUpload:
        assert credential == "synthetic-key"
        self.uploads.append((content, content_type))
        return HeyGenUpload(asset_id="asset_123")

    def submit_avatar_video(
        self, *, credential: str, payload: dict[str, object]
    ) -> HeyGenSubmission:
        assert credential == "synthetic-key"
        self.payloads.append(payload)
        return HeyGenSubmission(request_id="video_456")


def _audio(path: Path) -> Path:
    content = b"RIFF" + b"\x00" * 128
    path.write_bytes(content)
    return path


def _authorization(preview: object) -> PaidOperationAuthorization:
    catalog = PricingCatalog(
        entries=[
            PricingRate(
                provider="heygen",
                service="avatar",
                billing_unit=BillingUnit.SECOND,
                usd_per_unit=Decimal("0.01"),
                pricing_status=PricingStatus.ESTIMATED,
                effective_date=date(2026, 8, 27),
                source="synthetic-test-rate",
            )
        ]
    )
    estimate = estimate_avatar_seconds(
        seconds=Decimal("6"),
        catalog=catalog,
    )
    evaluation = evaluate_budget(
        estimate=estimate,
        policy=BudgetPolicy(per_paid_operation_usd=Decimal("1")),
    )
    return authorize_paid_operation(
        estimate=estimate,
        evaluation=evaluation,
        subject=preview,
    )


def test_avatar_segment_requires_preview_and_submits_once(tmp_path: Path) -> None:
    audio = _audio(tmp_path / "voice.wav")
    preview = preview_avatar_segment_request(
        scene_id="scene-01",
        audio_path=audio,
        locale="zh-CN",
        aspect_ratio=AvatarAspectRatio.LANDSCAPE,
        avatar_id="avatar_abc",
        duration_seconds=6,
    )
    transport = _Transport()

    receipt = generate_segment(
        request=preview,
        audio_path=audio,
        credential="synthetic-key",
        authorization=_authorization(preview),
        transport=transport,
    )

    assert len(transport.uploads) == 1
    assert len(transport.payloads) == 1
    assert receipt.provider_request_id == "video_456"
    assert receipt.audio_sha256 == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert transport.payloads[0]["dimension"] == {"width": 1920, "height": 1080}


def test_avatar_segment_fails_closed_when_audio_changes(tmp_path: Path) -> None:
    audio = _audio(tmp_path / "voice.wav")
    preview = preview_avatar_segment_request(
        scene_id="scene-01",
        audio_path=audio,
        locale="en-US",
        aspect_ratio=AvatarAspectRatio.PORTRAIT,
        avatar_id="avatar_abc",
        duration_seconds=6,
    )
    audio.write_bytes(audio.read_bytes() + b"changed")
    transport = _Transport()

    with pytest.raises(HeyGenProviderError, match="changed"):
        generate_segment(
            request=preview,
            audio_path=audio,
            credential="synthetic-key",
            authorization=_authorization(preview),
            transport=transport,
        )

    assert transport.uploads == []
    assert transport.payloads == []


def test_avatar_segment_rejects_authorization_for_another_request(
    tmp_path: Path,
) -> None:
    audio = _audio(tmp_path / "voice.wav")
    preview = preview_avatar_segment_request(
        scene_id="scene-01",
        audio_path=audio,
        locale="en-US",
        aspect_ratio=AvatarAspectRatio.PORTRAIT,
        avatar_id="avatar_abc",
        duration_seconds=6,
    )
    authorization = _authorization(preview)
    changed = preview.model_copy(update={"scene_id": "scene-02"})
    transport = _Transport()

    with pytest.raises(HeyGenProviderError, match="budget authorization"):
        generate_segment(
            request=changed,
            audio_path=audio,
            credential="synthetic-key",
            authorization=authorization,
            transport=transport,
        )

    assert transport.uploads == []
    assert transport.payloads == []


def test_avatar_receipt_persists_no_audio_path_or_credential(tmp_path: Path) -> None:
    audio = _audio(tmp_path / "voice.wav")
    preview = preview_avatar_segment_request(
        scene_id="scene-01",
        audio_path=audio,
        locale="zh-CN",
        aspect_ratio=AvatarAspectRatio.LANDSCAPE,
        avatar_id="avatar_abc",
        duration_seconds=6,
    )
    receipt = generate_segment(
        request=preview,
        audio_path=audio,
        credential="synthetic-key",
        authorization=_authorization(preview),
        transport=_Transport(),
    )
    path = save_avatar_submission_receipt(
        data_root=tmp_path / "data",
        run_id="content-run-01",
        receipt=receipt,
    )
    raw = path.read_text()
    decoded = json.loads(raw)

    assert decoded["publication_performed"] is False
    assert "synthetic-key" not in raw
    assert str(audio) not in raw
