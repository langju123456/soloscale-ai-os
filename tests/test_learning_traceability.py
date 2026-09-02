import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from soloscale import learning_traceability
from soloscale.evidence_hub import EvidenceHub
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
    LearningResponseReceipt,
    LearningTask,
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
from soloscale.learning_traceability import (
    ARTIFACT_FILES,
    CASE_ID,
    LearningFixtureAnchorError,
    LearningTraceabilityError,
    inspect_learning_project,
    load_interview_anchor_pack,
    missing_learning_case_anchors,
    run_learning_traceability,
    save_learning_response,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _expected_repository_ref() -> str:
    branch = _git("branch", "--show-current")
    if branch:
        return branch
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("GITHUB_SHA") == _git("rev-parse", "HEAD")
    github_ref = os.environ.get("GITHUB_REF", "")
    assert github_ref.startswith("refs/")
    return github_ref.removeprefix("refs/")


def test_required_contracts_are_public_and_mastery_is_separate() -> None:
    required_contracts = (
        SourceRecord,
        ReasoningArtifact,
        DistilledInsight,
        EngineeringDecision,
        ImplementedCapability,
        TechnicalConcept,
        CodeAnchor,
        VerificationAnchor,
        ContributionAttribution,
        MasteryState,
        LearningTask,
        InterviewQuestion,
        ClaimEligibility,
        KnowledgeGraphNode,
        KnowledgeGraphEdge,
        LearningResponseReceipt,
    )
    assert all(contract.model_fields for contract in required_contracts)

    with pytest.raises(ValidationError, match="interview_ready requires L5 Defend"):
        MasteryState(
            case_id=CASE_ID,
            level=MasteryLevel.L0_SEEN,
            completed_actions=[],
            next_action=MasteryAction.EXPLAIN,
            interview_ready=True,
        )

    claim_ready_without_mastery = ClaimEligibility(
        case_id=CASE_ID,
        target_requirement="Build retrieval systems for AI context and memory.",
        engineering_truth_stage=TruthStage.APPROVED_CLAIM,
        ownership_confidence=OwnershipConfidence.CONFIRMED,
        mastery_level=MasteryLevel.L0_SEEN,
        interview_ready=False,
        resume_eligible=True,
        approved_claim="Implemented bounded retrieval with stable chunk identity.",
        safe_verbs=["Implemented"],
        prohibited_phrasing=[],
        rationale="Truth and ownership are proven; personal mastery remains unproven.",
    )
    assert claim_ready_without_mastery.resume_eligible is True
    assert claim_ready_without_mastery.interview_ready is False

    with pytest.raises(
        ValidationError,
        match="resume_eligible must match truth and ownership gates",
    ):
        ClaimEligibility(
            case_id=CASE_ID,
            target_requirement="Build retrieval systems for AI context and memory.",
            engineering_truth_stage=TruthStage.APPROVED_CLAIM,
            ownership_confidence=OwnershipConfidence.UNKNOWN,
            mastery_level=MasteryLevel.L5_DEFEND,
            interview_ready=True,
            resume_eligible=True,
            approved_claim="Implemented bounded retrieval.",
            safe_verbs=["Implemented"],
            prohibited_phrasing=[],
            rationale="Mastery alone cannot prove personal ownership.",
        )

    with pytest.raises(ValidationError, match="interview_ready requires L5 Defend"):
        ClaimEligibility(
            case_id=CASE_ID,
            target_requirement="Build retrieval systems for AI context and memory.",
            engineering_truth_stage=TruthStage.VERIFIED_EVIDENCE,
            ownership_confidence=OwnershipConfidence.UNKNOWN,
            mastery_level=MasteryLevel.L0_SEEN,
            interview_ready=True,
            resume_eligible=False,
            approved_claim=None,
            safe_verbs=[],
            prohibited_phrasing=[],
            rationale="Interview readiness cannot exist at L0.",
        )


def test_repository_identity_accepts_only_verified_detached_github_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = _git("rev-parse", "HEAD")
    original_git = learning_traceability._git

    def detached_git(
        repository_root: Path, *args: str, required: bool = True
    ) -> str | None:
        if args == ("branch", "--show-current"):
            return ""
        return original_git(repository_root, *args, required=required)

    monkeypatch.setattr(learning_traceability, "_git", detached_git)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/8/merge")
    monkeypatch.setenv("GITHUB_SHA", commit)

    identity = learning_traceability._repository_identity(REPOSITORY_ROOT)
    assert identity.branch == "pull/8/merge"
    assert identity.commit == commit

    monkeypatch.setenv("GITHUB_SHA", "0" * 40)
    with pytest.raises(LearningTraceabilityError, match="verified GitHub Actions ref"):
        learning_traceability._repository_identity(REPOSITORY_ROOT)


def test_golden_case_writes_private_grounded_traceability_packet(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    run_dir = Path(run.private_run_path)

    assert run.case_id == CASE_ID
    assert run.case_kind == "SEED_CASE"
    assert run.project_source_id.startswith("source-")
    assert run.branch == _expected_repository_ref()
    assert run.commit == _git("rev-parse", "HEAD")
    assert run.engineering_state == "ENGINEERING_VERIFIED"
    assert run.mastery_level is MasteryLevel.L0_SEEN
    assert run.next_action is MasteryAction.EXPLAIN
    assert run.model_calls == 0
    assert run.network_used is False
    assert tuple(sorted(path.name for path in run_dir.iterdir())) == tuple(
        sorted(ARTIFACT_FILES)
    )
    assert os.stat(data_root).st_mode & 0o777 == 0o700
    assert os.stat(run_dir).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in run_dir.iterdir())

    case = _read_json(run_dir / "01_case.json")
    assert case["engineering_state"] == "ENGINEERING_VERIFIED"
    assert case["missing_evidence"] == [
        "Raw case-specific conversation receipt was not provided to this run.",
        "No CI execution receipt is asserted.",
    ]
    input_payload = _read_json(run_dir / "00_input.json")
    assert input_payload["private_source_bodies_read"] is False
    assert input_payload["project_binding"]["project_source_id"] == run.project_source_id
    assert case["project_binding"]["project_source_id"] == run.project_source_id

    anchors = _read_json(run_dir / "03_code_anchors.json")
    code_anchors = anchors["code_anchors"]
    verification_anchors = anchors["verification_anchors"]
    assert isinstance(code_anchors, list)
    assert isinstance(verification_anchors, list)
    all_anchors = [*code_anchors, *verification_anchors]
    assert len(all_anchors) == 8
    for raw_anchor in all_anchors:
        assert isinstance(raw_anchor, dict)
        relative_file = str(raw_anchor["file"])
        assert _git("ls-files", "--error-unmatch", relative_file) == relative_file
        committed_file = subprocess.run(
            ["git", "show", f"{run.commit}:{relative_file}"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert raw_anchor["file_sha256"] == hashlib.sha256(committed_file).hexdigest()
        assert 1 <= int(raw_anchor["line_start"]) <= int(raw_anchor["line_end"])
        line_count = len(committed_file.decode("utf-8").splitlines())
        assert int(raw_anchor["line_end"]) <= line_count

    graph = _read_json(run_dir / "04_evidence_graph.json")
    nodes = graph["nodes"]
    edges = graph["edges"]
    assert isinstance(nodes, list) and isinstance(edges, list)
    node_ids = {node["id"] for node in nodes}
    assert {
        "PROJECT-SOLOSCALE",
        "CONCEPT-CHUNKING",
        "CODE-CHUNKING",
        "TEST-CHUNKING",
    } <= node_ids
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in edges)
    assert any(
        edge["source"] == "CONCEPT-CHUNKING"
        and edge["target"] == "MASTERY-CURRENT"
        for edge in edges
    )

    contribution = _read_json(run_dir / "05_contribution.json")
    mastery = _read_json(run_dir / "06_mastery.json")
    claim = _read_json(run_dir / "09_claim_eligibility.json")
    verification = _read_json(run_dir / "10_verification.json")
    assert contribution["ownership_confidence"] == "unknown"
    assert contribution["implementation_performed_by"] is None
    assert mastery["level"] == "L0 Seen"
    assert mastery["interview_ready"] is False
    assert claim["engineering_truth_stage"] == "VERIFIED_EVIDENCE"
    assert claim["resume_eligible"] is False
    assert claim["interview_ready"] is False
    assert claim["approved_claim"] is None
    assert "personal contribution is not proven" in claim["rationale"]
    assert "personal contribution and L5" not in claim["rationale"]
    assert verification["tests_executed_by_learning_run"] is False
    assert verification["approved_claim_created"] is False
    assert EvidenceHub(data_root).status().asset_count == len(ARTIFACT_FILES)


@pytest.mark.parametrize("filename", ["main.py", "package.json"])
def test_generic_git_project_is_accepted_but_seed_case_owns_its_anchors(
    tmp_path: Path, filename: str
) -> None:
    repository = tmp_path / filename.replace(".", "-")
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    (repository / filename).write_text("project evidence\n", encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "project evidence",
        ],
        cwd=repository,
        check=True,
    )

    binding = inspect_learning_project(repository)

    assert binding.commit == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert binding.branch == "main"
    assert "src/soloscale/knowledge_store.py" in missing_learning_case_anchors(
        repository
    )
    with pytest.raises(LearningFixtureAnchorError):
        run_learning_traceability(data_root=tmp_path / "data", repository_root=repository)


def test_learning_material_is_cached_by_evidence_hash(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    first = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
        target_requirement="Build retrieval systems for AI context and memory.",
    )
    second = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
        target_requirement="Build retrieval systems for AI context and memory.",
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.cache_key == second.cache_key
    assert first.run_id != second.run_id
    first_plan = _read_json(Path(first.private_run_path) / "07_learning_plan.json")
    second_plan = _read_json(Path(second.private_run_path) / "07_learning_plan.json")
    assert first_plan == second_plan
    assert first_plan["evidence_hash"] == first.cache_key


def test_learning_run_rejects_symlinked_private_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / ".soloscale"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(LearningTraceabilityError, match="cannot be a symlink"):
        run_learning_traceability(
            data_root=linked,
            repository_root=REPOSITORY_ROOT,
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("file_sha256", "0" * 64),
        ("symbol", "missing_symbol"),
        ("line_start", 999999),
    ],
)
def test_interview_anchor_pack_rejects_tampered_code_anchor(
    tmp_path: Path, field: str, replacement: object
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    anchor_path = Path(run.private_run_path) / "03_code_anchors.json"
    payload = _read_json(anchor_path)
    code_anchors = payload["code_anchors"]
    assert isinstance(code_anchors, list)
    assert isinstance(code_anchors[0], dict)
    code_anchors[0][field] = replacement
    anchor_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LearningTraceabilityError):
        load_interview_anchor_pack(
            data_root=data_root,
            repository_root=REPOSITORY_ROOT,
            run_id=run.run_id,
        )


def test_interview_anchor_pack_rejects_symlinked_artifact(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    anchor_path = Path(run.private_run_path) / "03_code_anchors.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(anchor_path.read_bytes())
    anchor_path.unlink()
    anchor_path.symlink_to(outside)

    with pytest.raises(LearningTraceabilityError, match="unavailable"):
        load_interview_anchor_pack(
            data_root=data_root,
            repository_root=REPOSITORY_ROOT,
            run_id=run.run_id,
        )


def test_learning_response_is_private_pending_and_does_not_advance_mastery(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    run_dir = Path(run.private_run_path)
    mastery_before = (run_dir / "06_mastery.json").read_bytes()

    receipt, receipt_path = save_learning_response(
        data_root=data_root,
        run_id=run.run_id,
        stage=MasteryAction.EXPLAIN,
        response="I traced stable UTF-8 chunk boundaries and their semantic limit.",
    )

    assert receipt.status == "SUBMITTED_REQUIRES_REVIEW"
    assert receipt.mastery_advanced is False
    assert receipt.truth_stage.value == "RAW_STATEMENT"
    assert receipt.response.startswith("I traced")
    assert receipt_path.parent == run_dir / "practice-responses"
    assert os.stat(receipt_path.parent).st_mode & 0o777 == 0o700
    assert os.stat(receipt_path).st_mode & 0o777 == 0o600
    assert _read_json(receipt_path)["mastery_advanced"] is False
    assert (run_dir / "06_mastery.json").read_bytes() == mastery_before


def test_learning_response_rejects_invalid_input_and_symlinked_storage(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    with pytest.raises(ValueError, match="must not be empty"):
        save_learning_response(
            data_root=data_root,
            run_id=run.run_id,
            stage=MasteryAction.EXPLAIN,
            response="   ",
        )
    with pytest.raises(ValueError, match="only Explain and Trace"):
        save_learning_response(
            data_root=data_root,
            run_id=run.run_id,
            stage=MasteryAction.REBUILD,
            response="A response",
        )

    outside = tmp_path / "outside-responses"
    outside.mkdir()
    response_root = Path(run.private_run_path) / "practice-responses"
    response_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LearningTraceabilityError, match="cannot be a symlink"):
        save_learning_response(
            data_root=data_root,
            run_id=run.run_id,
            stage=MasteryAction.TRACE,
            response="A bounded trace response",
        )
    assert list(outside.iterdir()) == []
