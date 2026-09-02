"""Durable Career application records linked to Resume runs and Learning cases.

One canonical owner per application: the private application bundle created by
the Resume workspace. This module reads, lists, transitions, and links those
records without ever inventing an external application result.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from soloscale.casebook_store import CasebookStore
from soloscale.models import ContractModel, utc_now

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]
StableId = Annotated[
    str,
    Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]

_LEGACY_DRAFT = "DRAFT_REQUIRES_HUMAN_REVIEW"


class ApplicationStatus(StrEnum):
    DRAFT = "DRAFT"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ApplicationRecord(ContractModel):
    application_id: StableId
    directory_name: NonBlankStr
    company: str | None = None
    role: str | None = None
    job_id: str | None = None
    source_url: str | None = None
    captured_at: str | None = None
    soloscale_run_id: StableId
    resume_filename: str | None = None
    status: ApplicationStatus = ApplicationStatus.DRAFT
    updated_at: datetime = Field(default_factory=utc_now)
    next_action: str | None = None
    learning_case_id: str | None = None
    interview_ready: bool | None = None
    notes: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_learning_link(self) -> ApplicationRecord:
        if self.interview_ready is not None and self.learning_case_id is None:
            raise ValueError("interview_ready requires a linked learning case")
        return self


def applications_root(library_root: Path) -> Path:
    return Path(library_root) / "applications"


def load_application_record(application_dir: Path) -> ApplicationRecord:
    """Load one application bundle, migrating the legacy fixed status."""

    payload_path = Path(application_dir) / "application.json"
    if not payload_path.is_file() or payload_path.is_symlink():
        raise ValueError(f"application bundle is missing application.json: {application_dir}")
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("application.json must be a JSON object")
    raw_status = raw.get("status", _LEGACY_DRAFT)
    if isinstance(raw_status, str) and raw_status == _LEGACY_DRAFT:
        raw_status = ApplicationStatus.DRAFT.value
    try:
        status = ApplicationStatus(raw_status)
    except ValueError as exc:
        raise ValueError(f"application status is invalid: {raw_status}") from exc
    run_id = str(raw.get("soloscale_run_id") or "")
    if not run_id:
        raise ValueError("application bundle is missing soloscale_run_id")
    record = ApplicationRecord(
        application_id=run_id,
        directory_name=Path(application_dir).name,
        company=_optional_text(raw.get("company")),
        role=_optional_text(raw.get("role")),
        job_id=_optional_text(raw.get("job_id")),
        source_url=_optional_text(raw.get("source_url")),
        captured_at=_optional_text(raw.get("captured_at")),
        soloscale_run_id=run_id,
        resume_filename=_optional_text(raw.get("resume_filename")),
        status=status,
        next_action=_optional_text(raw.get("next_action")),
        learning_case_id=_optional_text(raw.get("learning_case_id")),
        interview_ready=(
            raw.get("interview_ready")
            if isinstance(raw.get("interview_ready"), bool)
            else None
        ),
        notes=[
            str(item)
            for item in raw.get("notes", [])
            if isinstance(item, str) and item.strip()
        ],
    )
    return record


def list_application_records(library_root: Path) -> list[ApplicationRecord]:
    root = applications_root(library_root)
    if not root.is_dir():
        return []
    records: list[ApplicationRecord] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
            continue
        try:
            records.append(load_application_record(directory))
        except (ValueError, OSError):
            continue
    return records


def update_application_status(
    *,
    application_dir: Path,
    status: ApplicationStatus,
    note: str | None = None,
    next_action: str | None = None,
) -> ApplicationRecord:
    """Record one operator-confirmed application outcome.

    Status is always operator-set after a real external event; this module never
    invents APPLIED/INTERVIEW/OFFER/REJECTED by itself.
    """

    record = load_application_record(application_dir)
    payload_path = Path(application_dir) / "application.json"
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    notes = [str(item) for item in raw.get("notes", []) if isinstance(item, str) and item.strip()]
    if note and note.strip():
        notes.append(note.strip())
    updated = record.model_copy(
        update={
            "status": ApplicationStatus(status),
            "updated_at": utc_now(),
            "next_action": next_action.strip() if next_action else None,
            "notes": notes,
        }
    )
    raw.update(
        {
            "status": updated.status.value,
            "updated_at": updated.updated_at.isoformat(),
            "next_action": updated.next_action,
            "notes": notes,
        }
    )
    _atomic_json(payload_path, raw)
    return updated


def link_learning_case(
    *,
    application_dir: Path,
    learning_case_id: str,
    data_root: Path,
) -> ApplicationRecord:
    """Link one application to a real Learning case and refresh readiness truth."""

    store = CasebookStore(data_root)
    store.load_case(learning_case_id)
    mastery = store.mastery(learning_case_id)
    record = load_application_record(application_dir)
    payload_path = Path(application_dir) / "application.json"
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    updated = record.model_copy(
        update={
            "learning_case_id": learning_case_id,
            "interview_ready": mastery.interview_ready,
            "updated_at": utc_now(),
        }
    )
    raw.update(
        {
            "learning_case_id": learning_case_id,
            "interview_ready": mastery.interview_ready,
            "updated_at": updated.updated_at.isoformat(),
        }
    )
    _atomic_json(payload_path, raw)
    return updated


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
