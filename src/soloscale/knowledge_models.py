from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from soloscale.models import ContractModel

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]
Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
NonNegativeInt = Annotated[int, Field(ge=0)]


class SourceKind(StrEnum):
    CODEX_SESSION = "codex_session"
    CHATGPT_EXPORT = "chatgpt_export"
    BUILDLOG_RUN = "buildlog_run"


class ContentRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    ARTIFACT = "artifact"


class NormalizedDocument(ContractModel):
    id: NonBlankStr
    source_kind: SourceKind
    external_id: NonBlankStr
    locator: NonBlankStr
    title: str | None = None
    content_sha256: Sha256Digest
    byte_size: NonNegativeInt
    observed_at: datetime | None = None
    parent_external_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("title", "parent_external_id")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("optional text must be nonblank when present")
        return value


class NormalizedChunk(ContractModel):
    id: NonBlankStr
    document_id: NonBlankStr
    ordinal: NonNegativeInt
    role: ContentRole
    timestamp: datetime | None = None
    text: NonBlankStr
    text_sha256: Sha256Digest
    metadata: dict[str, str] = Field(default_factory=dict)


class ParsedSource(ContractModel):
    document: NormalizedDocument
    chunks: list[NormalizedChunk] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_chunks(self) -> ParsedSource:
        chunk_ids = [chunk.id for chunk in self.chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("chunk ids must be unique within a document")
        ordinals = [chunk.ordinal for chunk in self.chunks]
        if ordinals != list(range(len(self.chunks))):
            raise ValueError("chunk ordinals must be contiguous and ordered from zero")
        if any(chunk.document_id != self.document.id for chunk in self.chunks):
            raise ValueError("every chunk must reference its containing document")
        return self


class SourceFailure(ContractModel):
    source_locator: NonBlankStr
    code: NonBlankStr
    source_kind: SourceKind | None = None


class SyncReport(ContractModel):
    discovered: NonNegativeInt = 0
    imported: NonNegativeInt = 0
    updated: NonNegativeInt = 0
    skipped: NonNegativeInt = 0
    failed: NonNegativeInt = 0
    documents: NonNegativeInt = 0
    chunks_written: NonNegativeInt = 0
    failures: list[SourceFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_failure_count(self) -> SyncReport:
        if self.failed != len(self.failures):
            raise ValueError("failed must equal the number of failure receipts")
        return self


class KnowledgeStatus(ContractModel):
    documents: NonNegativeInt
    chunks: NonNegativeInt
    source_counts: dict[str, NonNegativeInt] = Field(default_factory=dict)
    last_synced_at: datetime | None = None


class KnowledgeCatalogDocument(ContractModel):
    """Metadata-only document projection for downstream local catalogs."""

    document_id: NonBlankStr
    native_id: NonBlankStr
    source_kind: SourceKind
    project: str | None = None
    locator: NonBlankStr
    title: str | None = None
    content_sha256: Sha256Digest
    byte_size: NonNegativeInt
    observed_at: datetime | None = None
    metadata_sha256: Sha256Digest
    metadata: dict[str, str] = Field(default_factory=dict)


class KnowledgeCatalogChunk(ContractModel):
    """Metadata-only chunk projection; deliberately has no body or locator."""

    chunk_id: NonBlankStr
    document_id: NonBlankStr
    ordinal: NonNegativeInt
    role: ContentRole
    timestamp: datetime | None = None
    text_sha256: Sha256Digest
    metadata_sha256: Sha256Digest


class KnowledgeCatalogSnapshot(ContractModel):
    documents: list[KnowledgeCatalogDocument] = Field(default_factory=list)
    chunks: list[KnowledgeCatalogChunk] = Field(default_factory=list)


class RetrievalHit(ContractModel):
    chunk_id: NonBlankStr
    document_id: NonBlankStr
    source_kind: SourceKind
    external_id: NonBlankStr
    locator: NonBlankStr
    title: str | None
    matched_metadata: str | None = None
    searchable_metadata_sha256: Sha256Digest | None = None
    role: ContentRole
    timestamp: datetime | None
    excerpt: NonBlankStr
    chunk_sha256: Sha256Digest
    document_sha256: Sha256Digest
    score: float = Field(ge=0)
    channels: list[NonBlankStr] = Field(min_length=1)

    @field_validator("matched_metadata")
    @classmethod
    def validate_optional_matched_metadata(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("matched metadata must be nonblank when present")
        return value

    @field_validator("channels")
    @classmethod
    def validate_unique_channels(cls, channels: list[str]) -> list[str]:
        if len(channels) != len(set(channels)):
            raise ValueError("retrieval channels must be unique")
        return channels
