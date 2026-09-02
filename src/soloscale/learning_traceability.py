from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from soloscale.evidence_capture import capture_assets, capture_outcome
from soloscale.evidence_hub import EvidenceHub, EvidenceHubError, inspect_git_repository
from soloscale.learning_models import (
    ClaimEligibility,
    CodeAnchor,
    ContributionAttribution,
    DistilledInsight,
    EngineeringDecision,
    ImplementedCapability,
    InterviewQuestion,
    KnowledgeGraphEdge,
    KnowledgeGraphNode,
    LearningProjectBinding,
    LearningResponseReceipt,
    LearningTask,
    LearningTraceabilityRun,
    MasteryAction,
    MasteryLevel,
    MasteryState,
    OwnershipConfidence,
    ReasoningArtifact,
    SourceRecord,
    TechnicalConcept,
    TruthStage,
    VerificationAnchor,
)

CASE_ID = "conversation-rag-chunking-retrieval"
DEFAULT_TARGET_REQUIREMENT = (
    "Design and build context, memory, tooling, and retrieval systems for AI products."
)
ARTIFACT_FILES = (
    "00_input.json",
    "01_case.json",
    "02_concepts.json",
    "03_code_anchors.json",
    "04_evidence_graph.json",
    "05_contribution.json",
    "06_mastery.json",
    "07_learning_plan.json",
    "08_interview_questions.json",
    "09_claim_eligibility.json",
    "10_verification.json",
    "run.json",
)
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_LEARNING_RUN_ID_PATTERN = re.compile(
    r"^learning-\d{8}T\d{6}Z-[0-9a-f]{10}$"
)


@dataclass(frozen=True)
class LearningCaseDefinition:
    """Fixture-owned anchors; these are never generic repository requirements."""

    case_id: str
    case_kind: Literal["SEED_CASE", "DOGFOOD_CASE"]
    required_paths: tuple[str, ...]


CONVERSATION_RAG_SEED_CASE = LearningCaseDefinition(
    case_id=CASE_ID,
    case_kind="SEED_CASE",
    required_paths=(
        "docs/conversations/2026-08-06-agent-swarm-to-surface-routing.md",
        "docs/decisions/ADR-0004-private-conversation-rag.md",
        "docs/conversation-rag.md",
        "src/soloscale/conversation_intake.py",
        "src/soloscale/knowledge_store.py",
        "tests/test_conversation_intake.py",
        "tests/test_knowledge_store.py",
    ),
)


class LearningTraceabilityError(Exception):
    """Raised when the bounded case cannot be grounded or stored safely."""


class LearningFixtureAnchorError(LearningTraceabilityError):
    """Raised when a valid project does not satisfy one fixture's own anchors."""

    def __init__(self, case_id: str, missing_paths: tuple[str, ...]) -> None:
        super().__init__(f"learning fixture anchors are unavailable for {case_id}")
        self.case_id = case_id
        self.missing_paths = missing_paths


@dataclass(frozen=True)
class _RepositoryIdentity:
    name: str
    branch: str
    commit: str
    remote: str | None


@dataclass(frozen=True)
class _ResolvedSymbol:
    file: str
    symbol: str
    line_start: int
    line_end: int
    file_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _private_json(path: Path, label: str) -> dict[str, object]:
    """Read one private artifact without following links or accepting malformed JSON."""
    if path.is_symlink() or not path.is_file():
        raise LearningTraceabilityError(f"{label} is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LearningTraceabilityError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise LearningTraceabilityError(f"{label} is invalid")
    return payload


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _git(repository_root: Path, *args: str, required: bool = True) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    if not required:
        return None
    raise LearningTraceabilityError(
        f"repository identity command failed: git {' '.join(args)}"
    )


def _git_bytes(repository_root: Path, *args: str) -> bytes | None:
    completed = subprocess.run(
        ["git", *args], cwd=repository_root, capture_output=True, check=False
    )
    return completed.stdout if completed.returncode == 0 else None


def _repository_identity(repository_root: Path) -> _RepositoryIdentity:
    top_level = _git(repository_root, "rev-parse", "--show-toplevel")
    if top_level is None or Path(top_level).resolve() != repository_root.resolve():
        raise LearningTraceabilityError("repository_root is not the selected Git worktree")
    branch = _git(repository_root, "branch", "--show-current")
    commit = _git(repository_root, "rev-parse", "HEAD")
    if not commit:
        raise LearningTraceabilityError("a repository commit is required")
    if not branch:
        github_ref = os.environ.get("GITHUB_REF", "")
        github_sha = os.environ.get("GITHUB_SHA", "")
        if (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and github_sha == commit
            and github_ref.startswith("refs/")
        ):
            branch = github_ref.removeprefix("refs/")
        else:
            raise LearningTraceabilityError(
                "a named branch or verified GitHub Actions ref is required"
            )
    remote = _git(repository_root, "remote", "get-url", "origin", required=False)
    remote_tail = (remote or "").rstrip("/").rsplit("/", maxsplit=1)[-1]
    remote_tail = remote_tail.rsplit(":", maxsplit=1)[-1]
    repository_name = remote_tail.removesuffix(".git") or repository_root.name
    return _RepositoryIdentity(
        name=repository_name,
        branch=branch,
        commit=commit,
        remote=remote or None,
    )


def inspect_learning_project(repository_root: Path) -> LearningProjectBinding:
    """Resolve one selected Git project through the canonical Evidence source adapter."""

    root = Path(repository_root).expanduser().resolve()
    identity = _repository_identity(root)
    try:
        source, _items = inspect_git_repository(root)
    except EvidenceHubError as exc:
        raise LearningTraceabilityError("selected project evidence is unavailable") from exc
    return LearningProjectBinding(
        project_source_id=source.source_id,
        project=source.project or identity.name,
        repository=identity.name,
        branch=identity.branch,
        commit=identity.commit,
    )


def missing_learning_case_anchors(
    repository_root: Path,
    definition: LearningCaseDefinition = CONVERSATION_RAG_SEED_CASE,
) -> tuple[str, ...]:
    """Return fixture-owned tracked paths missing from the selected project HEAD."""

    root = Path(repository_root).expanduser().resolve()
    identity = _repository_identity(root)
    missing: list[str] = []
    for relative_path in definition.required_paths:
        tracked = _git(
            root,
            "ls-files",
            "--error-unmatch",
            relative_path,
            required=False,
        )
        if not tracked or _git_bytes(root, "show", f"{identity.commit}:{relative_path}") is None:
            missing.append(relative_path)
    return tuple(missing)


def _resolve_symbol(
    repository_root: Path,
    relative_file: str,
    qualified_symbol: str,
    commit: str,
) -> _ResolvedSymbol:
    tracked = _git(
        repository_root,
        "ls-files",
        "--error-unmatch",
        relative_file,
        required=False,
    )
    if not tracked:
        raise LearningTraceabilityError(f"code anchor is not tracked: {relative_file}")
    content = _git_bytes(repository_root, "show", f"{commit}:{relative_file}")
    if content is None:
        raise LearningTraceabilityError(f"code anchor is unavailable: {relative_file}")
    try:
        tree = ast.parse(content.decode("utf-8"), filename=relative_file)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise LearningTraceabilityError(
            f"code anchor could not be parsed: {relative_file}"
        ) from exc

    parts = qualified_symbol.split(".")
    definition_types = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    candidates: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = [
        node for node in tree.body if isinstance(node, definition_types)
    ]
    resolved: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for index, part in enumerate(parts):
        resolved = next(
            (
                node
                for node in candidates
                if node.name == part
            ),
            None,
        )
        if resolved is None:
            raise LearningTraceabilityError(
                f"code anchor symbol is missing: {relative_file}:{qualified_symbol}"
            )
        if index < len(parts) - 1:
            candidates = (
                [node for node in resolved.body if isinstance(node, definition_types)]
                if isinstance(resolved, ast.ClassDef)
                else []
            )
    assert resolved is not None
    return _ResolvedSymbol(
        file=relative_file,
        symbol=qualified_symbol,
        line_start=resolved.lineno,
        line_end=resolved.end_lineno or resolved.lineno,
        file_sha256=_sha256_bytes(content),
    )


def load_interview_anchor_pack(
    *, data_root: Path, repository_root: Path, run_id: str
) -> dict[str, object]:
    """Fail closed while turning one recorded Learning run into safe, public locators.

    This deliberately reads only the run's structured artifacts and tracked Git objects; it
    never reads ignored conversation material or creates a Learning run.
    """
    if _LEARNING_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise LearningTraceabilityError("learning run identity is invalid")
    runs_root = data_root / "learning-runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise LearningTraceabilityError("learning runs are unavailable")
    run_dir = runs_root / run_id
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise LearningTraceabilityError("learning run is unavailable")
    run = _private_json(run_dir / "run.json", "learning run")
    case = _private_json(run_dir / "01_case.json", "learning case")
    anchors = _private_json(run_dir / "03_code_anchors.json", "learning anchors")
    if (
        run.get("run_id") != run_id
        or run.get("case_id") != CASE_ID
        or case.get("case_id") != CASE_ID
    ):
        raise LearningTraceabilityError("learning run identity does not match the supported case")
    repository = run.get("repository")
    branch = run.get("branch")
    commit = run.get("commit")
    if not all(isinstance(value, str) and value for value in (repository, branch, commit)):
        raise LearningTraceabilityError("learning run repository identity is invalid")
    identity = _repository_identity(repository_root.resolve())
    commit_exists = _git(
        repository_root,
        "cat-file",
        "-e",
        f"{commit}^{{commit}}",
        required=False,
    )
    if identity.name != repository or commit_exists is None:
        raise LearningTraceabilityError("learning run repository or commit is unavailable")
    source_records = case.get("source_records")
    reasoning = case.get("reasoning")
    if not isinstance(source_records, list) or not isinstance(reasoning, dict):
        raise LearningTraceabilityError("learning reasoning anchors are invalid")
    reasoning_ids = reasoning.get("source_record_ids")
    if not isinstance(reasoning_ids, list) or not reasoning_ids:
        raise LearningTraceabilityError("learning reasoning anchors are invalid")
    source_by_id = {item.get("id"): item for item in source_records if isinstance(item, dict)}
    reasoning_sources: list[dict[str, str]] = []
    for source_id in reasoning_ids:
        source = source_by_id.get(source_id)
        if not isinstance(source, dict):
            raise LearningTraceabilityError("learning reasoning source is unavailable")
        path = source.get("source_path")
        digest = source.get("content_sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise LearningTraceabilityError("learning reasoning source is invalid")
        tracked = _git(
            repository_root,
            "ls-tree",
            "-r",
            "--name-only",
            str(commit),
            "--",
            path,
            required=False,
        )
        content = _git_bytes(repository_root, "show", f"{commit}:{path}")
        if not tracked or content is None or _sha256_bytes(content) != digest:
            raise LearningTraceabilityError("learning reasoning source hash is invalid")
        reasoning_sources.append({"path": path, "sha256": digest})
    raw_code = anchors.get("code_anchors")
    raw_tests = anchors.get("verification_anchors")
    if (
        not isinstance(raw_code, list)
        or not raw_code
        or not isinstance(raw_tests, list)
        or not raw_tests
    ):
        raise LearningTraceabilityError("learning anchor categories are incomplete")

    def validate_anchor(raw: object, *, test: bool) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise LearningTraceabilityError("learning anchor is invalid")
        file = raw.get("file")
        symbol = raw.get("symbol")
        digest = raw.get("file_sha256")
        line_start = raw.get("line_start")
        line_end = raw.get("line_end")
        anchor_id = raw.get("id")
        if (
            not isinstance(anchor_id, str)
            or not anchor_id
            or not isinstance(file, str)
            or not isinstance(symbol, str)
            or not isinstance(digest, str)
        ):
            raise LearningTraceabilityError("learning anchor is invalid")
        if not isinstance(line_start, int) or not isinstance(line_end, int):
            raise LearningTraceabilityError("learning anchor is invalid")
        expected_identity = {
            "repository": repository,
            "branch": branch,
            "commit": commit,
        }
        if any(raw.get(key) != value for key, value in expected_identity.items()):
            raise LearningTraceabilityError("learning anchor identity is invalid")
        content = _git_bytes(repository_root, "show", f"{commit}:{file}")
        if content is None or _sha256_bytes(content) != digest:
            raise LearningTraceabilityError("learning anchor hash is invalid")
        try:
            tree = ast.parse(content.decode("utf-8"), filename=file)
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise LearningTraceabilityError("learning anchor source is invalid") from exc
        resolved = _resolve_symbol_from_tree(tree, file, symbol, digest)
        if (resolved.line_start, resolved.line_end) != (line_start, line_end):
            raise LearningTraceabilityError("learning anchor line range is invalid")
        result: dict[str, object] = {
            "id": anchor_id,
            "file": file,
            "symbol": symbol,
            "line_start": line_start,
            "line_end": line_end,
            "sha256": digest,
        }
        if test:
            command = raw.get("verification_command")
            receipt_state = raw.get("receipt_state")
            if (
                not isinstance(command, str)
                or not command
                or receipt_state != "committed_test_definition"
            ):
                raise LearningTraceabilityError("learning test anchor is invalid")
            result["command"] = command
            result["receipt_state"] = receipt_state
        return result

    code = [validate_anchor(item, test=False) for item in raw_code]
    tests = [validate_anchor(item, test=True) for item in raw_tests]
    anchor_ids = [str(item["id"]) for item in [*code, *tests]]
    if len(set(anchor_ids)) != len(anchor_ids):
        raise LearningTraceabilityError("learning anchor identities are ambiguous")
    return {
        "case_id": CASE_ID,
        "learning_run_id": run_id,
        "project": {
            "repository": repository,
            "branch": branch,
            "commit": commit,
        },
        "reasoning": reasoning_sources,
        "code": code,
        "tests": tests,
        "keywords": ["Conversation RAG", "chunking", "retrieval", "lineage"],
    }


def _resolve_symbol_from_tree(
    tree: ast.Module, file: str, symbol: str, digest: str
) -> _ResolvedSymbol:
    parts = symbol.split(".")
    definition_types = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    candidates: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = [
        node for node in tree.body if isinstance(node, definition_types)
    ]
    resolved: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for index, part in enumerate(parts):
        resolved = next((node for node in candidates if node.name == part), None)
        if resolved is None:
            raise LearningTraceabilityError(f"learning anchor symbol is missing: {file}:{symbol}")
        candidates = (
            [
                node
                for node in resolved.body
                if isinstance(node, definition_types)
            ]
            if index < len(parts) - 1 and isinstance(resolved, ast.ClassDef)
            else []
        )
    assert resolved is not None
    return _ResolvedSymbol(
        file=file,
        symbol=symbol,
        line_start=resolved.lineno,
        line_end=resolved.end_lineno or resolved.lineno,
        file_sha256=digest,
    )


def _source_record(
    repository_root: Path,
    *,
    record_id: str,
    source_kind: str,
    title: str,
    relative_file: str,
    commit: str,
) -> SourceRecord:
    tracked = _git(
        repository_root,
        "ls-files",
        "--error-unmatch",
        relative_file,
        required=False,
    )
    content = _git_bytes(repository_root, "show", f"{commit}:{relative_file}")
    if not tracked or content is None:
        raise LearningTraceabilityError(f"source record is missing: {relative_file}")
    return SourceRecord(
        id=record_id,
        source_kind=source_kind,
        title=title,
        source_path=relative_file,
        content_sha256=_sha256_bytes(content),
    )


def _code_anchor(
    identity: _RepositoryIdentity,
    resolved: _ResolvedSymbol,
    *,
    anchor_id: str,
    capability_ids: list[str],
) -> CodeAnchor:
    return CodeAnchor(
        id=anchor_id,
        repository=identity.name,
        branch=identity.branch,
        commit=identity.commit,
        file=resolved.file,
        symbol=resolved.symbol,
        line_start=resolved.line_start,
        line_end=resolved.line_end,
        file_sha256=resolved.file_sha256,
        capability_ids=capability_ids,
    )


def _verification_anchor(
    identity: _RepositoryIdentity,
    resolved: _ResolvedSymbol,
    *,
    anchor_id: str,
    command: str,
    capability_ids: list[str],
) -> VerificationAnchor:
    return VerificationAnchor(
        id=anchor_id,
        repository=identity.name,
        branch=identity.branch,
        commit=identity.commit,
        file=resolved.file,
        symbol=resolved.symbol,
        line_start=resolved.line_start,
        line_end=resolved.line_end,
        file_sha256=resolved.file_sha256,
        verification_command=command,
        capability_ids=capability_ids,
    )


def _build_anchors(
    repository_root: Path,
    identity: _RepositoryIdentity,
) -> tuple[list[CodeAnchor], list[VerificationAnchor]]:
    code_specs = (
        (
            "CODE-CHUNKING",
            "src/soloscale/conversation_intake.py",
            "_text_segments",
            ["CAP-STABLE-CHUNKING"],
        ),
        (
            "CODE-CODEX-NORMALIZATION",
            "src/soloscale/conversation_intake.py",
            "parse_codex_session",
            ["CAP-STABLE-CHUNKING"],
        ),
        (
            "CODE-SEARCH",
            "src/soloscale/knowledge_store.py",
            "KnowledgeStore.search",
            ["CAP-BOUNDED-RETRIEVAL"],
        ),
        (
            "CODE-RRF",
            "src/soloscale/knowledge_store.py",
            "_reciprocal_rank_fusion",
            ["CAP-BOUNDED-RETRIEVAL"],
        ),
        (
            "CODE-LINEAGE",
            "src/soloscale/knowledge_store.py",
            "KnowledgeStore._retrieval_hit",
            ["CAP-BOUNDED-RETRIEVAL"],
        ),
    )
    code_anchors = [
        _code_anchor(
            identity,
            _resolve_symbol(repository_root, relative_file, symbol, identity.commit),
            anchor_id=anchor_id,
            capability_ids=capability_ids,
        )
        for anchor_id, relative_file, symbol, capability_ids in code_specs
    ]
    chunk_command = (
        "pytest -q tests/test_conversation_intake.py::"
        "test_codex_long_message_is_split_into_stable_overlapping_chunks"
    )
    retrieval_command = (
        "pytest -q tests/test_knowledge_store.py::"
        "test_search_fuses_fts_and_alias_channels_with_lineage"
    )
    verification_specs = (
        (
            "TEST-CHUNKING",
            "tests/test_conversation_intake.py",
            "test_codex_long_message_is_split_into_stable_overlapping_chunks",
            chunk_command,
            ["CAP-STABLE-CHUNKING"],
        ),
        (
            "TEST-RETRIEVAL",
            "tests/test_knowledge_store.py",
            "test_search_fuses_fts_and_alias_channels_with_lineage",
            retrieval_command,
            ["CAP-BOUNDED-RETRIEVAL"],
        ),
        (
            "TEST-INTEGRITY",
            "tests/test_knowledge_store.py",
            "test_retrieval_detects_body_or_fts_tampering_and_resync_repairs_it",
            (
                "pytest -q tests/test_knowledge_store.py::"
                "test_retrieval_detects_body_or_fts_tampering_and_resync_repairs_it"
            ),
            ["CAP-BOUNDED-RETRIEVAL"],
        ),
    )
    verification_anchors = [
        _verification_anchor(
            identity,
            _resolve_symbol(repository_root, relative_file, symbol, identity.commit),
            anchor_id=anchor_id,
            command=command,
            capability_ids=capability_ids,
        )
        for anchor_id, relative_file, symbol, command, capability_ids in verification_specs
    ]
    return code_anchors, verification_anchors


def _build_learning_material(
    *,
    cache_key: str,
    target_requirement: str,
    code_anchors: list[CodeAnchor],
) -> dict[str, object]:
    anchor_path = [
        f"{anchor.file}:{anchor.symbol}:{anchor.line_start}-{anchor.line_end}"
        for anchor in code_anchors
    ]
    return {
        "schema_version": "0.1",
        "case_id": CASE_ID,
        "evidence_hash": cache_key,
        "compilation": {
            "mode": "single-selected-case-grounded-packet",
            "runtime_model_calls": 0,
            "note": (
                "This checked-in implementation supplies one bounded, code-grounded packet; "
                "the local runtime does not call a model or network."
            ),
        },
        "plain_language_30_seconds": (
            "SoloScale turns long conversation messages into stable, UTF-8-safe overlapping "
            "chunks. Search combines full-text and exact matching, fuses their rankings, and "
            "checks the stored body and source lineage before returning evidence."
        ),
        "architecture_walkthrough_5_minutes": [
            "Source adapters normalize only allowed user/assistant or safe structured content.",
            "Long content is split at 1,200 UTF-8 bytes with up to 200 bytes of overlap.",
            "Stable document/message identity plus segment index produces stable chunk IDs.",
            (
                "KnowledgeStore.search validates the query, obtains FTS and exact candidates, "
                "and uses reciprocal-rank fusion for deterministic ordering."
            ),
            (
                "RetrievalHit construction rechecks body hashes and the FTS projection before "
                "exposing an excerpt and canonical lineage."
            ),
        ],
        "technical_deep_dive_15_minutes": {
            "chunking": (
                "The splitter operates on encoded UTF-8 bytes, backs off continuation bytes "
                "at boundaries, and advances from an overlap-safe byte position. The parser "
                "then binds each segment to document, message, ordinal, and SHA-256 identity."
            ),
            "retrieval": (
                "Search tokenizes bounded mixed Latin/CJK input, runs parameterized FTS and "
                "exact channels, then sums reciprocal ranks with stable ID tie-breaking."
            ),
            "integrity": (
                "A returned row must match its stored body hash, document identity, and unique "
                "FTS projection. Corruption fails closed instead of silently returning text."
            ),
            "boundary": (
                "These checks prove identity and structural lineage, not semantic entailment. "
                "Promotion to a career or public claim still requires human review."
            ),
        },
        "actual_code_path": anchor_path,
        "decisions_and_trade_offs": [
            "Byte-bounded overlap preserves tail context but duplicates bounded text.",
            "Lexical FTS plus exact matching is deterministic and local but lacks embeddings.",
            "Integrity checks fail closed; repair requires an explicit approved resync.",
        ],
        "known_failures_and_unknowns": [
            "A changed private Codex record format can reduce ingestion coverage.",
            "Prompt injection and semantically irrelevant citations remain possible.",
            "This learning run did not read raw private conversations.",
            "Operator authorship and independent debugging ability are not established.",
        ],
        "glossary": {
            "chunk": "A bounded normalized retrieval unit with stable identity and hash.",
            "overlap": "Repeated boundary bytes that preserve context across adjacent chunks.",
            "FTS": "SQLite full-text search used as one deterministic retrieval channel.",
            "RRF": "Reciprocal-rank fusion, combining ranked lists without score calibration.",
            "lineage": "The document, chunk, locator, role, timestamp, and hashes behind a hit.",
        },
        "exercises": {
            "Explain": {
                "prompt": (
                    "Without opening the answer key, explain why UTF-8 byte boundaries and "
                    "overlap both matter, then state what lineage checks do not prove."
                ),
                "start_with": ["CODE-CHUNKING", "TEST-CHUNKING"],
                "answer_revealed": False,
            },
            "Trace": {
                "prompt": (
                    "Trace one query from KnowledgeStore.search through both candidate channels, "
                    "rank fusion, row loading, integrity checking, and RetrievalHit output."
                ),
                "start_with": ["CODE-SEARCH", "CODE-RRF", "CODE-LINEAGE"],
                "answer_revealed": False,
            },
            "Rebuild": {
                "prompt": (
                    "Implement a tiny UTF-8-safe overlapping splitter from the tests alone, "
                    "without copying the production function."
                ),
                "answer_revealed": False,
            },
            "Debug": {
                "prompt": (
                    "A retrieval row's body no longer matches text_sha256. Predict the safe "
                    "behavior and identify the explicit recovery path."
                ),
                "answer_revealed": False,
            },
        },
        "teach_back_rubric": [
            "Separates stable identity from semantic truth.",
            "Can trace chunk creation and retrieval through named symbols.",
            "Explains the overlap, lexical-retrieval, and fail-closed trade-offs.",
            "Names what is unknown instead of claiming implementation ownership.",
        ],
        "target_jd_relevance": target_requirement,
    }


def _build_graph(
    *,
    source_records: list[SourceRecord],
    reasoning: ReasoningArtifact,
    insight: DistilledInsight,
    decisions: list[EngineeringDecision],
    capabilities: list[ImplementedCapability],
    concepts: list[TechnicalConcept],
    code_anchors: list[CodeAnchor],
    verification_anchors: list[VerificationAnchor],
    mastery: MasteryState,
    task: LearningTask,
    claim: ClaimEligibility,
) -> tuple[list[KnowledgeGraphNode], list[KnowledgeGraphEdge]]:
    project_anchor = code_anchors[0]
    nodes = [
        KnowledgeGraphNode(
            id="PROJECT-SOLOSCALE",
            kind="PROJECT",
            label=project_anchor.repository,
            detail={
                "repository": project_anchor.repository,
                "branch": project_anchor.branch,
                "commit": project_anchor.commit,
            },
        ),
        *[
            KnowledgeGraphNode(
                id=item.id,
                kind="SOURCE",
                label=item.title,
                truth_stage=item.truth_stage,
                detail={"source_kind": item.source_kind, "source_path": item.source_path},
            )
            for item in source_records
        ],
        KnowledgeGraphNode(
            id=reasoning.id,
            kind="REASONING",
            label="Evidence-bound retrieval context",
            truth_stage=reasoning.truth_stage,
            detail={"summary": reasoning.summary, "limitations": reasoning.limitations},
        ),
        KnowledgeGraphNode(
            id=insight.id,
            kind="INSIGHT",
            label="Stable lineage before synthesis",
            truth_stage=insight.truth_stage,
            detail={"statement": insight.statement},
        ),
        *[
            KnowledgeGraphNode(
                id=item.id,
                kind="DECISION",
                label=item.decision,
                truth_stage=item.truth_stage,
                detail={"rationale": item.rationale, "trade_offs": item.trade_offs},
            )
            for item in decisions
        ],
        *[
            KnowledgeGraphNode(
                id=item.id,
                kind="CAPABILITY",
                label=item.name,
                truth_stage=item.truth_stage,
                detail={"description": item.description},
            )
            for item in capabilities
        ],
        *[
            KnowledgeGraphNode(
                id=item.id,
                kind="CONCEPT",
                label=item.name,
                detail={"explanation": item.explanation, "glossary": item.glossary},
            )
            for item in concepts
        ],
        *[
            KnowledgeGraphNode(
                id=item.id,
                kind="CODE",
                label=f"{item.file}:{item.symbol}",
                truth_stage=TruthStage.IMPLEMENTED_CAPABILITY,
                detail={
                    "file": item.file,
                    "symbol": item.symbol,
                    "line_start": item.line_start,
                    "line_end": item.line_end,
                    "commit": item.commit,
                },
            )
            for item in code_anchors
        ],
        *[
            KnowledgeGraphNode(
                id=item.id,
                kind="TEST",
                label=f"{item.file}:{item.symbol}",
                truth_stage=TruthStage.VERIFIED_EVIDENCE,
                detail={
                    "file": item.file,
                    "symbol": item.symbol,
                    "line_start": item.line_start,
                    "line_end": item.line_end,
                    "receipt_state": item.receipt_state,
                },
            )
            for item in verification_anchors
        ],
        KnowledgeGraphNode(
            id="JD-FAROS-RETRIEVAL",
            kind="JD_REQUIREMENT",
            label=claim.target_requirement,
            detail={"supported_by": [item.id for item in capabilities]},
        ),
        KnowledgeGraphNode(
            id="MASTERY-CURRENT",
            kind="MASTERY",
            label=mastery.level.value,
            truth_stage=mastery.truth_stage,
            detail={
                "interview_ready": mastery.interview_ready,
                "next_action": mastery.next_action.value if mastery.next_action else None,
            },
        ),
        KnowledgeGraphNode(
            id=task.id,
            kind="LEARNING_TASK",
            label=task.title,
            detail={"action": task.action.value, "status": task.status},
        ),
        KnowledgeGraphNode(
            id="CLAIM-GATE",
            kind="CLAIM_ELIGIBILITY",
            label="Resume claim blocked pending ownership",
            detail={
                "resume_eligible": claim.resume_eligible,
                "rationale": claim.rationale,
            },
        ),
    ]
    edges: list[KnowledgeGraphEdge] = []
    for source_id in reasoning.source_record_ids:
        edges.append(
            KnowledgeGraphEdge(source=source_id, target=reasoning.id, relation="grounds")
        )
    edges.append(
        KnowledgeGraphEdge(source=reasoning.id, target=insight.id, relation="distills")
    )
    for decision in decisions:
        edges.append(
            KnowledgeGraphEdge(source=insight.id, target=decision.id, relation="informs")
        )
    for capability in capabilities:
        edges.append(
            KnowledgeGraphEdge(
                source="PROJECT-SOLOSCALE",
                target=capability.id,
                relation="contains",
            )
        )
        for decision_id in capability.decision_ids:
            edges.append(
                KnowledgeGraphEdge(
                    source=decision_id,
                    target=capability.id,
                    relation="implemented_as",
                )
            )
        for anchor_id in capability.code_anchor_ids:
            edges.append(
                KnowledgeGraphEdge(
                    source=capability.id,
                    target=anchor_id,
                    relation="located_at",
                )
            )
    for concept in concepts:
        for capability_id in concept.capability_ids:
            edges.append(
                KnowledgeGraphEdge(
                    source=capability_id,
                    target=concept.id,
                    relation="teaches",
                )
            )
    for verification in verification_anchors:
        for capability_id in verification.capability_ids:
            edges.append(
                KnowledgeGraphEdge(
                    source=capability_id,
                    target=verification.id,
                    relation="verified_by_definition",
                )
            )
    for capability in capabilities:
        edges.append(
            KnowledgeGraphEdge(
                source=capability.id,
                target="JD-FAROS-RETRIEVAL",
                relation="relevant_to",
            )
        )
    edges.extend(
        [
            KnowledgeGraphEdge(
                source="CONCEPT-CHUNKING",
                target="MASTERY-CURRENT",
                relation="mastery_state",
            ),
            KnowledgeGraphEdge(
                source="MASTERY-CURRENT", target=task.id, relation="next_action"
            ),
            KnowledgeGraphEdge(
                source="JD-FAROS-RETRIEVAL",
                target="CLAIM-GATE",
                relation="claim_gate",
            ),
            KnowledgeGraphEdge(
                source="MASTERY-CURRENT",
                target="CLAIM-GATE",
                relation="readiness_gate",
            ),
        ]
    )
    node_ids = {node.id for node in nodes}
    if len(node_ids) != len(nodes):
        raise LearningTraceabilityError("graph node identities must be unique")
    if any(edge.source not in node_ids or edge.target not in node_ids for edge in edges):
        raise LearningTraceabilityError("every graph edge must resolve to a real node")
    return nodes, edges


def _ensure_private_directory(path: Path, *, parents: bool = False) -> None:
    if path.is_symlink():
        raise LearningTraceabilityError(f"private directory cannot be a symlink: {path}")
    try:
        path.mkdir(mode=_DIRECTORY_MODE, parents=parents, exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise LearningTraceabilityError(f"unsafe private directory: {path}")
        path.chmod(_DIRECTORY_MODE)
    except OSError as exc:
        raise LearningTraceabilityError(f"private directory creation failed: {path}") from exc


def _atomic_private_json(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        raise LearningTraceabilityError(f"private artifact already exists: {path.name}")
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            _FILE_MODE,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, _FILE_MODE)
        os.rename(temporary, path)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise LearningTraceabilityError(f"private artifact write failed: {path.name}") from exc


def _load_cached_material(cache_file: Path, cache_key: str) -> dict[str, object] | None:
    if cache_file.is_symlink():
        raise LearningTraceabilityError("learning cache cannot be a symlink")
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LearningTraceabilityError("learning cache is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("evidence_hash") != cache_key:
        raise LearningTraceabilityError("learning cache failed its evidence-hash check")
    return payload


def save_learning_response(
    *,
    data_root: Path,
    run_id: str,
    stage: MasteryAction | str,
    response: str,
    evidence_hub: EvidenceHub | None = None,
) -> tuple[LearningResponseReceipt, Path]:
    """Save one private response candidate without creating a mastery receipt."""

    if _LEARNING_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("learning run id is invalid")
    try:
        normalized_stage = MasteryAction(stage)
    except ValueError:
        raise ValueError("learning response stage is invalid") from None
    accepted_stage: Literal[MasteryAction.EXPLAIN, MasteryAction.TRACE]
    if normalized_stage is MasteryAction.EXPLAIN:
        accepted_stage = MasteryAction.EXPLAIN
    elif normalized_stage is MasteryAction.TRACE:
        accepted_stage = MasteryAction.TRACE
    else:
        raise ValueError("only Explain and Trace responses are accepted here")
    normalized_response = response.strip()
    if not normalized_response:
        raise ValueError("learning response must not be empty")
    if len(normalized_response) > 20_000:
        raise ValueError("learning response exceeds the 20000-character limit")

    selected_root = data_root.expanduser().absolute()
    runs_root = selected_root / "learning-runs"
    run_dir = runs_root / run_id
    for path, label in (
        (selected_root, "data root"),
        (runs_root, "learning runs root"),
        (run_dir, "learning run"),
    ):
        if path.is_symlink():
            raise LearningTraceabilityError(f"{label} cannot be a symlink")
        if not path.is_dir():
            raise LearningTraceabilityError(f"{label} is unavailable")

    run_file = run_dir / "run.json"
    case_file = run_dir / "01_case.json"
    mastery_file = run_dir / "06_mastery.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (run_file, case_file, mastery_file)
    ):
        raise LearningTraceabilityError("learning run artifacts are unavailable")
    try:
        run_payload = json.loads(run_file.read_text(encoding="utf-8"))
        case_payload = json.loads(case_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LearningTraceabilityError("learning run artifacts are unreadable") from exc
    if not isinstance(run_payload, dict) or run_payload.get("run_id") != run_id:
        raise LearningTraceabilityError("learning run identity does not match its path")
    case_id = case_payload.get("case_id") if isinstance(case_payload, dict) else None
    if not isinstance(case_id, str) or not case_id:
        raise LearningTraceabilityError("learning case identity is unavailable")

    submitted_at = datetime.now(UTC)
    receipt = LearningResponseReceipt(
        id=f"response-{uuid4().hex[:16]}",
        run_id=run_id,
        case_id=case_id,
        stage=accepted_stage,
        response=normalized_response,
        submitted_at=submitted_at,
    )
    response_root = run_dir / "practice-responses"
    _ensure_private_directory(response_root)
    filename = (
        f"{submitted_at:%Y%m%dT%H%M%S%fZ}-"
        f"{accepted_stage.value.lower()}-{receipt.id}.json"
    )
    receipt_path = response_root / filename
    _atomic_private_json(receipt_path, receipt.model_dump(mode="json"))
    response_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    captured_assets = capture_assets(
        data_root=selected_root,
        run_dir=run_dir,
        owner="learning_response",
        run_id=run_id,
        artifact_names=[f"practice-responses/{filename}"],
        evidence_hub=evidence_hub,
    )
    capture_outcome(
        data_root=selected_root,
        run_dir=run_dir,
        owner="learning_response",
        run_id=run_id,
        outcome_type="learning_response",
        platform="local",
        status="submitted_requires_review",
        final_sha256=response_sha256,
        metadata={"stage": accepted_stage.value, "receipt_id": receipt.id},
        asset_id=captured_assets.get(f"practice-responses/{filename}"),
        evidence_hub=evidence_hub,
    )
    return receipt, receipt_path


def run_learning_traceability(
    *,
    data_root: Path,
    repository_root: Path,
    target_requirement: str = DEFAULT_TARGET_REQUIREMENT,
    evidence_hub: EvidenceHub | None = None,
    evidence_bundle_id: str | None = None,
) -> LearningTraceabilityRun:
    """Build the one selected traceability case without reading private conversation bodies."""

    bundle_evidence_ids: list[str] = []
    if evidence_bundle_id is not None:
        selected_hub = evidence_hub or EvidenceHub(data_root)
        bundle, _items = selected_hub.resolve_bundle(evidence_bundle_id)
        bundle_evidence_ids = bundle.evidence_ids
        evidence_hub = selected_hub

    requirement = " ".join(target_requirement.split())
    if not requirement:
        raise ValueError("target_requirement must not be empty")
    repository_root = repository_root.resolve()
    data_root = data_root.expanduser().absolute()
    identity = _repository_identity(repository_root)
    project_binding = inspect_learning_project(repository_root)
    missing_fixture_paths = missing_learning_case_anchors(repository_root)
    if missing_fixture_paths:
        raise LearningFixtureAnchorError(CASE_ID, missing_fixture_paths)
    source_records = [
        _source_record(
            repository_root,
            record_id="SOURCE-CONVERSATION-DISTILLATION",
            source_kind="tracked_conversation_distillation",
            title="Agent swarm to surface routing distillation",
            relative_file="docs/conversations/2026-08-06-agent-swarm-to-surface-routing.md",
            commit=identity.commit,
        ),
        _source_record(
            repository_root,
            record_id="SOURCE-ADR-0004",
            source_kind="architecture_decision",
            title="Private evidence-bound Conversation RAG decision",
            relative_file="docs/decisions/ADR-0004-private-conversation-rag.md",
            commit=identity.commit,
        ),
        _source_record(
            repository_root,
            record_id="SOURCE-RAG-GUIDE",
            source_kind="technical_guide",
            title="Private Conversation RAG v0.2 guide",
            relative_file="docs/conversation-rag.md",
            commit=identity.commit,
        ),
    ]
    reasoning = ReasoningArtifact(
        id="REASONING-EVIDENCE-DEBT",
        summary=(
            "The tracked distillation and ADR describe evidence debt, typed handoffs, private "
            "source boundaries, and the need for stable citation lineage."
        ),
        source_record_ids=[record.id for record in source_records],
        limitations=[
            "The source is a tracked distillation, not the raw private conversation.",
            "This run deliberately did not read ignored conversation bodies.",
        ],
    )
    insight = DistilledInsight(
        id="INSIGHT-STABLE-LINEAGE",
        statement=(
            "Conversation evidence must be normalized into stable hashed units and retrieved "
            "through bounded channels before synthesis can retain inspectable lineage."
        ),
        reasoning_artifact_ids=[reasoning.id],
    )
    decisions = [
        EngineeringDecision(
            id="DECISION-OVERLAPPING-CHUNKS",
            decision="Use deterministic UTF-8-safe byte-bounded overlapping chunks.",
            rationale="Long messages need bounded retrieval units without losing all tail context.",
            insight_ids=[insight.id],
            alternatives_considered=["Index each full message as one unbounded unit."],
            trade_offs=["Overlap duplicates a bounded amount of text across adjacent chunks."],
        ),
        EngineeringDecision(
            id="DECISION-LEXICAL-RRF",
            decision="Fuse FTS and exact retrieval, then fail closed on lineage mismatch.",
            rationale=(
                "Two deterministic local channels cover narrative and identifier matches while "
                "hash/projection checks keep returned evidence tied to stored records."
            ),
            insight_ids=[insight.id],
            alternatives_considered=["Add an embedding service to the first local slice."],
            trade_offs=["Lexical retrieval misses semantic matches without shared terms."],
        ),
    ]
    code_anchors, verification_anchors = _build_anchors(repository_root, identity)
    capabilities = [
        ImplementedCapability(
            id="CAP-STABLE-CHUNKING",
            name="Stable conversation chunking",
            description=(
                "Long normalized messages become stable UTF-8-safe overlapping chunks with "
                "message, segment, ordinal, and SHA-256 lineage."
            ),
            decision_ids=["DECISION-OVERLAPPING-CHUNKS"],
            code_anchor_ids=["CODE-CHUNKING", "CODE-CODEX-NORMALIZATION"],
        ),
        ImplementedCapability(
            id="CAP-BOUNDED-RETRIEVAL",
            name="Bounded lineage-checked retrieval",
            description=(
                "KnowledgeStore combines bounded FTS/exact candidate sets, stable rank fusion, "
                "and integrity-checked RetrievalHit construction."
            ),
            decision_ids=["DECISION-LEXICAL-RRF"],
            code_anchor_ids=["CODE-SEARCH", "CODE-RRF", "CODE-LINEAGE"],
        ),
    ]
    concepts = [
        TechnicalConcept(
            id="CONCEPT-CHUNKING",
            name="UTF-8-safe overlapping chunking",
            explanation=(
                "Byte limits bound storage and model context, UTF-8 boundary checks avoid broken "
                "characters, and overlap preserves nearby context across segment boundaries."
            ),
            capability_ids=["CAP-STABLE-CHUNKING"],
            glossary={
                "chunk": "A bounded normalized retrieval unit.",
                "overlap": "Context repeated between adjacent units.",
            },
        ),
        TechnicalConcept(
            id="CONCEPT-RRF",
            name="Deterministic reciprocal-rank fusion",
            explanation=(
                "RRF combines independently ranked FTS and exact-match candidates without "
                "assuming their scores share a scale."
            ),
            capability_ids=["CAP-BOUNDED-RETRIEVAL"],
            glossary={"RRF": "A sum of reciprocal rank contributions from each channel."},
        ),
        TechnicalConcept(
            id="CONCEPT-LINEAGE",
            name="Retrieval lineage integrity",
            explanation=(
                "A hit is exposed only when its body hash and FTS projection still match the "
                "stored document/chunk identity."
            ),
            capability_ids=["CAP-BOUNDED-RETRIEVAL"],
            glossary={"lineage": "The exact source and hash trail behind retrieved text."},
        ),
    ]
    contribution = ContributionAttribution(
        case_id=CASE_ID,
        ai_assistance=[
            "The repository history alone does not distinguish human-written and AI-written code."
        ],
        ownership_confidence=OwnershipConfidence.UNKNOWN,
        unknowns=[
            "Problem-framing actor is not proven by repository evidence.",
            "Requirements author is not proven by repository evidence.",
            "Decision maker or approver is not proven by repository evidence.",
            "Implementation actor is not proven by repository evidence.",
            "Reviewer and verifier identities are not proven by repository evidence.",
            "No independent operator modification or debugging receipt exists for this case.",
        ],
    )
    mastery = MasteryState(
        case_id=CASE_ID,
        level=MasteryLevel.L0_SEEN,
        completed_actions=[],
        next_action=MasteryAction.EXPLAIN,
        interview_ready=False,
        receipt_ids=[],
    )
    task = LearningTask(
        id="TASK-EXPLAIN-CHUNKING",
        case_id=CASE_ID,
        title="Explain chunking and its truth boundary",
        action=MasteryAction.EXPLAIN,
        objective=(
            "Explain the byte-boundary, overlap, stable-identity, and semantic-truth boundaries "
            "without reading an answer."
        ),
        instructions=[
            "Open CODE-CHUNKING and TEST-CHUNKING.",
            "Give a 90-second explanation in your own words.",
            "Name one trade-off and one thing the integrity check does not prove.",
        ],
        anchor_ids=["CODE-CHUNKING", "TEST-CHUNKING"],
        completion_evidence="A user-authored explanation receipt reviewed against the rubric.",
    )
    claim = ClaimEligibility(
        case_id=CASE_ID,
        target_requirement=requirement,
        engineering_truth_stage=TruthStage.VERIFIED_EVIDENCE,
        ownership_confidence=OwnershipConfidence.UNKNOWN,
        mastery_level=mastery.level,
        interview_ready=mastery.interview_ready,
        resume_eligible=False,
        approved_claim=None,
        safe_verbs=["Reviewed", "Traced"],
        prohibited_phrasing=[
            "Architected the Conversation RAG system",
            "Owned retrieval reliability end to end",
        ],
        rationale=(
            "Code and committed test definitions are real, but personal contribution is not "
            "proven by repository evidence. Interview readiness stays NOT_READY until L5 "
            "mastery receipts exist; resume eligibility remains pending ownership proof."
        ),
    )
    questions = [
        InterviewQuestion(
            id="QUESTION-CHUNK-BOUNDARY",
            case_id=CASE_ID,
            prompt=(
                "Why does this splitter operate on UTF-8 bytes, and how would you test a boundary "
                "that lands inside a multibyte character?"
            ),
            target_level=MasteryLevel.L4_DEBUG,
            anchor_ids=["CODE-CHUNKING", "TEST-CHUNKING"],
            strong_answer_signals=[
                "Explains continuation-byte detection.",
                "Covers stable limits, overlap, and a non-ASCII fixture.",
            ],
        ),
        InterviewQuestion(
            id="QUESTION-RRF-TRADEOFF",
            case_id=CASE_ID,
            prompt=(
                "Defend reciprocal-rank fusion here instead of calibrated weighted scores or "
                "embeddings, then name the migration signal for changing that decision."
            ),
            target_level=MasteryLevel.L5_DEFEND,
            anchor_ids=["CODE-SEARCH", "CODE-RRF"],
            strong_answer_signals=[
                "Separates deterministic local scope from semantic recall.",
                "Names an evaluation-driven trigger rather than preference.",
            ],
        ),
        InterviewQuestion(
            id="QUESTION-LINEAGE-TRUTH",
            case_id=CASE_ID,
            prompt=(
                "What does RetrievalHit integrity prove, what does it not prove, and where should "
                "the human promotion gate sit?"
            ),
            target_level=MasteryLevel.L5_DEFEND,
            anchor_ids=["CODE-LINEAGE", "TEST-INTEGRITY", "SOURCE-ADR-0004"],
            strong_answer_signals=[
                "Distinguishes source identity from semantic support.",
                "Places human review before career or public claims.",
            ],
        ),
    ]
    evidence_projection = {
        "source_records": [item.model_dump(mode="json") for item in source_records],
        "reasoning": reasoning.model_dump(mode="json"),
        "insight": insight.model_dump(mode="json"),
        "decisions": [item.model_dump(mode="json") for item in decisions],
        "capabilities": [item.model_dump(mode="json") for item in capabilities],
        "concepts": [item.model_dump(mode="json") for item in concepts],
        "code_anchors": [item.model_dump(mode="json") for item in code_anchors],
        "verification_anchors": [
            item.model_dump(mode="json") for item in verification_anchors
        ],
        "target_requirement": requirement,
    }
    cache_key = _canonical_sha256(evidence_projection)
    nodes, edges = _build_graph(
        source_records=source_records,
        reasoning=reasoning,
        insight=insight,
        decisions=decisions,
        capabilities=capabilities,
        concepts=concepts,
        code_anchors=code_anchors,
        verification_anchors=verification_anchors,
        mastery=mastery,
        task=task,
        claim=claim,
    )

    _ensure_private_directory(data_root, parents=True)
    runs_root = data_root / "learning-runs"
    cache_root = data_root / "learning-cache"
    _ensure_private_directory(runs_root)
    _ensure_private_directory(cache_root)
    cache_directory = cache_root / cache_key
    _ensure_private_directory(cache_directory)
    cache_file = cache_directory / "material.json"
    material = _load_cached_material(cache_file, cache_key)
    cache_hit = material is not None
    if material is None:
        material = _build_learning_material(
            cache_key=cache_key,
            target_requirement=requirement,
            code_anchors=code_anchors,
        )
        _atomic_private_json(cache_file, material)

    run_id = f"learning-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:10]}"
    run_dir = runs_root / run_id
    staging_dir = runs_root / f".{run_id}.{uuid4().hex}.tmp"
    _ensure_private_directory(staging_dir)
    run = LearningTraceabilityRun(
        run_id=run_id,
        case_id=CASE_ID,
        evidence_bundle_id=evidence_bundle_id,
        project_source_id=project_binding.project_source_id,
        case_kind=CONVERSATION_RAG_SEED_CASE.case_kind,
        repository=identity.name,
        branch=identity.branch,
        commit=identity.commit,
        cache_key=cache_key,
        cache_hit=cache_hit,
        private_run_path=str(run_dir),
        artifact_files=list(ARTIFACT_FILES),
        model_calls=0,
        network_used=False,
        mastery_level=mastery.level,
        next_action=mastery.next_action,
        limitations=[
            "Raw private conversation bodies were not read or copied.",
            "Committed test definitions are resolved; this run does not execute pytest.",
            "Contribution ownership and human mastery remain unverified.",
            "DEFERRED_PRODUCTION_HARDENING: authentication, multi-user support, queues, and cloud.",
        ],
    )
    artifacts: dict[str, object] = {
        "00_input.json": {
            "schema_version": "0.1",
            "case_id": CASE_ID,
            "case_kind": CONVERSATION_RAG_SEED_CASE.case_kind,
            "target_requirement": requirement,
            "repository_root": str(repository_root),
            "project_binding": project_binding.model_dump(mode="json"),
            "private_source_bodies_read": False,
        },
        "01_case.json": {
            "schema_version": "0.1",
            "case_id": CASE_ID,
            "case_kind": CONVERSATION_RAG_SEED_CASE.case_kind,
            "project_binding": project_binding.model_dump(mode="json"),
            "title": "Conversation RAG: stable chunking and bounded retrieval",
            "engineering_state": "ENGINEERING_VERIFIED",
            "source_records": [item.model_dump(mode="json") for item in source_records],
            "reasoning": reasoning.model_dump(mode="json"),
            "insight": insight.model_dump(mode="json"),
            "decisions": [item.model_dump(mode="json") for item in decisions],
            "capabilities": [item.model_dump(mode="json") for item in capabilities],
            "missing_evidence": [
                "Raw case-specific conversation receipt was not provided to this run.",
                "No CI execution receipt is asserted.",
            ],
        },
        "02_concepts.json": {
            "schema_version": "0.1",
            "concepts": [item.model_dump(mode="json") for item in concepts],
        },
        "03_code_anchors.json": {
            "schema_version": "0.1",
            "code_anchors": [item.model_dump(mode="json") for item in code_anchors],
            "verification_anchors": [
                item.model_dump(mode="json") for item in verification_anchors
            ],
        },
        "04_evidence_graph.json": {
            "schema_version": "0.1",
            "nodes": [item.model_dump(mode="json") for item in nodes],
            "edges": [item.model_dump(mode="json") for item in edges],
        },
        "05_contribution.json": contribution.model_dump(mode="json"),
        "06_mastery.json": mastery.model_dump(mode="json"),
        "07_learning_plan.json": material,
        "08_interview_questions.json": {
            "schema_version": "0.1",
            "questions": [item.model_dump(mode="json") for item in questions],
        },
        "09_claim_eligibility.json": claim.model_dump(mode="json"),
        "10_verification.json": {
            "schema_version": "0.1",
            "code_anchor_count": len(code_anchors),
            "verification_anchor_count": len(verification_anchors),
            "all_anchors_resolved_to_tracked_files": True,
            "all_graph_edges_resolved": True,
            "source_and_file_hashes_recorded": True,
            "tests_executed_by_learning_run": False,
            "engineering_state_basis": (
                "Real committed implementation plus committed test definitions; fresh test "
                "execution is an external verification gate."
            ),
            "truth_stages_present": sorted(
                {
                    stage.value
                    for stage in TruthStage
                    if stage is not TruthStage.APPROVED_CLAIM
                }
            ),
            "approved_claim_created": False,
            "private_source_bodies_read": False,
        },
        "run.json": run.model_dump(mode="json"),
    }
    try:
        for filename in ARTIFACT_FILES:
            _atomic_private_json(staging_dir / filename, artifacts[filename])
        os.rename(staging_dir, run_dir)
    except BaseException:
        if staging_dir.exists() and not staging_dir.is_symlink():
            shutil.rmtree(staging_dir)
        raise
    capture_assets(
        data_root=data_root,
        run_dir=run_dir,
        owner="learning",
        run_id=run_id,
        artifact_names=list(ARTIFACT_FILES),
        evidence_bundle_id=evidence_bundle_id,
        evidence_item_ids=bundle_evidence_ids,
        evidence_hub=evidence_hub,
    )
    return run
