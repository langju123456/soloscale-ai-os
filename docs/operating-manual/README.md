# SoloScale Execution Manual v1

**版本日期：2026-08-06**

这套文档把长对话收敛成两个相互配合、但不混在一起的执行主线：

1. **SoloScale AI OS Core**  
   负责个人 AI 工作流、任务路由、ChatGPT ↔ Codex 交接、证据、审批、验证和未来多 Agent / 云端编排。

2. **Creator Revenue & Video Factory**  
   负责把真实工程工作转成 X、LinkedIn、YouTube、Instagram、Facebook 等平台的内容、视频、流量、线索和收入。

二者共享同一个证据层：

```text
真实任务
→ 决策
→ 本地实现
→ 测试与审查
→ GitHub 证据
→ BuildLog
→ 内容与视频
→ 流量 / 线索 / 收入
→ 新的产品 Backlog
```

## 当前基线

根据当前对话中提供的 clean-room 报告，SoloScale 当前状态为：

- Private GitHub readiness：PASS
- Public GitHub readiness：FAIL
- First dogfood readiness：PASS（CLI 路径）
- 28 tests、Ruff、mypy、Bundle 恢复、wheel clean install 与制品检查已通过
- 先前代码审查已诚实降级为 `same-session self-review`

这些状态是**输入事实**，本手册没有重新连接你的 Mac 本地目录进行二次验证。

## 黄金分工

```text
ChatGPT Chat
= 默认大脑：研究、产品、商业、架构、计划、在线插件动作、最终审查

Codex
= 本地工程执行器：读取本地 Repo、修改代码、运行命令、测试、构建、打包、生成 Diff

Plugins / Apps
= 云端执行器：GitHub、Figma、Vercel、视频 SaaS、邮件等在线动作

Deterministic Tools
= 事实验证器：pytest、lint、typecheck、build、CI、FFmpeg、Remotion render

Human
= 最终目标、预算、账号授权、生产环境、公开发布和不可逆动作的审批者

API Runtime
= 只有实时、重复、事件驱动或无人值守的软件运行才使用
```

## 从哪里开始

按顺序阅读：

1. [`docs/00_QUICK_START.md`](docs/00_QUICK_START.md)
2. [`docs/01_MASTER_SYSTEM_MAP.md`](docs/01_MASTER_SYSTEM_MAP.md)
3. [`docs/02_DIVISION_OF_LABOR.md`](docs/02_DIVISION_OF_LABOR.md)
4. [`docs/03_SOLOSCALE_CORE_ROADMAP.md`](docs/03_SOLOSCALE_CORE_ROADMAP.md)
5. [`docs/04_CREATOR_REVENUE_VIDEO_ROADMAP.md`](docs/04_CREATOR_REVENUE_VIDEO_ROADMAP.md)
6. [`docs/05_ACTION_ROUTER_AND_REGISTER.md`](docs/05_ACTION_ROUTER_AND_REGISTER.md)
7. `prompts/` 下的可复制 Prompt
8. `templates/` 下的长期交接协议

## 目录

```text
soloscale-execution-manual-v1/
├── README.md
├── docs/
│   ├── 00_QUICK_START.md
│   ├── 01_MASTER_SYSTEM_MAP.md
│   ├── 02_DIVISION_OF_LABOR.md
│   ├── 03_SOLOSCALE_CORE_ROADMAP.md
│   ├── 04_CREATOR_REVENUE_VIDEO_ROADMAP.md
│   ├── 05_ACTION_ROUTER_AND_REGISTER.md
│   ├── 06_GITHUB_EVIDENCE_SYSTEM.md
│   ├── 07_METRICS_BUDGET_AND_GATES.md
│   ├── 08_30_DAY_EXECUTION_PLAN.md
│   ├── 09_DAILY_WEEKLY_RUNBOOK.md
│   ├── 10_PUBLIC_CONTENT_SERIES.md
│   ├── 11_SOURCES_AND_TOOL_NOTES.md
│   └── 12_PROMPT_INDEX.md
├── action-register.csv
├── prompts/
│   ├── CHATGPT_ACTION_PROMPTS.md
│   ├── CODEX_ACTION_PROMPTS.md
│   └── PLUGIN_ACTION_PROMPTS.md
└── templates/
    ├── TASK_ENVELOPE.md
    ├── CODEX_EXECUTION_PACKET.md
    ├── INDEPENDENT_REVIEW_PACKET.md
    ├── VIDEO_CONTENT_BRIEF.md
    ├── RUN_EVIDENCE_SUMMARY.md
    └── WEEKLY_REVIEW.md
```

## 强制原则

- **默认从 ChatGPT 开始，不从 Codex 开始。**
- Codex 没有收到已批准的执行包，不进入写代码阶段。
- Codex 不负责市场研究、商业策略、内容选题、在线插件动作或开放式架构辩论。
- 同一问题不让多个 Agent 重复讨论。
- 需要真正独立审查时，使用新的 ChatGPT 对话，只给需求、计划、Diff 和验证证据。
- 任何公开、收费、部署、生产数据、账号权限或不可逆动作必须人工确认。
- 每次真实 Build 都尽量沉淀为 GitHub 证据和可发布内容，但**没有证据就不宣称结果**。
