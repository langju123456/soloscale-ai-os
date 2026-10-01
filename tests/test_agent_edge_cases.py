from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from soloscale.evidence_agent import (
    BoundedEvidenceAgent,
    EvidenceAgentContractError,
    EvidenceAgentTimeoutError,
    OllamaReasoner,
    QueryPlan,
    Reasoner,
    ReasonerTimeoutError,
    ResponseModelT,
)
from soloscale.knowledge_models import ContentRole, RetrievalHit, SourceKind
from soloscale.knowledge_store import KnowledgeStore


class ScriptedReasoner:
    model = "synthetic/scripted"

    def __init__(self, responses: Sequence[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[type[BaseModel], str, str]] = []

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
    ) -> ResponseModelT:
        self.calls.append((schema, system, user))
        return schema.model_validate(self.responses.pop(0))


class SyntheticStore:
    def __init__(
        self,
        results: dict[str, list[RetrievalHit]],
        *,
        citation_timeout: bool = False,
        neighbor_timeout: bool = False,
        search_timeout: bool = False,
    ) -> None:
        self.results = results
        self.citation_timeout = citation_timeout
        self.neighbor_timeout = neighbor_timeout
        self.search_timeout = search_timeout
        self.search_calls = 0
        self.neighbor_calls = 0
        self.citation_calls = 0
        self._hits = {hit.chunk_id: hit for hits in results.values() for hit in hits}

    def search(
        self,
        query: str,
        limit: int = 10,
        source_kinds: Sequence[SourceKind] | None = None,
    ) -> list[RetrievalHit]:
        del limit, source_kinds
        self.search_calls += 1
        if self.search_timeout:
            raise TimeoutError("synthetic search deadline")
        return list(self.results.get(query, []))

    def get_neighbors(self, ids: Sequence[str], *, radius: int = 1) -> list[RetrievalHit]:
        del ids, radius
        self.neighbor_calls += 1
        if self.neighbor_timeout:
            raise TimeoutError("synthetic neighbor deadline")
        return []

    def get_chunks(self, ids: Sequence[str]) -> list[RetrievalHit]:
        self.citation_calls += 1
        if self.citation_timeout:
            raise TimeoutError("synthetic citation deadline")
        return [self._hits[chunk_id] for chunk_id in ids if chunk_id in self._hits]


class FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


def _hit(chunk_id: str, excerpt: str) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        document_id=f"document-{chunk_id}",
        source_kind=SourceKind.CHATGPT_EXPORT,
        external_id=f"synthetic-{chunk_id}",
        locator=f"private://synthetic/{chunk_id}",
        title=f"Synthetic {chunk_id}",
        role=ContentRole.ASSISTANT,
        timestamp=None,
        excerpt=excerpt,
        chunk_sha256=hashlib.sha256(chunk_id.encode("utf-8")).hexdigest(),
        document_sha256=("d" * 64),
        score=1.0,
        channels=["synthetic"],
    )


def _agent(
    tmp_path: Path,
    store: SyntheticStore,
    reasoner: Reasoner,
    **kwargs: Any,
) -> BoundedEvidenceAgent:
    return BoundedEvidenceAgent(
        cast(KnowledgeStore, store), reasoner, tmp_path / ".soloscale", **kwargs
    )


def _failure(run_root: Path) -> dict[str, object]:
    run_dir = next((run_root / ".soloscale" / "knowledge" / "agent-runs").iterdir())
    value = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_empty_evidence_abstains_without_grounded_draft_call(tmp_path: Path) -> None:
    reasoner = ScriptedReasoner(
        [
            {"queries": ["missing evidence"]},
            {"finish": True, "additional_queries": [], "limitations": ["No local match."]},
        ]
    )

    result = _agent(tmp_path, SyntheticStore({}), reasoner).run("Was this verified?")

    assert result.claims == []
    assert result.refs == []
    assert result.unsupported == [
        "Information is insufficient: no retrieved evidence fit the grounded-draft context."
    ]
    assert len(reasoner.calls) == 2
    assert reasoner.calls[0][0] is QueryPlan
    assert reasoner.calls[1][0].__name__ == "CoverageDecision"
    assert "No evidence-backed claim" in result.answer


def test_conflicting_sources_remain_attributed_for_human_review(tmp_path: Path) -> None:
    first = _hit("chunk-a", "Release evidence says the migration completed.")
    second = _hit("chunk-b", "Incident evidence says the migration remains incomplete.")
    reasoner = ScriptedReasoner(
        [
            {"queries": ["migration status"]},
            {"finish": True, "additional_queries": [], "limitations": []},
            {
                "claims": [
                    {
                        "text": "Release evidence says the migration completed.",
                        "evidence_chunk_ids": ["chunk-a"],
                    },
                    {
                        "text": "Incident evidence says the migration remains incomplete.",
                        "evidence_chunk_ids": ["chunk-b"],
                    },
                ],
                "unsupported": ["The sources disagree; a human must resolve the migration status."],
                "open_questions": ["Which source is authoritative for the final migration state?"],
                "suggested_case_title": None,
                "suggested_outputs": [],
            },
        ]
    )

    result = _agent(
        tmp_path, SyntheticStore({"migration status": [first, second]}), reasoner
    ).run("Did the migration finish?")

    final_user = json.loads(reasoner.calls[-1][2])
    assert {record["chunk_id"] for record in final_user["evidence_records"]} == {
        "chunk-a",
        "chunk-b",
    }
    assert "conflict" in reasoner.calls[-1][1].lower()
    assert result.status == "CANDIDATE_REQUIRES_HUMAN_CONFIRMATION"
    assert [claim.evidence_chunk_ids for claim in result.claims] == [["chunk-a"], ["chunk-b"]]
    assert [reference.chunk_id for reference in result.refs] == ["chunk-a", "chunk-b"]
    assert [reference.excerpt for reference in result.refs] == [first.excerpt, second.excerpt]
    assert result.unsupported == [
        "The sources disagree; a human must resolve the migration status."
    ]


def test_long_utf8_context_keeps_focused_critical_tail(tmp_path: Path) -> None:
    critical = "关键结论：采用持久队列和幂等键。"
    hit = _hit("chunk-c", ("填" * 1_000) + critical)
    reasoner = ScriptedReasoner(
        [
            {"queries": ["关键结论"]},
            {"finish": True, "additional_queries": [], "limitations": []},
            {
                "claims": [
                    {"text": "采用持久队列和幂等键。", "evidence_chunk_ids": ["chunk-c"]}
                ],
                "unsupported": [],
                "open_questions": [],
                "suggested_case_title": None,
                "suggested_outputs": [],
            },
        ]
    )

    result = _agent(
        tmp_path,
        SyntheticStore({"关键结论": [hit]}),
        reasoner,
        excerpt_byte_budget=120,
        context_byte_budget=700,
    ).run("关键结论是什么？")

    assert result.context_bytes_used <= 700
    assert critical in reasoner.calls[-1][2]
    assert result.refs[0].chunk_id == "chunk-c"


@pytest.mark.parametrize("fault", ["reasoner", "search", "neighbors", "citations"])
def test_timeouts_have_sanitized_receipts_and_no_result(tmp_path: Path, fault: str) -> None:
    if fault == "reasoner":
        class TimeoutReasoner:
            model = "synthetic/timeout"

            def __init__(self) -> None:
                self.calls = 0

            def complete(
                self,
                schema: type[ResponseModelT],
                *,
                system: str,
                user: str,
            ) -> ResponseModelT:
                del schema, system, user
                self.calls += 1
                raise ReasonerTimeoutError("private timeout detail")

        reasoner: Reasoner = TimeoutReasoner()
        store = SyntheticStore({})
    else:
        responses: list[dict[str, Any]] = [
            {"queries": ["timeout evidence"]},
        ]
        if fault == "citations":
            responses.extend(
                [
                    {"finish": True, "additional_queries": [], "limitations": []},
                    {
                        "claims": [
                            {
                                "text": "Synthetic evidence exists.",
                                "evidence_chunk_ids": ["chunk-t"],
                            }
                        ],
                        "unsupported": [],
                        "open_questions": [],
                        "suggested_case_title": None,
                        "suggested_outputs": [],
                    },
                ]
            )
        reasoner = ScriptedReasoner(responses)
        store = SyntheticStore(
            {"timeout evidence": [_hit("chunk-t", "synthetic evidence")]},
            citation_timeout=fault == "citations",
            search_timeout=fault == "search",
            neighbor_timeout=fault == "neighbors",
        )

    with pytest.raises(EvidenceAgentTimeoutError) as caught:
        _agent(tmp_path, store, reasoner).run("Exercise timeout handling")

    failure = _failure(tmp_path)
    run_dir = next((tmp_path / ".soloscale" / "knowledge" / "agent-runs").iterdir())
    assert type(caught.value).__name__ == "EvidenceAgentTimeoutError"
    assert "private" not in str(caught.value)
    assert failure["error_type"] == "EvidenceAgentTimeoutError"
    assert failure["raw_model_response_persisted"] is False
    assert not (run_dir / "04_result.json").exists()
    if fault == "reasoner":
        assert cast(Any, reasoner).calls == 1
    elif fault == "search":
        assert store.search_calls == 1
    elif fault == "neighbors":
        assert store.neighbor_calls == 1
    else:
        assert store.citation_calls == 1


@pytest.mark.parametrize(
    "content",
    ["{not-json", json.dumps({"queries": [7]})],
    ids=["malformed-json", "schema-invalid"],
)
def test_ollama_malformed_output_fails_closed_without_accepted_result(
    tmp_path: Path, content: str
) -> None:
    calls = 0

    def opener(*_: object, **__: object) -> FakeHTTPResponse:
        nonlocal calls
        calls += 1
        return FakeHTTPResponse(json.dumps({"message": {"content": content}}).encode("utf-8"))

    reasoner = OllamaReasoner(opener=opener)
    with pytest.raises(EvidenceAgentContractError, match="invalid structured output"):
        _agent(tmp_path, SyntheticStore({}), reasoner).run("Exercise malformed output")

    failure = _failure(tmp_path)
    run_dir = next((tmp_path / ".soloscale" / "knowledge" / "agent-runs").iterdir())
    assert calls == 1
    assert failure["error_type"] == "EvidenceAgentContractError"
    assert failure["raw_model_response_persisted"] is False
    assert not (run_dir / "04_result.json").exists()
