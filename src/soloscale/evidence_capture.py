"""Best-effort metadata capture for private application artifacts.

This boundary deliberately receives file paths only long enough to calculate a digest.
It never copies artifact bodies into the EvidenceHub and failures are retained locally
as retry information without changing the product operation that created the artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from soloscale.evidence_hub import EvidenceHub
from soloscale.evidence_hub_models import (
    AssetRecord,
    EvidenceBundle,
    EvidenceItem,
    SourceRecord,
    TruthClass,
)

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_WARNING_FILE = "evidence_capture_warning.json"


class _EvidenceRegistrar(Protocol):
    def register_source(
        self, source: SourceRecord, *, items: list[EvidenceItem]
    ) -> SourceRecord: ...

    def build_bundle(
        self,
        evidence_ids: list[str],
        *,
        intent: str,
        coverage: list[str],
        gaps: list[str],
    ) -> EvidenceBundle: ...

    def register_bundle(self, bundle: EvidenceBundle) -> EvidenceBundle: ...

    def register_asset(
        self,
        *,
        owner: str,
        asset_type: str,
        content_sha256: str,
        bundle_id: str | None,
        private_locator: str,
        provenance: dict[str, str],
        evidence_ids: list[str],
    ) -> AssetRecord: ...

    def register_outcome(
        self,
        *,
        outcome_type: str,
        platform: str,
        status: str,
        final_sha256: str,
        external_id: str | None,
        url: str | None,
        metadata: dict[str, str],
        evidence_ids: list[str],
        asset_id: str,
    ) -> object: ...


def private_locator(*parts: str) -> str:
    """Return a stable private locator that never exposes a local filesystem path."""

    cleaned = [part.strip("/ ") for part in parts]
    if not cleaned or any(not part or "/" in part for part in cleaned):
        raise ValueError("private locator parts must be nonblank path components")
    return "private://" + "/".join(cleaned)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\0".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()}"


def _write_warning(run_dir: Path, payload: dict[str, object]) -> None:
    """Persist one replaceable, private recovery record without leaking local paths."""

    try:
        os.chmod(run_dir, _DIRECTORY_MODE)
        destination = run_dir / _WARNING_FILE
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".evidence-capture-", dir=run_dir)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, _FILE_MODE)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()
    except OSError:
        # Evidence capture is never allowed to make the source operation fail.
        return


def _registrar(data_root: Path, evidence_hub: _EvidenceRegistrar | None) -> _EvidenceRegistrar:
    return evidence_hub if evidence_hub is not None else EvidenceHub(data_root)


def _ensure_run_bundle(
    *,
    hub: _EvidenceRegistrar,
    owner: str,
    run_id: str,
    seed_name: str,
    seed_path: Path,
) -> tuple[str, list[str]]:
    """Create a metadata-only provenance bundle for a run without input evidence."""

    digest = _sha256_file(seed_path)
    captured_at = datetime.now(UTC)
    source_id = _stable_id("source", "internal_product_run", owner, run_id)
    evidence_id = _stable_id("evidence", source_id, seed_name, digest)
    source = SourceRecord(
        source_id=source_id,
        native_id=run_id,
        source_system="soloscale",
        source_type="internal_product_run",
        original_locator=private_locator(owner, run_id, *seed_name.split("/")),
        captured_at=captured_at,
        source_at=datetime.fromtimestamp(seed_path.stat().st_mtime, tz=UTC),
        content_sha256=digest,
        sensitivity="private",
        truth_class=TruthClass.PERSONAL_CONTEXT,
        raw_available=True,
        adapter="internal_product_event",
        metadata={"owner": owner, "run_id": run_id, "capture": "metadata_only"},
    )
    item = EvidenceItem(
        evidence_id=evidence_id,
        source_id=source_id,
        native_id=seed_name,
        evidence_type="internal_product_input_metadata",
        captured_at=captured_at,
        source_at=source.source_at,
        provenance_locator=source.original_locator,
        truth_class=TruthClass.PERSONAL_CONTEXT,
        trust_state="operator_input",
        public_safe_summary=f"{owner} run input metadata",
        verification={"content_sha256": digest},
        verification_status="hash_captured",
        content_sha256=digest,
    )
    hub.register_source(source, items=[item])
    bundle = hub.register_bundle(
        hub.build_bundle(
            [item.evidence_id],
            intent=f"Preserve provenance for {owner} run {run_id}",
            coverage=["Internal run input metadata captured"],
            gaps=["Artifact creation does not prove an external outcome"],
        )
    )
    return bundle.bundle_id, [item.evidence_id]


def capture_assets(
    *,
    data_root: Path,
    run_dir: Path,
    owner: str,
    run_id: str,
    artifact_names: list[str],
    evidence_bundle_id: str | None = None,
    evidence_item_ids: list[str] | None = None,
    evidence_hub: _EvidenceRegistrar | None = None,
) -> dict[str, str]:
    """Register saved artifacts, recording a private retry hint if cataloging fails."""

    try:
        hub = _registrar(data_root, evidence_hub)
        available = [
            (name, run_dir / name)
            for name in artifact_names
            if not (run_dir / name).is_symlink() and (run_dir / name).is_file()
        ]
        if not available:
            return {}
        selected_bundle_id = evidence_bundle_id
        selected_evidence_ids = list(evidence_item_ids or [])
        if selected_bundle_id is None:
            preferred = available[0]
            for candidate_name in ("00_input.json", "run.json"):
                candidate_path = run_dir / candidate_name
                if not candidate_path.is_symlink() and candidate_path.is_file():
                    preferred = (candidate_name, candidate_path)
                    break
            selected_bundle_id, selected_evidence_ids = _ensure_run_bundle(
                hub=hub,
                owner=owner,
                run_id=run_id,
                seed_name=preferred[0],
                seed_path=preferred[1],
            )
        registered: dict[str, str] = {}
        for name, path in available:
            asset = hub.register_asset(
                owner=owner,
                asset_type=f"{owner}_artifact",
                content_sha256=_sha256_file(path),
                bundle_id=selected_bundle_id,
                private_locator=private_locator(owner, run_id, *name.split("/")),
                provenance={"run_id": run_id, "artifact_name": name, "capture": "metadata_only"},
                evidence_ids=selected_evidence_ids,
            )
            registered[name] = asset.asset_id
        return registered
    except Exception as exc:
        _write_warning(
            run_dir,
            {
                "status": "PENDING_EVIDENCE_CAPTURE_RETRY",
                "operation": "register_assets",
                "owner": owner,
                "run_id": run_id,
                "artifact_count": len(artifact_names),
                "error_type": type(exc).__name__,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        return {}


def capture_outcome(
    *,
    data_root: Path,
    run_dir: Path,
    owner: str,
    run_id: str,
    outcome_type: str,
    platform: str,
    status: str,
    final_sha256: str,
    external_id: str | None = None,
    url: str | None = None,
    metadata: dict[str, str] | None = None,
    evidence_item_ids: list[str] | None = None,
    asset_id: str | None = None,
    evidence_hub: _EvidenceRegistrar | None = None,
) -> None:
    """Register an already-successful outcome; this function has no retry side effects."""

    try:
        if asset_id is None:
            raise ValueError("outcomes require an exact asset link")
        _registrar(data_root, evidence_hub).register_outcome(
            outcome_type=outcome_type,
            platform=platform,
            status=status,
            final_sha256=final_sha256,
            external_id=external_id,
            url=url,
            metadata={
                "owner": owner,
                "run_id": run_id,
                "capture": "metadata_only",
                **(metadata or {}),
            },
            evidence_ids=evidence_item_ids or [],
            asset_id=asset_id,
        )
    except Exception as exc:
        _write_warning(
            run_dir,
            {
                "status": "PENDING_EVIDENCE_CAPTURE_RETRY",
                "operation": "register_outcome",
                "owner": owner,
                "run_id": run_id,
                "outcome_type": outcome_type,
                "error_type": type(exc).__name__,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
