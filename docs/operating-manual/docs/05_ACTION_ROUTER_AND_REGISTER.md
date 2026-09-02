# Action Router & Master Register

## 1. 路由标签

```text
CHAT      — ChatGPT 推理与交付物
PLUGIN    — ChatGPT 调用在线工具
CODEX     — 本地 Repo / Terminal / Tests
VERIFY    — 确定性工具
REVIEW    — Fresh ChatGPT 独立审查
HUMAN     — 审批与不可逆动作
RUNTIME   — 未来 API 自动运行
```

## 2. Master Action Register

| ID | Action | Owner | 输入 | 输出 | Gate | Prompt |
|---|---|---|---|---|---|---|
| A001 | 每周收敛优先级 | CHAT | Backlog、指标、约束 | 本周 3 个 Outcomes | 无 | CHAT-WEEKLY-001 |
| A002 | 任务分诊 | CHAT | 模糊需求 | Task Envelope + Route | 无 | CHAT-ROUTE-001 |
| A003 | 市场/用户研究 | CHAT | 命题、用户、渠道 | Evidence-backed Research | 无 | CHAT-RESEARCH-001 |
| A004 | 产品与架构计划 | CHAT | Issue、Repo、约束 | Approved Plan 草案 | Human approve | CHAT-PLAN-001 |
| A005 | Codex 执行包 | CHAT | Approved Plan | Bounded Packet | Human approve | CHAT-PACKET-001 |
| A006 | 创建 GitHub Issue | PLUGIN | Task Envelope | Issue URL | 写入确认 | PLUGIN-GH-001 |
| A007 | 创建 Figma 架构图 | PLUGIN | Visual Brief | Editable Board | 写入确认 | PLUGIN-FIGMA-001 |
| A008 | 只读 Repo 检查 | CODEX | Plan + Packet | Compatibility Report | 禁止写 | CODEX-REPO-001 |
| A009 | 本地实现 | CODEX | Approved Packet | Minimal Diff | Stop Conditions | CODEX-IMPLEMENT-001 |
| A010 | 本地验证 | CODEX + VERIFY | Changed Repo | Test Evidence | 无 | CODEX-VERIFY-001 |
| A011 | Branch / Commit | CODEX | Verified Diff | Local Git History | Human approve | CODEX-GIT-001 |
| A012 | Push / PR | PLUGIN 或 CODEX | Approved Branch | PR | Human approve | PLUGIN-GH-002 |
| A013 | Fresh Independent Review | REVIEW | Issue + Plan + Diff + CI | P0/P1/P2 | 无 | CHAT-REVIEW-001 |
| A014 | 修 P0/P1 | CODEX | Review Findings | Minimal Fix | 禁止 P2 扩张 | CODEX-FIX-001 |
| A015 | Evidence Pack | CODEX + VERIFY | Final Repo | Run Artifacts | 无 | CODEX-EVIDENCE-001 |
| A016 | BuildLog Narrative | CHAT / BuildLog | Evidence Pack | Canonical Story | Human fact check | CHAT-NARRATIVE-001 |
| A017 | Creator Benchmark | CHAT | Creator samples | Skill Cards | 合规采样 | CHAT-CREATOR-001 |
| A018 | 短视频脚本批次 | CHAT | Canonical Brief | 10 scripts | Claim check | CHAT-SHORTS-001 |
| A019 | 长视频脚本 | CHAT | Evidence + Audience | Script + Storyboard | Claim check | CHAT-LONGVIDEO-001 |
| A020 | Carousel 视觉 | PLUGIN | Visual Brief | Figma/PPT asset | Human brand check | PLUGIN-FIGMA-002 |
| A021 | Remotion 模板 | CODEX | Video Spec | Code + Render | Local only | CODEX-VIDEO-001 |
| A022 | 视频批量渲染 | CODEX + VERIFY | Assets + Manifest | MP4/SRT/Pack | Evidence validation | CODEX-RENDER-001 |
| A023 | Avatar 片段 | PLUGIN | Script + approved avatar | A-roll clips | Human identity approval | PLUGIN-AVATAR-001 |
| A024 | 长转短 | PLUGIN | Master video | Shorts candidates | Human select | PLUGIN-CLIP-001 |
| A025 | Landing Page Copy | CHAT | Offer + Proof | Page copy | Human business approval | CHAT-OFFER-001 |
| A026 | Landing Page Code | CODEX | Approved copy/design | Local site | 无 | CODEX-LANDING-001 |
| A027 | Preview Deployment | PLUGIN | Repo/branch | Preview URL | Human deploy approval | PLUGIN-VERCEL-001 |
| A028 | Stripe Product Setup | HUMAN | Price/terms | Payment link | Financial approval | Human checklist |
| A029 | Publish Content | PLUGIN + HUMAN | Final pack | Publication URL | Explicit confirm | PLUGIN-PUBLISH-001 |
| A030 | Analytics Review | CHAT | Platform metrics | Learnings + experiments | 无 | CHAT-ANALYTICS-001 |
| A031 | API Runtime Feature | RUNTIME + CODEX | Stable manual workflow | Service | Architecture gate | Future |
| A032 | Public Release | HUMAN + PLUGIN | Clean audit + license | Public repo/release | Public readiness PASS | Release checklist |

## 3. 当前优先级

### NOW

```text
A001–A016
A018–A022
A025–A027
```

### NEXT

```text
A017 Creator benchmark
A023 Avatar
A024 Long-to-short
A028 Stripe offers
A029 Publishing
A030 Analytics
```

### LATER

```text
A031 API runtime
A032 Public product release
```

## 4. 并行规则

允许同时进行：

```text
ChatGPT 正在做下一任务规划
Codex 正在实现上一任务
Plugin 正在生成设计或 Preview
Human 正在做最终审查
```

禁止同时进行：

```text
两个 Codex 写同一个 Repo
多个 Agent 重复规划同一个问题
实现尚未批准时提前写代码
Review 尚未结束时继续扩大 Scope
```
