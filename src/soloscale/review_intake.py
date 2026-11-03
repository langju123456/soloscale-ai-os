"""Read saved Evidence Agent receipts and verify their citations for human review.

This module is the only bridge from the private agent-run directory to the Review
Workbench.  It never calls a model.  It reads ``04_result.json``, cross-checks it against
the run's own input and retrieval manifests, and re-resolves every cited chunk in the
current KnowledgeStore so a reviewer sees whether the evidence is still the evidence the
model saw.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from soloscale.evidence_agent import AgentRunResult, EvidenceReference, _sha256_json
from soloscale.knowledge_store import (
    CorruptKnowledgeStoreError,
    KnowledgeStore,
    KnowledgeStoreError,
)
from soloscale.review_models import CitationCheck, EvidenceStatus
from soloscale.review_workflow import ReviewError, derive_evidence_status

RESULT_FILE = "04_result.json"
FAILURE_FILE = "failure.json"
INPUT_MANIFEST_FILE = "00_input.json"
RETRIEVAL_MANIFEST_FILE = "03_retrieval_manifest.json"
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_LISTED_DRAFTS = 200
_RUN_ID = re.compile(r"^evidence-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class DraftNotFoundError(ReviewError):
    status_code = 404
    code = "DRAFT_NOT_FOUND"


class DraftInvalidError(ReviewError):
    status_code = 422
    code = "DRAFT_INVALID"


class ReceiptIntegrityError(ReviewError):
    status_code = 422
    code = "RECEIPT_INTEGRITY_FAILED"


class KnowledgeIndexUnavailableError(ReviewError):
    status_code = 503
    code = "KNOWLEDGE_INDEX_UNAVAILABLE"


@dataclass(frozen=True)
class RecordedCitation:
    """What the receipt says the model saw for one cited chunk."""

    chunk_id: str
    document_id: str | None
    chunk_sha256: str | None
    document_sha256: str | None


@dataclass(frozen=True)
class CurrentCheck:
    check: CitationCheck
    current_chunk_sha256: str | None
    current_document_sha256: str | None


@dataclass(frozen=True)
class CitationSnapshot:
    ordinal: int
    recorded: RecordedCitation
    reference: EvidenceReference | None
    current: CurrentCheck


@dataclass(frozen=True)
class ClaimCandidate:
    claim_index: int
    text: str
    citations: tuple[CitationSnapshot, ...]
    evidence_status: EvidenceStatus


@dataclass(frozen=True)
class VerifiedDraft:
    result: AgentRunResult
    draft_sha256: str
    knowledge_index_checked: bool
    checked_at: datetime
    claims: tuple[ClaimCandidate, ...]


def agent_runs_root(data_root: Path) -> Path:
    return Path(data_root) / "knowledge" / "agent-runs"


def validate_run_id(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise DraftNotFoundError("run id is not a valid Evidence Agent run identifier")
    return run_id


def knowledge_index_present(data_root: Path) -> bool:
    path = Path(data_root) / "knowledge" / "index.sqlite3"
    return path.is_file() and not path.is_symlink()


def list_agent_drafts(
    data_root: Path, *, limit: int = MAX_LISTED_DRAFTS
) -> tuple[list[AgentRunResult], int]:
    """Return completed, schema-valid drafts newest first, plus a count of skipped runs."""

    root = agent_runs_root(data_root)
    if not root.is_dir() or root.is_symlink():
        return [], 0
    drafts: list[AgentRunResult] = []
    skipped = 0
    names = sorted(
        (entry.name for entry in root.iterdir() if _RUN_ID.fullmatch(entry.name)),
        reverse=True,
    )
    for name in names[:limit]:
        run_dir = root / name
        if run_dir.is_symlink() or not run_dir.is_dir() or (run_dir / FAILURE_FILE).exists():
            continue
        if not (run_dir / RESULT_FILE).is_file():
            continue
        try:
            result, _ = _read_result(run_dir, name)
        except ReviewError:
            skipped += 1
            continue
        drafts.append(result)
    return drafts, skipped


def load_verified_draft(data_root: Path, run_id: str) -> VerifiedDraft:
    """Load one completed agent run and verify its receipts and citations.

    The receipt cross-checks fail closed: an internally inconsistent run is not imported.
    Citation drift against the current index does not fail import; it is recorded per
    citation and routes the claim to human review.
    """

    validate_run_id(run_id)
    run_dir = agent_runs_root(data_root) / run_id
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise DraftNotFoundError("Evidence Agent run was not found")
    if (run_dir / FAILURE_FILE).exists():
        raise DraftInvalidError("this Evidence Agent run failed and has no draft to review")
    result, draft_sha256 = _read_result(run_dir, run_id)
    _verify_input_manifest(run_dir, result)
    _verify_retrieval_manifest(run_dir, result)

    refs_by_id = {reference.chunk_id: reference for reference in result.refs}
    recorded_by_id: dict[str, RecordedCitation] = {}
    for claim in result.claims:
        for raw_chunk_id in claim.evidence_chunk_ids:
            chunk_id = raw_chunk_id.strip()
            reference = refs_by_id.get(chunk_id)
            recorded_by_id.setdefault(
                chunk_id,
                RecordedCitation(
                    chunk_id=chunk_id,
                    document_id=reference.document_id if reference else None,
                    chunk_sha256=reference.chunk_sha256 if reference else None,
                    document_sha256=reference.document_sha256 if reference else None,
                ),
            )
    index_checked = knowledge_index_present(data_root)
    current = check_current_citations(data_root, list(recorded_by_id.values()))

    claims: list[ClaimCandidate] = []
    for claim_index, claim in enumerate(result.claims):
        snapshots: list[CitationSnapshot] = []
        for ordinal, raw_chunk_id in enumerate(claim.evidence_chunk_ids):
            chunk_id = raw_chunk_id.strip()
            reference = refs_by_id.get(chunk_id)
            snapshots.append(
                CitationSnapshot(
                    ordinal=ordinal,
                    recorded=recorded_by_id[chunk_id],
                    reference=reference,
                    current=current[chunk_id],
                )
            )
        claims.append(
            ClaimCandidate(
                claim_index=claim_index,
                text=claim.text.strip(),
                citations=tuple(snapshots),
                evidence_status=derive_evidence_status(
                    [snapshot.current.check for snapshot in snapshots]
                ),
            )
        )
    return VerifiedDraft(
        result=result,
        draft_sha256=draft_sha256,
        knowledge_index_checked=index_checked,
        checked_at=datetime.now(UTC),
        claims=tuple(claims),
    )


def check_current_citations(
    data_root: Path, recorded: Sequence[RecordedCitation]
) -> dict[str, CurrentCheck]:
    """Re-resolve each recorded chunk in the current index and compare its lineage.

    Each chunk is resolved separately so one corrupt row cannot hide the state of the
    others.  Without an index nothing can be verified, so every citation is UNCHECKED.
    """

    results: dict[str, CurrentCheck] = {}
    if not recorded:
        return results
    if not knowledge_index_present(data_root):
        for citation in recorded:
            check = (
                CitationCheck.NOT_IN_RECEIPT
                if citation.chunk_sha256 is None
                else CitationCheck.UNCHECKED
            )
            results[citation.chunk_id] = CurrentCheck(check, None, None)
        return results
    try:
        store = KnowledgeStore(Path(data_root))
    except CorruptKnowledgeStoreError:
        for citation in recorded:
            results[citation.chunk_id] = CurrentCheck(CitationCheck.INTEGRITY_FAILED, None, None)
        return results
    except (KnowledgeStoreError, OSError):
        raise KnowledgeIndexUnavailableError("knowledge index could not be opened") from None

    for citation in recorded:
        if citation.chunk_sha256 is None:
            results[citation.chunk_id] = CurrentCheck(CitationCheck.NOT_IN_RECEIPT, None, None)
            continue
        try:
            hits = store.get_chunks([citation.chunk_id])
        except CorruptKnowledgeStoreError:
            results[citation.chunk_id] = CurrentCheck(CitationCheck.INTEGRITY_FAILED, None, None)
            continue
        except (KnowledgeStoreError, OSError, ValueError):
            raise KnowledgeIndexUnavailableError("knowledge index could not be read") from None
        if not hits:
            results[citation.chunk_id] = CurrentCheck(CitationCheck.SOURCE_MISSING, None, None)
            continue
        hit = hits[0]
        if hit.chunk_sha256 != citation.chunk_sha256 or hit.document_id != citation.document_id:
            check = CitationCheck.HASH_MISMATCH
        elif hit.document_sha256 != citation.document_sha256:
            check = CitationCheck.DOCUMENT_REVISED
        else:
            check = CitationCheck.VERIFIED
        results[citation.chunk_id] = CurrentCheck(check, hit.chunk_sha256, hit.document_sha256)
    return results


def _read_result(run_dir: Path, run_id: str) -> tuple[AgentRunResult, str]:
    raw = _read_receipt_bytes(run_dir / RESULT_FILE)
    try:
        result = AgentRunResult.model_validate_json(raw)
    except ValidationError:
        raise DraftInvalidError("draft receipt does not match the Evidence Agent schema") from None
    if result.run_id != run_id:
        raise ReceiptIntegrityError("draft receipt run id does not match its directory")
    return result, hashlib.sha256(raw).hexdigest()


def _verify_input_manifest(run_dir: Path, result: AgentRunResult) -> None:
    manifest = _read_json_object(run_dir / INPUT_MANIFEST_FILE)
    if _sha256_json(manifest) != result.input_manifest_sha256:
        raise ReceiptIntegrityError("input manifest hash does not match the draft receipt")
    if manifest.get("run_id") != result.run_id or manifest.get("question") != result.question:
        raise ReceiptIntegrityError("input manifest does not describe this draft")


def _verify_retrieval_manifest(run_dir: Path, result: AgentRunResult) -> None:
    manifest = _read_json_object(run_dir / RETRIEVAL_MANIFEST_FILE)
    if _sha256_json(manifest) != result.retrieval_manifest_sha256:
        raise ReceiptIntegrityError("retrieval manifest hash does not match the draft receipt")
    hits = manifest.get("hits")
    if not isinstance(hits, list):
        raise ReceiptIntegrityError("retrieval manifest has no hit list")
    by_id: dict[str, Mapping[str, Any]] = {}
    for hit in hits:
        if isinstance(hit, Mapping) and isinstance(hit.get("chunk_id"), str):
            by_id[str(hit["chunk_id"])] = hit
    for reference in result.refs:
        hit = by_id.get(reference.chunk_id)
        if (
            hit is None
            or hit.get("in_context") is not True
            or hit.get("chunk_sha256") != reference.chunk_sha256
            or hit.get("document_id") != reference.document_id
            or hit.get("excerpt") != reference.excerpt
        ):
            raise ReceiptIntegrityError(
                "a cited excerpt is not the excerpt recorded as model-visible for this run"
            )


def _read_json_object(path: Path) -> dict[str, Any]:
    raw = _read_receipt_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReceiptIntegrityError(f"{path.name} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ReceiptIntegrityError(f"{path.name} is not a JSON object")
    return value


def _read_receipt_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReceiptIntegrityError(f"{path.name} is missing from the agent run")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
    except OSError:
        raise ReceiptIntegrityError(f"{path.name} could not be read") from None
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ReceiptIntegrityError(f"{path.name} exceeds the receipt size limit")
    return raw
