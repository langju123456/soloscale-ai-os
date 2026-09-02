# ruff: noqa: E501
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from soloscale.content_ui import editorial_publishing_page
from soloscale.editorial_publishing_handoff import (
    EditorialPublishingError,
    editorial_image_preview,
    editorial_publishing_status,
    preview_editorial_day,
    publish_editorial_preview,
)
from soloscale.platform_accounts import ConnectedIdentity, save_connected_identity

_X_ACCOUNT_REFERENCE = hashlib.sha256(b"123").hexdigest()[:20]


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x04\xb0\x00\x00\x02v"


def _sealed_day(tmp_path: Path, *, x_thread: str = "1/2 First\n\nline\n\n2/2 Second\n") -> Path:
    batch = tmp_path / "editorial-week"
    day = batch / "day-01"
    visual = day / "visual"
    visual.mkdir(parents=True)
    files = {
        "linkedin.md": b"LinkedIn exact text\n",
        "x-thread.md": x_thread.encode(),
        "visual/alt-text.md": b"Exact accessible description.\n",
        "visual/visual.png": _png(),
        "post-revision-review.json": b'{"verdict":"ready"}\n',
    }
    for name, content in files.items():
        (day / name).write_bytes(content)
    artifacts = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in files.items()
        if name != "post-revision-review.json"
    }
    receipt = {"package_id": "day-01-grounded", "publication_performed": False, "human_gate_required": True, "artifacts": artifacts}
    (day / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    day_hash = hashlib.sha256((day / "receipt.json").read_bytes()).hexdigest()
    week = {"day_receipts": {"day-01/receipt.json": day_hash}}
    (batch / "week-receipt.json").write_text(json.dumps(week), encoding="utf-8")
    final = {
        "status": "READY_FOR_HUMAN_PUBLICATION",
        "publication_performed": False,
        "human_gate_required": True,
        "week_receipt_sha256": hashlib.sha256((batch / "week-receipt.json").read_bytes()).hexdigest(),
        "post_revision_review_sha256": hashlib.sha256((day / "post-revision-review.json").read_bytes()).hexdigest(),
    }
    (batch / "final-validation-receipt.json").write_text(json.dumps(final), encoding="utf-8")
    return day


def _connect_x(data_root: Path) -> None:
    save_connected_identity(
        data_root,
        ConnectedIdentity(
            platform="x",
            external_account_id="123",
            display_name="Solo Builder",
            handle="solo_builder",
            avatar_url=None,
            scopes=("tweet.read", "users.read", "tweet.write", "offline.access"),
            token_reference="pending",
            connected_at="2026-08-28T00:00:00+00:00",
        ),
        token_payload={"access_token": "synthetic"},
    )


class _Gateway:
    def __init__(self) -> None:
        self.published: dict[str, object] | None = None

    def stage(self, **kwargs: object) -> SimpleNamespace:
        assert kwargs["text_parts"] == ["1/2 First\n\nline", "2/2 Second"]
        assert Path(str(kwargs["image_path"])).name == "visual.png"
        return SimpleNamespace(plan_id="plan-1")

    def preview(self, plan_id: str) -> SimpleNamespace:
        assert plan_id == "plan-1"
        return SimpleNamespace(
            plan_id=plan_id,
            plan_hash="a" * 64,
            account_reference=_X_ACCOUNT_REFERENCE,
            account_display_name="Solo Builder",
            parts=["1/2 First\n\nline", "2/2 Second"],
            image=SimpleNamespace(
                sha256=hashlib.sha256(_png()).hexdigest(), mime_type="image/png", width=1200,
                height=630, alt_text="Exact accessible description.", filename="image.png",
            ),
            duplicate_found=False,
            indeterminate_found=False,
        )

    def publish(self, plan_id: str, **kwargs: object) -> SimpleNamespace:
        self.published = {"plan_id": plan_id, **kwargs}
        return SimpleNamespace(
            plan_id=plan_id, plan_hash="a" * 64, platform="x", account_reference=_X_ACCOUNT_REFERENCE,
            post_receipt_ids=["receipt-1", "receipt-2"], external_post_ids=["post-1", "post-2"],
            status="succeeded",
        )


def test_editorial_day_handoff_preserves_sealed_package_and_uses_server_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = _sealed_day(tmp_path)
    original = {path: path.read_bytes() for path in day.rglob("*") if path.is_file()}
    gateway = _Gateway()
    monkeypatch.setattr("soloscale.editorial_publishing_handoff._gateway", lambda *_: gateway)
    _connect_x(tmp_path / ".soloscale")

    preview = preview_editorial_day(data_root=tmp_path / ".soloscale", day_directory=day, channel="x")
    result = publish_editorial_preview(data_root=tmp_path / ".soloscale", channel="x", confirmation="PUBLISH")

    assert preview["source_image_path"] == "visual/visual.png"
    assert preview["duplicate"] is False
    assert preview["indeterminate"] is False
    assert result["status"] == "succeeded"
    assert gateway.published == {
        "plan_id": "plan-1", "confirmation": "PUBLISH", "approved_plan_hash": "a" * 64,
        "approved_account_reference": _X_ACCOUNT_REFERENCE,
    }
    assert {path: path.read_bytes() for path in day.rglob("*") if path.is_file()} == original
    assert editorial_publishing_status(tmp_path / ".soloscale", "linkedin") == (None, None)


def test_editorial_image_preview_returns_only_the_hash_bound_staged_png(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    records = data_root / "editorial-publishing"
    image_root = (
        data_root
        / "publishing"
        / "publication-plans"
        / "plan-1"
    )
    records.mkdir(parents=True)
    image_root.mkdir(parents=True)
    (image_root / "image.png").write_bytes(_png())
    (records / "linkedin-preview.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "image": {"sha256": hashlib.sha256(_png()).hexdigest()},
            }
        ),
        encoding="utf-8",
    )

    assert editorial_image_preview(data_root, "linkedin") == _png()
    (image_root / "image.png").write_bytes(_png() + b"tampered")
    with pytest.raises(EditorialPublishingError, match="does not match"):
        editorial_image_preview(data_root, "linkedin")


def test_editorial_handoff_rejects_tampered_receipt_and_nonconsecutive_thread(tmp_path: Path) -> None:
    day = _sealed_day(tmp_path, x_thread="1/2 First\n\n3/2 Second\n")
    with pytest.raises(EditorialPublishingError, match="consecutive"):
        preview_editorial_day(data_root=tmp_path / ".soloscale", day_directory=day, channel="x")

    day = _sealed_day(tmp_path / "tampered")
    (day / "linkedin.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(EditorialPublishingError, match="hash does not match"):
        preview_editorial_day(data_root=tmp_path / ".soloscale", day_directory=day, channel="linkedin")

    secret_day = _sealed_day(
        tmp_path / "secret",
        x_thread="1/2 First\n\n2/2 Bearer abcdefghijklmnopqrstuv\n",
    )
    with pytest.raises(EditorialPublishingError, match="credential-like"):
        preview_editorial_day(
            data_root=tmp_path / ".soloscale",
            day_directory=secret_day,
            channel="x",
        )


def test_failed_publish_is_terminal_and_legacy_buildlog_retry_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / ".soloscale"
    gateway = _Gateway()
    monkeypatch.setattr("soloscale.editorial_publishing_handoff._gateway", lambda *_: gateway)
    preview_editorial_day(
        data_root=data_root,
        day_directory=_sealed_day(tmp_path),
        channel="x",
    )
    _connect_x(data_root)

    def fail(*args: object, **kwargs: object) -> None:
        raise TimeoutError("ambiguous provider outcome")

    monkeypatch.setattr(gateway, "publish", fail)
    with pytest.raises(EditorialPublishingError, match="inspect"):
        publish_editorial_preview(data_root=data_root, channel="x", confirmation="PUBLISH")
    preview, receipt = editorial_publishing_status(data_root, "x")
    assert preview is not None and receipt is not None
    assert receipt["status"] == "PUBLICATION_FAILED_OR_INDETERMINATE_DO_NOT_RETRY"
    assert "PUBLISH X" not in editorial_publishing_page(data_root=data_root)

    receipt["plan_id"] = "plan-from-an-older-preview"
    (data_root / "editorial-publishing" / "x-receipt.json").write_text(json.dumps(receipt))
    assert "PUBLISH X" not in editorial_publishing_page(data_root=data_root)


def test_publish_rejects_preview_bound_to_a_different_connected_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / ".soloscale"
    gateway = _Gateway()
    monkeypatch.setattr(
        "soloscale.editorial_publishing_handoff._gateway", lambda *_: gateway
    )
    preview_editorial_day(
        data_root=data_root,
        day_directory=_sealed_day(tmp_path),
        channel="x",
    )
    save_connected_identity(
        data_root,
        ConnectedIdentity(
            platform="x",
            external_account_id="999",
            display_name="Different Account",
            handle="different",
            avatar_url=None,
            scopes=("tweet.read", "users.read", "tweet.write", "offline.access"),
            token_reference="pending",
            connected_at="2026-08-28T00:00:00+00:00",
        ),
        token_payload={"access_token": "synthetic"},
    )

    with pytest.raises(EditorialPublishingError, match="does not match"):
        publish_editorial_preview(
            data_root=data_root, channel="x", confirmation="PUBLISH"
        )
    assert gateway.published is None
