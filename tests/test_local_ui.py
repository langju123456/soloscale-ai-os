import hashlib
import http.client
import io
import json
import os
import subprocess
import threading
import time
import urllib.parse
import zipfile
from http.server import HTTPServer
from pathlib import Path
from typing import Literal, TypeVar

import pytest
from pydantic import BaseModel

from soloscale.content_models import ContentReviewDecision
from soloscale.content_ui import run_content_form
from soloscale.content_workspace import save_content_review
from soloscale.conversation_intake import parse_codex_session
from soloscale.evidence_hub import EvidenceHub
from soloscale.knowledge_models import ContentRole, RetrievalHit, SourceKind
from soloscale.learning_traceability import run_learning_traceability
from soloscale.local_ui import (
    OllamaReadiness,
    ResumeJobManager,
    ResumeJobSnapshot,
    SoloScaleLocalUIHandler,
    UIActionResult,
    UploadedFile,
    _ai_settings_page,
    _applications_section_html,
    _apply_ai_provider_preference,
    _create_learning_case_ui,
    _create_resume_pdf_preview,
    _finalize_resume_preview,
    _heygen_settings_page,
    _home_page,
    _interview_defense_panel,
    _learning_page,
    _load_ai_provider_preference,
    _page,
    _parse_submission,
    _result_card,
    _resume_graph,
    _run_action,
    _run_learning_workspace,
    _run_user_resume,
    _save_ai_provider_preference,
    _search_job_evidence,
    _serve_resume_download,
    _split_path_list,
    _user_page,
    _workspace_path,
    _workspace_paths,
    _write_private_bytes,
    _write_private_json,
)
from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayDescriptor,
    GatewayTransportScope,
    ModelProviderId,
)
from soloscale.resume_docx import read_template_paragraphs
from soloscale.resume_models import CandidateProfile
from soloscale.resume_template_intake import inspect_template_html
from soloscale.resume_workspace import (
    map_interview_defense_bullet,
    run_resume_workspace,
)
from soloscale.work_ui import CodexImportJobManager

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


def _expected_repository_ref() -> str:
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if branch:
        return branch
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    github_ref = os.environ.get("GITHUB_REF", "")
    assert github_ref.startswith("refs/")
    return github_ref.removeprefix("refs/")


def _uploaded_resume_docx() -> bytes:
    def paragraph(text: str, *, bullet: bool = False) -> str:
        numbering = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        return f"<w:p>{numbering if bullet else ''}<w:r><w:t>{text}</w:t></w:r></w:p>"

    values = [
        paragraph("LANG JU"),
        paragraph("AI Engineer"),
        paragraph("lang@example.com"),
        paragraph("SUMMARY"),
        paragraph("Evidence-grounded engineer."),
        paragraph("PROJECT HIGHLIGHTS"),
        paragraph("RAG Project"),
        paragraph("Built Python RAG retrieval.", bullet=True),
        paragraph("EDUCATION"),
        paragraph("M.S. Information Systems"),
        paragraph("TECHNICAL SKILLS"),
        paragraph("Python, RAG", bullet=True),
        paragraph("WORK EXPERIENCE"),
        paragraph("Example Company"),
        paragraph("Delivered production systems.", bullet=True),
    ]
    document = (
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(values)
        + "<w:sectPr/></w:body></w:document>"
    ).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"content-types")
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", b"styles")
    return target.getvalue()


def _role_resume_docx() -> bytes:
    def paragraph(text: str, *, bullet: bool = False) -> str:
        numbering = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        return f"<w:p>{numbering if bullet else ''}<w:r><w:t>{text}</w:t></w:r></w:p>"

    values = [
        paragraph("LANG JU"),
        paragraph("AI Engineer"),
        paragraph("lang@example.com"),
        paragraph("SUMMARY"),
        paragraph("Evidence-grounded engineer."),
        paragraph("PROJECT HIGHLIGHTS"),
        paragraph("Client Delivery"),
        paragraph(
            "Translated customer requirements with Ardent Mills and Kangni stakeholders.",
            bullet=True,
        ),
        paragraph("SoloScale"),
        paragraph(
            "Rapidly prototyped SoloScale full-stack GenAI workflows with RAG and agents.",
            bullet=True,
        ),
        paragraph("BuildLog"),
        paragraph(
            "Designed BuildLog architecture for AI-assisted development and evals.",
            bullet=True,
        ),
        paragraph("EDUCATION"),
        paragraph("M.S. Information Systems"),
        paragraph("TECHNICAL SKILLS"),
        paragraph("Customer delivery, requirements, stakeholders", bullet=True),
        paragraph("Python, RAG, agents, evals", bullet=True),
        paragraph("WORK EXPERIENCE"),
        paragraph("Example Company"),
        paragraph(
            "Improved agent reliability for customer-facing stakeholder delivery "
            "and requirements translation.",
            bullet=True,
        ),
    ]
    document = (
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(values)
        + "<w:sectPr/></w:body></w:document>"
    ).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"content-types")
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", b"styles")
    return target.getvalue()


class RecordingResumeGateway:
    descriptor = GatewayDescriptor(
        provider=ModelProviderId.OLLAMA,
        display_name="Test gateway",
        configuration_state=GatewayConfigurationState.CONFIGURED,
        transport_scope=GatewayTransportScope.LOOPBACK,
        model="test-model",
        base_url="http://127.0.0.1:11434",
    )

    def __init__(self) -> None:
        self.requests: list[str] = []

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        assert "approved Candidate Profile facts" in system
        assert "Deterministic hiring signals:" in system
        assert "top_hiring_signals" not in schema.model_json_schema()["properties"]
        assert reasoning_effort == "none"
        self.requests.append(user)
        request_payload = json.loads(user)
        fact_ids_by_source: dict[str, list[str]] = {}
        for fact in request_payload["candidate_profile"]["atomic_facts"]:
            if fact["source_kind"] != "PROFILE_ENTRY":
                continue
            fact_ids_by_source.setdefault(fact["profile_entry_id"], []).append(
                fact["fact_id"]
            )
        exact = {
            "PROFILE-01": (
                "Improved agent reliability for customer-facing stakeholder delivery "
                "and requirements translation."
            ),
            "PROFILE-02": (
                "Translated customer requirements with Ardent Mills and Kangni stakeholders."
            ),
            "PROFILE-03": (
                "Rapidly prototyped SoloScale full-stack GenAI workflows with RAG and agents."
            ),
            "PROFILE-04": "Designed BuildLog architecture for AI-assisted development and evals.",
        }
        if "Engineer in Residence" in user:
            priority = ["PROFILE-03", "PROFILE-04", "PROFILE-01", "PROFILE-02"]
            skills = [
                "Python, RAG, agents, evals",
                "Customer delivery, requirements, stakeholders",
            ]
            rewrites = dict(exact)
            rewrites["PROFILE-03"] = (
                "SoloScale full-stack GenAI workflows with RAG and agents; "
                "BuildLog architecture for AI-assisted development and evals."
            )
            synthesis_target = "PROFILE-03"
            synthesis_sources = ["PROFILE-03", "PROFILE-04"]
            summary_rewrite: dict[str, object] | None = {
                "text": (
                    "SoloScale full-stack GenAI workflows with RAG and agents; "
                    "BuildLog architecture for AI-assisted development and evals."
                ),
                "source_fact_ids": [
                    fact_id
                    for source_id in synthesis_sources
                    for fact_id in fact_ids_by_source[source_id]
                ],
            }
            unsupported: list[str] = []
            guidance = "Lead with product prototyping and architecture iteration."
        elif "FPGA compiler" in user:
            priority = list(exact)
            skills = [
                "Python, RAG, agents, evals",
                "Customer delivery, requirements, stakeholders",
            ]
            rewrites = dict(exact)
            synthesis_target = None
            synthesis_sources = []
            summary_rewrite = None
            unsupported = [
                "Required: FPGA compiler design and semiconductor verification."
            ]
            guidance = (
                "Keep approved facts unchanged and expose the unrelated requirement gap."
            )
        else:
            priority = ["PROFILE-01", "PROFILE-02", "PROFILE-03", "PROFILE-04"]
            skills = [
                "Customer delivery, requirements, stakeholders",
                "Python, RAG, agents, evals",
            ]
            rewrites = dict(exact)
            rewrites["PROFILE-01"] = (
                "customer-facing stakeholder delivery and requirements translation with "
                "Ardent Mills and Kangni stakeholders."
            )
            synthesis_target = "PROFILE-01"
            synthesis_sources = ["PROFILE-01", "PROFILE-02"]
            summary_rewrite = {
                "text": (
                    "customer-facing stakeholder delivery and requirements translation; "
                    "Ardent Mills and Kangni stakeholders."
                ),
                "source_fact_ids": [
                    fact_id
                    for source_id in synthesis_sources
                    for fact_id in fact_ids_by_source[source_id]
                ],
            }
            unsupported = []
            guidance = "Lead with customer delivery and requirements translation."
        output_locale = request_payload.get("output_locale", "en-US")
        role_summary = "JD-conditioned strategy for the selected role."
        if output_locale == "zh-CN":
            translations = {
                "PROFILE-01": "围绕 customer-facing 场景改进智能体可靠性与需求转化。",
                "PROFILE-02": "面向 Ardent Mills 与 Kangni 完成需求转化和协作。",
                "PROFILE-03": "快速构建 SoloScale 全栈生成式 AI 工作流，覆盖 RAG 与 agents。",
                "PROFILE-04": "设计 BuildLog 架构，支持 AI-assisted 开发与 evals。",
            }
            rewrites = translations
            if synthesis_target == "PROFILE-01":
                rewrites["PROFILE-01"] = (
                    "围绕 customer-facing 场景完成需求转化，并与 Ardent Mills "
                    "及 Kangni 协作改进智能体可靠性。"
                )
            elif synthesis_target == "PROFILE-03":
                rewrites["PROFILE-03"] = (
                    "快速构建 SoloScale 全栈生成式 AI 工作流，并以 BuildLog "
                    "支持 RAG、agents 与 AI-assisted 开发。"
                )
            if summary_rewrite is not None:
                summary_rewrite = {
                    "text": rewrites[synthesis_target or "PROFILE-01"],
                    "source_fact_ids": [
                        fact_id
                        for source_id in synthesis_sources
                        for fact_id in fact_ids_by_source[source_id]
                    ],
                }
            role_summary = "面向目标岗位、受事实约束的简历策略。"
            guidance = "使用自然中文重组已批准事实，不增加经历。"
        skill_ids = {
            "Customer delivery, requirements, stakeholders": "SKILL-01",
            "Python, RAG, agents, evals": "SKILL-02",
        }
        return schema.model_validate(
            {
                "role_summary": role_summary,
                "evidence_priority": priority,
                "skill_priority": [skill_ids[item] for item in skills],
                "bullet_rewrites": {
                    entry_id: {
                        "kind": (
                            "SYNTHESIS"
                            if entry_id == synthesis_target
                            else "REWRITE"
                        ),
                        "text": text,
                        "source_fact_ids": [
                            fact_id
                            for source_id in (
                                synthesis_sources
                                if entry_id == synthesis_target
                                else [entry_id]
                            )
                            for fact_id in fact_ids_by_source[source_id]
                        ],
                    }
                    for entry_id, text in rewrites.items()
                },
                "summary_rewrite": summary_rewrite,
                "unsupported_requirements": unsupported,
                "rewrite_guidance": guidance,
            }
        )


class RecordingExpertReviewGateway:
    descriptor = GatewayDescriptor(
        provider=ModelProviderId.OPENAI_COMPATIBLE,
        display_name="OpenAI expert review",
        configuration_state=GatewayConfigurationState.CONFIGURED,
        transport_scope=GatewayTransportScope.EXTERNAL,
        model="gpt-5.6-sol",
        base_url="https://api.openai.com/v1/chat/completions",
    )

    def __init__(
        self,
        *,
        propose_new_fact: bool = False,
        stale_patch: bool = False,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.propose_new_fact = propose_new_fact
        self.stale_patch = stale_patch

    def complete(
        self,
        schema: type[ResponseModelT],
        *,
        system: str,
        user: str,
        reasoning_effort: Literal["none", "low"] = "low",
    ) -> ResponseModelT:
        assert "Return patches only" in system
        assert reasoning_effort == "low"
        payload = json.loads(user)
        self.requests.append(payload)
        draft = payload["draft_resume_bullets"]
        assert isinstance(draft, dict)
        before = str(draft["PROFILE-03"])
        return schema.model_validate(
            {
                "summary": "Tighten the strongest AI product bullet.",
                "patches": [
                    {
                        "profile_entry_id": "PROFILE-03",
                        "before_sha256": (
                            "0" * 64
                            if self.stale_patch
                            else hashlib.sha256(before.encode("utf-8")).hexdigest()
                        ),
                        "after": (
                            "Integrated SoloScale full-stack GenAI workflows with RAG "
                            "and agents and BuildLog architecture for AI-assisted "
                            "development and evals."
                        ),
                        "new_factual_claims": (
                            ["Served thousands of users"]
                            if self.propose_new_fact
                            else []
                        ),
                        "rationale": "Remove low-value trailing wording.",
                    }
                ],
                "omitted_high_value_profile_entry_ids": [],
            }
        )


def test_split_path_list_supports_comma_and_newline() -> None:
    assert _split_path_list("a, b\nc,, d") == ["a", "b", "c", "d"]


def test_home_keeps_three_outcomes_visible_and_resume_flow_intact(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    resume_dir = data_root / "resume-runs" / "resume-20260820T100000Z-aaaaaaaaaa"
    resume_dir.mkdir(parents=True)
    (resume_dir / "08_resume.docx").write_bytes(b"synthetic-docx")
    (resume_dir / "09_user_ui.json").write_text(
        json.dumps(
            {
                "download_url": f"/downloads/{resume_dir.name}/resume.docx",
                "output_filename": "Resume_Synthetic_Role.docx",
            }
        ),
        encoding="utf-8",
    )
    content_result = run_content_form(
        {
            "topic": "Evidence-backed owner dogfood",
            "audience": "AI builders",
            "language": "English",
            "source_label": "synthetic receipt",
            "verified_claims": (
                "A synthetic content run completed. | synthetic receipt | "
                "No external outcome is claimed."
            ),
            "call_to_action": "Review the bundle.",
            "generation_mode": "template",
        },
        data_root,
    )
    assert content_result.run_id is not None
    save_content_review(
        data_root=data_root,
        run_id=content_result.run_id,
        decision=ContentReviewDecision.APPROVED,
    )

    home = _home_page(data_root=data_root)
    assert "你今天想完成什么" in home
    assert "找到机会" in home
    assert "能解释自己" in home
    assert "建立影响力" in home
    assert 'href="/resume?lang=zh-CN"' in home
    assert 'href="/learning?lang=zh-CN"' in home
    assert 'href="/creator?lang=zh-CN"' in home
    assert 'href="/video?lang=zh-CN"' in home
    assert 'href="/creator/publish?lang=zh-CN"' in home
    assert 'href="/work?lang=zh-CN"' in home
    assert home.count('class="outcome-hitbox"') == 3
    assert 'id="page-progress"' in home
    assert "showProgress" in home
    assert "我的工作资料" in home
    assert "继续上次工作" in home
    assert "最近简历可以继续下载" in home
    assert "Resume_Synthetic_Role.docx" in home
    assert "最近内容包" in home
    assert "已批准" in home
    assert "扫描今天" in home
    assert "EvidenceHub" not in home
    assert "Ollama" not in home

    active_job = ResumeJobSnapshot(
        job_id="resume-job-aaaaaaaaaaaa",
        phase="GENERATING",
        result=None,
        stage_durations_ms={"post_response_ms": 4},
        total_elapsed_ms=12_300,
        preview_state="pending",
        failed_phase=None,
    )
    active_home = _home_page(data_root=data_root, resume_job=active_job)
    assert "简历正在后台生成" in active_home
    assert "当前阶段：生成针对性内容 · 12.3s" in active_home
    assert (
        'href="/resume/jobs/resume-job-aaaaaaaaaaaa?lang=zh-CN"'
        in active_home
    )

    page = _user_page(None, tmp_path / ".soloscale", {})
    assert 'action="/generate"' in page
    assert 'name="resume_template"' in page
    assert 'accept=".pdf,.docx,.txt,.md"' in page
    assert 'name="job_description_file"' in page
    assert 'name="support_document"' in page
    assert 'name="job_description"' in page
    assert 'name="tailoring_instructions"' in page
    assert 'action="/resume/template-preview"' in page
    assert 'name="layout_template_file"' in page
    assert 'name="layout_template_url"' in page
    assert 'name="layout_template_html"' in page
    assert 'name="resume_output_language"' in page
    assert '<option value="both"' in page
    assert 'name="approve_resume_processing"' in page
    assert 'name="retention_mode"' not in page
    assert 'name="approve_candidate_claims"' not in page
    assert 'name="approve_model_context"' not in page
    assert "不会静默改用其他服务" in page
    assert "使用当前 AI 服务生成" in page
    assert 'href="/?lang=zh-CN"' in page
    assert 'href="/resume?lang=zh-CN"' in page
    assert 'href="/advanced?lang=zh-CN"' in page
    assert 'href="/creator?lang=zh-CN"' in page
    assert 'href="/learning?lang=zh-CN"' in page
    assert 'href="/creator/publish?lang=zh-CN"' in page
    assert 'href="/work?lang=zh-CN"' in page
    assert "使用我的工作资料" in page
    assert "knowledge-sync" not in page
    assert 'name="model"' not in page
    assert 'name="source_kind"' not in page

    english = _user_page(None, tmp_path / ".soloscale", {}, "en")
    assert '<html lang="en">' in english
    assert "Turn your real experience into a resume for this role." in english
    assert 'href="/creator?lang=en"' in english
    assert 'name="ui_locale" value="en"' in english
    english_home = _home_page("en", data_root=tmp_path / ".soloscale")
    assert "What do you want to accomplish today?" in english_home
    assert "Get the job" in english_home
    assert "Defend the job" in english_home
    assert "Build visibility" in english_home
    assert "Your work" in english_home


def test_advanced_page_is_bilingual_and_hides_command_language(tmp_path: Path) -> None:
    page = _page(None, tmp_path / ".soloscale", {})
    assert "检查资料库状态" in page
    assert "更新工程概览" in page
    assert "搜索本地证据" in page
    assert "当前 AI 服务（只读诊断）" in page
    assert "SoloScale 托管 AI" in page
    assert "刷新本地资料索引" not in page
    assert "包含 Codex 对话记录" not in page
    assert "旧版简历工程工作区" not in page
    assert "运行证据问答" not in page
    assert 'id="ai-providers"' not in page
    assert 'href="/settings/ai?lang=zh-CN"' not in page
    assert "管理 AI 服务" not in page
    assert 'action="/settings/ai-provider"' not in page
    assert "用于下一次内容生成" not in page
    assert "用于下一次简历生成" not in page
    assert 'class="check-row"' not in page
    assert '<input type="checkbox"' not in page
    assert "Run knowledge-sync" not in page
    assert "--no-codex" not in page
    assert "未匹配到动作" not in page

    english = _page(None, tmp_path / ".soloscale", {}, "en")
    assert '<html lang="en">' in english
    assert "Check knowledge status" in english
    assert "Search local evidence" in english
    assert "Current AI service (read-only diagnostic)" in english
    assert "Refresh the local knowledge index" not in english
    assert "Legacy resume engineering workspace" not in english
    assert "Run knowledge-status" not in english


def test_packaged_knowledge_refresh_runs_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_subprocess(*_args: object, **_kwargs: object) -> UIActionResult:
        raise AssertionError("packaged knowledge refresh must not launch the backend as Python")

    monkeypatch.setattr("soloscale.local_ui._run_command", fail_subprocess)

    result = _run_action(
        {"action": "knowledge-sync"},
        tmp_path / "data",
        tmp_path,
    )

    assert result is not None
    assert result.return_code == 0
    assert result.command == "in-process knowledge refresh"
    assert "Discovered 0" in result.stdout


def test_ai_provider_preference_is_shared_and_persists_privately(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    preference = _save_ai_provider_preference(
        data_root,
        provider=ModelProviderId.OLLAMA.value,
        model="qwen3:8b",
    )

    assert preference.provider is ModelProviderId.OLLAMA
    assert _load_ai_provider_preference(data_root) == preference
    resume_form: dict[str, str] = {}
    content_form: dict[str, str] = {}
    _apply_ai_provider_preference(resume_form, data_root)
    _apply_ai_provider_preference(content_form, data_root)
    assert resume_form == content_form == {
        "generation_mode": ModelProviderId.OLLAMA.value,
        "provider_model": "qwen3:8b",
    }
    rendered = _page(None, data_root, {})
    assert "本地 AI" in rendered
    assert "qwen3:8b" in rendered
    assert 'name="provider"' not in rendered
    settings_path = data_root / "settings" / "ai-provider.json"
    assert settings_path.stat().st_mode & 0o777 == 0o600
    assert settings_path.parent.stat().st_mode & 0o777 == 0o700
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload["default_ai_provider"] == ModelProviderId.OLLAMA.value
    assert "api_key" not in payload


def test_ai_service_pages_show_one_default_and_keep_openai_secret_out_of_html(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "soloscale.local_ui._ollama_readiness",
        lambda preference: OllamaReadiness(False, False, False),
    )
    data_root = tmp_path / ".soloscale"
    overview = _ai_settings_page(data_root)
    assert "当前 AI 服务" in overview
    assert 'href="/settings/ai/local?lang=zh-CN"' in overview
    assert 'href="/settings/ai/openai?lang=zh-CN"' in overview
    assert "选择一次，所有工作流自动使用" in overview
    assert "创作与发布服务" in overview
    assert "HeyGen" in overview
    assert 'href="/settings/media/heygen?lang=zh-CN"' in overview
    assert "LinkedIn" in overview
    assert "YouTube" in overview

    local_page = _ai_settings_page(data_root, detail="local")
    assert 'value="use_default" type="submit" disabled' in local_page

    hosted_page = _ai_settings_page(data_root, detail="hosted")
    assert 'value="use_default" type="submit" disabled' in hosted_page

    monkeypatch.setattr(
        "soloscale.local_ui._ollama_readiness",
        lambda preference: OllamaReadiness(True, True, True),
    )
    ready_local_page = _ai_settings_page(data_root, detail="local")
    assert 'value="use_default" type="submit" disabled' not in ready_local_page

    browser_openai = _ai_settings_page(data_root, detail="openai")
    assert "普通浏览器不会接收密钥" in browser_openai
    assert 'id="save-openai-key" type="submit" disabled' in browser_openai

    from soloscale.desktop_credentials import (
        _clear_for_tests,
        _frame_for_tests,
        configure_openai_credential_from_stdin,
    )

    sentinel = "synthetic-openai-key-never-render"
    configure_openai_credential_from_stdin(_frame_for_tests(sentinel.encode()))
    try:
        desktop_openai = _ai_settings_page(
            data_root, detail="openai", desktop_mode=True
        )
        assert "已配置" in desktop_openai
        assert "soloscaleCredentials" in desktop_openai
        assert sentinel not in desktop_openai
    finally:
        _clear_for_tests()

    browser_heygen = _heygen_settings_page(data_root)
    assert "普通浏览器不会接收它" in browser_heygen
    assert "测试 Avatar · 需先预估费用" in browser_heygen
    assert 'id="heygen-api-key"' in browser_heygen
    assert "HEYGEN_API_KEY" not in browser_heygen


def test_interview_defense_ui_requires_explicit_mapping_and_opens_exact_run(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    resume = run_resume_workspace(
        data_root=data_root,
        job_description="Required: Python retrieval",
        candidate_profile=CandidateProfile(
            experience_bullets=["Built an operator-supplied retrieval workflow."]
        ),
        evidence_hits=[],
    )
    before_learning = _interview_defense_panel(
        data_root=data_root,
        run_id=resume.run_id,
        repo_root=REPOSITORY_ROOT,
    )
    assert "需要关联" in before_learning
    assert "面试答辩 →" not in before_learning

    selected = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    newer = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
    )
    before_mapping = _interview_defense_panel(
        data_root=data_root,
        run_id=resume.run_id,
        repo_root=REPOSITORY_ROOT,
    )
    assert "确认关联对话式 RAG 锚点" in before_mapping
    assert max(selected.run_id, newer.run_id) in before_mapping
    assert _expected_repository_ref() in before_mapping
    assert "面试答辩 →" not in before_mapping

    map_interview_defense_bullet(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
        resume_run_id=resume.run_id,
        bullet_id="PROFILE-01",
        learning_run_id=selected.run_id,
    )
    mapped_panel = _interview_defense_panel(
        data_root=data_root,
        run_id=resume.run_id,
        repo_root=REPOSITORY_ROOT,
    )
    assert "面试答辩 →" in mapped_panel
    assert selected.run_id in mapped_panel
    assert "#interview-defense" in mapped_panel

    run_count = len(list((data_root / "learning-runs").iterdir()))
    page = _learning_page(
        data_root,
        REPOSITORY_ROOT,
        {
            "run_id": selected.run_id,
            "resume_run_id": resume.run_id,
            "bullet_id": "PROFILE-01",
        },
    )
    assert 'id="interview-defense"' in page
    assert f"精确学习记录： <code>{selected.run_id}</code>" in page
    assert "推理锚点" in page
    assert "代码锚点" in page
    assert "测试锚点" in page
    assert "练习语言" in page
    assert "独立于界面语言" in page
    assert "src/soloscale/knowledge_store.py" in page
    assert "复制锚点包" in page
    assert "不证明作者身份" in page
    assert len(list((data_root / "learning-runs").iterdir())) == run_count

    anchors_path = data_root / "learning-runs" / selected.run_id / "03_code_anchors.json"
    anchors = json.loads(anchors_path.read_text(encoding="utf-8"))
    anchors["code_anchors"][0]["file_sha256"] = "0" * 64
    anchors_path.write_text(json.dumps(anchors), encoding="utf-8")
    stale_panel = _interview_defense_panel(
        data_root=data_root,
        run_id=resume.run_id,
        repo_root=REPOSITORY_ROOT,
    )
    assert "需要关联" in stale_panel
    assert "面试答辩 →" not in stale_panel
    assert "关联已失效" in stale_panel

    invalid = _learning_page(
        data_root,
        REPOSITORY_ROOT,
        {"run_id": "not-a-learning-run"},
    )
    assert "Interactive evidence graph" not in invalid

    no_project = _learning_page(data_root, None, {"run_id": selected.run_id})
    assert "这个练习需要一个已连接的代码项目" in no_project
    assert 'href="/work?lang=zh-CN"' in no_project
    assert 'data-learning-capability="explain" data-state="AVAILABLE"' in no_project
    assert (
        'data-learning-capability="trace" data-state="NEEDS_PROJECT_EVIDENCE"'
        in no_project
    )
    assert (
        'data-learning-capability="debug" data-state="NEEDS_PROJECT_EVIDENCE"'
        in no_project
    )
    assert 'id="learning-explain-response"' in no_project
    assert 'id="learning-trace-response"' not in no_project
    assert "This is not a supported SoloScale checkout" not in no_project


def test_learning_wrong_project_degrades_only_the_seed_case(tmp_path: Path) -> None:
    repository = tmp_path / "another-project"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    (repository / "README.md").write_text("another project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
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
            "initial evidence",
        ],
        cwd=repository,
        check=True,
    )

    page = _learning_page(tmp_path / "data", repository, {})

    assert "当前项目已连接，但不适用于这个示例练习" in page
    assert "这个项目没有找到此练习需要的代码锚点" in page
    assert "This is not a supported SoloScale checkout" not in page
    assert "pyproject.toml" not in page
    assert "knowledge_store.py" not in page


def test_learning_run_preflights_stale_project_evidence_before_build(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"

    result = _run_learning_workspace(
        {"target_requirement": "Build retrieval systems for AI context and memory."},
        data_root,
        REPOSITORY_ROOT,
    )

    assert result.return_code == 0
    snapshot = EvidenceHub(data_root).git_repository_snapshot(REPOSITORY_ROOT)
    assert snapshot is not None


def test_parse_submission_reads_text_and_docx_upload() -> None:
    boundary = "SoloScaleBoundary"
    template = _uploaded_resume_docx()
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="job_description"\r\n\r\n'
            "Required: Python RAG\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="resume_template"; filename="resume.docx"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + template
        + f"\r\n--{boundary}--\r\n".encode()
    )

    submission = _parse_submission(body, f"multipart/form-data; boundary={boundary}")

    assert submission.fields["job_description"] == "Required: Python RAG"
    assert submission.files["resume_template"].filename == "resume.docx"
    assert submission.files["resume_template"].content == template


def test_job_evidence_search_uses_one_bounded_high_signal_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    accepted_queries: list[str] = []

    class FakeStore:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            assert limit == 8
            assert len(query.split()) <= 8
            accepted_queries.append(query)
            return [
                RetrievalHit(
                    chunk_id=f"chunk-{len(accepted_queries)}",
                    document_id="doc",
                    source_kind=SourceKind.CODEX_SESSION,
                    external_id="thread",
                    locator="private",
                    title="local evidence",
                    role=ContentRole.ASSISTANT,
                    timestamp=None,
                    excerpt=query,
                    chunk_sha256="a" * 64,
                    document_sha256="b" * 64,
                    score=1,
                    channels=["fts"],
                )
            ]

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", FakeStore)
    terms = [f"requirement{index}" for index in range(40)]
    job_description = (
        " ".join(terms) + "\n" + "\n".join(f"line{index} python" for index in range(30))
    )

    hits = _search_job_evidence(job_description, tmp_path)

    expected_query = " ".join(
        ["python", *(f"requirement{index}" for index in range(7))]
    )
    assert accepted_queries == [expected_query]
    assert len(hits) == 1


def test_create_resume_pdf_preview_uses_isolated_local_renderer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "resume.docx"
    target = tmp_path / "preview.pdf"
    source.write_bytes(_uploaded_resume_docx())

    def fake_run(
        command: list[str], *, capture_output: bool, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        assert capture_output is True
        assert check is False
        assert timeout == 30
        assert any(item.startswith("-env:UserInstallation=file:") for item in command)
        output_dir = Path(command[command.index("--outdir") + 1])
        (output_dir / "resume.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("soloscale.local_ui._find_soffice", lambda: "/local/soffice")
    monkeypatch.setattr("soloscale.local_ui.subprocess.run", fake_run)

    assert _create_resume_pdf_preview(source, target) is True
    assert target.read_bytes().startswith(b"%PDF-")
    assert target.stat().st_mode & 0o777 == 0o600


def test_desktop_resume_download_returns_exactly_one_docx_body(tmp_path: Path) -> None:
    run_id = "resume-20260828T010101Z-aaaaaaaaaa"
    run_dir = tmp_path / "resume-runs" / run_id
    run_dir.mkdir(parents=True)
    content = _uploaded_resume_docx()
    (run_dir / "08_resume.docx").write_bytes(content)
    _write_private_json(
        run_dir / "09_user_ui.json",
        {"output_filename": "Resume_LANG-JU_BeaconFire.docx"},
    )

    class FakeHandler:
        def __init__(self) -> None:
            self.status = 0
            self.headers: dict[str, str] = {}
            self.wfile = io.BytesIO()

        def send_response(self, status: int) -> None:
            self.status = status

        def send_header(self, key: str, value: str) -> None:
            self.headers[key] = value

        def end_headers(self) -> None:
            return

        def send_error(self, status: int, message: str) -> None:
            raise AssertionError((status, message))

    handler = FakeHandler()
    _serve_resume_download(  # type: ignore[arg-type]
        handler,
        tmp_path,
        run_id,
        desktop_mode=True,
    )

    assert handler.status == 200
    assert handler.headers["Content-Type"].endswith("wordprocessingml.document")
    assert handler.headers["Content-Disposition"].startswith("attachment;")
    assert handler.wfile.getvalue() == content
    assert b"<html" not in handler.wfile.getvalue()


def test_codex_import_http_returns_immediately_and_exposes_polling_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    session = home / ".codex" / "sessions" / "2026" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-28T08:00:00Z",
                "type": "session_meta",
                "payload": {"id": "http-background-session"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-08-28T08:01:00Z",
                "type": "response_item",
                "payload": {
                    "id": "http-message",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "private work"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    def slow_parse(path: Path) -> object:
        time.sleep(0.25)
        return parse_codex_session(path)

    monkeypatch.setattr("soloscale.work_ui.parse_codex_session", slow_parse)
    manager = CodexImportJobManager(parse_workers=1)
    SoloScaleLocalUIHandler.ui_data_root = tmp_path / "data"
    SoloScaleLocalUIHandler.desktop_session_token = None
    SoloScaleLocalUIHandler.desktop_expected_host = None
    SoloScaleLocalUIHandler.codex_import_job_manager = manager
    server = HTTPServer(("127.0.0.1", 0), SoloScaleLocalUIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        body = urllib.parse.urlencode({"approve": "yes", "ui_locale": "en"})
        started = time.monotonic()
        connection.request(
            "POST",
            "/work/import-codex",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        elapsed = time.monotonic() - started
        assert response.status == 303
        assert elapsed < 0.5
        location = response.getheader("Location") or ""
        job_id = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)[
            "codex_job"
        ][0]

        connection.request("GET", "/health")
        health = connection.getresponse()
        assert health.status == 200
        health.read()

        deadline = time.monotonic() + 5
        payload: dict[str, object] = {}
        while time.monotonic() < deadline:
            connection.request("GET", f"/work/import-codex/jobs/{job_id}")
            status = connection.getresponse()
            payload = json.loads(status.read())
            assert status.status == 200
            if payload["phase"] in {"READY", "FAILED"}:
                break
            time.sleep(0.02)
        assert payload["phase"] == "READY"
        assert payload["processed"] == 1
        assert payload["imported"] == 1
        assert "private work" not in json.dumps(payload)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        manager.shutdown()
        SoloScaleLocalUIHandler.codex_import_job_manager = None


def test_legacy_publishing_page_redirects_to_creator_publish_queue(
    tmp_path: Path,
) -> None:
    previous_data_root = SoloScaleLocalUIHandler.ui_data_root
    previous_token = SoloScaleLocalUIHandler.desktop_session_token
    previous_host = SoloScaleLocalUIHandler.desktop_expected_host
    SoloScaleLocalUIHandler.ui_data_root = tmp_path / "data"
    SoloScaleLocalUIHandler.desktop_session_token = None
    SoloScaleLocalUIHandler.desktop_expected_host = None
    server = HTTPServer(("127.0.0.1", 0), SoloScaleLocalUIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(
            "GET",
            "/publishing?run_id=content-20260828T120000Z-aaaaaaaaaa&lang=en",
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        assert response.getheader("Location") == (
            "/creator/publish?run_id=content-20260828T120000Z-aaaaaaaaaa&lang=en"
        )
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        SoloScaleLocalUIHandler.ui_data_root = previous_data_root
        SoloScaleLocalUIHandler.desktop_session_token = previous_token
        SoloScaleLocalUIHandler.desktop_expected_host = previous_host


def _wait_for_resume_job(
    manager: ResumeJobManager,
    job_id: str,
    phases: set[str],
) -> ResumeJobSnapshot:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = manager.get(job_id)
        if snapshot is not None and snapshot.phase in phases:
            return snapshot
        time.sleep(0.01)
    snapshot = manager.get(job_id)
    raise AssertionError(f"job did not reach {phases}: {snapshot}")


def test_resume_job_manager_runs_one_job_at_a_time_and_exposes_docx_before_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_started = threading.Event()
    allow_generation = threading.Event()
    preview_started = threading.Event()
    allow_preview = threading.Event()
    second_started = threading.Event()
    order: list[str] = []

    def fake_run(
        form: dict[str, str],
        files: dict[str, UploadedFile],
        data_root: Path,
        repo_root: Path,
        **kwargs: object,
    ) -> UIActionResult:
        del files, repo_root
        label = form["label"]
        order.append(label)
        progress = kwargs["progress"]
        timing = kwargs["timing"]
        assert callable(progress)
        assert callable(timing)
        progress("PREPARING")
        timing("profile_extract_ms", 4)
        if label == "first":
            first_started.set()
            assert allow_generation.wait(timeout=2)
        else:
            second_started.set()
        progress("GENERATING")
        timing("model_generation_ms", 8)
        progress("VERIFYING")
        timing("verification_ms", 2)
        timing("retrieval_ms", 0)
        progress("EXPORTING")
        timing("docx_ms", 3)
        run_id = (
            "resume-20260826T010101Z-aaaaaaaaaa"
            if label == "first"
            else "resume-20260826T010102Z-bbbbbbbbbb"
        )
        return UIActionResult(
            "tailored-resume",
            "synthetic",
            0,
            f"Resume workspace: {data_root / 'resume-runs' / run_id}",
            "",
            17,
        )

    def fake_preview(data_root: Path, result: UIActionResult) -> bool:
        del data_root, result
        preview_started.set()
        assert allow_preview.wait(timeout=2)
        return True

    monkeypatch.setattr("soloscale.local_ui._run_user_resume", fake_run)
    monkeypatch.setattr("soloscale.local_ui._finalize_resume_preview", fake_preview)
    manager = ResumeJobManager()
    try:
        first = manager.submit(
            form={"label": "first"},
            files={},
            data_root=tmp_path,
            repo_root=tmp_path,
            gateway=None,
            initial_timings_ms={"post_response_ms": 6},
        )
        assert first_started.wait(timeout=1)
        second = manager.submit(
            form={"label": "second"},
            files={},
            data_root=tmp_path,
            repo_root=tmp_path,
            gateway=None,
            initial_timings_ms={"post_response_ms": 5},
        )
        assert manager.get(second) is not None
        assert manager.get(second).phase == "QUEUED"  # type: ignore[union-attr]
        assert manager.latest() is not None
        assert manager.latest().job_id == second  # type: ignore[union-attr]
        assert not second_started.is_set()

        allow_generation.set()
        assert preview_started.wait(timeout=1)
        previewing = _wait_for_resume_job(manager, first, {"PREVIEWING"})
        assert previewing.result is not None
        assert previewing.result.return_code == 0
        assert previewing.preview_state == "rendering"
        assert not second_started.is_set()

        allow_preview.set()
        completed = _wait_for_resume_job(manager, first, {"COMPLETE"})
        second_completed = _wait_for_resume_job(manager, second, {"COMPLETE"})
        assert order == ["first", "second"]
        assert completed.preview_state == "ready"
        assert completed.stage_durations_ms["post_response_ms"] == 6
        assert "pdf_preview_ms" in completed.stage_durations_ms
        frozen_elapsed = completed.total_elapsed_ms
        time.sleep(0.02)
        assert manager.get(first).total_elapsed_ms == frozen_elapsed  # type: ignore[union-attr]
        assert second_completed.result is not None
    finally:
        allow_generation.set()
        allow_preview.set()
        manager.shutdown()


def test_resume_job_page_shows_live_timing_and_download_before_pdf(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run_dir = data_root / "resume-runs" / "resume-20260826T010103Z-cccccccccc"
    run_dir.mkdir(parents=True)
    (run_dir / "04_resume.md").write_text("Grounded resume preview\n", encoding="utf-8")
    (run_dir / "08_resume.docx").write_bytes(b"synthetic-docx")
    (run_dir / "05_gaps.json").write_text('{"gaps": []}\n', encoding="utf-8")
    (run_dir / "07_verification.json").write_text(
        '{"coverage": {"total": 1}}\n', encoding="utf-8"
    )
    (run_dir / "09_user_ui.json").write_text(
        json.dumps(
            {
                "output_filename": "Resume_Synthetic.docx",
                "download_url": f"/downloads/{run_dir.name}/resume.docx",
                "preview_url": "",
                "preview_generated": False,
                "retention": "request_scoped_sources_not_persisted",
            }
        ),
        encoding="utf-8",
    )
    result = UIActionResult(
        "tailored-resume",
        "synthetic",
        0,
        f"Resume workspace: {run_dir}",
        "",
        1200,
    )
    snapshot = ResumeJobSnapshot(
        job_id="resume-job-aaaaaaaaaaaa",
        phase="PREVIEWING",
        result=result,
        stage_durations_ms={
            "post_response_ms": 9,
            "profile_extract_ms": 11,
            "retrieval_ms": 0,
            "model_generation_ms": 900,
            "verification_ms": 20,
            "docx_ms": 180,
        },
        total_elapsed_ms=1300,
        preview_state="rendering",
        failed_phase=None,
    )

    page = _user_page(result, data_root, {}, resume_job=snapshot)

    assert 'data-phase="PREVIEWING"' in page
    assert '<progress value="95" max="100">' in page
    assert "模型生成" in page
    assert "PDF 预览仍在后台生成" in page
    assert "下载 DOCX 简历" in page
    assert f"/downloads/{run_dir.name}/resume.docx" in page
    assert "window.location.reload()" in page
    assert 'class="resume-pdf-preview"' not in page


def test_failed_resume_job_does_not_kill_worker_or_keep_polling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(
        form: dict[str, str],
        files: dict[str, UploadedFile],
        data_root: Path,
        repo_root: Path,
        **kwargs: object,
    ) -> UIActionResult:
        del files, repo_root
        progress = kwargs["progress"]
        assert callable(progress)
        progress("PREPARING")
        if form["outcome"] == "fail":
            return UIActionResult("tailored-resume", "synthetic", 1, "", "safe failure", 3)
        run_id = "resume-20260826T010104Z-dddddddddd"
        return UIActionResult(
            "tailored-resume",
            "synthetic",
            0,
            f"Resume workspace: {data_root / 'resume-runs' / run_id}",
            "",
            4,
        )

    monkeypatch.setattr("soloscale.local_ui._run_user_resume", fake_run)
    monkeypatch.setattr(
        "soloscale.local_ui._finalize_resume_preview", lambda data_root, result: False
    )
    manager = ResumeJobManager()
    try:
        failed_id = manager.submit(
            form={"outcome": "fail"},
            files={},
            data_root=tmp_path,
            repo_root=tmp_path,
            gateway=None,
        )
        failed = _wait_for_resume_job(manager, failed_id, {"FAILED"})
        next_id = manager.submit(
            form={"outcome": "pass"},
            files={},
            data_root=tmp_path,
            repo_root=tmp_path,
            gateway=None,
        )
        assert _wait_for_resume_job(manager, next_id, {"COMPLETE"}).phase == "COMPLETE"
        page = _user_page(failed.result, tmp_path, {}, resume_job=failed)
        assert 'data-phase="FAILED"' in page
        assert "safe failure" in page
        assert "window.location.reload()" not in page
        assert failed.failed_phase == "PREPARING"
    finally:
        manager.shutdown()


def test_ui_private_artifact_writers_are_atomic_and_reject_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "run.json"
    target.write_text("old", encoding="utf-8")
    original_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated UI receipt durability failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="durability"):
        _write_private_json(target, {"state": "new"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"state": "new"}

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    link = tmp_path / "preview.pdf"
    link.symlink_to(outside)
    with pytest.raises(OSError, match="regular file"):
        _write_private_bytes(link, b"%PDF-new")
    assert outside.read_bytes() == b"outside"


def test_user_resume_rejects_symlinked_data_root_before_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data_root = tmp_path / ".soloscale"
    data_root.symlink_to(outside, target_is_directory=True)

    class ForbiddenStore:
        def __init__(self, root: Path) -> None:
            raise AssertionError(f"KnowledgeStore must not open symlinked root: {root}")

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", ForbiddenStore)
    result = _run_user_resume(
        {
            "job_description": "Required: Python",
            "generation_mode": "template",
            "approve_candidate_claims": "yes",
            "resume_library_root": str(tmp_path / "Resume Applications"),
        },
        {
            "resume_template": UploadedFile(
                filename="synthetic.docx",
                content_type="application/octet-stream",
                content=_uploaded_resume_docx(),
            )
        },
        data_root,
        tmp_path / "repo",
    )

    assert result.return_code == 1
    assert "symlink" in result.stderr
    assert list(outside.iterdir()) == []


def test_user_resume_flow_generates_matching_private_and_application_docx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return [
                RetrievalHit(
                    chunk_id="chunk-python-rag",
                    document_id="doc",
                    source_kind=SourceKind.CODEX_SESSION,
                    external_id="thread",
                    locator="private",
                    title="local evidence",
                    role=ContentRole.ASSISTANT,
                    timestamp=None,
                    excerpt="Built and verified Python RAG retrieval.",
                    chunk_sha256="a" * 64,
                    document_sha256="b" * 64,
                    score=1,
                    channels=["fts"],
                )
            ]

    def no_subprocess(command: list[str], cwd: Path) -> UIActionResult:
        raise AssertionError(f"user resume flow must not run subprocess: {command} {cwd}")

    def fake_preview(source: Path, target: Path) -> bool:
        assert source.name == "08_resume.docx"
        target.write_bytes(b"%PDF-1.7\n%%EOF\n")
        target.chmod(0o600)
        return True

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", FakeStore)
    monkeypatch.setattr("soloscale.local_ui._run_command", no_subprocess)
    monkeypatch.setattr("soloscale.local_ui._create_resume_pdf_preview", fake_preview)
    library_root = tmp_path / "Resume Applications"
    result = _run_user_resume(
        {
            "job_description": "AI Engineer\nRequired: Python and RAG",
            "generation_mode": "template",
            "company_name": "Example AI",
            "job_title": "GenAI Engineer",
            "job_id": "1234567",
            "tailoring_instructions": "Prioritize RAG and Python delivery.",
            "approve_candidate_claims": "yes",
            "resume_library_root": str(library_root),
        },
        {
            "resume_template": UploadedFile(
                filename="Lang_Ju_Resume.docx",
                content_type="application/octet-stream",
                content=_uploaded_resume_docx(),
            )
        },
        tmp_path / ".soloscale",
        tmp_path / "repo",
        allow_persistent_storage=True,
        create_preview=False,
    )

    assert result.return_code == 0, result.stderr
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    private_docx = run_dir / "08_resume.docx"
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    assert metadata["preview_generated"] is False
    assert metadata["preview_url"] == ""
    assert not (run_dir / "10_resume_preview.pdf").exists()
    assert _finalize_resume_preview(tmp_path / ".soloscale", result) is True
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    external_docx = Path(metadata["external_docx"])
    assert private_docx.is_file()
    assert external_docx.is_file()
    assert private_docx.read_bytes() == external_docx.read_bytes()
    assert metadata["claims_preserved"] is True
    assert metadata["network_used"] is False
    assert metadata["download_url"].endswith("/resume.docx")
    assert metadata["preview_url"].endswith("/resume.pdf")
    assert (run_dir / "10_resume_preview.pdf").is_file()
    application_receipt = json.loads((run_dir / "application_receipt.json").read_text())
    assert application_receipt["status"] == "PRIVATE_APPLICATION_DRAFT_SAVED"
    assert application_receipt["operator_approved_profile_claims"]
    assert application_receipt["all_exported_claims_supported"] is True
    assert application_receipt["job_application_submitted"] is False
    provenance = json.loads((run_dir / "12_resume_provenance.json").read_text())
    assert provenance["all_exported_claims_supported"] is True
    assert {claim["status"] for claim in provenance["claims"]} == {"VERIFIED"}
    assert all(
        claim["evidence_ids"] == [claim["profile_entry_id"]]
        for claim in provenance["claims"]
    )
    run_payload = json.loads((run_dir / "run.json").read_text())
    assert {
        "08_resume.docx",
        "09_user_ui.json",
        "10_resume_preview.pdf",
        "12_resume_provenance.json",
        "application_receipt.json",
    } <= set(run_payload["artifact_paths"])
    application_metadata = json.loads((external_docx.parent / "application.json").read_text())
    assert application_metadata["resume_docx_filename"] == external_docx.name
    rendered = _user_page(result, tmp_path / ".soloscale", {})
    assert "针对性简历已生成" in rendered
    assert metadata["download_url"] in rendered
    assert metadata["preview_url"] in rendered
    assert 'class="resume-pdf-preview"' in rendered
    assert "在新窗口打开" in rendered
    assert "安全离线模式" in rendered
    assert "为什么这些内容会出现在我的简历里" in rendered
    assert "原文已核对" in rendered


def test_resume_ui_generation_is_jd_conditioned_and_keeps_unrelated_gaps_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class EmptyStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return []

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", EmptyStore)
    monkeypatch.setattr(
        "soloscale.local_ui._create_resume_pdf_preview", lambda source, target: False
    )
    gateway = RecordingResumeGateway()
    template = _role_resume_docx()

    def run(label: str, job_description: str) -> tuple[UIActionResult, Path, list[str]]:
        result = _run_user_resume(
            {
                "job_description": job_description,
                "generation_mode": "ollama",
                "provider_model": "test-model",
                "company_name": label,
                "job_title": label,
                "approve_resume_processing": "yes",
                "resume_library_root": str(tmp_path / f"applications-{label}"),
            },
            {
                "resume_template": UploadedFile(
                    filename="Lang_Ju_Resume.docx",
                    content_type="application/octet-stream",
                    content=template,
                )
            },
            tmp_path / f"data-{label}",
            tmp_path / "repo",
            gateway=gateway,
        )
        assert result.return_code == 0, result.stderr
        run_dir = _workspace_path(result.stdout)
        assert run_dir is not None
        paragraphs = [
            item.text
            for item in read_template_paragraphs((run_dir / "08_resume.docx").read_bytes())
            if item.text
        ]
        return result, run_dir, paragraphs

    fde_result, fde_run, fde = run(
        "FDE",
        "Forward Deployed Engineer\ncustomer-facing requirements translation "
        "stakeholder reliable agents",
    )
    _, eir_run, eir = run(
        "EIR",
        "Engineer in Residence\nrapid prototyping full-stack GenAI AI-assisted "
        "coding agents RAG evals architecture",
    )
    unrelated_result, unrelated_run, unrelated = run(
        "Unrelated",
        "Required: FPGA compiler design and semiconductor verification.",
    )

    assert fde != eir
    assert fde.index("Client Delivery") < fde.index("SoloScale")
    assert eir.index("SoloScale") < eir.index("Client Delivery")
    assert any("customer-facing stakeholder delivery" in item for item in fde)
    assert any("SoloScale full-stack GenAI" in item for item in eir)
    assert all("FPGA" not in item and "semiconductor" not in item for item in unrelated)
    unrelated_gaps = json.loads((unrelated_run / "05_gaps.json").read_text())
    assert unrelated_gaps["gaps"]
    unrelated_metadata = json.loads((unrelated_run / "09_user_ui.json").read_text())
    assert unrelated_metadata["unsupported_requirement_count"] == 1
    assert {item["status"] for item in unrelated_metadata["evidence_requirements"]} == {
        "GAP"
    }
    rendered_unrelated = _user_page(
        unrelated_result, tmp_path / "data-Unrelated", {}
    )
    assert "有 1 项要求暂未找到受支持证据" in rendered_unrelated
    assert "FPGA compiler design" not in rendered_unrelated
    assert len(gateway.requests) == 3
    assert "Forward Deployed Engineer" in gateway.requests[0]
    assert "Engineer in Residence" in gateway.requests[1]
    paragraphs_by_run = {
        fde_run: fde,
        eir_run: eir,
        unrelated_run: unrelated,
    }
    for run_dir in (fde_run, eir_run, unrelated_run):
        metadata = json.loads((run_dir / "09_user_ui.json").read_text())
        assert metadata["composition_status"] == "CONTENT_READY"
        assert metadata["truth_status"] == "VERIFIED"
        assert metadata["docx_status"] == "DOCX_READY"
        assert metadata["pdf_status"] == "NEEDS_ATTENTION"
        assert metadata["pdf_failure_reason"] == "LOCAL_PDF_RENDER_UNAVAILABLE_OR_FAILED"
        assert metadata["model_call_performed"] is True
        assert metadata["evidence_requirements"]
        assert len(metadata["evidence_source_summary"]) == 9
        assert metadata["generation_mode"] == "ai"
        assert metadata["provider"] == "ollama"
        assert metadata["model_call_profile"]["model_call_count"] == 1
        assert (
            metadata["model_call_profile"]["output_contract"]
            == "evidence_backed_resume_composition_v0.1"
        )
        assert not (run_dir / "11_role_strategy.json").exists()
        provenance = json.loads((run_dir / "12_resume_provenance.json").read_text())
        assert provenance["contains_source_bodies"] is False
        assert provenance["all_exported_claims_supported"] is True
        statuses = {claim["status"] for claim in provenance["claims"]}
        if run_dir == unrelated_run:
            assert statuses == {"VERIFIED"}
            assert metadata["synthesized_rewrites"] == 0
            assert metadata["summary_rewritten"] is False
            assert all(
                not claim["fact_ids"] and not claim["source_fact_sha256s"]
                for claim in provenance["claims"]
            )
        else:
            assert statuses == {"SUPPORTED", "VERIFIED"}
            assert metadata["synthesized_rewrites"] == 1
            assert metadata["summary_rewritten"] is True
            assert sum(
                claim["status"] == "SUPPORTED" for claim in provenance["claims"]
            ) == 2
            assert all(
                bool(claim["source_fact_sha256s"])
                == (claim["status"] == "SUPPORTED")
                for claim in provenance["claims"]
            )
            assert all(
                len(claim["fact_ids"]) == len(claim["source_fact_sha256s"])
                for claim in provenance["claims"]
            )
            synthesis_claims = [
                claim
                for claim in provenance["claims"]
                if claim["verification_basis"]
                == "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS"
            ]
            assert len(synthesis_claims) == 2
            assert {claim["render_location"] for claim in synthesis_claims} == {
                "SUMMARY",
                "BULLET",
            }
            assert all(
                len(claim["evidence_ids"])
                == len(claim["approved_evidence_sha256s"])
                == 2
                and len(claim["fact_ids"])
                == len(claim["source_fact_sha256s"])
                for claim in synthesis_claims
            )
        assert all(
            claim["final_text"] in paragraphs_by_run[run_dir]
            for claim in provenance["claims"]
        )
    rendered_fde = _user_page(fde_result, tmp_path / "data-FDE", {})
    assert "简历已经生成并可下载 DOCX" in rendered_fde
    assert "PDF 预览未完成" in rendered_fde
    assert "查看本次证据来源" in rendered_fde
    assert "查看 JD 要求覆盖" in rendered_fde


def test_resume_ui_generates_bilingual_variants_from_one_evidence_pack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class EmptyStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return []

    def create_preview(_source: Path, target: Path) -> bool:
        _write_private_bytes(target, b"%PDF-1.4\nsynthetic bilingual preview\n")
        return True

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", EmptyStore)
    monkeypatch.setattr(
        "soloscale.local_ui._create_resume_pdf_preview", create_preview
    )
    gateway = RecordingResumeGateway()
    data_root = tmp_path / "data-bilingual"
    result = _run_user_resume(
        {
            "job_description": (
                "Engineer in Residence\nrapid prototyping full-stack GenAI "
                "AI-assisted coding agents RAG evals architecture"
            ),
            "generation_mode": "ollama",
            "provider_model": "test-model",
            "company_name": "BeaconFire",
            "job_title": "AI Engineer",
            "approve_resume_processing": "yes",
            "resume_output_language": "both",
        },
        {
            "resume_template": UploadedFile(
                filename="Lang_Ju_Resume.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        data_root,
        tmp_path / "repo",
        gateway=gateway,
    )

    assert result.return_code == 0, result.stderr
    run_dirs = _workspace_paths(result.stdout)
    assert len(run_dirs) == 2
    metadata = [
        json.loads((run_dir / "09_user_ui.json").read_text())
        for run_dir in run_dirs
    ]
    assert {item["output_locale"] for item in metadata} == {"en-US", "zh-CN"}
    assert {item["source_resume_locale"] for item in metadata} == {"en-US"}
    assert {item["target_locale"] for item in metadata} == {"en-US", "zh-CN"}
    assert {item["docx_status"] for item in metadata} == {"DOCX_READY"}
    assert {item["pdf_status"] for item in metadata} == {"PDF_READY"}
    assert len({item["resume_project_id"] for item in metadata}) == 1
    assert len({item["candidate_evidence_pack_sha256"] for item in metadata}) == 1
    assert all((run_dir / "08_resume.docx").is_file() for run_dir in run_dirs)
    assert all((run_dir / "10_resume_preview.pdf").is_file() for run_dir in run_dirs)
    assert {Path(item["output_filename"]).stem.rsplit("_", 1)[-1] for item in metadata} == {
        "en-US",
        "zh-CN",
    }
    provenance = [
        json.loads((run_dir / "12_resume_provenance.json").read_text())
        for run_dir in run_dirs
    ]
    assert all(receipt["all_exported_claims_supported"] is True for receipt in provenance)
    request_payloads = [json.loads(request) for request in gateway.requests]
    assert request_payloads[0]["candidate_profile"]["atomic_facts"] == (
        request_payloads[1]["candidate_profile"]["atomic_facts"]
    )
    request_locales = [payload["output_locale"] for payload in request_payloads]
    assert request_locales == ["en-US", "zh-CN"]
    rendered = _user_page(result, data_root, {})
    assert "en-US" in rendered
    assert "zh-CN" in rendered
    chinese_dir = next(
        run_dir
        for run_dir, item in zip(run_dirs, metadata, strict=True)
        if item["output_locale"] == "zh-CN"
    )
    chinese_paragraphs = [
        item.text
        for item in read_template_paragraphs(
            (chinese_dir / "08_resume.docx").read_bytes()
        )
        if item.text
    ]
    assert "个人简介" in chinese_paragraphs
    assert "项目经历" in chinese_paragraphs
    assert any("快速构建 SoloScale" in item for item in chinese_paragraphs)


def test_resume_ui_uses_confirmed_template_only_for_section_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "soloscale.local_ui._create_resume_pdf_preview", lambda _source, _target: False
    )
    receipt = inspect_template_html(
        """
        <h2>Skills</h2><p>Senior Java Engineer</p>
        <h2>Summary</h2><p>Led a $10M customer platform.</p>
        <h2>Projects</h2><h2>Experience</h2><h2>Education</h2>
        """
    )
    data_root = tmp_path / "data-template"
    result = _run_user_resume(
        {
            "job_description": "Required: Python, RAG, and agents.",
            "generation_mode": "template",
            "approve_resume_processing": "yes",
            "resume_output_language": "en-US",
            "resume_template_preview_id": receipt.preview_id,
            "approve_layout_template": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="Lang_Ju_Resume.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        data_root,
        tmp_path / "repo",
        template_receipt=receipt,
    )

    assert result.return_code == 0, result.stderr
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    paragraphs = [
        item.text
        for item in read_template_paragraphs((run_dir / "08_resume.docx").read_bytes())
        if item.text
    ]
    assert paragraphs.index("TECHNICAL SKILLS") < paragraphs.index("SUMMARY")
    assert "Senior Java Engineer" not in paragraphs
    assert "Led a $10M customer platform." not in paragraphs
    run_payload = json.loads((run_dir / "run.json").read_text())
    template_receipt = run_payload["template_receipt"]
    assert template_receipt["source_sha256"] == receipt.source_sha256
    assert template_receipt["candidate_facts_imported"] is False
    assert "Senior Java Engineer" not in json.dumps(template_receipt)


def test_resume_ui_rejects_only_unsafe_rewrite_and_keeps_original_bullet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class EmptyStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return []

    class PartiallyUnsafeGateway(RecordingResumeGateway):
        def complete(
            self,
            schema: type[ResponseModelT],
            *,
            system: str,
            user: str,
            reasoning_effort: Literal["none", "low"] = "low",
        ) -> ResponseModelT:
            response = super().complete(
                schema,
                system=system,
                user=user,
                reasoning_effort=reasoning_effort,
            )
            payload = response.model_dump(mode="json")
            rewrites = payload["bullet_rewrites"]
            assert isinstance(rewrites, dict)
            unsafe = dict(rewrites)
            unsafe["PROFILE-03"] = {
                "kind": "REWRITE",
                "text": "Led an unsupported FPGA compiler program by 40%.",
                "source_fact_ids": rewrites["PROFILE-03"]["source_fact_ids"],
            }
            payload["bullet_rewrites"] = unsafe
            return schema.model_validate(payload)

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", EmptyStore)
    monkeypatch.setattr(
        "soloscale.local_ui._create_resume_pdf_preview", lambda source, target: False
    )
    result = _run_user_resume(
        {
            "job_description": (
                "Forward Deployed Engineer\ncustomer-facing requirements translation "
                "stakeholder reliable agents"
            ),
            "generation_mode": "ollama",
            "provider_model": "test-model",
            "approve_resume_processing": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="Synthetic.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        tmp_path / "data",
        tmp_path / "repo",
        gateway=PartiallyUnsafeGateway(),
    )

    assert result.return_code == 0, result.stderr
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    assert metadata["grounded_rewrites"] == 2
    assert metadata["rejected_rewrites"] == 1
    adoption = metadata["evidence_adoption"]
    assert any(item["proposed"] for item in adoption)
    assert any(
        "CLAIM_NEW_NUMBER" in item["rejection_rule_codes"] for item in adoption
    )
    assert any(item["accepted"] and item["rendered"] for item in adoption)
    paragraphs = {
        item.text
        for item in read_template_paragraphs((run_dir / "08_resume.docx").read_bytes())
    }
    assert (
        "Rapidly prototyped SoloScale full-stack GenAI workflows with RAG and agents."
        in paragraphs
    )
    assert all("FPGA compiler" not in item and "40%" not in item for item in paragraphs)
    candidate_paths = list((tmp_path / "data" / "resume-candidates").glob("*.json"))
    assert len(candidate_paths) == 1
    candidate = json.loads(candidate_paths[0].read_text())
    assert candidate["status"] == "REJECTED"
    assert candidate["submission_status"] == "NOT_FOR_SUBMISSION"
    assert candidate["label"] == "REJECTED / NOT FOR SUBMISSION"
    raw_rewrites = candidate["structured_candidate"]["bullet_rewrites"]
    assert any("FPGA compiler" in rewrite["text"] for rewrite in raw_rewrites)
    rendered = _user_page(result, tmp_path / "data", {})
    assert "1 项未通过事实校验，已逐项回退" in rendered
    assert "Summary已重写" in rendered


def test_resume_ui_uses_safe_strategy_after_global_truth_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class GloballyRejectedGateway(RecordingResumeGateway):
        def complete(
            self,
            schema: type[ResponseModelT],
            *,
            system: str,
            user: str,
            reasoning_effort: Literal["none", "low"] = "low",
        ) -> ResponseModelT:
            response = super().complete(
                schema,
                system=system,
                user=user,
                reasoning_effort=reasoning_effort,
            )
            payload = response.model_dump(mode="json")
            payload["unsupported_requirements"] = [
                "Private requirement absent from the supplied JD."
            ]
            return schema.model_validate(payload)

    candidate_statuses: list[str] = []

    def tracking_write(path: Path, payload: object) -> None:
        if path.parent.name == "resume-candidates":
            assert isinstance(payload, dict)
            candidate_statuses.append(str(payload["status"]))
        _write_private_json(path, payload)

    monkeypatch.setattr("soloscale.local_ui._write_private_json", tracking_write)
    data_root = tmp_path / "data"
    result = _run_user_resume(
        {
            "job_description": "Required: Python and RAG.",
            "generation_mode": "ollama",
            "provider_model": "test-model",
            "approve_resume_processing": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="Synthetic.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        data_root,
        tmp_path / "repo",
        gateway=GloballyRejectedGateway(),
    )

    assert result.return_code == 0, result.stderr
    assert candidate_statuses == ["PENDING_VALIDATION", "REJECTED"]
    candidate_paths = list((data_root / "resume-candidates").glob("*.json"))
    assert len(candidate_paths) == 1
    candidate_path = candidate_paths[0]
    candidate = json.loads(candidate_path.read_text())
    assert candidate["status"] == "REJECTED"
    assert candidate["submission_status"] == "NOT_FOR_SUBMISSION"
    assert candidate["label"] == "REJECTED / NOT FOR SUBMISSION"
    assert candidate["validation_diagnostics"]["validator_status"] == "fallback_pass"
    assert {
        failure["rule_code"]
        for failure in candidate["validation_diagnostics"]["failures"]
    } == {"GAP_NOT_SOURCE_GROUNDED"}
    assert candidate_path.stat().st_mode & 0o777 == 0o600
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    assert metadata["role_strategy_fallback_applied"] is True
    assert metadata["role_strategy_fallback_code"] == "ROLE_STRATEGY_TRUTH_REJECTED"
    assert (run_dir / "08_resume.docx").is_file()


def test_resume_optional_expert_review_returns_patches_and_reverifies_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "soloscale.local_ui._create_resume_pdf_preview", lambda source, target: False
    )
    generation_gateway = RecordingResumeGateway()
    expert_gateway = RecordingExpertReviewGateway()
    result = _run_user_resume(
        {
            "job_description": (
                "Engineer in Residence\nrapid prototyping full-stack GenAI "
                "AI-assisted coding agents RAG evals architecture"
            ),
            "generation_mode": "ollama",
            "provider_model": "test-model",
            "expert_review_mode": "openai_sol",
            "approve_expert_review": "yes",
            "approve_resume_processing": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="Synthetic.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        tmp_path / "data",
        tmp_path / "repo",
        gateway=generation_gateway,
        expert_gateway=expert_gateway,
    )

    assert result.return_code == 0, result.stderr
    assert len(generation_gateway.requests) == 1
    assert len(expert_gateway.requests) == 1
    outbound = expert_gateway.requests[0]
    assert set(outbound) == {
        "draft_resume_bullets",
        "evidence_bundle",
        "hiring_signals",
    }
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    expert_receipt = json.loads((run_dir / "13_expert_review.json").read_text())
    assert expert_receipt["status"] == "PATCHES_REVERIFIED"
    assert expert_receipt["model"] == "gpt-5.6-sol"
    assert expert_receipt["patch_count"] == 1
    assert expert_receipt["new_factual_claims_accepted"] == 0
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    assert metadata["expert_review_performed"] is True
    assert metadata["expert_rewrites"] == 1
    assert metadata["network_used"] is True
    rendered = _user_page(result, tmp_path / "data", {})
    assert "GPT-5.6 Sol" in rendered
    assert "再次通过事实校验" in rendered


def test_resume_expert_review_new_fact_is_discarded_and_base_resume_is_saved(
    tmp_path: Path,
) -> None:
    result = _run_user_resume(
        {
            "job_description": (
                "Engineer in Residence\nrapid prototyping full-stack GenAI architecture"
            ),
            "generation_mode": "ollama",
            "provider_model": "test-model",
            "expert_review_mode": "openai_sol",
            "approve_expert_review": "yes",
            "approve_resume_processing": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="Synthetic.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        tmp_path / "data",
        tmp_path / "repo",
        gateway=RecordingResumeGateway(),
        expert_gateway=RecordingExpertReviewGateway(propose_new_fact=True),
    )

    assert result.return_code == 0, result.stderr
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    receipt = json.loads((run_dir / "13_expert_review.json").read_text())
    assert receipt["status"] == "SKIPPED_UNSUPPORTED_FACT"
    assert receipt["failure_code"] == "EXPERT_PATCH_UNSUPPORTED_FACT"
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    assert metadata["expert_review_attempted"] is True
    assert metadata["expert_review_performed"] is False
    assert metadata["expert_review_skipped_code"] == "EXPERT_PATCH_UNSUPPORTED_FACT"
    assert (run_dir / "08_resume.docx").is_file()


def test_resume_expert_review_stale_patch_preserves_base_resume(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    result = _run_user_resume(
        {
            "job_description": (
                "Engineer in Residence\nrapid prototyping full-stack GenAI "
                "architecture"
            ),
            "generation_mode": "ollama",
            "provider_model": "test-model",
            "expert_review_mode": "openai_sol",
            "approve_expert_review": "yes",
            "approve_resume_processing": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="Synthetic.docx",
                content_type="application/octet-stream",
                content=_role_resume_docx(),
            )
        },
        data_root,
        tmp_path / "repo",
        gateway=RecordingResumeGateway(),
        expert_gateway=RecordingExpertReviewGateway(stale_patch=True),
    )

    assert result.return_code == 0, result.stderr
    run_dir = _workspace_path(result.stdout)
    assert run_dir is not None
    receipt = json.loads((run_dir / "13_expert_review.json").read_text())
    assert receipt["status"] == "SKIPPED_DRAFT_MISMATCH"
    assert receipt["failure_code"] == "EXPERT_PATCH_SOURCE_MISMATCH"
    candidates = [
        json.loads(path.read_text())
        for path in (data_root / "resume-candidates").glob("*.json")
    ]
    assert any(
        candidate.get("candidate_kind") == "RESUME_EXPERT_REVIEW_PATCH"
        and candidate["status"] == "REJECTED"
        for candidate in candidates
    )
    assert (run_dir / "08_resume.docx").is_file()


def test_resume_hosted_provider_unavailable_never_silently_saves_offline_draft(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    result = _run_user_resume(
        {
            "job_description": "Required: Python and RAG",
            "generation_mode": "soloscale_hosted",
            "approve_resume_processing": "yes",
            "resume_library_root": str(tmp_path / "applications"),
        },
        {
            "resume_template": UploadedFile(
                filename="Lang_Ju_Resume.docx",
                content_type="application/octet-stream",
                content=_uploaded_resume_docx(),
            )
        },
        data_root,
        tmp_path / "repo",
    )

    assert result.return_code == 1
    assert "没有生成通用简历" in result.stderr
    assert not (data_root / "resume-runs").exists()


def test_run_action_knowledge_status_builds_expected_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run_command(command: list[str], cwd: Path) -> UIActionResult:
        calls.append(command)
        return UIActionResult(
            name=command[0],
            command=" ".join(command),
            return_code=0,
            stdout="ok",
            stderr="",
            elapsed_ms=1,
        )

    monkeypatch.setattr("soloscale.local_ui._run_command", fake_run_command)

    result = _run_action({"action": "knowledge-status"}, Path(".soloscale"), tmp_path)
    assert result is not None
    assert result.return_code == 0
    assert calls == [["knowledge-status", "--data-root", ".soloscale"]]


def test_resume_graph_renders_clickable_native_svg(tmp_path: Path) -> None:
    run_dir = tmp_path / "resume-run"
    run_dir.mkdir()
    nodes = [
        {"id": f"N-{index}", "kind": "EVIDENCE", "label": str(index), "detail": {}}
        for index in range(30)
    ]
    (run_dir / "06_graph.json").write_text(
        json.dumps({"nodes": nodes, "edges": []}), encoding="utf-8"
    )
    output = _resume_graph(f"Resume workspace: {run_dir}")
    assert 'id="resume-graph"' in output
    assert "onclick" in output
    assert "ondblclick" in output
    assert "height=Math.max" in output
    assert "overflow:auto" in output


def test_resume_workspace_defaults_to_documents_application_library(tmp_path: Path) -> None:
    expected = Path.home() / "Documents" / "Resume Applications"
    advanced = _page(None, tmp_path / ".soloscale", {})
    assert "resume_library_root" not in advanced
    resume_page = _user_page(None, tmp_path / ".soloscale", {})
    assert f'value="{expected}"' in resume_page


def test_resume_workspace_rejects_unknown_mode_without_crashing(tmp_path: Path) -> None:
    result = _run_action(
        {
            "action": "resume-workspace",
            "job_description": "Required: Python",
            "resume_mode": "unknown",
        },
        tmp_path / ".soloscale",
        tmp_path,
    )

    assert result is not None
    assert result.return_code == 2
    assert "Resume mode 无效" in result.stderr


def test_resume_workspace_is_local_only_and_renders_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return [
                RetrievalHit(
                    chunk_id="chunk-python",
                    document_id="doc",
                    source_kind=SourceKind.CODEX_SESSION,
                    external_id="thread",
                    locator="private",
                    title="local",
                    role=ContentRole.ASSISTANT,
                    timestamp=None,
                    excerpt="Python RAG implementation",
                    chunk_sha256="a" * 64,
                    document_sha256="b" * 64,
                    score=1,
                    channels=["fts"],
                )
            ]

    def no_subprocess(command: list[str], cwd: Path) -> UIActionResult:
        raise AssertionError(f"workspace must not run subprocess: {command} {cwd}")

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", FakeStore)
    monkeypatch.setattr("soloscale.local_ui._run_command", no_subprocess)
    result = _run_action(
        {
            "action": "resume-workspace",
            "job_description": "Required: Python and RAG",
            "candidate_base_resume": "Operator supplied Python work.",
            "candidate_skills": "Python, RAG",
            "company_name": "Faros",
            "job_title": "AI-Native Builder",
            "job_id": "4432211307",
            "resume_library_root": str(tmp_path / "Resume Applications"),
        },
        tmp_path / ".soloscale",
        tmp_path / "repo",
    )
    assert result is not None and result.return_code == 0
    page = _result_card(result)
    assert "One-page resume preview" in page
    assert "Requirements:" in page
    assert "Private artifacts:" in page
    assert "Application library:" in page
    assert "Resume Applications" in page
    assert 'id="resume-graph"' in page
    application_dirs = list((tmp_path / "Resume Applications" / "applications").iterdir())
    assert len(application_dirs) == 1
    assert (application_dirs[0] / "JD.md").is_file()
    assert (application_dirs[0] / "application.json").is_file()


def test_old_jd_resume_action_is_disabled_and_not_rendered(tmp_path: Path) -> None:
    result = _run_action(
        {"action": "jd-resume-draft", "job_description": "Required: Python"},
        tmp_path / ".soloscale",
        tmp_path,
    )
    assert result is not None
    assert result.return_code == 2
    assert "Evidence Agent" in result.stderr
    page = _page(None, tmp_path / ".soloscale", {})
    assert 'name="action" value="jd-resume-draft"' not in page
    assert "简历生成在“找到机会”页面" in page


def test_resume_workspace_rejects_symlinked_application_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeStore:
        def __init__(self, root: Path) -> None:
            del root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return []

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", FakeStore)
    outside = tmp_path / "outside"
    outside.mkdir()
    library = tmp_path / "Resume Applications"
    library.symlink_to(outside, target_is_directory=True)
    result = _run_action(
        {
            "action": "resume-workspace",
            "job_description": "Required: Python",
            "resume_library_root": str(library),
        },
        tmp_path / ".soloscale",
        tmp_path / "repo",
    )
    assert result is not None
    assert result.return_code == 1
    assert result.stderr == "application library save failed; inspect delivery.json"
    assert list(outside.iterdir()) == []


def test_create_learning_case_ui_archives_real_ci_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workflow = repo / ".github/workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    data_root = tmp_path / ".soloscale"
    result = _create_learning_case_ui(
        {"target_requirement": "Automate verification with GitHub Actions"},
        data_root,
        repo,
    )
    assert result is not None
    assert result.return_code == 0
    from soloscale.casebook_store import CasebookStore

    cases = CasebookStore(data_root).list_cases()
    assert [case.id for case in cases] == ["ci-cd-automation"]


def test_applications_section_truthfully_separates_drafts_and_applications(
    tmp_path: Path,
) -> None:
    library = tmp_path / "Resume Applications"
    app_dir = library / "applications" / "app-one"
    app_dir.mkdir(parents=True)
    (app_dir / "application.json").write_text(
        json.dumps(
            {
                "soloscale_run_id": "resume-run-1",
                "company": "Faros",
                "role": "AI-Native Builder",
                "status": "DRAFT",
                "resume_filename": "resume.docx",
            }
        ),
        encoding="utf-8",
    )
    html = _applications_section_html(library, locale="zh-CN")
    assert "申请与机会" in html
    assert "更新状态" in html
    assert "打开简历" in html
    assert "准备面试 / 练习缺口" in html
