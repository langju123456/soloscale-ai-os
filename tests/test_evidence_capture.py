from __future__ import annotations

import hashlib
import json
from pathlib import Path

from soloscale.evidence_capture import capture_assets, capture_outcome
from soloscale.evidence_hub import EvidenceHub


def test_capture_is_metadata_only_and_failure_leaves_private_retry_record(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    run_dir = root / "content-runs" / "content-test"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "draft.md"
    artifact.write_text("private artifact body", encoding="utf-8")
    hub = EvidenceHub(root)

    captured = capture_assets(
        data_root=root,
        run_dir=run_dir,
        owner="content",
        run_id="content-test",
        artifact_names=["draft.md"],
        evidence_hub=hub,
    )
    asset = hub.recent_assets()[0]
    assert asset.content_sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert asset.private_locator == "private://content/content-test/draft.md"
    assert asset.bundle_id is not None
    assert captured == {"draft.md": asset.asset_id}
    assert "private artifact body" not in hub.database_path.read_bytes().decode("latin1")

    capture_outcome(
        data_root=root,
        run_dir=run_dir,
        owner="content",
        run_id="content-test",
        outcome_type="publication",
        platform="linkedin",
        status="published",
        final_sha256=asset.content_sha256,
        asset_id=asset.asset_id,
        evidence_hub=hub,
    )
    assert hub.recent_outcomes()[0].asset_id == asset.asset_id

    class BrokenHub:
        def register_outcome(self, **kwargs: object) -> object:
            raise RuntimeError("catalog unavailable")

    capture_outcome(
        data_root=root,
        run_dir=run_dir,
        owner="content",
        run_id="content-test",
        outcome_type="publication",
        platform="linkedin",
        status="published",
        final_sha256=asset.content_sha256,
        asset_id=asset.asset_id,
        evidence_hub=BrokenHub(),  # type: ignore[arg-type]
    )
    warning = json.loads((run_dir / "evidence_capture_warning.json").read_text(encoding="utf-8"))
    assert warning["status"] == "PENDING_EVIDENCE_CAPTURE_RETRY"
    assert warning["operation"] == "register_outcome"
