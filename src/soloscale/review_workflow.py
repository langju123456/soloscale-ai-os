"""Deterministic review rules: evidence status, the item state machine, and text diffs.

Nothing here performs I/O.  The store calls these functions inside its write
transaction, so the same rules apply to every client; a UI can only display the
``allowed_actions`` computed here, it cannot widen them.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from typing import Any

from soloscale.review_models import (
    CitationCheck,
    ConfirmRequirement,
    DiffOp,
    EvidenceStatus,
    ReviewAction,
    ReviewState,
)

PENDING_STATES = frozenset({ReviewState.PENDING_REVIEW, ReviewState.NEEDS_REVIEW})
DECIDED_STATES = frozenset({ReviewState.CONFIRMED, ReviewState.CORRECTED, ReviewState.REJECTED})

# The only state changes a reviewer action can cause.  REOPEN is handled separately
# because its target depends on the item's evidence status.
TRANSITIONS: dict[tuple[ReviewState, ReviewAction], ReviewState] = {
    (ReviewState.PENDING_REVIEW, ReviewAction.CONFIRM): ReviewState.CONFIRMED,
    (ReviewState.PENDING_REVIEW, ReviewAction.CORRECT): ReviewState.CORRECTED,
    (ReviewState.PENDING_REVIEW, ReviewAction.REJECT): ReviewState.REJECTED,
    (ReviewState.NEEDS_REVIEW, ReviewAction.CONFIRM): ReviewState.CONFIRMED,
    (ReviewState.NEEDS_REVIEW, ReviewAction.CORRECT): ReviewState.CORRECTED,
    (ReviewState.NEEDS_REVIEW, ReviewAction.REJECT): ReviewState.REJECTED,
}

_EVIDENCE_SEVERITY = {
    EvidenceStatus.HASH_MISMATCH: 0,
    EvidenceStatus.UNVERIFIED: 1,
    EvidenceStatus.PARTIALLY_VERIFIED: 2,
    EvidenceStatus.FULLY_VERIFIED: 3,
}
_STATE_PRIORITY = {
    ReviewState.NEEDS_REVIEW: 0,
    ReviewState.PENDING_REVIEW: 1,
    ReviewState.CORRECTED: 2,
    ReviewState.CONFIRMED: 2,
    ReviewState.REJECTED: 2,
}
_CJK_RANGES = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# Whitespace runs, single CJK characters, non-CJK word runs, or one punctuation mark.
_DIFF_TOKEN = re.compile(f"\\s+|[{_CJK_RANGES}]|(?:(?![{_CJK_RANGES}])[^\\W_])+|\\S")


class ReviewError(Exception):
    """A rule violation with a stable code; the API maps it to an HTTP error envelope."""

    status_code = 409
    code = "REVIEW_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ReviewNotFoundError(ReviewError):
    status_code = 404
    code = "NOT_FOUND"


class VersionNotFoundError(ReviewError):
    status_code = 404
    code = "VERSION_NOT_FOUND"


class StaleRevisionError(ReviewError):
    status_code = 409
    code = "STALE_REVISION"


class InvalidTransitionError(ReviewError):
    status_code = 409
    code = "INVALID_TRANSITION"


class NoteRequiredError(ReviewError):
    status_code = 422
    code = "NOTE_REQUIRED"


class ConfirmBlockedError(ReviewError):
    status_code = 409
    code = "CONFIRM_BLOCKED_HASH_MISMATCH"


class LowConfidenceBatchError(ReviewError):
    status_code = 409
    code = "LOW_CONFIDENCE_REQUIRES_INDIVIDUAL_REVIEW"


class EvidenceChangedError(ReviewError):
    status_code = 409
    code = "EVIDENCE_CHANGED"


class NoChangeError(ReviewError):
    status_code = 422
    code = "NO_CHANGE"


# How much a citation check still supports the claim; used to detect degradation.
CHECK_RANK = {
    CitationCheck.VERIFIED: 3,
    CitationCheck.DOCUMENT_REVISED: 2,
    CitationCheck.SOURCE_MISSING: 1,
    CitationCheck.NOT_IN_RECEIPT: 1,
    CitationCheck.UNCHECKED: 1,
    CitationCheck.HASH_MISMATCH: 0,
    CitationCheck.INTEGRITY_FAILED: 0,
}


def derive_evidence_status(checks: Sequence[CitationCheck]) -> EvidenceStatus:
    """Map per-citation checks to one documented, rule-based item status.

    The Evidence Agent produces no numeric confidence, so none is invented here.
    - HASH_MISMATCH: any cited chunk changed, or the index failed its integrity check.
    - FULLY_VERIFIED: every cited chunk and its document match what the model saw.
    - PARTIALLY_VERIFIED: some cited chunk text still matches, but not all citations
      fully verify (a document was revised, or another citation is missing/unchecked).
    - UNVERIFIED: no citation could be matched (missing, unchecked, or no receipt).
    """

    if any(CHECK_RANK[check] == 0 for check in checks):
        return EvidenceStatus.HASH_MISMATCH
    if checks and all(check is CitationCheck.VERIFIED for check in checks):
        return EvidenceStatus.FULLY_VERIFIED
    if any(CHECK_RANK[check] >= 2 for check in checks):
        return EvidenceStatus.PARTIALLY_VERIFIED
    return EvidenceStatus.UNVERIFIED


def initial_state(evidence_status: EvidenceStatus) -> ReviewState:
    """Anything not fully verified is routed to NEEDS_REVIEW; nothing starts confirmed."""

    if evidence_status is EvidenceStatus.FULLY_VERIFIED:
        return ReviewState.PENDING_REVIEW
    return ReviewState.NEEDS_REVIEW


def confirm_requirement(state: ReviewState, evidence_status: EvidenceStatus) -> ConfirmRequirement:
    if state not in PENDING_STATES:
        return ConfirmRequirement.BLOCKED
    if evidence_status is EvidenceStatus.HASH_MISMATCH:
        return ConfirmRequirement.BLOCKED
    if evidence_status is EvidenceStatus.FULLY_VERIFIED:
        return ConfirmRequirement.NONE
    return ConfirmRequirement.NOTE_REQUIRED


def allowed_actions(state: ReviewState, evidence_status: EvidenceStatus) -> list[ReviewAction]:
    if state in DECIDED_STATES:
        return [ReviewAction.REOPEN]
    actions = [ReviewAction.CORRECT, ReviewAction.REJECT]
    if confirm_requirement(state, evidence_status) is not ConfirmRequirement.BLOCKED:
        actions.insert(0, ReviewAction.CONFIRM)
    return actions


def plan_transition(
    state: ReviewState,
    action: ReviewAction,
    evidence_status: EvidenceStatus,
    *,
    note: str,
) -> ReviewState:
    """Return the target state or raise; this is the single transition authority."""

    if action is ReviewAction.REOPEN:
        if state not in DECIDED_STATES:
            raise InvalidTransitionError(
                f"cannot reopen an item in {state.value}",
                current_state=state.value,
                action=action.value,
            )
        if not note:
            raise NoteRequiredError("reopening a decided item requires a note")
        return initial_state(evidence_status)

    target = TRANSITIONS.get((state, action))
    if target is None:
        raise InvalidTransitionError(
            f"{action.value} is not allowed from {state.value}",
            current_state=state.value,
            action=action.value,
        )
    if action is ReviewAction.CONFIRM:
        requirement = confirm_requirement(state, evidence_status)
        if requirement is ConfirmRequirement.BLOCKED:
            raise ConfirmBlockedError(
                "a cited source changed after the model saw it; correct or reject instead",
                evidence_status=evidence_status.value,
            )
        if requirement is ConfirmRequirement.NOTE_REQUIRED and not note:
            raise NoteRequiredError(
                "confirming an item without fully verified evidence requires a note",
                evidence_status=evidence_status.value,
            )
    elif not note:
        raise NoteRequiredError(f"{action.value} requires a reviewer note")
    return target


def queue_priority(state: ReviewState, evidence_status: EvidenceStatus) -> tuple[int, int]:
    """Queue tier: NEEDS_REVIEW, then PENDING_REVIEW, then decided; weakest evidence first."""

    return (_STATE_PRIORITY[state], _EVIDENCE_SEVERITY[evidence_status])


def diff_text(before: str, after: str) -> list[DiffOp]:
    """Word-level diff (per character for CJK) suitable for an inline review view."""

    before_tokens = _DIFF_TOKEN.findall(before)
    after_tokens = _DIFF_TOKEN.findall(after)
    matcher = difflib.SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False)
    ops: list[DiffOp] = []

    def emit(op: str, tokens: Sequence[str]) -> None:
        text = "".join(tokens)
        if not text:
            return
        if ops and ops[-1].op == op:
            ops[-1] = DiffOp(op=ops[-1].op, text=ops[-1].text + text)
            return
        ops.append(DiffOp.model_validate({"op": op, "text": text}))

    for tag, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if tag == "equal":
            emit("equal", before_tokens[a_start:a_end])
            continue
        if tag in ("delete", "replace"):
            emit("delete", before_tokens[a_start:a_end])
        if tag in ("insert", "replace"):
            emit("insert", after_tokens[b_start:b_end])
    return ops
