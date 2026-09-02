from __future__ import annotations

import re
from pathlib import Path

import pytest

from soloscale.content_ui import content_page, editorial_publishing_page
from soloscale.creator_accounts import creator_accounts_page
from soloscale.learning_models import MasteryAction, MasteryLevel, TruthStage
from soloscale.learning_traceability import run_learning_traceability
from soloscale.local_ui import _learning_page, _user_page
from soloscale.platform_accounts import provider_label
from soloscale.ui_shell import render_source_state, ui_display_value

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def learning_pages(tmp_path: Path) -> tuple[str, str]:
    data_root = tmp_path / ".soloscale"
    run = run_learning_traceability(
        data_root=data_root,
        repository_root=REPOSITORY_ROOT,
        target_requirement="Build RAG pipelines with grounded retrieval",
    )
    form = {
        "run_id": run.run_id,
        "target_requirement": "Build RAG pipelines with grounded retrieval",
    }
    return (
        _learning_page(data_root, REPOSITORY_ROOT, form, locale="zh-CN"),
        _learning_page(data_root, REPOSITORY_ROOT, form, locale="en"),
    )


def test_learning_chrome_is_consistently_localized(
    learning_pages: tuple[str, str],
) -> None:
    chinese, english = learning_pages

    for copy in (
        "工程状态",
        "个人掌握",
        "明确下一步",
        "工程能力已验证",
        "L0 · 已见过",
        "面试就绪： 否",
        "讲解",
        "不会自动升级",
        "30 秒讲解",
        "对话式 RAG · 分块与检索",
        "开始讲解",
        "开始追踪",
    ):
        assert copy in chinese
    for raw_copy in (
        "<strong>ENGINEERING_VERIFIED</strong>",
        "<strong>L0 Seen</strong>",
        "Interview ready: False",
        "<strong>Explain</strong>",
        "Start Explain",
        "No automatic promotion",
    ):
        assert raw_copy not in chinese

    for copy in (
        "Engineering",
        "Human mastery",
        "Exact next action",
        "Engineering verified",
        "L0 · Seen",
        "Interview ready: No",
        "Explain",
        "No automatic promotion",
        "30-second explanation",
        "Conversation RAG · Chunking + Retrieval",
        "Start Explain",
        "Start Trace",
    ):
        assert copy in english


def test_learning_preserves_source_language_and_locale_across_actions(
    learning_pages: tuple[str, str],
) -> None:
    chinese, english = learning_pages
    source_requirement = "Build RAG pipelines with grounded retrieval"

    assert source_requirement in chinese
    assert source_requirement in english
    assert chinese.count('name="ui_locale" value="zh-CN"') >= 3
    assert english.count('name="ui_locale" value="en"') >= 3
    assert "'&lang='+encodeURIComponent(locale)" in chinese
    assert 'href="/creator?lang=zh-CN"' in chinese
    assert 'href="/creator?lang=en"' in english
    assert re.search(r'class="locale-switch" href="[^"]*lang=en', chinese)
    assert re.search(r'class="locale-switch" href="[^"]*lang=zh-CN', english)


def test_display_translation_does_not_mutate_internal_contract_values() -> None:
    assert TruthStage.VERIFIED_EVIDENCE.value == "VERIFIED_EVIDENCE"
    assert MasteryLevel.L0_SEEN.value == "L0 Seen"
    assert MasteryAction.EXPLAIN.value == "Explain"
    assert ui_display_value("zh-CN", "ENGINEERING_VERIFIED") == "工程能力已验证"
    assert ui_display_value("zh-CN", TruthStage.VERIFIED_EVIDENCE) == "已验证证据"
    assert ui_display_value("zh-CN", MasteryLevel.L0_SEEN) == "L0 · 已见过"
    assert ui_display_value("en", MasteryAction.EXPLAIN) == "Explain"

    state = render_source_state("NOT_CONNECTED", "zh-CN")
    assert 'data-source-state="NOT_CONNECTED"' in state
    assert 'title="未连接"' in state
    assert 'title="NOT_CONNECTED"' not in state


def test_creator_accounts_publish_queue_and_story_bank_share_locale(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    accounts_zh = creator_accounts_page(data_root, locale="zh-CN")
    accounts_en = creator_accounts_page(data_root, locale="en")
    publish_zh = editorial_publishing_page(
        data_root=data_root, locale="zh-CN", creator_mode=True
    )
    publish_en = editorial_publishing_page(
        data_root=data_root, locale="en", creator_mode=True
    )
    stories_zh = content_page(
        data_root=data_root, locale="zh-CN", workspace_view="stories"
    )

    assert "平台认证中心" in accounts_zh
    assert "客户端 ID" in accounts_zh
    assert 'href="/creator/publish?lang=zh-CN"' in accounts_zh
    assert "Platform Authentication Hub" in accounts_en
    assert "Client ID" in accounts_en
    assert 'href="/creator/publish?lang=en"' in accounts_en
    assert "已批准、可继续处理的发布包" in publish_zh
    assert 'aria-current="page">发布队列</a>' in publish_zh
    assert "Approved packages ready for the next action" in publish_en
    assert 'aria-current="page">Publish Queue</a>' in publish_en
    assert "第一个月 · 工程故事库" in stories_zh
    assert "第 1 周" in stories_zh


def test_platform_labels_follow_product_locale_policy() -> None:
    assert provider_label("douyin", "zh-CN") == "抖音"
    assert provider_label("xiaohongshu", "zh-CN") == "小红书"
    assert provider_label("douyin", "en") == "Douyin"
    assert provider_label("xiaohongshu", "en") == "rednote"
    for platform in ("youtube", "x", "linkedin", "github"):
        assert provider_label(platform, "zh-CN") == provider_label(platform, "en")


def test_chinese_resume_ui_preserves_english_job_description(tmp_path: Path) -> None:
    job_description = "Design RAG pipelines with reranking and grounded evaluation."
    page = _user_page(
        None,
        tmp_path / ".soloscale",
        {"job_description": job_description},
        locale="zh-CN",
    )

    assert "职位描述（JD）" in page
    assert "在此粘贴完整职位描述…" in page
    assert job_description in page
