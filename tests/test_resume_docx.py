import hashlib
import io
import json
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from pydantic import BaseModel, ValidationError

from soloscale.content_canon import StoryReadiness, load_month_one_canon
from soloscale.evidence_hub import EvidenceHub, EvidenceHubError
from soloscale.knowledge_models import ContentRole, RetrievalHit, SourceKind
from soloscale.local_ui import _build_resume_provenance_receipt
from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayDescriptor,
    GatewayTransportScope,
    ModelProviderId,
)
from soloscale.resume_docx import (
    ResumeTemplateError,
    ResumeValidationRuleCode,
    TailoredDocx,
    _deterministic_hiring_signals,
    _exact_role_strategy_model,
    _remove_trailing_empty_paragraphs,
    _role_strategy_from_exact,
    _select_safe_rewrites,
    _validate_role_strategy,
    apply_resume_expert_review,
    apply_resume_template_structure,
    extract_candidate_profile,
    read_template_paragraphs,
    tailor_resume_docx,
    tailor_resume_docx_with_gateway,
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
    ResumeExpertReviewResult,
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


def _project_description_template_docx() -> bytes:
    paragraphs = [
        _paragraph("LANG JU"),
        _paragraph("AI Engineer"),
        _paragraph("PROJECT HIGHLIGHTS"),
        _paragraph("Search Project"),
        _paragraph("Search project description."),
        _paragraph("Built Python RAG retrieval.", bullet=True),
        _paragraph("Platform Project"),
        _paragraph("Platform project description."),
        _paragraph("Shipped Docker automation.", bullet=True),
        _paragraph("EDUCATION"),
        _paragraph("M.S. Information Systems"),
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
    return target.getvalue()


def _chinese_literal_bullet_template_docx() -> bytes:
    paragraphs = [
        _paragraph("测试工程师"),
        _paragraph("AI 应用工程师"),
        _paragraph("项目经历"),
        _paragraph("检索项目"),
        _paragraph("• Built RAG retrieval service."),
        _paragraph("教育背景"),
        _paragraph("计算机科学硕士"),
        _paragraph("专业技能"),
        _paragraph("Python"),
        _paragraph("Docker", bullet=True),
        _paragraph("• RAG"),
        _paragraph("工作经历"),
        _paragraph("示例公司"),
        _paragraph("• Delivered verified AI workflow."),
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
    return target.getvalue()


def _duplicate_claim_template_docx() -> bytes:
    repeated = "Built Python evidence service."
    paragraphs = [
        _paragraph("LANG JU"),
        _paragraph("AI Engineer"),
        _paragraph("SUMMARY"),
        _paragraph("Evidence-grounded engineer."),
        _paragraph("PROJECT HIGHLIGHTS"),
        _paragraph("Search Project"),
        _paragraph(repeated, bullet=True),
        _paragraph("Platform Project"),
        _paragraph(repeated, bullet=True),
        _paragraph("TECHNICAL SKILLS"),
        _paragraph("Python, RAG", bullet=True),
        _paragraph("Python, RAG", bullet=True),
        _paragraph("WORK EXPERIENCE"),
        _paragraph("Example Company"),
        _paragraph(repeated, bullet=True),
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
    return target.getvalue()


def _large_profile_template_docx(bullet_count: int = 100) -> bytes:
    paragraphs = [
        _paragraph("LANG JU"),
        _paragraph("AI Engineer"),
        _paragraph("SUMMARY"),
        _paragraph("Evidence-grounded engineer."),
        _paragraph("TECHNICAL SKILLS"),
        _paragraph("Python", bullet=True),
        _paragraph("WORK EXPERIENCE"),
        _paragraph("Example Company"),
        *[
            _paragraph(f"Delivered verified resume result {index}.", bullet=True)
            for index in range(1, bullet_count + 1)
        ],
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
    return target.getvalue()


def test_ai_tailoring_accepts_duplicate_claim_and_skill_text_by_position() -> None:
    template = _duplicate_claim_template_docx()
    profile = extract_candidate_profile(template)
    entry_ids = ["PROFILE-01", "PROFILE-02", "PROFILE-03"]

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[object],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> object:
            del system, user, reasoning_effort
            return schema.model_validate(  # type: ignore[attr-defined]
                {
                    "role_summary": "Build Python evidence services.",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-01", "SKILL-02"],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "SYNTHESIS",
                            "text": profile.experience_bullets[0]
                            if entry_id == "PROFILE-01"
                            else profile.project_bullets[0],
                            "source_fact_ids": [_fact_ids(profile, entry_id)[0]],
                        }
                        for entry_id in entry_ids
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "Preserve each positional claim.",
                }
            )

    tailored = tailor_resume_docx_with_gateway(
        template,
        "Build Python evidence services.",
        gateway=Gateway(),  # type: ignore[arg-type]
    )

    duplicate_bullets = [
        paragraph
        for paragraph in read_template_paragraphs(tailored.content)
        if paragraph.is_bullet and paragraph.text == "Built Python evidence service."
    ]
    assert len(duplicate_bullets) == 3
    assert tailored.rendered_profile_entry_ids == tuple(entry_ids)
    assert tailored.role_strategy is not None
    assert [
        rewrite.profile_entry_id for rewrite in tailored.role_strategy.bullet_rewrites
    ] == entry_ids
    assert {
        rewrite.kind for rewrite in tailored.role_strategy.bullet_rewrites
    } == {"REWRITE"}

    source_text = profile.project_bullets[0]
    reviewed = apply_resume_expert_review(
        tailored,
        profile=profile,
        job_description="Build Python evidence services.",
        review=ResumeExpertReviewResult.model_validate(
            {
                "summary": "Tighten one positional project claim.",
                "patches": [
                    {
                        "profile_entry_id": "PROFILE-02",
                        "before_sha256": hashlib.sha256(
                            source_text.encode("utf-8")
                        ).hexdigest(),
                        "after": "Built an evidence service with Python.",
                        "new_factual_claims": [],
                        "rationale": "Improve clarity without changing facts.",
                    }
                ],
                "omitted_high_value_profile_entry_ids": [],
            }
        ),
        expert_provider="test",
        expert_model="test-model",
    )
    visible = [paragraph.text for paragraph in read_template_paragraphs(reviewed.content)]
    assert visible[visible.index("Search Project") + 1] == (
        "Built an evidence service with Python."
    )
    assert visible[visible.index("Platform Project") + 1] == source_text
    assert visible[visible.index("Example Company") + 1] == source_text
    assert reviewed.grounded_rewrites == 1
    assert reviewed.unverified_rewrites == 0
    assert reviewed.validation_diagnostics is not None
    assert reviewed.validation_diagnostics.verified_count == 3
    assert reviewed.validation_diagnostics.supported_count == 1


def test_expert_review_zh_lexical_warning_is_unverified_not_supported() -> None:
    template = _chinese_literal_bullet_template_docx()
    profile = extract_candidate_profile(template)
    entries = {
        f"PROFILE-{index:02d}": text
        for index, text in enumerate(
            profile.experience_bullets + profile.project_bullets, start=1
        )
    }
    facts = build_resume_atomic_facts(profile)
    fact_ids_by_entry: dict[str, list[str]] = {}
    for fact in facts:
        fact_ids_by_entry.setdefault(fact.profile_entry_id, []).append(fact.fact_id)
    strategy = RoleStrategy(
        role_summary="检索工程岗位",
        top_hiring_signals=["检索服务"],
        evidence_priority=list(entries),
        skill_priority=list(profile.skills),
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id=entry_id,
                text=text,
                source_profile_entry_ids=[entry_id],
                source_fact_ids=fact_ids_by_entry[entry_id],
            )
            for entry_id, text in entries.items()
        ],
        rewrite_guidance="保留已批准事实。",
    )
    initial = TailoredDocx(
        content=template,
        template_sha256=hashlib.sha256(template).hexdigest(),
        output_sha256=hashlib.sha256(template).hexdigest(),
        project_blocks_reordered=0,
        skill_bullets_reordered=0,
        source_paragraph_count=len(read_template_paragraphs(template)),
        claims_preserved=True,
        output_locale="zh-CN",
        role_strategy=strategy,
        rendered_profile_entry_ids=tuple(entries),
    )
    source_text = entries["PROFILE-01"]
    reviewed = apply_resume_expert_review(
        initial,
        profile=profile,
        job_description="检索服务",
        review=ResumeExpertReviewResult.model_validate(
            {
                "summary": "自然改写一个项目条目。",
                "patches": [
                    {
                        "profile_entry_id": "PROFILE-01",
                        "before_sha256": hashlib.sha256(
                            source_text.encode("utf-8")
                        ).hexdigest(),
                        "after": "构建查询服务。",
                        "new_factual_claims": [],
                        "rationale": "压缩原有表述。",
                    }
                ],
                "omitted_high_value_profile_entry_ids": [],
            }
        ),
        expert_provider="test",
        expert_model="test-model",
    )

    assert reviewed.grounded_rewrites == 0
    assert reviewed.unverified_rewrites == 1
    assert reviewed.validation_diagnostics is not None
    assert reviewed.validation_diagnostics.supported_count == 0
    assert [warning.claim_id for warning in reviewed.validation_diagnostics.editorial_warnings] == [
        "PROFILE-01"
    ]


def test_expert_review_retains_rendered_zh_summary_warning() -> None:
    paragraphs = [
        _paragraph("测试工程师"),
        _paragraph("AI 应用工程师"),
        _paragraph("SUMMARY"),
        _paragraph("Python RAG and Docker automation."),
        _paragraph("PROJECT HIGHLIGHTS"),
        _paragraph("检索项目"),
        _paragraph("Built Python RAG retrieval.", bullet=True),
        _paragraph("WORK EXPERIENCE"),
        _paragraph("示例公司"),
        _paragraph("Delivered Docker automation.", bullet=True),
        _paragraph("TECHNICAL SKILLS"),
        _paragraph("Python", bullet=True),
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
    template = target.getvalue()
    profile = extract_candidate_profile(template)
    entry_ids = ["PROFILE-01", "PROFILE-02"]
    source_by_id = dict(
        zip(
            entry_ids,
            [*profile.experience_bullets, *profile.project_bullets],
            strict=True,
        )
    )

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[BaseModel],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> BaseModel:
            del system, user, reasoning_effort
            return schema.model_validate(
                {
                    "role_summary": "检索工程岗位",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-01"],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "REWRITE",
                            "text": source_by_id[entry_id],
                            "source_fact_ids": _fact_ids(profile, entry_id),
                        }
                        for entry_id in entry_ids
                    },
                    "summary_rewrite": {
                        "text": "面向智能系统的工程实践。",
                        "source_fact_ids": _fact_ids(
                            profile, "PROFILE-01", "PROFILE-02"
                        ),
                    },
                    "unsupported_requirements": [],
                    "rewrite_guidance": "保留已批准事实。",
                }
            )

    initial = tailor_resume_docx_with_gateway(
        template,
        "检索服务",
        gateway=Gateway(),  # type: ignore[arg-type]
        output_locale="zh-CN",
    )
    assert initial.summary_rewritten is True
    assert initial.validation_diagnostics is not None
    assert any(
        warning.claim_id == "SUMMARY"
        for warning in initial.validation_diagnostics.editorial_warnings
    )
    patch_source = source_by_id["PROFILE-02"]
    reviewed = apply_resume_expert_review(
        initial,
        profile=profile,
        job_description="检索服务",
        review=ResumeExpertReviewResult.model_validate(
            {
                "summary": "编辑一个项目条目。",
                "patches": [
                    {
                        "profile_entry_id": "PROFILE-02",
                        "before_sha256": hashlib.sha256(
                            patch_source.encode("utf-8")
                        ).hexdigest(),
                        "after": "构建 Python RAG 检索服务。",
                        "new_factual_claims": [],
                        "rationale": "压缩原有表述。",
                    }
                ],
                "omitted_high_value_profile_entry_ids": [],
            }
        ),
        expert_provider="test",
        expert_model="test-model",
    )

    assert reviewed.summary_rewritten is True
    assert reviewed.validation_diagnostics is not None
    assert reviewed.unverified_rewrites == 1
    assert any(
        warning.claim_id == "SUMMARY"
        for warning in reviewed.validation_diagnostics.editorial_warnings
    )
    provenance = _build_resume_provenance_receipt(
        run_id="expert-summary-warning",
        job_description="检索服务",
        profile=profile,
        tailored=reviewed,
    )
    assert provenance.claims[0].status == ResumeClaimVerificationStatus.UNVERIFIED


def test_one_project_zh_resume_keeps_legacy_full_rewrite_map() -> None:
    paragraphs = [
        _paragraph("测试工程师"),
        _paragraph("AI 应用工程师"),
        _paragraph("SUMMARY"),
        _paragraph("Python RAG 工程师。"),
        _paragraph("PROJECT HIGHLIGHTS"),
        _paragraph("检索项目"),
        _paragraph("Built Python RAG retrieval.", bullet=True),
        _paragraph("WORK EXPERIENCE"),
        _paragraph("示例公司"),
        _paragraph("Delivered Docker automation.", bullet=True),
        _paragraph("TECHNICAL SKILLS"),
        _paragraph("Python", bullet=True),
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
    template = target.getvalue()
    profile = extract_candidate_profile(template)
    entry_ids = ["PROFILE-01", "PROFILE-02"]
    source_by_id = dict(
        zip(
            entry_ids,
            [*profile.experience_bullets, *profile.project_bullets],
            strict=True,
        )
    )

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[BaseModel],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> BaseModel:
            del system, reasoning_effort
            payload = json.loads(user)
            assert "requested_edit_profile_entry_ids" not in payload
            assert set(
                schema.model_json_schema()["$defs"]["ExactBulletRewrites"][
                    "properties"
                ]
            ) == set(entry_ids)
            assert schema.model_json_schema()["properties"]["summary_rewrite"] != {
                "type": "null"
            }
            return schema.model_validate(
                {
                    "role_summary": "检索工程岗位",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-01"],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "REWRITE",
                            "text": source_by_id[entry_id],
                            "source_fact_ids": _fact_ids(profile, entry_id),
                        }
                        for entry_id in entry_ids
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "保留已批准事实。",
                }
            )

    tailored = tailor_resume_docx_with_gateway(
        template,
        "检索服务",
        gateway=Gateway(),  # type: ignore[arg-type]
        output_locale="zh-CN",
    )

    assert tailored.requested_edit_profile_entry_ids == ()
    assert tailored.requested_project_material_rewrites == 0
    assert tailored.requested_project_rewrite_quality_status == "NOT_APPLICABLE"


def test_expert_review_contract_accepts_every_supported_profile_entry() -> None:
    review = ResumeExpertReviewResult.model_validate(
        {
            "summary": "Review every supported profile entry.",
            "patches": [
                {
                    "profile_entry_id": f"PROFILE-{index:02d}",
                    "before_sha256": hashlib.sha256(
                        f"before-{index}".encode()
                    ).hexdigest(),
                    "after": f"Evidence-preserving rewrite {index}.",
                    "new_factual_claims": [],
                    "rationale": "Improve clarity without changing facts.",
                }
                for index in range(1, 121)
            ],
            "omitted_high_value_profile_entry_ids": [],
        }
    )

    assert len(review.patches) == 120
    assert review.patches[-1].profile_entry_id == "PROFILE-120"


def test_ai_tailoring_preserves_three_digit_profile_identity_and_provenance() -> None:
    template = _large_profile_template_docx()
    profile = extract_candidate_profile(template)
    entry_ids = [f"PROFILE-{index:02d}" for index in range(1, 101)]
    source_by_id = dict(zip(entry_ids, profile.experience_bullets, strict=True))
    facts_by_entry: dict[str, list[str]] = {}
    for fact in build_resume_atomic_facts(profile):
        facts_by_entry.setdefault(fact.profile_entry_id, []).append(fact.fact_id)

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[object],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> object:
            del system, user, reasoning_effort
            return schema.model_validate(  # type: ignore[attr-defined]
                {
                    "role_summary": "Deliver reliable resume systems.",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-01"],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "REWRITE",
                            "text": source_by_id[entry_id],
                            "source_fact_ids": facts_by_entry[entry_id],
                        }
                        for entry_id in entry_ids
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "Preserve every approved result.",
                }
            )

    tailored = tailor_resume_docx_with_gateway(
        template,
        "Deliver reliable resume systems.",
        gateway=Gateway(),  # type: ignore[arg-type]
    )
    receipt = _build_resume_provenance_receipt(
        run_id="resume-20260904T000000Z-three-digit",
        job_description="Deliver reliable resume systems.",
        profile=profile,
        tailored=tailored,
    )

    assert tailored.rendered_profile_entry_ids[-1] == "PROFILE-100"
    assert tailored.role_strategy is not None
    assert len(tailored.role_strategy.bullet_rewrites) == 100
    assert len(receipt.claims) == 101
    assert receipt.claims[-1].claim_id == "CLAIM-101"
    assert receipt.claims[-1].profile_entry_id == "PROFILE-100"


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


def test_project_reordering_keeps_plain_descriptions_with_their_bullets() -> None:
    template = _project_description_template_docx()
    profile = extract_candidate_profile(template)
    expected_project_order = [
        "Platform Project",
        "Platform project description.",
        "Shipped Docker automation.",
        "Search Project",
        "Search project description.",
        "Built Python RAG retrieval.",
    ]

    deterministic = tailor_resume_docx(template, "Required: Docker platform delivery")
    deterministic_text = [
        paragraph.text for paragraph in read_template_paragraphs(deterministic.content)
    ]
    start = deterministic_text.index("Platform Project")
    assert deterministic_text[start : start + 6] == expected_project_order

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[object],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> object:
            del system, user, reasoning_effort
            return schema.model_validate(  # type: ignore[attr-defined]
                {
                    "role_summary": "Prioritize platform delivery.",
                    "evidence_priority": ["PROFILE-02", "PROFILE-01"],
                    "skill_priority": [],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "REWRITE",
                            "text": text,
                            "source_fact_ids": _fact_ids(profile, entry_id),
                        }
                        for entry_id, text in zip(
                            ["PROFILE-01", "PROFILE-02"],
                            profile.project_bullets,
                            strict=True,
                        )
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "Preserve approved facts.",
                }
            )

    prioritized = tailor_resume_docx_with_gateway(
        template,
        "Required: Docker platform delivery",
        gateway=Gateway(),  # type: ignore[arg-type]
    )
    prioritized_text = [
        paragraph.text for paragraph in read_template_paragraphs(prioritized.content)
    ]
    start = prioritized_text.index("Platform Project")
    assert prioritized_text[start : start + 6] == expected_project_order
    assert sorted(prioritized_text) == sorted(
        paragraph.text for paragraph in read_template_paragraphs(template)
    )


def test_chinese_literal_bullets_and_plain_skill_lines_reparse_after_tailoring() -> None:
    template = _chinese_literal_bullet_template_docx()
    profile = extract_candidate_profile(template)

    assert profile.project_bullets == ["• Built RAG retrieval service."]
    assert profile.experience_bullets == ["• Delivered verified AI workflow."]
    assert profile.skills == ["Python", "Docker", "• RAG"]
    assert profile.education == ["计算机科学硕士"]

    tailored = tailor_resume_docx(template, "Docker")
    reparsed = extract_candidate_profile(tailored.content)

    assert reparsed.project_bullets == profile.project_bullets
    assert reparsed.experience_bullets == profile.experience_bullets
    assert reparsed.education == profile.education
    visible = [item.text for item in read_template_paragraphs(tailored.content)]
    assert visible[visible.index("专业技能") + 1 : visible.index("工作经历")] == [
        "Docker", "Python", "• RAG"
    ]


@pytest.mark.parametrize(
    ("output_locale", "required_prompt_fragments", "excluded_prompt_fragment"),
    [
        (
            "zh-CN",
            (
                "可自然压缩、组合和重排重点，不要求保留每项细节或逐词对应",
                "PROFILE key 绑定原始履历，不能按 evidence_priority 重新编号",
                "target_fact_id 必须属于该 key",
            ),
            "Construct the strongest truthful one-page Application Resume",
        ),
        (
            "en-US",
            (
                "Construct the strongest truthful one-page Application Resume",
                "natural professional US English",
                "A REWRITE may cite facts only from its target PROFILE entry",
            ),
            "至少对两条与 JD 最相关的项目 bullet 做实质改写",
        ),
    ],
)
def test_gateway_uses_locale_specific_resume_editor_prompt(
    output_locale: str,
    required_prompt_fragments: tuple[str, ...],
    excluded_prompt_fragment: str,
) -> None:
    template = _template_docx()
    profile = extract_candidate_profile(template)
    entry_ids = ["PROFILE-01", "PROFILE-02", "PROFILE-03"]
    captured_system: list[str] = []
    captured_user_payloads: list[dict[str, object]] = []
    captured_rewrite_keys: list[set[str]] = []

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[BaseModel],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> object:
            request_payload = json.loads(user)
            del reasoning_effort
            captured_system.append(system)
            captured_user_payloads.append(request_payload)
            captured_rewrite_keys.append(
                set(
                    schema.model_json_schema()["$defs"]["ExactBulletRewrites"][
                        "properties"
                    ]
                )
            )
            requested_ids = request_payload.get(
                "requested_edit_profile_entry_ids", entry_ids
            )
            assert isinstance(requested_ids, list)
            def rewrite_text(entry_id: str, text: str) -> str:
                if output_locale == "zh-CN" and entry_id == "PROFILE-02":
                    return "构建 Python RAG 检索服务。"
                if output_locale == "zh-CN" and entry_id == "PROFILE-03":
                    return "完成 Docker 与 Kubernetes 自动化交付。"
                return text

            return schema.model_validate(
                {
                    "role_summary": "Build Python RAG systems.",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-01", "SKILL-02"],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "REWRITE",
                            "text": rewrite_text(entry_id, text),
                            "source_fact_ids": _fact_ids(profile, entry_id),
                        }
                        for entry_id, text in (
                            (entry_id, text)
                            for entry_id, text in zip(
                            entry_ids,
                            [
                                *profile.experience_bullets,
                                *profile.project_bullets,
                            ],
                            strict=True,
                            )
                            if entry_id in requested_ids
                        )
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "Preserve approved facts.",
                }
            )

    job_description = "Build Python RAG systems."
    tailored = tailor_resume_docx_with_gateway(
        template,
        job_description,
        gateway=Gateway(),  # type: ignore[arg-type]
        output_locale=output_locale,  # type: ignore[arg-type]
    )

    assert len(captured_system) == 1
    assert all(fragment in captured_system[0] for fragment in required_prompt_fragments)
    assert len(captured_user_payloads) == 1
    assert excluded_prompt_fragment not in captured_system[0]
    assert captured_system[0].endswith(
        json.dumps(_deterministic_hiring_signals(job_description), ensure_ascii=False)
    )
    if output_locale == "zh-CN":
        prompt_length = len(captured_system[0]) - len(
            json.dumps(_deterministic_hiring_signals(job_description), ensure_ascii=False)
        )
        assert 600 <= prompt_length <= 900
        assert "只编辑以下两个项目 PROFILE" in captured_system[0]
        assert captured_rewrite_keys == [{"PROFILE-02", "PROFILE-03"}]
        assert captured_user_payloads[0]["requested_edit_profile_entry_ids"] == [
            "PROFILE-02",
            "PROFILE-03",
        ]
    else:
        assert captured_rewrite_keys == [set(entry_ids)]
        assert "requested_edit_profile_entry_ids" not in captured_user_payloads[0]
    assert tailored.validation_diagnostics is not None
    assert tailored.validation_diagnostics.validator_status == "accepted"
    assert tailored.validation_diagnostics.rejected_count == 0
    assert (
        tailored.validation_diagnostics.verified_count
        + tailored.validation_diagnostics.supported_count
        + tailored.validation_diagnostics.unverified_count
        == tailored.validation_diagnostics.candidate_count
    )
    assert tailored.rendered_profile_entry_ids == tuple(entry_ids)
    if output_locale == "zh-CN":
        assert tailored.grounded_rewrites == 2
        assert tailored.unverified_rewrites == 0
        assert tailored.validation_diagnostics.supported_count == 2
        assert tailored.validation_diagnostics.unverified_count == 0
        assert tailored.requested_edit_profile_entry_ids == (
            "PROFILE-02",
            "PROFILE-03",
        )
        assert tailored.requested_project_material_rewrites == 2
        assert tailored.requested_project_rewrite_quality_status == "QUALITY_MET"
        receipt = _build_resume_provenance_receipt(
            run_id="resume-zh-editorial-warning",
            job_description=job_description,
            profile=profile,
            tailored=tailored,
        )
        assert receipt.all_exported_claims_supported is True


def test_fixed_rewrite_wire_slot_restricts_target_and_keeps_cross_profile_support() -> None:
    profile = CandidateProfile(
        skills=["RAG"],
        project_bullets=["Built RAG retrieval.", "Added FastAPI orchestration."],
    )
    fact_ids = _fact_ids(profile, "PROFILE-01", "PROFILE-02")
    schema = _exact_role_strategy_model(
        ["PROFILE-01", "PROFILE-02"],
        skill_ids=["SKILL-01"],
        fact_ids=fact_ids,
        include_summary=False,
    )
    payload = {
        "role_summary": "RAG role",
        "evidence_priority": ["PROFILE-01", "PROFILE-02"],
        "skill_priority": ["SKILL-01"],
        "bullet_rewrites": {
            "PROFILE-01": {
                "kind": "SYNTHESIS",
                "text": "Built RAG retrieval with FastAPI orchestration.",
                "target_fact_id": _fact_ids(profile, "PROFILE-01")[0],
                "supporting_fact_ids": _fact_ids(profile, "PROFILE-02"),
            },
            "PROFILE-02": {
                "kind": "REWRITE",
                "text": "Added FastAPI orchestration.",
                "target_fact_id": _fact_ids(profile, "PROFILE-02")[0],
                "supporting_fact_ids": [],
            },
        },
        "summary_rewrite": None,
        "unsupported_requirements": [],
        "rewrite_guidance": "Preserve approved facts.",
    }
    exact = schema.model_validate(payload)
    strategy = _role_strategy_from_exact(
        exact,
        entry_ids=["PROFILE-01", "PROFILE-02"],
        sanitized_skill_by_id={"SKILL-01": "RAG"},
        fact_source_by_id={
            fact_id: "PROFILE-01" if "PROFILE-01" in fact_id else "PROFILE-02"
            for fact_id in fact_ids
        },
        top_hiring_signals=["Required: RAG and FastAPI."],
    )
    assert strategy.bullet_rewrites[0].profile_entry_id == "PROFILE-01"
    assert strategy.bullet_rewrites[0].source_profile_entry_ids == [
        "PROFILE-01",
        "PROFILE-02",
    ]
    invalid = json.loads(json.dumps(payload))
    invalid["bullet_rewrites"]["PROFILE-01"]["target_fact_id"] = _fact_ids(
        profile, "PROFILE-02"
    )[0]
    with pytest.raises(ValidationError):
        schema.model_validate(invalid)
    legacy = json.loads(json.dumps(payload))
    legacy["bullet_rewrites"]["PROFILE-01"] = {
        "kind": "SYNTHESIS",
        "text": "Built RAG retrieval with FastAPI orchestration.",
        "source_fact_ids": fact_ids,
    }
    legacy_exact = schema.model_validate(legacy).model_dump(mode="json")
    assert legacy_exact["bullet_rewrites"]["PROFILE-01"]["target_fact_id"] == fact_ids[0]
    reversed_legacy = json.loads(json.dumps(legacy))
    reversed_legacy["bullet_rewrites"]["PROFILE-01"]["source_fact_ids"] = [
        *_fact_ids(profile, "PROFILE-02"),
        *_fact_ids(profile, "PROFILE-01"),
    ]
    reversed_exact = schema.model_validate(reversed_legacy).model_dump(mode="json")
    assert reversed_exact["bullet_rewrites"]["PROFILE-01"] == {
        "kind": "SYNTHESIS",
        "text": "Built RAG retrieval with FastAPI orchestration.",
        "target_fact_id": _fact_ids(profile, "PROFILE-01")[0],
        "supporting_fact_ids": [
            *_fact_ids(profile, "PROFILE-02"),
            *_fact_ids(profile, "PROFILE-01")[1:],
        ],
    }
    missing_target_legacy = json.loads(json.dumps(legacy))
    missing_target_legacy["bullet_rewrites"]["PROFILE-01"]["source_fact_ids"] = (
        _fact_ids(profile, "PROFILE-02")
    )
    with pytest.raises(ValidationError):
        schema.model_validate(missing_target_legacy)
    focused_schema = _exact_role_strategy_model(
        ["PROFILE-01", "PROFILE-02"],
        skill_ids=["SKILL-01"],
        fact_ids=fact_ids,
        include_summary=False,
        requested_edit_ids=("PROFILE-01", "PROFILE-02"),
    )
    assert set(
        focused_schema.model_json_schema()["$defs"]["ExactBulletRewrites"][
            "properties"
        ]
    ) == {"PROFILE-01", "PROFILE-02"}
    foreign = json.loads(json.dumps(payload))
    foreign["bullet_rewrites"]["PROFILE-03"] = foreign["bullet_rewrites"][
        "PROFILE-01"
    ]
    with pytest.raises(ValidationError):
        focused_schema.model_validate(foreign)


@pytest.mark.parametrize(
    ("project_texts", "expected_count", "expected_status"),
    [
        (
            {
                "PROFILE-02": "Built Python RAG retrieval.",
                "PROFILE-03": "Shipped Docker and Kubernetes automation.",
            },
            0,
            "QUALITY_NOT_MET",
        ),
        (
            {
                "PROFILE-02": "Created Python RAG retrieval.",
                "PROFILE-03": "Delivered Docker and Kubernetes automation.",
            },
            0,
            "QUALITY_NOT_MET",
        ),
        (
            {
                "PROFILE-02": "构建 Python RAG 检索服务。",
                "PROFILE-03": "完成 Docker 与 Kubernetes 自动化交付。",
            },
            2,
            "QUALITY_MET",
        ),
    ],
)
def test_focused_project_quality_counts_only_material_final_requested_edits(
    project_texts: dict[str, str], expected_count: int, expected_status: str
) -> None:
    template = _template_docx()
    profile = extract_candidate_profile(template)
    entry_ids = ["PROFILE-01", "PROFILE-02", "PROFILE-03"]
    source_by_id = dict(
        zip(
            entry_ids,
            [*profile.experience_bullets, *profile.project_bullets],
            strict=True,
        )
    )

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[object],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> object:
            del system, reasoning_effort
            requested_ids = json.loads(user)["requested_edit_profile_entry_ids"]
            assert requested_ids == ["PROFILE-02", "PROFILE-03"]
            return schema.model_validate(  # type: ignore[attr-defined]
                {
                    "role_summary": "Build Python RAG systems.",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-01", "SKILL-02"],
                    "bullet_rewrites": {
                        entry_id: {
                            "kind": "REWRITE",
                            "text": project_texts[entry_id],
                            "source_fact_ids": _fact_ids(profile, entry_id),
                        }
                        for entry_id in requested_ids
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "Preserve approved facts.",
                }
            )

    tailored = tailor_resume_docx_with_gateway(
        template,
        "Build Python RAG systems.",
        gateway=Gateway(),  # type: ignore[arg-type]
        output_locale="zh-CN",
    )

    assert tailored.requested_edit_profile_entry_ids == ("PROFILE-02", "PROFILE-03")
    assert tailored.requested_project_material_rewrites == expected_count
    assert tailored.requested_project_rewrite_quality_status == expected_status
    assert tailored.role_strategy is not None
    assert next(
        rewrite.text
        for rewrite in tailored.role_strategy.bullet_rewrites
        if rewrite.profile_entry_id == "PROFILE-01"
    ) == source_by_id["PROFILE-01"]


def test_chinese_lexical_anchor_warning_survives_as_unverified_provenance() -> None:
    source = "搭建本地检索服务"
    profile = CandidateProfile(project_bullets=[source])
    strategy = RoleStrategy(
        role_summary="检索工程岗位",
        top_hiring_signals=["检索服务"],
        evidence_priority=["PROFILE-01"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="构建查询工作流",
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            )
        ],
        rewrite_guidance="自然改写且不增加事实。",
    )
    selected, _entries, diagnostics = _select_safe_rewrites(
        strategy,
        profile=profile,
        job_description="检索服务",
        output_locale="zh-CN",
    )
    assert diagnostics.rejected_count == 0
    assert [warning.code for warning in diagnostics.editorial_warnings] == [
        "LEXICAL_ANCHOR_NOT_CONFIRMED"
    ]


def test_ai_literal_bullet_rewrite_restores_original_marker_and_provenance() -> None:
    template = _chinese_literal_bullet_template_docx()
    profile = extract_candidate_profile(template)
    entry_ids = ["PROFILE-01", "PROFILE-02"]
    fact_ids = {
        entry_id: _fact_ids(profile, entry_id)
        for entry_id in entry_ids
    }

    class Gateway:
        descriptor = GatewayDescriptor(
            provider=ModelProviderId.OLLAMA,
            display_name="Local test model",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.LOOPBACK,
            model="test-model",
            base_url="http://127.0.0.1:11434",
        )
        last_call_profile = None

        def complete(
            self,
            schema: type[object],
            *,
            system: str,
            user: str,
            reasoning_effort: str = "none",
        ) -> object:
            del system, user, reasoning_effort
            return schema.model_validate(  # type: ignore[attr-defined]
                {
                    "role_summary": "构建 AI 工作流。",
                    "evidence_priority": entry_ids,
                    "skill_priority": ["SKILL-03", "SKILL-02", "SKILL-01"],
                    "bullet_rewrites": {
                        "PROFILE-01": {
                            "kind": "REWRITE",
                            "text": "• Delivered verified AI workflow.",
                            "source_fact_ids": fact_ids["PROFILE-01"],
                        },
                        "PROFILE-02": {
                            "kind": "REWRITE",
                            "text": "Built RAG retrieval service.",
                            "source_fact_ids": fact_ids["PROFILE-02"],
                        },
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "保留已批准事实。",
                }
            )

    tailored = tailor_resume_docx_with_gateway(
        template,
        "RAG Docker Python",
        gateway=Gateway(),  # type: ignore[arg-type]
    )
    receipt = _build_resume_provenance_receipt(
        run_id="resume-20260930T000000Z-chinese-literal",
        job_description="RAG Docker Python",
        profile=profile,
        tailored=tailored,
    )

    reparsed = extract_candidate_profile(tailored.content)
    assert reparsed.project_bullets == ["• Built RAG retrieval service."]
    assert reparsed.experience_bullets == ["• Delivered verified AI workflow."]
    assert reparsed.skills == ["• RAG", "Docker", "Python"]
    assert tailored.rendered_profile_entry_ids == tuple(entry_ids)
    assert tailored.role_strategy is not None
    assert [rewrite.text for rewrite in tailored.role_strategy.bullet_rewrites] == [
        "• Delivered verified AI workflow.",
        "• Built RAG retrieval service.",
    ]
    assert tailored.grounded_rewrites == 0
    assert tailored.validation_diagnostics is not None
    assert tailored.validation_diagnostics.verified_count == 2
    assert tailored.validation_diagnostics.supported_count == 0
    assert all(not item.accepted and not item.rendered for item in tailored.evidence_adoption)
    assert [claim.profile_entry_id for claim in receipt.claims] == entry_ids


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
        "unverified_count": 0,
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


def test_chinese_source_copies_without_literal_bullets_pass_the_anchor_gate() -> None:
    first_source = "• 负责AI应用开发"
    second_source = "• 维护本地检索系统"
    profile = CandidateProfile(project_bullets=[first_source, second_source])
    strategy = RoleStrategy(
        role_summary="AI application role",
        top_hiring_signals=["Required: AI application development."],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="负责 AI 应用开发",
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="维护本地检索系统",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
        rewrite_guidance="Preserve approved facts.",
    )

    _validate_role_strategy(
        strategy,
        profile=profile,
        job_description="Required: AI application development.",
        output_locale="zh-CN",
    )


def test_chinese_exact_identity_does_not_bypass_mutation_or_missing_synthesis_fact() -> None:
    first_source = "• 负责AI应用开发"
    second_source = "• 维护本地检索系统"
    profile = CandidateProfile(project_bullets=[first_source, second_source])
    mutated = RoleStrategy(
        role_summary="AI application role",
        top_hiring_signals=["Required: AI application development."],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        rewrite_guidance="Preserve approved facts.",
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="主导AI应用开发",
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="维护本地检索系统",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
    )
    with pytest.raises(ResumeTemplateError) as mutated_error:
        _validate_role_strategy(
            mutated,
            profile=profile,
            job_description="Required: AI application development.",
            output_locale="zh-CN",
        )
    assert mutated_error.value.validation_diagnostics is not None
    assert mutated_error.value.validation_diagnostics.failures

    missing_second_fact = RoleStrategy(
        role_summary="AI application role",
        top_hiring_signals=["Required: AI application development."],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        rewrite_guidance="Preserve approved facts.",
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                kind="SYNTHESIS",
                text="负责AI应用开发",
                source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
                source_fact_ids=_fact_ids(profile, "PROFILE-01", "PROFILE-02"),
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="维护本地检索系统",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
    )
    _entries, synthesis_warnings = _validate_role_strategy(
        missing_second_fact,
        profile=profile,
        job_description="Required: AI application development.",
        output_locale="zh-CN",
        return_editorial_warnings=True,
    )
    assert any(
        warning.fact_id.startswith("FACT-PROFILE-02-")
        for warning in synthesis_warnings
    )


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
                text="设计查询与服务协作流程。",
                source_profile_entry_ids=["PROFILE-01", "PROFILE-02"],
                source_fact_ids=fact_ids,
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="完成服务协作编排。",
                source_fact_ids=_fact_ids(profile, "PROFILE-02"),
            ),
        ],
        summary_rewrite=GroundedResumeSummaryRewrite(
            text="专注于查询与服务协作的证据驱动工程师。",
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
    assert {
        warning.claim_id for warning in diagnostics.editorial_warnings
    } >= {"PROFILE-01", "SUMMARY"}
    assert any(
        warning.fact_id.startswith("FACT-PROFILE-02-")
        for warning in diagnostics.editorial_warnings
    )

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


def test_chinese_rewrite_rejects_unmeasured_outcome_and_keeps_mechanism() -> None:
    source = "实现 RAG 检索，通过引用和来源哈希支持结果追溯。"
    profile = CandidateProfile(skills=["RAG"], project_bullets=[source])
    strategy = RoleStrategy(
        role_summary="RAG role",
        top_hiring_signals=["Required: RAG."],
        evidence_priority=["PROFILE-01"],
        skill_priority=["RAG"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="通过引用和来源哈希追溯 RAG 检索结果，提升模型输出的准确性。",
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            ),
        ],
        rewrite_guidance="仅使用来源事实。",
    )
    selected, _entries, diagnostics = _select_safe_rewrites(
        strategy, profile=profile, job_description="Required: RAG.", output_locale="zh-CN"
    )
    assert selected.bullet_rewrites[0].text == source
    assert ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION in {
        failure.rule_code for failure in diagnostics.failures
    }

    payload = strategy.model_dump(mode="json")
    payload["bullet_rewrites"][0]["text"] = "通过引用和来源哈希追溯 RAG 检索结果。"
    selected, _entries, diagnostics = _select_safe_rewrites(
        RoleStrategy.model_validate(payload),
        profile=profile,
        job_description="Required: RAG.",
        output_locale="zh-CN",
    )
    assert selected.bullet_rewrites[0].text == payload["bullet_rewrites"][0]["text"]
    assert diagnostics.validator_status == "accepted"


def test_chinese_rewrite_allows_source_supported_improvement_translation() -> None:
    source = "Improved RAG retrieval accuracy."
    profile = CandidateProfile(skills=["RAG"], project_bullets=[source])
    strategy = RoleStrategy(
        role_summary="RAG role",
        top_hiring_signals=["Required: RAG."],
        evidence_priority=["PROFILE-01"],
        skill_priority=["RAG"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text="提升 RAG 检索准确性。",
                source_fact_ids=_fact_ids(profile, "PROFILE-01"),
            ),
        ],
        rewrite_guidance="仅使用来源事实。",
    )
    selected, _entries, diagnostics = _select_safe_rewrites(
        strategy, profile=profile, job_description="Required: RAG.", output_locale="zh-CN"
    )
    assert selected.bullet_rewrites[0].text == "提升 RAG 检索准确性。"
    assert diagnostics.validator_status == "accepted"


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
