import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from soloscale.evidence_hub import EvidenceHub
from soloscale.knowledge_models import ContentRole, RetrievalHit, SourceKind
from soloscale.learning_traceability import run_learning_traceability
from soloscale.resume_models import CandidateProfile, JobResearchSource, ResumeMode, ResumeRun
from soloscale.resume_workspace import (
    ResumeWorkspaceStorageError,
    load_interview_defense_records,
    map_interview_defense_bullet,
    run_resume_workspace,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

AI_ENGINEER_JD = """AI Engineer
- Required: Python and RAG systems
- Required: Docker deployment and CI/CD
- Preferred: Kubernetes operations
"""


def _hit(chunk_id: str, excerpt: str) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        document_id="doc-1",
        source_kind=SourceKind.CODEX_SESSION,
        external_id="thread-1",
        locator="private",
        title="Local",
        role=ContentRole.ASSISTANT,
        timestamp=None,
        excerpt=excerpt,
        chunk_sha256="a" * 64,
        document_sha256="b" * 64,
        score=1,
        channels=["fts"],
    )


def _hits() -> list[RetrievalHit]:
    return [_hit("chunk-rag", "Python RAG evidence"), _hit("chunk-ci", "Docker CI/CD verification")]


def test_local_workspace_writes_private_artifacts_and_replayable_lineage(tmp_path: Path) -> None:
    run = run_resume_workspace(
        data_root=tmp_path / ".soloscale",
        job_description=AI_ENGINEER_JD,
        candidate_profile=CandidateProfile(
            summary="AI engineer",
            skills=["Python", "RAG"],
            experience_bullets=["Operator-supplied base-resume bullet."],
        ),
        evidence_hits=_hits(),
    )

    run_dir = tmp_path / ".soloscale" / "resume-runs" / run.run_id
    expected = {
        "00_input.json",
        "01_job_research.json",
        "02_requirements.json",
        "03_evidence_matches.json",
        "04_resume.md",
        "05_gaps.json",
        "06_graph.json",
        "07_verification.json",
        "interview_defense.json",
        "delivery.json",
        "run.json",
    }
    assert {path.name for path in run_dir.iterdir()} == expected
    assert stat.S_IMODE((tmp_path / ".soloscale" / "resume-runs").stat().st_mode) == 0o700
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in run_dir.iterdir())
    graph = json.loads((run_dir / "06_graph.json").read_text(encoding="utf-8"))
    assert {node["kind"] for node in graph["nodes"]} >= {"JOB", "REQUIREMENT", "EVIDENCE", "GAP"}
    assert not ({"PROJECT", "CODE", "VERIFICATION"} & {node["kind"] for node in graph["nodes"]})
    assert all({"source", "target", "relation"} <= set(edge) for edge in graph["edges"])
    verification = json.loads((run_dir / "07_verification.json").read_text(encoding="utf-8"))
    assert verification["candidate_lineage_replayable"] is True
    assert verification["resume_claims_reference_profile_entries"] is True
    assert verification["semantic_requirement_coverage_verified"] is False
    assert verification["coverage"]["total"] == 4
    matches = json.loads((run_dir / "03_evidence_matches.json").read_text(encoding="utf-8"))
    locator = matches[0]["locator"]
    assert locator["document_id"] == "doc-1"
    assert locator["source_kind"] == "codex_session"
    assert locator["external_id"] == "thread-1"
    assert locator["source_locator"] == "private"
    assert locator["chunk_sha256"] == "a" * 64
    assert locator["document_sha256"] == "b" * 64
    assert matches[0]["match_quality"].startswith("lexical_candidate_")
    defense = json.loads((run_dir / "interview_defense.json").read_text(encoding="utf-8"))
    assert defense["schema_version"] == "0.1"
    assert defense["resume_run_id"] == run.run_id
    assert len(defense["records"]) == 1
    assert defense["records"][0]["bullet_id"] == "PROFILE-01"
    assert defense["records"][0]["status"] == "NEEDS_MAPPING"
    assert len(run.artifact_paths) == len(set(run.artifact_paths))
    assert run.artifact_paths.count("interview_defense.json") == 1
    assert EvidenceHub(tmp_path / ".soloscale").status().asset_count == len(
        run.artifact_paths
    )


def test_interview_defense_mapping_is_explicit_and_validated(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    resume = run_resume_workspace(
        data_root=data_root,
        job_description="Required: Python",
        candidate_profile=CandidateProfile(experience_bullets=["Operator supplied Python work."]),
        evidence_hits=[],
    )
    learning = run_learning_traceability(data_root=data_root, repository_root=REPOSITORY_ROOT)

    mapped = map_interview_defense_bullet(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
        resume_run_id=resume.run_id,
        bullet_id="PROFILE-01",
        learning_run_id=learning.run_id,
    )

    assert mapped.status.value == "MAPPED"
    assert mapped.mapping is not None
    assert mapped.mapping.learning_run_id == learning.run_id
    assert mapped.mapping.case_id == "conversation-rag-chunking-retrieval"
    assert mapped.mapping.mapping_basis == "OPERATOR_CONFIRMED"
    assert load_interview_defense_records(data_root=data_root, run_id=resume.run_id)[0] == mapped

    with pytest.raises(ResumeWorkspaceStorageError, match="already mapped"):
        map_interview_defense_bullet(
            data_root=data_root,
            repository_root=REPOSITORY_ROOT,
            resume_run_id=resume.run_id,
            bullet_id="PROFILE-01",
            learning_run_id=learning.run_id,
        )


def test_interview_defense_rejects_tampered_bullet_identity(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    resume = run_resume_workspace(
        data_root=data_root,
        job_description="Required: Python",
        candidate_profile=CandidateProfile(
            experience_bullets=["Operator supplied Python work."]
        ),
        evidence_hits=[],
    )
    artifact = data_root / "resume-runs" / resume.run_id / "interview_defense.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["records"][0]["bullet_text"] = "Forged claim."
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResumeWorkspaceStorageError, match="do not match resume input"):
        load_interview_defense_records(data_root=data_root, run_id=resume.run_id)


def test_interview_defense_rejects_symlinked_data_root(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside-data"
    resume = run_resume_workspace(
        data_root=outside_root,
        job_description="Required: Python",
        candidate_profile=CandidateProfile(experience_bullets=["Operator Python work."]),
        evidence_hits=[],
    )
    linked_root = tmp_path / "linked-data"
    linked_root.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ResumeWorkspaceStorageError, match="ancestry"):
        load_interview_defense_records(data_root=linked_root, run_id=resume.run_id)


def test_interview_defense_records_follow_six_bullet_draft_limit(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    resume = run_resume_workspace(
        data_root=data_root,
        job_description="Required: Python",
        candidate_profile=CandidateProfile(
            experience_bullets=[f"Operator bullet {index}." for index in range(1, 8)]
        ),
        evidence_hits=[],
    )

    records = load_interview_defense_records(data_root=data_root, run_id=resume.run_id)

    assert [record.bullet_id for record in records] == [
        f"PROFILE-{index:02d}" for index in range(1, 7)
    ]
    assert all(record.status.value == "NEEDS_MAPPING" for record in records)


def test_profile_and_evidence_truth_boundary_and_repeat_runs(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    profile = CandidateProfile(experience_bullets=["Only operator supplied this experience."])
    first = run_resume_workspace(
        data_root=root,
        job_description=AI_ENGINEER_JD,
        candidate_profile=profile,
        evidence_hits=_hits(),
    )
    second = run_resume_workspace(
        data_root=root,
        job_description=AI_ENGINEER_JD,
        candidate_profile=profile,
        evidence_hits=_hits(),
    )

    assert first.run_id != second.run_id
    first_resume = (root / "resume-runs" / first.run_id / "04_resume.md").read_text(
        encoding="utf-8"
    )
    assert "Only operator supplied this experience." in first_resume
    assert "Python RAG evidence" not in first_resume
    assert "profile_entry_ids: PROFILE-01" in first_resume
    assert not (root / "resume-runs" / first.run_id / "04_resume.md").samefile(
        root / "resume-runs" / second.run_id / "04_resume.md"
    )


def test_dual_writes_application_bundle_without_overwriting(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    library_root = tmp_path / "Resume Applications"
    profile = CandidateProfile(
        full_name="Lang Ju",
        skills=["Python", "RAG"],
        project_bullets=["Built an operator-supplied Python RAG workflow."],
    )

    def run_once() -> ResumeRun:
        return run_resume_workspace(
            data_root=data_root,
            job_description=AI_ENGINEER_JD,
            candidate_profile=profile,
            evidence_hits=_hits(),
            company_name="Faros",
            company_url="https://www.linkedin.com/jobs/view/4432211307/",
            job_title="AI-Native Builder",
            job_id="4432211307",
            application_library_root=library_root,
        )

    first = run_once()
    second = run_once()

    application_dirs = sorted((library_root / "applications").iterdir())
    assert len(application_dirs) == 2
    assert stat.S_IMODE(library_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((library_root / "applications").stat().st_mode) == 0o700
    assert application_dirs[0].name.startswith("20")
    assert application_dirs[0].name != application_dirs[1].name
    for application_dir in application_dirs:
        assert {path.name for path in application_dir.iterdir()} == {
            "JD.md",
            "Resume_Lang-Ju_Faros_AI-Native-Builder.md",
            "application.json",
        }
        assert stat.S_IMODE(application_dir.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in application_dir.iterdir())
        jd = (application_dir / "JD.md").read_text(encoding="utf-8")
        assert "AI-Native Builder" in jd
        assert "4432211307" in jd
        resume = (application_dir / "Resume_Lang-Ju_Faros_AI-Native-Builder.md").read_text(
            encoding="utf-8"
        )
        assert "Built an operator-supplied Python RAG workflow." in resume
    first_run = json.loads(
        (data_root / "resume-runs" / first.run_id / "run.json").read_text(encoding="utf-8")
    )
    second_run = json.loads(
        (data_root / "resume-runs" / second.run_id / "run.json").read_text(encoding="utf-8")
    )
    assert first_run["route"]["application_library_saved"] is True
    assert second_run["route"]["application_library_saved"] is True
    assert (
        first_run["route"]["application_library_path"]
        != second_run["route"]["application_library_path"]
    )


def test_docx_is_complete_inside_staging_before_atomic_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from soloscale import resume_workspace

    content = b"synthetic-docx-bytes"
    filename = "Resume_Candidate_Example_AI-Engineer.docx"
    observed = False
    original_publish = resume_workspace._atomic_rename_directory_no_replace

    def inspect_before_publish(source: Path, destination: Path) -> None:
        nonlocal observed
        assert (source / filename).read_bytes() == content
        metadata = json.loads((source / "application.json").read_text(encoding="utf-8"))
        assert metadata["resume_docx_filename"] == filename
        assert metadata["resume_docx_sha256"] == hashlib.sha256(content).hexdigest()
        assert metadata["claims_preserved"] is True
        observed = True
        original_publish(source, destination)

    monkeypatch.setattr(
        resume_workspace,
        "_atomic_rename_directory_no_replace",
        inspect_before_publish,
    )
    run = run_resume_workspace(
        data_root=tmp_path / ".soloscale",
        job_description="Required: Python",
        candidate_profile=CandidateProfile(full_name="Candidate"),
        evidence_hits=[],
        company_name="Example",
        job_title="AI Engineer",
        application_library_root=tmp_path / "Resume Applications",
        application_resume_bytes=content,
        application_resume_filename=filename,
        application_resume_metadata={"claims_preserved": True},
    )

    assert observed is True
    run_dir = tmp_path / ".soloscale" / "resume-runs" / run.run_id
    application_dir = Path(str(run.route["application_library_path"]))
    assert (run_dir / "08_resume.docx").read_bytes() == content
    assert (application_dir / filename).read_bytes() == content


def test_hybrid_requires_explicit_provider_without_network(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit JobResearchProvider"):
        run_resume_workspace(
            data_root=tmp_path / ".soloscale",
            job_description=AI_ENGINEER_JD,
            candidate_profile=CandidateProfile(),
            evidence_hits=_hits(),
            mode=ResumeMode.HYBRID,
        )


def test_hybrid_route_records_network_boundary_when_provider_is_invoked(tmp_path: Path) -> None:
    class FakeProvider:
        def research(
            self, *, job_description: str, company_name: str | None, company_url: str | None
        ) -> list[JobResearchSource]:
            del job_description, company_name, company_url
            return [JobResearchSource(title="Imported research")]

    run = run_resume_workspace(
        data_root=tmp_path / ".soloscale",
        job_description="Required: Python",
        candidate_profile=CandidateProfile(),
        evidence_hits=[],
        mode=ResumeMode.HYBRID,
        research_provider=FakeProvider(),
    )
    payload = json.loads(
        (tmp_path / ".soloscale" / "resume-runs" / run.run_id / "run.json").read_text()
    )
    assert payload["route"]["network_allowed"] is True


def test_critical_generic_requirement_remains_gap_and_profile_ids_are_deterministic(
    tmp_path: Path,
) -> None:
    run = run_resume_workspace(
        data_root=tmp_path / ".soloscale",
        job_description="Required: leadership experience",
        candidate_profile=CandidateProfile(experience_bullets=["Led a local club."]),
        evidence_hits=[_hit("chunk", "leadership experience")],
    )
    run_dir = tmp_path / ".soloscale" / "resume-runs" / run.run_id
    verification = json.loads((run_dir / "07_verification.json").read_text(encoding="utf-8"))
    assert verification["coverage"]["no_lexical_candidate"] == 1
    resume = (run_dir / "04_resume.md").read_text(encoding="utf-8")
    assert "Led a local club." in resume
    assert "profile_entry_ids: PROFILE-01" in resume


def test_rejects_managed_symlink_and_tightens_existing_private_roots(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data_root = tmp_path / ".soloscale"
    data_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ResumeWorkspaceStorageError, match="symlink"):
        run_resume_workspace(
            data_root=data_root,
            job_description="Required: Python",
            candidate_profile=CandidateProfile(),
            evidence_hits=[],
        )
    assert list(outside.iterdir()) == []

    data_root.unlink()
    data_root.mkdir(mode=0o755)
    library_root = tmp_path / "Resume Applications"
    library_root.mkdir(mode=0o755)
    run_resume_workspace(
        data_root=data_root,
        job_description="Required: Python",
        candidate_profile=CandidateProfile(),
        evidence_hits=[],
        application_library_root=library_root,
    )
    assert stat.S_IMODE(data_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(library_root.stat().st_mode) == 0o700


def test_rejects_symlink_in_managed_root_ancestry(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ResumeWorkspaceStorageError, match="ancestry"):
        run_resume_workspace(
            data_root=linked_parent / ".soloscale",
            job_description="Required: Python",
            candidate_profile=CandidateProfile(),
            evidence_hits=[],
        )

    assert list(outside.iterdir()) == []


def test_post_replace_fsync_failure_preserves_final_resume_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from soloscale import resume_workspace

    target = tmp_path / "receipt.json"
    target.write_text("old", encoding="utf-8")
    original_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory durability failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="durability"):
        resume_workspace._atomic_private_write(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
def test_external_failure_keeps_recovery_receipt_and_no_partial_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from soloscale import resume_workspace

    original = resume_workspace._atomic_private_write

    def fail_resume_file(path: Path, text: str) -> None:
        if path.parent.name.endswith(".staging") and path.name.startswith("Resume_"):
            raise OSError("private detail must not escape")
        original(path, text)

    monkeypatch.setattr(resume_workspace, "_atomic_private_write", fail_resume_file)
    data_root = tmp_path / ".soloscale"
    library_root = tmp_path / "Resume Applications"
    with pytest.raises(ResumeWorkspaceStorageError, match="inspect delivery.json"):
        run_resume_workspace(
            data_root=data_root,
            job_description="Required: Python",
            candidate_profile=CandidateProfile(full_name="Candidate"),
            evidence_hits=[],
            application_library_root=library_root,
        )

    run_dir = next((data_root / "resume-runs").iterdir())
    delivery = json.loads((run_dir / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["state"] == "APPLICATION_LIBRARY_FAILED"
    assert delivery["error_type"] == "OSError"
    assert delivery["retry_safe"] is False
    assert not (run_dir / "run.json").exists()
    assert list((library_root / "applications").iterdir()) == []


def test_post_rename_fsync_failure_records_published_uncertain_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / ".soloscale"
    library_root = tmp_path / "Resume Applications"
    applications_root = library_root / "applications"
    original_open = os.open
    original_fsync = os.fsync
    applications_root_descriptors: set[int] = set()

    def track_applications_root(
        path: Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if Path(os.fsdecode(path)) == applications_root:
            applications_root_descriptors.add(descriptor)
        return descriptor

    def fail_applications_root_fsync(descriptor: int) -> None:
        if descriptor in applications_root_descriptors:
            applications_root_descriptors.remove(descriptor)
            raise OSError("simulated published bundle durability failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "open", track_applications_root)
    monkeypatch.setattr(os, "fsync", fail_applications_root_fsync)
    docx_content = b"complete-docx-before-durability-failure"
    docx_filename = "Resume_Candidate_Company_Role.docx"

    with pytest.raises(ResumeWorkspaceStorageError, match="published with uncertain durability"):
        run_resume_workspace(
            data_root=data_root,
            job_description="Required: Python",
            candidate_profile=CandidateProfile(full_name="Candidate"),
            evidence_hits=[],
            application_library_root=library_root,
            application_resume_bytes=docx_content,
            application_resume_filename=docx_filename,
        )

    run_dir = next((data_root / "resume-runs").iterdir())
    delivery = json.loads((run_dir / "delivery.json").read_text(encoding="utf-8"))
    published_path = Path(delivery["application_library_path"])
    assert delivery["state"] == "APPLICATION_LIBRARY_PUBLISHED_DURABILITY_UNCERTAIN"
    assert delivery["error_type"] == "OSError"
    assert delivery["retry_safe"] is False
    assert published_path.is_dir()
    assert published_path.parent == applications_root
    assert (published_path / docx_filename).read_bytes() == docx_content
    assert not (run_dir / "run.json").exists()


def test_publication_race_never_replaces_new_empty_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from soloscale import resume_workspace

    original_publish = resume_workspace._atomic_rename_directory_no_replace
    raced_destination: Path | None = None
    raced_inode: int | None = None

    def create_destination_immediately_before_publish(source: Path, destination: Path) -> None:
        nonlocal raced_destination, raced_inode
        destination.mkdir(mode=0o700)
        raced_destination = destination
        raced_inode = destination.stat().st_ino
        original_publish(source, destination)

    monkeypatch.setattr(
        resume_workspace,
        "_atomic_rename_directory_no_replace",
        create_destination_immediately_before_publish,
    )
    data_root = tmp_path / ".soloscale"
    library_root = tmp_path / "Resume Applications"
    docx_content = b"complete-docx-before-publication-race"
    docx_filename = "Resume_Candidate_Company_Role.docx"

    with pytest.raises(ResumeWorkspaceStorageError, match="inspect delivery.json"):
        run_resume_workspace(
            data_root=data_root,
            job_description="Required: Python",
            candidate_profile=CandidateProfile(full_name="Candidate"),
            evidence_hits=[],
            application_library_root=library_root,
            application_resume_bytes=docx_content,
            application_resume_filename=docx_filename,
        )

    assert raced_destination is not None
    assert raced_inode is not None
    assert raced_destination.is_dir()
    assert raced_destination.stat().st_ino == raced_inode
    assert list(raced_destination.iterdir()) == []
    assert not any(
        path.name.endswith(".staging")
        for path in (library_root / "applications").iterdir()
    )
    run_dir = next((data_root / "resume-runs").iterdir())
    delivery = json.loads((run_dir / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["state"] == "APPLICATION_LIBRARY_FAILED"
    assert delivery["error_type"] == "FileExistsError"
    assert (run_dir / "08_resume.docx").read_bytes() == docx_content
def test_final_internal_failure_leaves_saved_delivery_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from soloscale import resume_workspace

    original = resume_workspace._write_json

    def fail_run_json(path: Path, value: object) -> None:
        if path.name == "run.json":
            raise OSError("simulated final receipt failure")
        original(path, value)

    monkeypatch.setattr(resume_workspace, "_write_json", fail_run_json)
    data_root = tmp_path / ".soloscale"
    library_root = tmp_path / "Resume Applications"
    with pytest.raises(OSError, match="simulated final receipt failure"):
        run_resume_workspace(
            data_root=data_root,
            job_description="Required: Python",
            candidate_profile=CandidateProfile(full_name="Candidate"),
            evidence_hits=[],
            application_library_root=library_root,
        )

    run_dir = next((data_root / "resume-runs").iterdir())
    delivery = json.loads((run_dir / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["state"] == "APPLICATION_LIBRARY_SAVED"
    assert Path(delivery["application_library_path"]).is_dir()
    assert not (run_dir / "run.json").exists()


def test_rejects_application_library_inside_repository_before_writing(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    data_root = repository_root / ".soloscale"
    with pytest.raises(ValueError, match="outside every Git repository"):
        run_resume_workspace(
            data_root=data_root,
            job_description="Required: Python",
            candidate_profile=CandidateProfile(),
            evidence_hits=[],
            application_library_root=repository_root / "resume-data",
            repository_root=repository_root,
        )
    assert not data_root.exists()
