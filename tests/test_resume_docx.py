import hashlib
import io
import json
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError

from soloscale.content_canon import StoryReadiness, load_month_one_canon
from soloscale.evidence_hub import EvidenceHub, EvidenceHubError
from soloscale.knowledge_models import ContentRole, RetrievalHit, SourceKind
from soloscale.resume_docx import (
    ResumeTemplateError,
    ResumeValidationRuleCode,
    _deterministic_hiring_signals,
    _remove_trailing_empty_paragraphs,
    _select_safe_rewrites,
    _validate_role_strategy,
    apply_resume_template_structure,
    extract_candidate_profile,
    read_template_paragraphs,
    tailor_resume_docx,
)
from soloscale.resume_evidence_pack import (
    _compact_verified_facts,
    build_candidate_evidence_pack,
    build_composition_evidence_plan,
    build_jd_positioning_brief,
    build_resume_evidence_retrieval_trace,
)
from soloscale.resume_models import (
    CandidateProfile,
    GroundedResumeBulletRewrite,
    GroundedResumeSummaryRewrite,
    ResumeAtomicFact,
    ResumeClaimProvenance,
    ResumeClaimVerificationStatus,
    RoleStrategy,
    build_resume_atomic_facts,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _fact_ids(profile: CandidateProfile, *source_ids: str) -> list[str]:
    return [
        fact.fact_id
        for fact in build_resume_atomic_facts(profile)
        if fact.profile_entry_id in source_ids
    ]


def _retrieval_hit(
    source_kind: SourceKind,
    text: str,
    *,
    suffix: str,
) -> RetrievalHit:
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    document_sha256 = hashlib.sha256(f"document-{suffix}".encode()).hexdigest()
    return RetrievalHit(
        chunk_id=f"chunk-{suffix}",
        document_id=f"document-{suffix}",
        source_kind=source_kind,
        external_id=f"external-{suffix}",
        locator=f"/private/{suffix}",
        title="Private local context",
        role=ContentRole.ASSISTANT,
        timestamp=datetime(2026, 8, 28, tzinfo=UTC),
        excerpt=text,
        chunk_sha256=text_sha256,
        document_sha256=document_sha256,
        score=1.0,
        channels=["fts"],
    )


def test_blank_page_guard_removes_only_body_final_empty_paragraphs() -> None:
    body = ElementTree.fromstring(
        f'<w:body xmlns:w="{W_NS}"><w:p><w:r><w:t>Keep</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="BodyText"/></w:pPr></w:p>'
        '<w:bookmarkEnd w:id="0"/><w:sectPr/></w:body>'
    )

    assert _remove_trailing_empty_paragraphs(body) == 1
    assert [node.text for node in body.iter(f"{{{W_NS}}}t")] == ["Keep"]


def _paragraph(text: str, *, bullet: bool = False) -> str:
    numbering = "<w:pPr><w:numPr><w:ilvl w:val=\"0\"/><w:numId w:val=\"1\"/></w:numPr></w:pPr>"
    return f"<w:p>{numbering if bullet else ''}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _template_docx() -> bytes:
    paragraphs = [
        _paragraph("LANG JU"),
        _paragraph("AI Engineer"),
        _paragraph("lang@example.com"),
        _paragraph("SUMMARY"),
        _paragraph("Evidence-grounded engineer."),
        _paragraph("PROJECT HIGHLIGHTS"),
        _paragraph("Search Project"),
        _paragraph("Built Python RAG retrieval.", bullet=True),
        _paragraph("Platform Project"),
        _paragraph("Shipped Docker and Kubernetes automation.", bullet=True),
        _paragraph("EDUCATION"),
        _paragraph("M.S. Information Systems"),
        _paragraph("TECHNICAL SKILLS"),
        _paragraph("Python, RAG", bullet=True),
        _paragraph("Docker, Kubernetes", bullet=True),
        _paragraph("WORK EXPERIENCE"),
        _paragraph("Example Company"),
        _paragraph("Delivered production systems.", bullet=True),
    ]
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(paragraphs)
        + "<w:sectPr/></w:body></w:document>"
    ).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"content-types")
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", b"styles-preserve-exactly")
        archive.writestr("word/numbering.xml", b"numbering-preserve-exactly")
    return target.getvalue()


def test_extract_profile_and_tailor_preserve_every_candidate_claim() -> None:
    template = _template_docx()
    profile = extract_candidate_profile(template)

    assert profile.full_name == "LANG JU"
    assert profile.headline == "AI Engineer"
    assert profile.summary == "Evidence-grounded engineer."
    assert profile.project_bullets == [
        "Built Python RAG retrieval.",
        "Shipped Docker and Kubernetes automation.",
    ]
    assert profile.skills == ["Python, RAG", "Docker, Kubernetes"]
    assert profile.experience_bullets == ["Delivered production systems."]

    output = tailor_resume_docx(template, "Required: Docker and Kubernetes platform delivery")
    assert output.claims_preserved is True
    assert output.project_blocks_reordered == 2
    assert output.skill_bullets_reordered == 2
    assert output.template_sha256 == hashlib.sha256(template).hexdigest()
    assert output.output_sha256 == hashlib.sha256(output.content).hexdigest()

    before = [item.text for item in read_template_paragraphs(template) if item.text]
    after = [item.text for item in read_template_paragraphs(output.content) if item.text]
    assert sorted(after) == sorted(before)
    assert after.index("Platform Project") < after.index("Search Project")
    assert after.index("Docker, Kubernetes") < after.index("Python, RAG")

    with zipfile.ZipFile(io.BytesIO(template)) as source, zipfile.ZipFile(
        io.BytesIO(output.content)
    ) as tailored:
        for name in source.namelist():
            if name != "word/document.xml":
                assert tailored.read(name) == source.read(name)


def test_external_template_reorders_sections_without_importing_body_copy() -> None:
    template = _template_docx()
    reordered = apply_resume_template_structure(
        template,
        [
            "TECHNICAL SKILLS",
            "SUMMARY",
            "PROJECT HIGHLIGHTS",
            "WORK EXPERIENCE",
            "EDUCATION",
        ],
    )

    before = [item.text for item in read_template_paragraphs(template) if item.text]
    after = [item.text for item in read_template_paragraphs(reordered) if item.text]
    assert sorted(after) == sorted(before)
    assert after.index("TECHNICAL SKILLS") < after.index("SUMMARY")
    assert "Senior Engineer at Example Template Company" not in after


def test_rejects_non_docx_upload() -> None:
    with pytest.raises(ResumeTemplateError, match="not a readable DOCX"):
        extract_candidate_profile(b"not-a-zip")


def test_hiring_signal_must_be_an_exact_jd_quote() -> None:
    source = "Built Python service."
    profile = CandidateProfile(skills=["Python"], project_bullets=[source])
    strategy = RoleStrategy(
        role_summary="Python application development",
        top_hiring_signals=["Python application development"],
        evidence_priority=["PROFILE-01"],
        skill_priority=["Python"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text=source,
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            )
        ],
        rewrite_guidance="Preserve the approved fact.",
    )

    with pytest.raises(ResumeTemplateError) as failure:
        _validate_role_strategy(
            strategy,
            profile=profile,
            job_description="Required: Python web application development.",
        )

    diagnostics = failure.value.validation_diagnostics
    assert diagnostics is not None
    assert diagnostics.as_dict() == {
        "validator_status": "rejected",
        "failure_count": 1,
        "failures": [
            {
                "rule_code": "HIRING_SIGNAL_NOT_SOURCE_GROUNDED",
                "json_path": "$.top_hiring_signals[0]",
                "claim_id": None,
            }
        ],
        "candidate_count": 1,
        "verified_count": 1,
        "supported_count": 0,
        "rejected_count": 0,
        "duplicate_count": 0,
        "source_span_failure_count": 1,
    }


def test_truth_validation_diagnostics_are_aggregated_and_body_free() -> None:
    source = "Built Python service for users."
    profile = CandidateProfile(skills=["Python"], project_bullets=[source])
    strategy = RoleStrategy(
        role_summary="Synthetic role",
        top_hiring_signals=["Python", "python"],
        evidence_priority=["PROFILE-01", "PROFILE-01"],
        skill_priority=["Python"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="Improved Python service by 25% with Django.",
                source_fact_ids=["FACT-PROFILE-99-01"],
            )
        ],
        unsupported_requirements=["Kubernetes private requirement"],
        rewrite_guidance="Synthetic guidance",
    )

    with pytest.raises(ResumeTemplateError) as failure:
        _validate_role_strategy(
            strategy,
            profile=profile,
            job_description="Required: Python.",
        )

    diagnostics = failure.value.validation_diagnostics
    assert diagnostics is not None
    payload = diagnostics.as_dict()
    failures = payload["failures"]
    assert isinstance(failures, list)
    codes = {
        item["rule_code"]
        for item in failures
        if isinstance(item, dict)
    }
    assert {
        ResumeValidationRuleCode.OUTPUT_DUPLICATE.value,
        ResumeValidationRuleCode.CLAIM_SOURCE_MISMATCH.value,
        ResumeValidationRuleCode.CLAIM_NO_EVIDENCE.value,
        ResumeValidationRuleCode.CLAIM_NEW_NUMBER.value,
        ResumeValidationRuleCode.REWRITE_FACT_MUTATION.value,
        ResumeValidationRuleCode.GAP_NOT_SOURCE_GROUNDED.value,
        ResumeValidationRuleCode.HIRING_SIGNAL_DUPLICATE.value,
    } <= codes
    assert payload["duplicate_count"] == 2
    assert payload["source_span_failure_count"] == 2
    assert payload["rejected_count"] == 1
    serialized = json.dumps(payload)
    assert source not in serialized
    assert "Kubernetes private requirement" not in serialized
    assert "Improved Python service" not in serialized


def test_hiring_signals_are_deterministic_exact_jd_spans() -> None:
    job_description = """Job Responsibilities:
• Build Python web applications.
• Design RAG pipelines.
Requirements:
• Use Git for version control.
• Test application quality.
"""

    signals = _deterministic_hiring_signals(job_description)

    assert signals == [
        "Build Python web applications.",
        "Design RAG pipelines.",
        "Use Git for version control.",
        "Test application quality.",
    ]
    assert all(signal in job_description for signal in signals)


def test_candidate_evidence_pack_adds_fresh_committed_project_facts(
    tmp_path: Path,
) -> None:
    profile = CandidateProfile(
        skills=["Python, RAG"],
        project_bullets=["Built SoloScale AI OS with evidence-grounded workflows."],
    )
    repository = tmp_path / "solo-scale-ai-os"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "feature.txt").write_text("background job", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "feat: add non-blocking resume jobs",
        ],
        check=True,
    )
    data_root = tmp_path / "data"
    EvidenceHub(data_root).sync_git_repository(repository)
    assert b"background job" not in EvidenceHub(data_root).database_path.read_bytes()

    first = build_candidate_evidence_pack(
        profile,
        data_root=data_root,
        repository_root=repository,
    )
    second = build_candidate_evidence_pack(
        profile,
        data_root=data_root,
        repository_root=repository,
    )

    assert first.pack_sha256 == second.pack_sha256
    source_ids = {source.evidence_id for source in first.sources}
    assert "EVIDENCE-LOCAL-GIT" in source_ids
    ready_story_ids = {
        f"EVIDENCE-{story.story_id}"
        for story in load_month_one_canon().stories
        if story.status is StoryReadiness.READY_FOR_PRODUCTION
    }
    assert ready_story_ids <= source_ids
    candidate_facts = [
        fact for fact in first.atomic_facts if fact.source_kind == "CANDIDATE_EVIDENCE"
    ]
    assert len(candidate_facts) >= 15
    assert {fact.profile_entry_id for fact in candidate_facts} == {"PROFILE-01"}
    assert any(
        fact.text == "Verified repository commit: feat: add non-blocking resume jobs"
        for fact in candidate_facts
    )

    brief = build_jd_positioning_brief(
        "AI Engineer\nBuild Python RAG pipelines and reliable background jobs.",
        first.atomic_facts,
    )
    assert brief.top_hiring_signals == [
        "AI Engineer",
        "Build Python RAG pipelines and reliable background jobs.",
    ]
    assert set(brief.priority_fact_ids) <= {
        fact.fact_id for fact in first.atomic_facts
    }

    (repository / "feature.txt").write_text("changed but not refreshed", encoding="utf-8")
    with pytest.raises(EvidenceHubError, match="stale"):
        build_candidate_evidence_pack(
            profile,
            data_root=data_root,
            repository_root=repository,
        )


def test_resume_retrieval_trace_is_body_free_and_never_promotes_conversations() -> None:
    profile = CandidateProfile(
        skills=["Python", "RAG"],
        project_bullets=["Built SoloScale evidence-grounded RAG workflows."],
    )
    job_description = "Build Python RAG workflows with reliable background jobs."
    pack = build_candidate_evidence_pack(
        profile,
        job_description=job_description,
    )
    private_claim = "Implemented an unverified production platform for 9 million users."
    hits = [
        _retrieval_hit(SourceKind.CODEX_SESSION, private_claim, suffix="codex"),
        _retrieval_hit(
            SourceKind.CHATGPT_EXPORT,
            "Discussed RAG workflow positioning.",
            suffix="chatgpt",
        ),
        _retrieval_hit(
            SourceKind.BUILDLOG_RUN,
            "Recorded background job verification context.",
            suffix="buildlog",
        ),
    ]
    trace = build_resume_evidence_retrieval_trace(
        job_description=job_description,
        facts=pack.atomic_facts,
        knowledge_hits=hits,
    )

    assert len(pack.atomic_facts) <= 80
    assert private_claim not in {fact.text for fact in pack.atomic_facts}
    assert trace.source_counts == {
        "buildlog_run": 1,
        "chatgpt_export": 1,
        "codex_session": 1,
    }
    assert trace.retrieved_count == len(pack.atomic_facts) + len(hits)
    assert trace.admitted_count == trace.sent_count == len(pack.atomic_facts)
    assert trace.requirements
    assert {item.status for item in trace.requirements} <= {
        "STRONG",
        "MEDIUM",
        "GAP",
    }
    source_summary = {item.source_type: item for item in trace.sources}
    assert source_summary["CODEX"].context_only_count == 1
    assert source_summary["CHATGPT"].context_only_count == 1
    assert source_summary["BUILDLOG"].context_only_count == 1
    assert source_summary["CODEX"].sent_count == 0
    assert source_summary["LEARNING"].state == "UNAVAILABLE"
    assert source_summary["RESUME_HISTORY"].state == "UNAVAILABLE"
    assert {hit.disposition for hit in trace.hits} == {"DISCOVERY_ONLY"}
    serialized = trace.model_dump_json()
    assert private_claim not in serialized
    assert "/private/" not in serialized
    assert trace.sent_fact_ids == [fact.fact_id for fact in pack.atomic_facts]


def test_compact_evidence_pack_balances_distinct_jd_requirements() -> None:
    profile = CandidateProfile(project_bullets=["Built a verified AI workflow."])
    profile_facts = build_resume_atomic_facts(profile)

    def candidate_fact(index: int, text: str, tags: list[str]) -> ResumeAtomicFact:
        fact_id = f"FACT-EVIDENCE-TEST-{index:02d}"
        source_sha256 = hashlib.sha256(b"test-source").hexdigest()
        return ResumeAtomicFact(
            fact_id=fact_id,
            profile_entry_id="PROFILE-01",
            evidence_id="EVIDENCE-TEST",
            source_kind="CANDIDATE_EVIDENCE",
            capability_tags=tags,
            text=text,
            source_sha256=source_sha256,
            fact_sha256=hashlib.sha256(
                f"{fact_id}\0PROFILE-01\0{text}".encode()
            ).hexdigest(),
        )

    python_facts = [
        candidate_fact(index, f"Built Python backend API {index}.", ["python", "backend"])
        for index in range(1, 77)
    ]
    observability_facts = [
        candidate_fact(
            index,
            f"Added observability monitoring and logging {index}.",
            ["observability", "monitoring"],
        )
        for index in range(77, 81)
    ]
    unrelated_facts = [
        candidate_fact(index, f"Designed media asset {index}.", ["media"])
        for index in range(81, 100)
    ]

    compact = _compact_verified_facts(
        job_description=(
            "Build Python backend applications.\n"
            "Ship observability, monitoring, and logging."
        ),
        atomic_facts=[
            *profile_facts,
            *python_facts,
            *observability_facts,
            *unrelated_facts,
        ],
    )

    selected_ids = {fact.fact_id for fact in compact}
    assert len(compact) == 80
    assert {fact.fact_id for fact in profile_facts} <= selected_ids
    assert {fact.fact_id for fact in observability_facts} <= selected_ids
    assert not ({fact.fact_id for fact in unrelated_facts} & selected_ids)


def test_composition_plan_prefers_rich_verified_event_over_thin_commit() -> None:
    source_sha256 = hashlib.sha256(b"verified-sources").hexdigest()

    def fact(
        fact_id: str,
        evidence_id: str,
        text: str,
        tags: list[str],
    ) -> ResumeAtomicFact:
        return ResumeAtomicFact(
            fact_id=fact_id,
            profile_entry_id="PROFILE-01",
            evidence_id=evidence_id,
            source_kind="CANDIDATE_EVIDENCE",
            capability_tags=tags,
            text=text,
            source_sha256=source_sha256,
            fact_sha256=hashlib.sha256(
                f"{fact_id}\0PROFILE-01\0{text}".encode()
            ).hexdigest(),
        )

    rich = fact(
        "FACT-EVIDENCE-M1-14-01",
        "EVIDENCE-M1-14",
        "Redesigned blocking resume generation into a reliable background job with polling.",
        ["background-jobs", "reliability"],
    )
    thin = fact(
        "FACT-EVIDENCE-LOCAL-GIT-01",
        "EVIDENCE-LOCAL-GIT",
        "Verified repository commit: feat add background jobs for resume delivery.",
        ["repository", "verified-commit"],
    )

    plan = build_composition_evidence_plan(
        "Build reliable Python background jobs for AI workflows.", [thin, rich]
    )

    assert plan.requirements[0].primary_fact_ids[0] == rich.fact_id
    assert thin.fact_id in plan.prioritized_fact_ids


def test_allowed_fact_number_passes_but_another_number_still_fails() -> None:
    profile = CandidateProfile(
        project_bullets=["Built SoloScale evidence workflows."]
    )
    profile_fact = build_resume_atomic_facts(profile)[0]
    metric_text = "Measured POST response latency during Desktop E2E."
    metric_fact = ResumeAtomicFact(
        fact_id="FACT-EVIDENCE-M1-14-01",
        profile_entry_id="PROFILE-01",
        evidence_id="EVIDENCE-M1-14",
        source_kind="CANDIDATE_EVIDENCE",
        capability_tags=["performance"],
        metric="POST response 19 ms",
        allowed_numbers=["19"],
        text=metric_text,
        source_sha256=hashlib.sha256(b"metric-source").hexdigest(),
        fact_sha256=hashlib.sha256(
            f"FACT-EVIDENCE-M1-14-01\0PROFILE-01\0{metric_text}".encode()
        ).hexdigest(),
    )

    def strategy(number: int) -> RoleStrategy:
        return RoleStrategy(
            role_summary="Build evidence workflows.",
            top_hiring_signals=["Build evidence workflows."],
            evidence_priority=["PROFILE-01"],
            skill_priority=[],
            bullet_rewrites=[
                GroundedResumeBulletRewrite(
                    profile_entry_id="PROFILE-01",
                    kind="SYNTHESIS",
                    text=(
                        "Built SoloScale evidence workflows with a measured "
                        f"POST response of {number} ms."
                    ),
                    source_profile_entry_ids=["PROFILE-01"],
                    source_fact_ids=[profile_fact.fact_id, metric_fact.fact_id],
                )
            ],
            unsupported_requirements=[],
            rewrite_guidance="Use verified evidence.",
        )

    _validate_role_strategy(
        strategy(19),
        profile=profile,
        job_description="Build evidence workflows.",
        atomic_facts=[profile_fact, metric_fact],
    )
    with pytest.raises(ResumeTemplateError) as rejected:
        _validate_role_strategy(
            strategy(20),
            profile=profile,
            job_description="Build evidence workflows.",
            atomic_facts=[profile_fact, metric_fact],
        )
    assert rejected.value.validation_diagnostics is not None
    assert ResumeValidationRuleCode.CLAIM_NEW_NUMBER in {
        failure.rule_code
        for failure in rejected.value.validation_diagnostics.failures
    }


def test_resume_lexical_retrieval_evaluation_fixture_has_stable_recall() -> None:
    profile = CandidateProfile(
        project_bullets=["Built SoloScale evidence-grounded RAG workflows."]
    )
    all_facts = build_candidate_evidence_pack(profile).atomic_facts
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "resume_retrieval_eval.json"
        ).read_text(encoding="utf-8")
    )
    recall_at_5: list[float] = []
    recall_at_10: list[float] = []
    reciprocal_ranks: list[float] = []
    for case in cases:
        ranked = _compact_verified_facts(
            job_description=case["query"],
            atomic_facts=all_facts,
        )
        ranked_evidence_ids = list(
            dict.fromkeys(
                fact.evidence_id
                for fact in ranked
                if fact.source_kind == "CANDIDATE_EVIDENCE"
            )
        )
        relevant = set(case["relevant_evidence_ids"])
        recall_at_5.append(len(relevant & set(ranked_evidence_ids[:5])) / len(relevant))
        recall_at_10.append(
            len(relevant & set(ranked_evidence_ids[:10])) / len(relevant)
        )
        first_rank = next(
            (
                index
                for index, evidence_id in enumerate(ranked_evidence_ids, start=1)
                if evidence_id in relevant
            ),
            None,
        )
        reciprocal_ranks.append(0 if first_rank is None else 1 / first_rank)

    assert sum(recall_at_5) / len(recall_at_5) >= 0.75
    assert sum(recall_at_10) / len(recall_at_10) >= 0.85
    assert sum(reciprocal_ranks) / len(reciprocal_ranks) >= 0.65


def test_selective_rewrite_keeps_supported_claim_and_restores_rejected_claim() -> None:
    first_source = "Built Python service for users."
    second_source = "Delivered RAG system."
    profile = CandidateProfile(
        skills=["Python", "RAG"],
        project_bullets=[first_source, second_source],
    )
    strategy = RoleStrategy(
        role_summary="Python and RAG role",
        top_hiring_signals=["Required: Python and RAG."],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        skill_priority=["Python", "RAG"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="Engineered a reliable Python service supporting users.",
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="Built a Django RAG system.",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
        rewrite_guidance="Prefer grounded wording.",
    )

    selected, _entries, diagnostics = _select_safe_rewrites(
        strategy,
        profile=profile,
        job_description="Required: Python and RAG.",
    )

    rewrites = {
        item.profile_entry_id: item.text for item in selected.bullet_rewrites
    }
    assert rewrites == {
        "PROFILE-01": "Engineered a reliable Python service supporting users.",
        "PROFILE-02": second_source,
    }
    assert diagnostics.validator_status == "selective_pass"
    assert diagnostics.supported_count == 1
    assert diagnostics.rejected_count == 1
    assert {
        failure.rule_code for failure in diagnostics.failures
    } >= {ResumeValidationRuleCode.CLAIM_TECHNOLOGY_INFLATION}


def test_multi_source_synthesis_uses_union_and_falls_back_only_unsafe_slots() -> None:
    first_source = "Built RAG retrieval."
    second_source = "Added FastAPI orchestration."
    third_source = "Validated citations."
    profile = CandidateProfile(
        summary="Evidence-grounded engineer.",
        skills=["RAG", "FastAPI"],
        project_bullets=[first_source, second_source, third_source],
    )
    strategy = RoleStrategy(
        role_summary="RAG and FastAPI role",
        top_hiring_signals=["Required: RAG and FastAPI."],
        evidence_priority=["PROFILE-01", "PROFILE-02", "PROFILE-03"],
        skill_priority=["RAG", "FastAPI"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                kind="SYNTHESIS",
                text="Built RAG retrieval with FastAPI orchestration.",
                source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
                source_fact_ids=_fact_ids(
                    profile, "PROFILE-01", "PROFILE-02"
                ),
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text=second_source,
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-03",
                text=(
                    "Led Django citations for clients in production and increased "
                    "results by 40%."
                ),
                source_fact_ids=_fact_ids(profile, "PROFILE-03"),
            ),
        ],
        summary_rewrite=GroundedResumeSummaryRewrite(
            text="Built RAG retrieval with FastAPI orchestration for 40% more clients.",
            source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
            source_fact_ids=_fact_ids(profile, "PROFILE-01", "PROFILE-02"),
        ),
        rewrite_guidance="Synthesize only approved facts.",
    )

    selected, _entries, diagnostics = _select_safe_rewrites(
        strategy,
        profile=profile,
        job_description="Required: RAG and FastAPI.",
    )

    selected_by_id = {
        rewrite.profile_entry_id: rewrite for rewrite in selected.bullet_rewrites
    }
    assert selected_by_id["PROFILE-01"].kind == "SYNTHESIS"
    assert selected_by_id["PROFILE-01"].text == (
        "Built RAG retrieval with FastAPI orchestration."
    )
    assert selected_by_id["PROFILE-03"].text == third_source
    assert selected.summary_rewrite is None
    assert diagnostics.validator_status == "selective_pass"
    assert diagnostics.rejected_count == 2
    assert {
        failure.claim_id
        for failure in diagnostics.failures
        if failure.claim_id is not None
    } == {"PROFILE-03", "SUMMARY"}
    assert {
        failure.rule_code for failure in diagnostics.failures
    } >= {
        ResumeValidationRuleCode.CLAIM_ROLE_INFLATION,
        ResumeValidationRuleCode.CLAIM_CLIENT_INFLATION,
        ResumeValidationRuleCode.CLAIM_SCALE_INFLATION,
        ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION,
        ResumeValidationRuleCode.CLAIM_TECHNOLOGY_INFLATION,
        ResumeValidationRuleCode.CLAIM_NEW_NUMBER,
    }


def test_chinese_editorial_synthesis_keeps_fact_boundary_and_rejects_inflation() -> None:
    profile = CandidateProfile(
        summary="Evidence-grounded engineer.",
        skills=["RAG", "FastAPI"],
        project_bullets=[
            "Built RAG retrieval.",
            "Added FastAPI orchestration.",
        ],
    )
    fact_ids = _fact_ids(profile, "PROFILE-01", "PROFILE-02")
    safe = RoleStrategy(
        role_summary="RAG and FastAPI role",
        top_hiring_signals=["Required: RAG and FastAPI."],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        skill_priority=["RAG", "FastAPI"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                kind="SYNTHESIS",
                text="设计并实现 RAG 检索与 FastAPI 编排工作流。",
                source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
                source_fact_ids=fact_ids,
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="通过 FastAPI 完成服务编排。",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
        summary_rewrite=GroundedResumeSummaryRewrite(
            text="专注于 RAG 检索与 FastAPI 编排的证据驱动工程师。",
            source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
            source_fact_ids=fact_ids,
        ),
        rewrite_guidance="使用自然中文表达，不增加事实。",
    )

    selected, _entries, diagnostics = _select_safe_rewrites(
        safe,
        profile=profile,
        job_description="Required: RAG and FastAPI.",
        output_locale="zh-CN",
    )
    assert diagnostics.validator_status == "accepted", [
        (failure.rule_code, failure.claim_id, failure.json_path)
        for failure in diagnostics.failures
    ]
    assert selected.bullet_rewrites[0].kind == "SYNTHESIS"
    assert selected.summary_rewrite is not None

    inflated_payload = safe.model_dump(mode="json")
    inflated_payload["bullet_rewrites"][0]["text"] = (
        "主导企业级 RAG 与 FastAPI 平台，为客户显著提升 40% 收入。"
    )
    inflated = RoleStrategy.model_validate(inflated_payload)
    selected, _entries, diagnostics = _select_safe_rewrites(
        inflated,
        profile=profile,
        job_description="Required: RAG and FastAPI.",
        output_locale="zh-CN",
    )
    assert selected.bullet_rewrites[0].text == "Built RAG retrieval."
    assert diagnostics.validator_status == "selective_pass"
    assert {
        failure.rule_code for failure in diagnostics.failures
    } >= {
        ResumeValidationRuleCode.CLAIM_ROLE_INFLATION,
        ResumeValidationRuleCode.CLAIM_CLIENT_INFLATION,
        ResumeValidationRuleCode.CLAIM_SCALE_INFLATION,
        ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION,
        ResumeValidationRuleCode.CLAIM_NEW_NUMBER,
    }


def test_cross_locale_fact_match_accepts_chinese_facts_in_english() -> None:
    profile = CandidateProfile(
        skills=["RAG", "FastAPI"],
        project_bullets=[
            "构建 RAG 检索工作流。",
            "通过 FastAPI 完成服务编排。",
        ],
    )
    fact_ids = _fact_ids(profile, "PROFILE-01", "PROFILE-02")
    strategy = RoleStrategy(
        role_summary="RAG and FastAPI role",
        top_hiring_signals=["Required: RAG retrieval and FastAPI orchestration."],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        skill_priority=["RAG", "FastAPI"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                kind="SYNTHESIS",
                text="Built a RAG retrieval workflow with FastAPI orchestration.",
                source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
                source_fact_ids=fact_ids,
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="Implemented FastAPI orchestration.",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
        rewrite_guidance="Use natural English without adding facts.",
    )

    selected, _entries, diagnostics = _select_safe_rewrites(
        strategy,
        profile=profile,
        job_description="Required: RAG retrieval and FastAPI orchestration.",
        output_locale="en-US",
    )

    assert diagnostics.validator_status == "accepted"
    assert selected.bullet_rewrites[0].kind == "SYNTHESIS"


def test_cross_locale_fact_match_still_rejects_new_facts() -> None:
    profile = CandidateProfile(
        skills=["RAG"],
        project_bullets=["构建 RAG 检索工作流。"],
    )
    fact_ids = _fact_ids(profile, "PROFILE-01")
    unsafe_cases = (
        (
            "Built a RAG retrieval workflow with 40% improvement.",
            ResumeValidationRuleCode.CLAIM_NEW_NUMBER,
        ),
        ("Led a RAG retrieval workflow.", ResumeValidationRuleCode.CLAIM_ROLE_INFLATION),
        (
            "Built a RAG retrieval workflow for Acme customers.",
            ResumeValidationRuleCode.CLAIM_CLIENT_INFLATION,
        ),
        (
            "Built an enterprise production RAG retrieval workflow.",
            ResumeValidationRuleCode.CLAIM_SCALE_INFLATION,
        ),
        (
            "Built a LangGraph RAG retrieval workflow.",
            ResumeValidationRuleCode.CLAIM_TECHNOLOGY_INFLATION,
        ),
        (
            "Reduced latency through a RAG retrieval workflow.",
            ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION,
        ),
    )
    for text, expected_rule in unsafe_cases:
        strategy = RoleStrategy(
            role_summary="RAG role",
            top_hiring_signals=["Required: RAG retrieval."],
            evidence_priority=["PROFILE-01"],
            skill_priority=["RAG"],
            bullet_rewrites=[
                GroundedResumeBulletRewrite(
                    profile_entry_id="PROFILE-01",
                    text=text,
                    source_fact_ids=fact_ids,
                )
            ],
            rewrite_guidance="Use natural English without adding facts.",
        )
        selected, _entries, diagnostics = _select_safe_rewrites(
            strategy,
            profile=profile,
            job_description="Required: RAG retrieval.",
            output_locale="en-US",
        )
        assert selected.bullet_rewrites[0].text == "构建 RAG 检索工作流。"
        assert expected_rule in {
            failure.rule_code for failure in diagnostics.failures
        }


def test_synthesis_provenance_rejects_misaligned_target_source_hash() -> None:
    final_text = "Built RAG retrieval with FastAPI orchestration."
    first_source_hash = hashlib.sha256(b"Built RAG retrieval.").hexdigest()
    second_source_hash = hashlib.sha256(b"Added FastAPI orchestration.").hexdigest()
    claim = ResumeClaimProvenance(
        claim_id="CLAIM-01",
        render_location="BULLET",
        final_text=final_text,
        final_text_sha256=hashlib.sha256(final_text.encode()).hexdigest(),
        profile_entry_id="PROFILE-01",
        approved_source_sha256=first_source_hash,
        evidence_ids=["PROFILE-01", "PROFILE-02"],
        approved_evidence_sha256s=[first_source_hash, second_source_hash],
        fact_ids=["FACT-PROFILE-01-01", "FACT-PROFILE-02-01"],
        source_fact_sha256s=[
            hashlib.sha256(
                b"FACT-PROFILE-01-01\0PROFILE-01\0Built RAG retrieval"
            ).hexdigest(),
            hashlib.sha256(
                b"FACT-PROFILE-02-01\0PROFILE-02\0FastAPI orchestration"
            ).hexdigest(),
        ],
        status=ResumeClaimVerificationStatus.SUPPORTED,
        verification_basis="DETERMINISTIC_MULTI_SOURCE_SYNTHESIS",
    )

    assert claim.evidence_ids == ["PROFILE-01", "PROFILE-02"]
    with pytest.raises(ValidationError, match="target source hash"):
        ResumeClaimProvenance.model_validate(
            {
                **claim.model_dump(mode="json"),
                "approved_evidence_sha256s": [
                    second_source_hash,
                    first_source_hash,
                ],
            }
        )
