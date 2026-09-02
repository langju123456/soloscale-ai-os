from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from soloscale.casebook_models import (
    AttemptOutcome,
    EvidenceKind,
    LearningCase,
    PracticeStage,
)
from soloscale.casebook_store import CasebookStore, SequentialPracticeError
from soloscale.learning_models import MasteryAction, MasteryLevel
from soloscale.learning_practice import (
    TUTOR_MODE_CONTRACT,
    ExerciseStatus,
    ExerciseType,
    LearningExercise,
    PracticeCompletionReceipt,
    PracticeLanguage,
    TutorEscalation,
    create_practice_workspace,
    exercise_to_practice_stage,
    generate_practice_exercise,
    ingest_practice_completion,
    list_exercises,
    load_exercise,
    open_practice_workspace,
    practice_workspace_root,
    save_exercise,
    tutor_mode_prompt,
)


class _CaseFacts(TypedDict):
    case_id: str
    title: str
    project: str
    problem: str
    expected_behavior: str
    actual_behavior: str
    root_cause: str
    resolution: str
    verification: list[str]
    concepts: list[str]
    repository: str
    alternatives_considered: list[str]
    trade_offs: list[str]
    unknowns: list[str]


def _case_facts(case_id: str = "practice-exercise-case") -> _CaseFacts:
    return {
        "case_id": case_id,
        "title": "Cache invalidation failure",
        "project": "SoloScale AI OS",
        "problem": "A stale response survived a completed mutation.",
        "expected_behavior": "The mutation exposes the current response.",
        "actual_behavior": "The stale response remained visible.",
        "root_cause": "The mutation omitted cache-tag invalidation.",
        "resolution": "Invalidate the tag after the transaction commits.",
        "verification": ["The focused regression test passes."],
        "concepts": ["cache invalidation", "transaction boundaries"],
        "repository": "example/soloscale",
        "alternatives_considered": ["Disable the cache."],
        "trade_offs": ["Fresh reads add invalidation work."],
        "unknowns": ["Cross-region propagation remains unmeasured."],
    }


def _create_case(tmp_path: Path) -> tuple[CasebookStore, LearningCase]:
    source = tmp_path / "raw evidence.txt"
    source.write_bytes(b"private evidence bytes")
    data_root = tmp_path / "data"
    store = CasebookStore(data_root)
    case = store.create_case(
        **_case_facts(),
        evidence_sources=[(EvidenceKind.TEST, source)],
    )
    return store, case


def test_generate_exercise_derives_real_gap_from_case(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement="Design reliable cache invalidation flows.",
        resume_claim="Built cache invalidation for a production API.",
        exercise_type=ExerciseType.IMPLEMENT,
        practice_language=PracticeLanguage.PYTHON,
    )

    assert exercise.case_id == case.id
    assert exercise.exercise_type is ExerciseType.IMPLEMENT
    assert exercise.mastery_capability is MasteryAction.REBUILD
    assert exercise.evidence_ids == [receipt.id for receipt in case.evidence]
    assert exercise.jd_requirement == "Design reliable cache invalidation flows."
    assert exercise.resume_claim == "Built cache invalidation for a production API."
    assert case.title in exercise.objective
    assert len(exercise.acceptance_criteria) >= 3
    assert exercise.status is ExerciseStatus.GENERATED
    assert exercise.workspace_path is None


def test_exercise_types_map_to_mastery_capabilities_and_stages() -> None:
    assert exercise_to_practice_stage(ExerciseType.EXPLAIN) is PracticeStage.EXPLAIN
    assert exercise_to_practice_stage(ExerciseType.TRACE) is PracticeStage.TRACE
    assert exercise_to_practice_stage(ExerciseType.IMPLEMENT) is PracticeStage.REBUILD
    assert exercise_to_practice_stage(ExerciseType.DSA) is PracticeStage.REBUILD
    assert exercise_to_practice_stage(ExerciseType.DEBUG) is PracticeStage.DEBUG
    assert exercise_to_practice_stage(ExerciseType.SYSTEM_DESIGN) is PracticeStage.DEFEND
    assert exercise_to_practice_stage(ExerciseType.DEFEND) is PracticeStage.DEFEND


def test_create_practice_workspace_writes_readme_starter_tests_and_exercise(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(case=case, jd_requirement="Implement cache invalidation.")
    workspace = create_practice_workspace(exercise, tmp_path / "data")

    assert workspace == practice_workspace_root(tmp_path / "data") / exercise.id
    readme = (workspace / "README.md").read_text(encoding="utf-8")
    assert "## Bounded task" in readme
    assert TUTOR_MODE_CONTRACT.splitlines()[0] in readme
    assert "python -m pytest -q" in readme
    assert (workspace / "starter.py").is_file()
    assert (workspace / "test_task.py").is_file()
    assert "NotImplementedError" in (workspace / "starter.py").read_text(encoding="utf-8")
    stored = load_exercise(exercise.id, tmp_path / "data")
    assert stored.status is ExerciseStatus.USER_PRACTICE_REQUIRED
    assert stored.workspace_path == str(workspace)


def test_non_python_workspace_gets_task_markdown(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement="Design cache invalidation.",
        exercise_type=ExerciseType.SYSTEM_DESIGN,
        practice_language=PracticeLanguage.GENERIC,
    )
    workspace = create_practice_workspace(exercise, tmp_path / "data")
    assert (workspace / "task.md").is_file()
    assert not (workspace / "test_task.py").exists()


def test_open_practice_workspace_reports_missing_code_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(case=case, jd_requirement="Implement cache invalidation.")
    workspace = create_practice_workspace(exercise, tmp_path / "data")
    monkeypatch.setattr("soloscale.learning_practice.shutil.which", lambda _name: None)
    assert open_practice_workspace(workspace) is False


def test_tutor_mode_prompt_scopes_contract_to_exercise(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(case=case, jd_requirement="Implement cache invalidation.")
    prompt = tutor_mode_prompt(exercise)
    assert exercise.id in prompt
    assert "guiding_question" in prompt
    assert "full_solution" in prompt
    assert "Do not silently edit" in prompt


def test_completion_with_evidence_advances_mastery(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement="Explain cache invalidation.",
        exercise_type=ExerciseType.EXPLAIN,
    )
    workspace = create_practice_workspace(exercise, tmp_path / "data")
    evidence = workspace / "my-answer.md"
    evidence.write_text("My bounded explanation with a trade-off and a truth boundary.")

    receipt, result = ingest_practice_completion(
        store=store,
        exercise=exercise,
        evidence_path=evidence,
        files_changed=["my-answer.md"],
        tests_passed=0,
        tests_failed=0,
        attempts=1,
        hints_used=[TutorEscalation.CONCEPTUAL_HINT],
        note="Self-assessed explain pass.",
    )

    assert isinstance(receipt, PracticeCompletionReceipt)
    assert receipt.completed is True
    assert receipt.mastery_before is MasteryLevel.L0_SEEN
    assert receipt.mastery_after is MasteryLevel.L1_EXPLAIN
    assert receipt.interview_ready is False
    assert receipt.user_code_sha256 is not None
    assert result.mastery.passed_stages == [PracticeStage.EXPLAIN]
    assert result.mastery.next_stage is PracticeStage.TRACE
    stored = load_exercise(exercise.id, tmp_path / "data")
    assert stored.status is ExerciseStatus.COMPLETED
    assert (workspace / "completion-receipt.json").is_file()


def test_no_evidence_never_records_a_pass(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement="Explain cache invalidation.",
        exercise_type=ExerciseType.EXPLAIN,
    )
    receipt, result = ingest_practice_completion(
        store=store,
        exercise=exercise,
        tests_passed=0,
        tests_failed=0,
        note="Attempted the exercise but could not attach evidence.",
    )
    assert receipt.completed is False
    assert result.mastery.passed_stages == []
    assert result.mastery.next_stage is PracticeStage.EXPLAIN


def test_needs_work_completion_requires_note(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement="Explain cache invalidation.",
        exercise_type=ExerciseType.EXPLAIN,
    )
    with pytest.raises(ValueError, match="note"):
        ingest_practice_completion(
            store=store,
            exercise=exercise,
            tests_passed=0,
            tests_failed=0,
            attempts=1,
        )


def test_sequential_mastery_gate_rejects_jumping_ahead(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement="Implement cache invalidation.",
        exercise_type=ExerciseType.IMPLEMENT,
    )
    workspace = create_practice_workspace(exercise, tmp_path / "data")
    evidence = workspace / "solution.py"
    evidence.write_text("def solve():\n    return True\n")

    with pytest.raises(SequentialPracticeError, match="rebuild"):
        ingest_practice_completion(
            store=store,
            exercise=exercise,
            evidence_path=evidence,
            tests_passed=1,
            tests_failed=0,
            note="Implementation without passing Explain.",
        )


def test_list_exercises_roundtrip(tmp_path: Path) -> None:
    store, case = _create_case(tmp_path)
    exercise = generate_practice_exercise(case=case, jd_requirement="Explain cache invalidation.")
    workspace = create_practice_workspace(exercise, tmp_path / "data")
    save_exercise(exercise.model_copy(update={"workspace_path": str(workspace)}), tmp_path / "data")

    exercises = list_exercises(tmp_path / "data")
    assert [item.id for item in exercises] == [exercise.id]


def test_ci_cd_case_generates_capability_specific_workflow_practice(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ci evidence.txt"
    source.write_bytes(b"ci evidence bytes")
    data_root = tmp_path / "data"
    store = CasebookStore(data_root)
    case = store.create_case(
        case_id="ci-cd-automation",
        title="Automate SoloScale verification with GitHub Actions",
        project="SoloScale AI OS",
        problem="The repository runs verification locally but still needs owned CI/CD.",
        expected_behavior="A GitHub Actions workflow runs lint, type-check, tests, and build.",
        actual_behavior="The workflow exists but still needs deliberate practice.",
        root_cause="Verification was historically orchestrated manually.",
        resolution="Complete a bounded CI/CD workflow exercise and run local validation.",
        verification=["ruff check .", "mypy src tests", "pytest -q", "python -m build"],
        concepts=["CI/CD", "GitHub Actions", "test automation", "verification gates"],
        repository="example/soloscale",
        alternatives_considered=["Manual verification."],
        trade_offs=["Automation adds pipeline maintenance."],
        unknowns=["Cross-platform runner timing."],
        evidence_sources=[(EvidenceKind.CI, source)],
    )
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement=(
            "Automate software verification with CI/CD: run lint, type-check, "
            "tests, and build gates on push and pull requests."
        ),
        exercise_type=ExerciseType.IMPLEMENT,
        practice_language=PracticeLanguage.PYTHON,
    )
    assert exercise.practice_language is PracticeLanguage.YAML
    assert exercise.capability_domain == "CI_CD"
    assert "GitHub Actions" in exercise.objective
    assert ".github/workflows/ci.yml" in exercise.bounded_task

    workspace = create_practice_workspace(exercise, data_root)
    assert (workspace / ".github/workflows/ci.yml").is_file()
    assert (workspace / "validate.py").is_file()
    assert not (workspace / "starter.py").exists()
    assert not (workspace / "test_task.py").exists()
    assert "python validate.py" in (workspace / "README.md").read_text(encoding="utf-8")


def _ci_cd_exercise(tmp_path: Path) -> tuple[CasebookStore, LearningExercise, Path]:
    source = tmp_path / "ci evidence.txt"
    source.write_bytes(b"ci evidence bytes")
    data_root = tmp_path / "data"
    store = CasebookStore(data_root)
    case = store.create_case(
        case_id="ci-cd-automation",
        title="Automate SoloScale verification with GitHub Actions",
        project="SoloScale AI OS",
        problem="The repository runs verification locally but still needs owned CI/CD.",
        expected_behavior="A GitHub Actions workflow runs lint, type-check, tests, and build.",
        actual_behavior="The workflow exists but still needs deliberate practice.",
        root_cause="Verification was historically orchestrated manually.",
        resolution="Complete a bounded CI/CD workflow exercise and run local validation.",
        verification=["ruff check .", "mypy src tests", "pytest -q", "python -m build"],
        concepts=["CI/CD", "GitHub Actions", "test automation", "verification gates"],
        repository="example/soloscale",
        alternatives_considered=["Manual verification."],
        trade_offs=["Automation adds pipeline maintenance."],
        unknowns=["Cross-platform runner timing."],
        evidence_sources=[(EvidenceKind.CI, source)],
    )
    exercise = generate_practice_exercise(
        case=case,
        jd_requirement=(
            "Automate software verification with CI/CD: run lint, type-check, "
            "tests, and build gates on push and pull requests."
        ),
        exercise_type=ExerciseType.IMPLEMENT,
        practice_language=PracticeLanguage.PYTHON,
    )
    workspace = create_practice_workspace(exercise, data_root)
    return store, load_exercise(exercise.id, data_root), workspace


def test_ci_cd_completion_rejects_arbitrary_evidence(tmp_path: Path) -> None:
    store, exercise, workspace = _ci_cd_exercise(tmp_path)
    evidence = workspace / "random.txt"
    evidence.write_text("this is not a workflow", encoding="utf-8")

    receipt, result = ingest_practice_completion(
        store=store,
        exercise=exercise,
        evidence_path=evidence,
        note="attempted",
    )

    assert receipt.completed is False
    assert result.mastery.passed_stages == []


def test_ci_cd_completion_rejects_workflow_missing_gate(tmp_path: Path) -> None:
    store, exercise, workspace = _ci_cd_exercise(tmp_path)
    workflow = workspace / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        "on: [push, pull_request]\n"
        "jobs:\n"
        "  verify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: pytest\n"
        "      - run: ruff check .\n"
        "      - run: python -m build\n",
        encoding="utf-8",
    )
    evidence = workspace / "evidence.md"
    evidence.write_text("completed workflow", encoding="utf-8")

    receipt, result = ingest_practice_completion(
        store=store,
        exercise=exercise,
        evidence_path=evidence,
        note="attempted",
    )

    assert receipt.completed is False
    assert result.mastery.passed_stages == []


def test_ci_cd_completion_passes_with_valid_workflow_without_auto_mastery(
    tmp_path: Path,
) -> None:
    store, exercise, workspace = _ci_cd_exercise(tmp_path)
    for stage, name in (
        (PracticeStage.EXPLAIN, "explain"),
        (PracticeStage.TRACE, "trace"),
    ):
        receipt = workspace / f"{name}.md"
        receipt.write_text(f"{name} evidence", encoding="utf-8")
        store.record_attempt(
            case_id=exercise.case_id,
            stage=stage,
            outcome=AttemptOutcome.PASS,
            receipt_path=receipt,
        )
    workflow = workspace / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        "name: SoloScale verification\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  verify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: pip install -e '.[dev]'\n"
        "      - run: pytest -q\n"
        "      - run: ruff check .\n"
        "      - run: mypy src tests\n"
        "      - run: python -m build\n",
        encoding="utf-8",
    )
    evidence = workspace / "evidence.md"
    evidence.write_text("completed CI/CD workflow", encoding="utf-8")

    receipt, result = ingest_practice_completion(
        store=store,
        exercise=exercise,
        evidence_path=evidence,
        note=None,
    )

    assert receipt.completed is True
    assert result.mastery.passed_stages == [
        PracticeStage.EXPLAIN,
        PracticeStage.TRACE,
        PracticeStage.REBUILD,
    ]
    assert result.mastery.interview_ready is False
    assert receipt.mastery_after is MasteryLevel.L3_REBUILD


def test_ci_cd_completion_rejects_failing_validator(tmp_path: Path) -> None:
    store, exercise, workspace = _ci_cd_exercise(tmp_path)
    workflow = workspace / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        "name: SoloScale verification\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  verify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: pytest -q\n"
        "      - run: ruff check .\n"
        "      - run: mypy src tests\n"
        "      - run: python -m build\n",
        encoding="utf-8",
    )
    (workspace / "validate.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")

    receipt, result = ingest_practice_completion(
        store=store,
        exercise=exercise,
        note="attempted",
    )

    assert receipt.completed is False
    assert result.mastery.passed_stages == []
