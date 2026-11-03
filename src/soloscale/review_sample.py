"""Build a clearly labeled SAMPLE Evidence Agent run for the Review Workbench demo.

The sample uses the real pipeline end to end: a real private ``KnowledgeStore`` is synced
with repo-owned synthetic documents, the real ``BoundedEvidenceAgent`` runs its bounded
retrieval loop and writes its normal receipts, and only the language model is replaced by
a scripted reasoner whose answers come from the fixture file.  Afterwards three sources
are edited in the index to reproduce evidence drifting between drafting and review.

Nothing here is presented as a real user's history: the question, model name and every
document title carry a SAMPLE/fixture label.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from soloscale.evidence_agent import AgentRunResult, BoundedEvidenceAgent, ResponseModelT
from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore

SAMPLE_FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "review_workbench_sample.json"
)
SAMPLE_LABEL = "[SAMPLE FIXTURE]"


class FixtureReasoner:
    """Returns pre-written structured outputs in order; it is not a language model."""

    def __init__(self, model: str, responses: Sequence[dict[str, Any]]) -> None:
        self.model = model
        self._responses = list(responses)

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
    ) -> ResponseModelT:
        del system, user
        if not self._responses:
            raise RuntimeError("fixture reasoner has no scripted response left")
        return schema.model_validate(self._responses.pop(0))


class SampleRun(BaseModel):
    run_id: str
    data_root: str
    claim_count: int
    edited_documents: list[str]
    provenance: str


def load_fixture(path: Path = SAMPLE_FIXTURE) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
        raise ValueError("review sample fixture has an unsupported shape")
    if SAMPLE_LABEL not in str(loaded.get("question", "")):
        raise ValueError("review sample question must carry the sample label")
    return cast(dict[str, Any], loaded)


def build_sample_run(data_root: Path, fixture_path: Path = SAMPLE_FIXTURE) -> SampleRun:
    """Sync sample sources, run the real agent with scripted answers, then drift sources."""

    fixture = load_fixture(fixture_path)
    store = KnowledgeStore(Path(data_root))
    documents = cast(list[dict[str, Any]], fixture["documents"])
    report = store.sync([_parsed_source(spec, offset) for offset, spec in enumerate(documents)])
    if report.failed:
        raise RuntimeError(f"sample sync failed: {[f.code for f in report.failures]}")

    reasoner = FixtureReasoner(
        str(fixture["reasoner_model"]),
        cast(list[dict[str, Any]], fixture["reasoner_responses"]),
    )
    agent = BoundedEvidenceAgent(store, reasoner, Path(data_root), max_rounds=1)
    result: AgentRunResult = agent.run(str(fixture["question"]))

    edits = cast(dict[str, Any], fixture["post_draft_source_edits"])
    by_id = {str(spec["id"]): spec for spec in documents}
    edited: list[ParsedSource] = []
    for offset, edit in enumerate(cast(list[dict[str, Any]], edits["documents"])):
        base = dict(by_id[str(edit["id"])])
        base["chunks"] = edit["chunks"]
        edited.append(_parsed_source(base, len(documents) + offset))
    edit_report = store.sync(edited)
    if edit_report.failed:
        raise RuntimeError(f"sample edit failed: {[f.code for f in edit_report.failures]}")
    return SampleRun(
        run_id=result.run_id,
        data_root=str(Path(data_root)),
        claim_count=len(result.claims),
        edited_documents=[source.document.id for source in edited],
        provenance=str(fixture["provenance"]),
    )


def _parsed_source(spec: dict[str, Any], observed_offset: int) -> ParsedSource:
    chunks = cast(list[dict[str, str]], spec["chunks"])
    body = "\n".join(chunk["text"] for chunk in chunks)
    observed_at = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=observed_offset)
    document = NormalizedDocument(
        id=str(spec["id"]),
        source_kind=SourceKind(str(spec["source_kind"])),
        external_id=str(spec["external_id"]),
        locator=str(spec["locator"]),
        title=str(spec["title"]),
        content_sha256=_sha256(body),
        byte_size=len(body.encode("utf-8")),
        observed_at=observed_at,
        metadata={"dataset": "review-workbench-sample-v1"},
    )
    return ParsedSource(
        document=document,
        chunks=[
            NormalizedChunk(
                id=chunk["id"],
                document_id=document.id,
                ordinal=ordinal,
                role=ContentRole(chunk["role"]),
                timestamp=observed_at + timedelta(milliseconds=ordinal),
                text=chunk["text"],
                text_sha256=_sha256(chunk["text"]),
            )
            for ordinal, chunk in enumerate(chunks)
        ],
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Create a SAMPLE Evidence Agent run for the Review Workbench demo."
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(".soloscale/review-demo"),
        help="Ignored private data root for the sample (default .soloscale/review-demo)",
    )
    args = parser.parse_args(argv)
    from soloscale.cli import _validate_private_data_root

    _validate_private_data_root(args.data_root)
    sample = build_sample_run(args.data_root)
    print(sample.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
