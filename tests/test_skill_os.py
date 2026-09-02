import hashlib
import json
import shutil
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import soloscale.skill_os as skill_os_module
from soloscale.cli import app
from soloscale.skill_models import (
    RunOutcomeState,
    SkillRegistry,
    SkillRunReceipt,
    SkillRunStatus,
)
from soloscale.skill_os import (
    SkillOSError,
    default_registry_path,
    load_skill_registry,
    persist_route_receipt,
    route_skill_request,
)

runner = CliRunner()


def test_registry_loads_exact_versioned_skill_set() -> None:
    registry = load_skill_registry()
    template_root = default_registry_path().parents[1] / "task-templates"
    task_schema = json.loads((template_root / "task-envelope.schema.json").read_text())

    assert [skill.name for skill in registry.skills] == [
        "task-intake-and-routing",
        "evidence-refresh-and-bundle",
        "editorial-package",
        "visual-storytelling",
        "linkedin-publishing",
        "x-thread-publishing",
        "resume-tailoring",
        "learning-gap-to-packet",
        "skill-distillation",
    ]
    assert not any(skill.status.value == "ACTIVE" for skill in registry.skills)
    assert all(skill.current_version == "0.1.0" for skill in registry.skills)
    assert registry.get("editorial-package").dependencies == [
        "evidence-refresh-and-bundle"
    ]
    assert registry.get("linkedin-publishing").dependencies == []
    example_task = route_skill_request("Refresh the latest Evidence bundle.").task
    task_keys = set(example_task.model_dump(mode="json"))
    assert task_keys <= set(task_schema["properties"])
    assert set(task_schema["required"]) <= task_keys
    receipt_schema = json.loads((template_root / "run-receipt.schema.json").read_text())
    assert receipt_schema["additionalProperties"] is False
    assert receipt_schema["$defs"]["selected_skill"]["additionalProperties"] is False
    for skill in registry.skills:
        contract = json.loads(
            (default_registry_path().parent / skill.name / "contract.schema.json").read_text()
        )
        assert contract["additionalProperties"] is False
        assert contract["properties"]["inputs"]["additionalProperties"] is False
        assert contract["properties"]["outputs"]["additionalProperties"] is False


def test_editorial_scenario_selects_evidence_review_and_visual_route() -> None:
    route = route_skill_request(
        "Use the latest Evidence about Learning Debt. Create LinkedIn, X Thread, and one "
        "diagram. Fresh-review and revise it. Stop before publication."
    )

    assert route.dependency_order == [
        "task-intake-and-routing",
        "evidence-refresh-and-bundle",
        "editorial-package",
        "visual-storytelling",
    ]
    assert route.primary_skill.name == "editorial-package"
    assert route.human_gates == [
        "Fact-check before public use",
        "Public-safety review before public use",
    ]
    assert route.unmet_preconditions == []
    assert not route.task.publication_intent
    assert all(phase.actual_provider_used is None for phase in route.model_route)
    assert all(phase.actual_model_used is None for phase in route.model_route)
    assert all(phase.actual_reasoning_effort is None for phase in route.model_route)

    dependency_route = route_skill_request("Create a LinkedIn draft and fresh-review it.")
    assert dependency_route.dependency_order == [
        "task-intake-and-routing",
        "evidence-refresh-and-bundle",
        "editorial-package",
    ]


def test_career_scenario_preserves_profile_and_learning_boundaries(tmp_path: Path) -> None:
    route = route_skill_request(
        "Use this JD and my approved profile to create a one-page resume, Evidence map, "
        "and Learning Gaps. Stop before application submission."
    )

    assert route.dependency_order == [
        "task-intake-and-routing",
        "evidence-refresh-and-bundle",
        "resume-tailoring",
        "learning-gap-to-packet",
    ]
    assert "Resume claims come only from the approved Candidate Profile" in (
        route.task.evidence_requirements
    )
    assert not route.task.application_submission_intent
    assert route.primary_skill.name == "resume-tailoring"
    assert route.unmet_preconditions == []
    assert route.human_gates == [
        "Candidate Profile approval before resume generation",
        "Application review before submission",
        "Mastery promotion review",
    ]

    missing_profile = route_skill_request("Use this JD to create a one-page resume.")
    assert missing_profile.unmet_preconditions == ["Operator-approved Candidate Profile"]
    receipt, _ = persist_route_receipt(missing_profile, data_root=tmp_path)
    assert receipt.final_status is SkillRunStatus.AWAITING_HUMAN_GATE


def test_publishing_scenario_stops_at_one_explicit_public_gate() -> None:
    route = route_skill_request("Publish the approved Day 2 package to LinkedIn and X.")

    assert route.dependency_order == [
        "task-intake-and-routing",
        "linkedin-publishing",
        "x-thread-publishing",
    ]
    assert route.primary_skill.name == "linkedin-publishing"
    assert route.task.publication_intent
    assert route.human_gates == [
        "Explicit PUBLISH confirmation immediately before platform calls"
    ]

    linkedin_only = route_skill_request(
        "Publish the approved package to LinkedIn, but do not publish to X."
    )
    assert linkedin_only.dependency_order == [
        "task-intake-and-routing",
        "linkedin-publishing",
    ]
    assert linkedin_only.task.publication_intent
    same_clause = route_skill_request(
        "Publish the approved package to LinkedIn and do not publish to X."
    )
    compact_negation = route_skill_request(
        "Publish the approved package to LinkedIn, not to X."
    )
    assert same_clause.dependency_order == linkedin_only.dependency_order
    assert compact_negation.dependency_order == linkedin_only.dependency_order


def test_route_receipt_is_private_non_overwriting_and_state_exact(tmp_path: Path) -> None:
    request = "Use the latest Evidence to create a LinkedIn draft. Stop before publication."
    route = route_skill_request(request)

    receipt, receipt_path = persist_route_receipt(route, data_root=tmp_path)
    second_receipt, second_path = persist_route_receipt(route, data_root=tmp_path)

    assert receipt.final_status is SkillRunStatus.ROUTED
    assert receipt.outcome_state == RunOutcomeState()
    assert second_receipt.receipt_id != receipt.receipt_id
    assert second_path != receipt_path
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "skills" / "runs").stat().st_mode) == 0o700
    route_path = tmp_path / "skills" / "runs" / receipt.receipt_id / "route.json"
    assert receipt.output_artifacts[0].sha256 == hashlib.sha256(route_path.read_bytes()).hexdigest()
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_schema = json.loads(
        (default_registry_path().parents[1] / "task-templates" / "run-receipt.schema.json")
        .read_text(encoding="utf-8")
    )
    assert "request_text" not in payload
    assert set(payload) <= set(receipt_schema["properties"])
    assert set(receipt_schema["required"]) <= set(payload)
    assert payload["input_hashes"]["operator_request"] == route.task.request_sha256
    assert not payload["outcome_state"]["workflow_completed"]


def test_registry_and_outcome_contracts_fail_closed(tmp_path: Path) -> None:
    payload = json.loads(default_registry_path().read_text(encoding="utf-8"))
    payload["skills"][0]["dependencies"] = ["missing-skill"]
    invalid = tmp_path / "registry.yaml"
    invalid.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SkillOSError, match="validation failed"):
        load_skill_registry(invalid)
    with pytest.raises(ValidationError, match="requires human approval"):
        RunOutcomeState(externally_published_or_submitted=True)

    conflict_payload = json.loads(default_registry_path().read_text(encoding="utf-8"))
    conflict_payload["skills"][1]["incompatible_skills"] = ["editorial-package"]
    conflicting_registry = SkillRegistry.model_validate(conflict_payload)
    with pytest.raises(SkillOSError, match="incompatible"):
        route_skill_request(
            "Create a LinkedIn draft and fresh-review it.",
            registry=conflicting_registry,
        )

    valid_receipt, _ = persist_route_receipt(
        route_skill_request("Refresh the latest Evidence bundle."),
        data_root=tmp_path / "receipts",
    )
    contradictory = valid_receipt.model_dump(mode="json")
    contradictory["final_status"] = "COMPLETED"
    with pytest.raises(ValidationError, match="workflow_completed"):
        SkillRunReceipt.model_validate(contradictory)
    receipt_schema = json.loads(
        (default_registry_path().parents[1] / "task-templates" / "run-receipt.schema.json")
        .read_text(encoding="utf-8")
    )
    contradiction_state = contradictory["outcome_state"]
    completed_rule = receipt_schema["allOf"][2]["then"]["properties"]["outcome_state"]
    assert completed_rule["properties"]["workflow_completed"] == {"const": True}
    assert contradiction_state["workflow_completed"] is False

    tracked_root = tmp_path / "tracked"
    (tracked_root / ".git").mkdir(parents=True)
    with pytest.raises(SkillOSError, match="Git worktree"):
        persist_route_receipt(
            route_skill_request("Refresh the latest Evidence bundle."),
            data_root=tracked_root / "private-runs",
        )

    private_root = tmp_path / "private"
    redirected = tmp_path / "redirected"
    private_root.mkdir()
    redirected.mkdir()
    (private_root / "skills").symlink_to(redirected, target_is_directory=True)
    with pytest.raises(SkillOSError, match="symlink"):
        persist_route_receipt(
            route_skill_request("Refresh the latest Evidence bundle."),
            data_root=private_root,
        )


def test_cli_lists_and_routes_without_external_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = runner.invoke(app, ["skill-list"])
    routed = runner.invoke(
        app,
        [
            "skill-route",
            "Publish the approved Day 2 package to LinkedIn and X.",
            "--data-root",
            str(tmp_path),
        ],
    )

    assert listed.exit_code == 0
    assert "task-intake-and-routing" in listed.stdout
    assert routed.exit_code == 0
    assert "AWAITING_HUMAN_GATE" in routed.stdout
    receipt_paths = list((tmp_path / "skills" / "receipts").glob("*.json"))
    assert len(receipt_paths) == 1
    receipt = SkillRunReceipt.model_validate_json(receipt_paths[0].read_text(encoding="utf-8"))
    assert not receipt.outcome_state.externally_published_or_submitted

    canonical_agents = default_registry_path().parents[1]
    installed_site = tmp_path / "installed-site"
    shutil.copytree(
        canonical_agents,
        installed_site / "share" / "soloscale-ai-os" / ".agents",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.delenv("SOLOSCALE_SKILL_REGISTRY", raising=False)
    monkeypatch.setattr(
        skill_os_module,
        "__file__",
        str(installed_site / "soloscale" / "skill_os.py"),
    )
    assert load_skill_registry().get("task-intake-and-routing").current_version == "0.1.0"
