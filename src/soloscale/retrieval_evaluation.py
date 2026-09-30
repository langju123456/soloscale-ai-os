"""Offline, reproducible reports for SoloScale's existing retrieval fixtures.

The reports are operator evidence, not a general benchmark framework.  They deliberately
exercise only the current synthetic Conversation fixture and canonical Resume fact ranking.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore
from soloscale.resume_evidence_pack import _compact_verified_facts, build_candidate_evidence_pack
from soloscale.resume_models import CandidateProfile

TOP_K = 5
RESUME_TOP_K = 10
_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


class RetrievalEvaluationError(ValueError):
    """Raised when a local evaluation fixture or comparison is invalid."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _fixture(name: str) -> tuple[dict[str, Any], str]:
    path = _FIXTURES / name
    try:
        raw = path.read_bytes()
        loaded = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalEvaluationError(f"invalid fixture {name}") from exc
    if not isinstance(loaded, (dict, list)):
        raise RetrievalEvaluationError(f"invalid fixture {name}: expected object or list")
    return cast(dict[str, Any], loaded), _sha256_bytes(raw)


def _render(value: str, *, index: int = 0, ordinal: int = 0) -> str:
    return value.format(index=index, ordinal=ordinal)


def _sources(fixture: dict[str, Any]) -> list[ParsedSource]:
    if fixture.get("schema_version") != 1 or not isinstance(fixture.get("documents"), list):
        raise RetrievalEvaluationError("invalid conversation fixture schema")
    specs = list(fixture["documents"])
    for family in fixture.get("document_families", []):
        if not isinstance(family, dict) or not isinstance(family.get("count"), int):
            raise RetrievalEvaluationError("invalid conversation document family")
        for index in range(family["count"]):
            specs.append(
                {
                    "id": _render(family["id_template"], index=index),
                    "external_id": _render(family["external_id_template"], index=index),
                    "locator": _render(family["locator_template"], index=index),
                    "source_kind": family["source_kind"],
                    "title": family.get("title"),
                    "aliases": family.get("aliases", ""),
                    "chunks": [
                        {
                            "id": _render(chunk["id_template"], index=index),
                            "role": chunk["role"],
                            "text": _render(chunk["text_template"], index=index),
                            "metadata": chunk.get("metadata", {}),
                        }
                        for chunk in family["chunks"]
                    ],
                }
            )
    result: list[ParsedSource] = []
    for offset, spec in enumerate(specs):
        if not isinstance(spec, dict) or not isinstance(spec.get("chunks"), list):
            raise RetrievalEvaluationError("invalid conversation document")
        chunks: list[dict[str, Any]] = []
        ordinal = 0
        for chunk in spec["chunks"]:
            if not isinstance(chunk, dict):
                raise RetrievalEvaluationError("invalid conversation chunk")
            for _ in range(chunk.get("repeat", 1)):
                try:
                    chunks.append(
                        {
                            "id": chunk.get("id") or _render(chunk["id_template"], ordinal=ordinal),
                            "text": chunk.get("text")
                            or _render(chunk["text_template"], ordinal=ordinal),
                            "role": chunk["role"],
                            "metadata": {
                                k: _render(v, ordinal=ordinal)
                                for k, v in chunk.get("metadata", {}).items()
                            },
                            "ordinal": ordinal,
                        }
                    )
                except (KeyError, AttributeError) as exc:
                    raise RetrievalEvaluationError("invalid conversation chunk") from exc
                ordinal += 1
        body = "\n".join(cast(str, item["text"]) for item in chunks)
        observed = datetime(2026, 8, 9, tzinfo=UTC) + timedelta(seconds=offset)
        try:
            document = NormalizedDocument(
                id=spec["id"],
                source_kind=SourceKind(spec["source_kind"]),
                external_id=spec["external_id"],
                locator=spec["locator"],
                title=spec.get("title"),
                content_sha256=_sha256_bytes(body.encode()),
                byte_size=len(body.encode()),
                observed_at=observed,
                metadata={"aliases": spec.get("aliases", ""), "dataset": "synthetic-rag-eval-v1"},
            )
            normalized = [
                NormalizedChunk(
                    id=item["id"],
                    document_id=document.id,
                    ordinal=item["ordinal"],
                    role=ContentRole(item["role"]),
                    timestamp=observed + timedelta(milliseconds=item["ordinal"]),
                    text=item["text"],
                    text_sha256=_sha256_bytes(item["text"].encode()),
                    metadata=item["metadata"],
                )
                for item in chunks
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise RetrievalEvaluationError("invalid conversation document labels") from exc
        result.append(ParsedSource(document=document, chunks=normalized))
    return result


def _rank_metrics(rows: list[dict[str, object]], *, top_k: int) -> dict[str, float | int]:
    scored = [row for row in rows if not row["expect_empty"]]
    if not scored:
        return {"evaluated_queries": 0, f"recall_at_{top_k}": 0.0, f"mrr_at_{top_k}": 0.0}
    recalls: list[float] = []
    reciprocal: list[float] = []
    for row in scored:
        relevant = set(cast(list[str], row["expected_ids"]))
        ranked = cast(list[str], row["ranked_ids"])[:top_k]
        recalls.append(len(relevant & set(ranked)) / len(relevant))
        first = next((rank for rank, item in enumerate(ranked, 1) if item in relevant), None)
        reciprocal.append(0.0 if first is None else 1.0 / first)
    return {
        "evaluated_queries": len(scored),
        f"recall_at_{top_k}": sum(recalls) / len(recalls),
        f"mrr_at_{top_k}": sum(reciprocal) / len(reciprocal),
    }


def _git_text(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _provenance() -> dict[str, object]:
    repo = Path(__file__).resolve().parents[2]
    paths = [
        repo / "src/soloscale/knowledge_store.py",
        repo / "src/soloscale/resume_evidence_pack.py",
        Path(__file__).resolve(),
    ]
    hashes = {
        str(path.relative_to(repo)): _sha256_bytes(path.read_bytes())
        for path in paths
        if path.exists()
    }
    return {
        "source_head": _git_text("-C", str(repo), "rev-parse", "HEAD"),
        "source_dirty": bool(_git_text("-C", str(repo), "status", "--porcelain")),
        "source_hashes": hashes,
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "model_calls": 0,
        },
    }


def _conversation_report() -> dict[str, object]:
    fixture, fixture_hash = _fixture("conversation_rag_eval.json")
    sources = _sources(fixture)
    corpus_hash = _sha256_json([source.model_dump(mode="json") for source in sources])
    known_ids = {chunk.id for source in sources for chunk in source.chunks}
    for query in fixture.get("queries", []):
        if not isinstance(query, dict) or not isinstance(query.get("relevant_chunk_ids"), list):
            raise RetrievalEvaluationError("invalid conversation query labels")
        if not query.get("expect_empty") and not query["relevant_chunk_ids"]:
            raise RetrievalEvaluationError("invalid conversation query labels: empty relevant ids")
        if not set(query["relevant_chunk_ids"]).issubset(known_ids):
            raise RetrievalEvaluationError("invalid conversation query labels: unknown relevant id")
    for case in fixture.get("context_cases", []):
        labels = (
            (
                case.get("primary_chunk_id"),
                *case.get("required_context_ids", []),
                *case.get("forbidden_context_ids", []),
            )
            if isinstance(case, dict)
            else ()
        )
        if not labels or not set(labels).issubset(known_ids):
            raise RetrievalEvaluationError("invalid conversation context labels: unknown id")
    with tempfile.TemporaryDirectory(prefix="soloscale-retrieval-eval-") as temp:
        first = KnowledgeStore(Path(temp) / "first" / ".soloscale")
        second = KnowledgeStore(Path(temp) / "second" / ".soloscale")
        if first.sync(sources).failed or second.sync(sources).failed:
            raise RetrievalEvaluationError("synthetic conversation corpus failed to sync")
        rows: list[dict[str, object]] = []
        latencies: list[float] = []
        for query in fixture.get("queries", []):
            if not isinstance(query, dict) or not isinstance(query.get("relevant_chunk_ids"), list):
                raise RetrievalEvaluationError("invalid conversation query labels")
            runs: list[list[str]] = []
            for store in (first, first, second):
                started = perf_counter()
                ids = [hit.chunk_id for hit in store.search(query["query"], limit=TOP_K)]
                latencies.append((perf_counter() - started) * 1000)
                runs.append(ids)
            stable = runs[0] == runs[1] == runs[2]
            expected = query["relevant_chunk_ids"]
            empty = bool(query.get("expect_empty"))
            rows.append(
                {
                    "id": query.get("id"),
                    "query": query.get("query"),
                    "ranked_ids": runs[0],
                    "expected_ids": expected,
                    "missing_ids": sorted(set(expected) - set(runs[0])),
                    "first_relevant_rank": next(
                        (i for i, item in enumerate(runs[0], 1) if item in expected), None
                    ),
                    "expect_empty": empty,
                    "stable_repeated_and_rebuilt": stable,
                }
            )
        contexts: list[dict[str, object]] = []
        for case in fixture.get("context_cases", []):
            started = perf_counter()
            primary = [hit.chunk_id for hit in first.search(case["query"], limit=TOP_K)]
            latencies.append((perf_counter() - started) * 1000)
            primary_found = case["primary_chunk_id"] in primary
            neighbors = (
                [hit.chunk_id for hit in first.get_neighbors([case["primary_chunk_id"]])]
                if primary_found
                else []
            )
            required, forbidden = (
                set(case["required_context_ids"]),
                set(case["forbidden_context_ids"]),
            )
            contexts.append(
                {
                    "id": case["id"],
                    "neighbor_ids": neighbors,
                    "required_ids": sorted(required),
                    "forbidden_ids": sorted(forbidden),
                    "missing_required_ids": sorted(required - set(neighbors)),
                    "returned_forbidden_ids": sorted(forbidden & set(neighbors)),
                    "primary_found": primary_found,
                }
            )
    metrics = cast(dict[str, object], _rank_metrics(rows, top_k=TOP_K))
    required_count = sum(len(cast(list[str], row["required_ids"])) for row in contexts)
    recovered = sum(
        len(cast(list[str], row["required_ids"]))
        - len(cast(list[str], row["missing_required_ids"]))
        for row in contexts
    )
    returned_count = sum(len(cast(list[str], row["neighbor_ids"])) for row in contexts)
    forbidden_count = sum(len(cast(list[str], row["returned_forbidden_ids"])) for row in contexts)
    metrics.update(
        {
            "context_recall_store_neighbors_only": recovered / required_count
            if required_count
            else 0.0,
            "forbidden_context_precision_store_neighbors_only": (returned_count - forbidden_count)
            / returned_count
            if returned_count
            else 1.0,
            "empty_unanswerable_queries": sum(bool(row["expect_empty"]) for row in rows),
            "latency_ms": {
                "n": len(latencies),
                "median": statistics.median(latencies),
                "max": max(latencies),
            },
        }
    )
    guards = fixture.get("guards", {})
    with tempfile.TemporaryDirectory(prefix="soloscale-retrieval-eval-guard-") as temp:
        guard_store = KnowledgeStore(Path(temp) / ".soloscale")
        if guard_store.sync(sources).failed:
            raise RetrievalEvaluationError("synthetic conversation corpus failed to sync")
        title_hits = guard_store.search(guards.get("title_query", ""), limit=TOP_K)
        duplicate_hits = guard_store.search(guards.get("duplicate_query", ""), limit=TOP_K)
    duplicate_hash = _sha256_bytes(str(guards.get("duplicate_text", "")).encode())
    thresholds = fixture.get("thresholds", {})
    gates = {
        "stable_rankings": all(bool(row["stable_repeated_and_rebuilt"]) for row in rows),
        "empty_cases_return_empty": all(
            not row["expect_empty"] or not row["ranked_ids"] for row in rows
        ),
        "context_primary_found": all(bool(row["primary_found"]) for row in contexts),
        "title_guard": sum(hit.document_id == guards.get("title_document_id") for hit in title_hits)
        <= guards.get("max_title_document_hits_at_5", 0),
        "duplicate_guard": sum(hit.chunk_sha256 == duplicate_hash for hit in duplicate_hits)
        <= guards.get("max_duplicate_text_hits_at_5", 0)
        and guards.get("required_unique_duplicate_chunk_id")
        in {hit.chunk_id for hit in duplicate_hits},
        "recall_threshold": metrics["recall_at_5"] >= thresholds.get("recall_at_5", 1.0),
        "mrr_threshold": metrics["mrr_at_5"] >= thresholds.get("mrr", 1.0),
        "context_recall_threshold": metrics["context_recall_store_neighbors_only"]
        >= thresholds.get("context_recall", 1.0),
        "forbidden_context_threshold": metrics["forbidden_context_precision_store_neighbors_only"]
        >= thresholds.get("forbidden_context_precision", 1.0),
        "latency_threshold": cast(dict[str, float], metrics["latency_ms"])["max"]
        < thresholds.get("max_search_latency_ms", float("inf")),
    }
    return {
        "dataset": "conversation_rag",
        "description": (
            "KnowledgeStore deterministic retrieval; context results are store-neighbor-only."
        ),
        "top_k": TOP_K,
        "dataset_sha256": fixture_hash,
        "corpus_sha256": corpus_hash,
        "corpus": {
            "documents": len(sources),
            "chunks": sum(len(source.chunks) for source in sources),
        },
        "queries": rows,
        "context_cases": contexts,
        "metrics": metrics,
        "gates": gates,
        "limitations": [
            fixture.get("limitations", {}).get("note", ""),
            (
                "Fixed curated synthetic developer regression data; "
                "not held-out or generalization evidence."
            ),
            "No semantic faithfulness or answer correctness evaluation.",
        ],
    }


def _resume_report() -> dict[str, object]:
    cases, fixture_hash = _fixture("resume_retrieval_eval.json")
    if not isinstance(cases, list):
        raise RetrievalEvaluationError("invalid resume fixture schema")
    profile = CandidateProfile(project_bullets=["Built SoloScale evidence-grounded RAG workflows."])
    facts = build_candidate_evidence_pack(profile).atomic_facts
    known_evidence_ids = {
        fact.evidence_id for fact in facts if fact.source_kind == "CANDIDATE_EVIDENCE"
    }
    rows: list[dict[str, object]] = []
    latencies: list[float] = []
    for index, case in enumerate(cases):
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("query"), str)
            or not isinstance(case.get("relevant_evidence_ids"), list)
            or not case["relevant_evidence_ids"]
        ):
            raise RetrievalEvaluationError("invalid resume query labels")
        if not set(case["relevant_evidence_ids"]).issubset(known_evidence_ids):
            raise RetrievalEvaluationError("invalid resume query labels: unknown relevant id")
        started = perf_counter()
        ranked = _compact_verified_facts(job_description=case["query"], atomic_facts=facts)
        latencies.append((perf_counter() - started) * 1000)
        ids = list(
            dict.fromkeys(
                fact.evidence_id for fact in ranked if fact.source_kind == "CANDIDATE_EVIDENCE"
            )
        )
        expected = case["relevant_evidence_ids"]
        rows.append(
            {
                "id": f"resume-{index + 1:02d}",
                "query": case["query"],
                "ranked_ids": ids,
                "expected_ids": expected,
                "missing_ids_at_5": sorted(set(expected) - set(ids[:TOP_K])),
                "missing_ids_at_10": sorted(set(expected) - set(ids[:RESUME_TOP_K])),
                "first_relevant_rank": next(
                    (i for i, item in enumerate(ids, 1) if item in expected), None
                ),
                "expect_empty": False,
            }
        )
    metrics: dict[str, object] = _rank_metrics(rows, top_k=TOP_K)
    metrics.update(_rank_metrics(rows, top_k=RESUME_TOP_K))
    full_reciprocal_ranks = [
        0.0 if row["first_relevant_rank"] is None else 1.0 / int(row["first_relevant_rank"])
        for row in rows
    ]
    metrics["mrr_unbounded"] = sum(full_reciprocal_ranks) / len(full_reciprocal_ranks)
    metrics["latency_ms"] = {
        "n": len(latencies),
        "median": statistics.median(latencies),
        "max": max(latencies),
    }
    corpus = [fact.model_dump(mode="json") for fact in facts]
    return {
        "dataset": "resume_canonical_fact_ranking",
        "description": (
            "Canonical CandidateProfile fact ranking/compaction; "
            "not KnowledgeStore or end-to-end RAG."
        ),
        "top_k": {"primary": TOP_K, "resume": RESUME_TOP_K},
        "dataset_sha256": fixture_hash,
        "corpus_sha256": _sha256_json(corpus),
        "corpus": {
            "atomic_facts": len(facts),
            "candidate_evidence_ids": len(
                {fact.evidence_id for fact in facts if fact.source_kind == "CANDIDATE_EVIDENCE"}
            ),
        },
        "queries": rows,
        "metrics": metrics,
        "gates": {
            "recall_at_5_baseline": metrics["recall_at_5"] >= 0.75,
            "recall_at_10_baseline": metrics["recall_at_10"] >= 0.85,
            "mrr_unbounded_baseline": metrics["mrr_unbounded"] >= 0.65,
        },
        "limitations": [
            (
                "Fixed curated developer regression data: 10 queries over five canonical evidence "
                "IDs; not held-out or generalization evidence."
            ),
            (
                "No negative or unanswerable cases, semantic faithfulness, "
                "or answer correctness evaluation."
            ),
        ],
    }


def _comparison_queries(report: dict[str, object]) -> list[dict[str, object]]:
    queries = report.get("queries")
    if not isinstance(queries, list) or any(
        not isinstance(row, dict) or "id" not in row or "ranked_ids" not in row for row in queries
    ):
        raise RetrievalEvaluationError("invalid comparison report")
    return cast(list[dict[str, object]], queries)


def _comparison_metrics(report: dict[str, object]) -> dict[str, object]:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise RetrievalEvaluationError("invalid comparison report")
    return cast(dict[str, object], metrics)


def _metric_deltas(current: dict[str, object], previous: dict[str, object]) -> dict[str, object]:
    deltas: dict[str, object] = {}
    for key in sorted(current.keys() & previous.keys()):
        new_value, old_value = current[key], previous[key]
        if isinstance(new_value, dict) and isinstance(old_value, dict):
            nested = _metric_deltas(
                cast(dict[str, object], new_value), cast(dict[str, object], old_value)
            )
            if nested:
                deltas[key] = nested
        elif (
            isinstance(new_value, (int, float))
            and not isinstance(new_value, bool)
            and isinstance(old_value, (int, float))
            and not isinstance(old_value, bool)
        ):
            deltas[key] = new_value - old_value
    return deltas


def compare_reports(current: dict[str, object], previous: dict[str, object]) -> dict[str, object]:
    for key in ("dataset", "dataset_sha256", "corpus_sha256", "top_k"):
        if current.get(key) != previous.get(key):
            raise RetrievalEvaluationError(f"incompatible report: {key} differs")
    current_queries = _comparison_queries(current)
    previous_queries = _comparison_queries(previous)
    old_queries = {
        str(row["id"]): row for row in previous_queries
    }
    changes = [
        {
            "id": row["id"],
            "previous_ranked_ids": old_queries.get(str(row["id"]), {}).get("ranked_ids"),
            "ranked_ids": row["ranked_ids"],
        }
        for row in current_queries
        if old_queries.get(str(row["id"]), {}).get("ranked_ids") != row["ranked_ids"]
    ]
    old_metrics, new_metrics = _comparison_metrics(previous), _comparison_metrics(current)
    deltas = _metric_deltas(new_metrics, old_metrics)
    return {"compatible": True, "metric_deltas": deltas, "changed_rankings": changes}


def _markdown(run: dict[str, object]) -> str:
    lines = ["# Offline retrieval evaluation", ""]
    provenance = cast(dict[str, object], run["provenance"])
    lines += [
        "## Run configuration",
        "",
        f"- source HEAD: `{provenance['source_head']}`",
        f"- source dirty: `{provenance['source_dirty']}`",
        f"- runtime: `{json.dumps(provenance['runtime'])}`",
    ]
    for report in cast(list[dict[str, object]], run["reports"]):
        lines += [
            "",
            f"## {report['dataset']}",
            "",
            str(report["description"]),
            "",
            "### Corpus and configuration",
            "",
            f"- dataset SHA-256: `{report['dataset_sha256']}`",
            f"- corpus SHA-256: `{report['corpus_sha256']}`",
            f"- corpus: `{json.dumps(report['corpus'])}`",
            f"- top-k: `{json.dumps(report['top_k'])}`",
            "",
            "### Metrics",
            "",
        ]
        lines += [
            f"- `{key}`: `{json.dumps(value)}`"
            for key, value in cast(dict[str, object], report["metrics"]).items()
        ]
        lines += ["", "### Gates", ""]
        lines += [
            f"- `{key}`: `{value}`" for key, value in cast(dict[str, bool], report["gates"]).items()
        ]
        lines += ["", "### Per-query rankings", ""]
        for row in cast(list[dict[str, object]], report["queries"]):
            missing = row.get("missing_ids", row.get("missing_ids_at_10", []))
            missing_at_5 = row.get("missing_ids_at_5")
            lines.append(
                f"- `{row['id']}`: ranked={row['ranked_ids']}; "
                f"expected={row['expected_ids']}; missing={missing}; "
                f"missing_at_5={missing_at_5}; "
                f"first_relevant_rank={row['first_relevant_rank']}"
            )
        lines += ["", "### Limitations", ""] + [
            f"- {item}" for item in cast(list[str], report["limitations"])
        ]
    if "comparison" in run:
        lines += [
            "",
            "## Comparison",
            "",
            f"```json\n{json.dumps(run['comparison'], ensure_ascii=False, indent=2)}\n```",
        ]
    return "\n".join(lines) + "\n"


def run_evaluations(output_root: Path, *, compare: Path | None = None) -> Path:
    output_root = Path(output_root)
    if not output_root.exists():
        output_root.mkdir(parents=True, mode=0o700)
        os.chmod(output_root, 0o700)
    reports = [_conversation_report(), _resume_report()]
    run: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance": _provenance(),
        "reports": reports,
    }
    if compare:
        try:
            compare_path = Path(compare)
            previous_path = compare_path / "report.json" if compare_path.is_dir() else compare_path
            previous_run = json.loads(previous_path.read_text(encoding="utf-8"))
            previous = {str(item["dataset"]): item for item in previous_run["reports"]}
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RetrievalEvaluationError("invalid comparison report") from exc
        comparisons = {}
        for report in reports:
            old = previous.get(str(report["dataset"]))
            if old is None:
                raise RetrievalEvaluationError(
                    f"incompatible report: missing dataset {report['dataset']}"
                )
            comparisons[str(report["dataset"])] = compare_reports(report, old)
        run["comparison"] = comparisons
    run["passed"] = all(all(cast(dict[str, bool], report["gates"]).values()) for report in reports)
    output = Path(tempfile.mkdtemp(prefix="retrieval-eval-", dir=output_root))
    os.chmod(output, 0o700)
    for name, content in (
        ("report.json", json.dumps(run, ensure_ascii=False, indent=2) + "\n"),
        ("report.md", _markdown(run)),
    ):
        path = output / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
    return output
