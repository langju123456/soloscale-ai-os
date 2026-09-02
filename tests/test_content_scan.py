import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from soloscale.content_scan import ScanRange, scan_recent_work


def _write_resume_receipt(
    data_root: Path,
    run_id: str,
    *,
    output_sha256: str,
    modified_at: datetime,
) -> None:
    run_dir = data_root / "resume-runs" / run_id
    run_dir.mkdir(parents=True)
    receipt = run_dir / "09_user_ui.json"
    receipt.write_text(
        json.dumps(
            {
                "output_sha256": output_sha256,
                "generation_mode": "template",
                "network_used": False,
                "model_call_performed": False,
                "operator_approved_profile_claims": [
                    {"id": f"PROFILE-{index:02d}", "sha256": "a" * 64}
                    for index in range(1, 10)
                ],
                "source_paragraph_count": 33,
                "unsupported_requirement_count": 14,
                "project_blocks_reordered": 2,
                "skill_bullets_reordered": 3,
            }
        ),
        encoding="utf-8",
    )
    timestamp = modified_at.timestamp()
    os.utime(receipt, (timestamp, timestamp))


def test_scan_recent_work_deduplicates_receipts_and_prefills_grounded_content(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)
    _write_resume_receipt(
        data_root,
        "resume-20260820T100000Z-aaaaaaaaaa",
        output_sha256="b" * 64,
        modified_at=now - timedelta(hours=2),
    )
    _write_resume_receipt(
        data_root,
        "resume-20260820T110000Z-bbbbbbbbbb",
        output_sha256="b" * 64,
        modified_at=now - timedelta(hours=1),
    )

    scan = scan_recent_work(data_root, ScanRange.TODAY, now=now)

    assert scan.items_scanned == 2
    assert scan.sources_used == ("Resume runs",)
    assert len(scan.candidates) == 1
    candidate = scan.candidates[0]
    assert candidate.confidence == "high"
    assert "1,147" not in candidate.what_happened
    assert "9 operator-approved" in candidate.claims[0].text
    assert "14 unsupported" in candidate.claims[1].text
    form = candidate.content_form()
    assert form["generation_mode"] == "template"
    assert "SoloScale resume receipt" in form["verified_claims"]
    assert str(tmp_path) not in repr(candidate)


def test_scan_recent_work_respects_today_and_last_seven_days(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)
    _write_resume_receipt(
        data_root,
        "resume-20260816T100000Z-cccccccccc",
        output_sha256="c" * 64,
        modified_at=now - timedelta(days=4),
    )

    assert scan_recent_work(data_root, "today", now=now).candidates == ()
    weekly = scan_recent_work(data_root, "7d", now=now)
    assert len(weekly.candidates) == 1
    assert weekly.scan_range is ScanRange.LAST_7_DAYS
