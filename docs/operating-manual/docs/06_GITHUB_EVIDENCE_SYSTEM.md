# GitHub Evidence System

## 1. GitHub 是消息总线，不是代码仓库而已

ChatGPT 与 Codex 不通过长聊天记录交接，而通过：

```text
Issue
ADR
Approved Plan
Branch
Commit
PR
CI
Review
Release
DevLog
```

## 2. 推荐 Repo 结构

SoloScale Repo：

```text
docs/
├── operating-manual/
├── architecture/
├── decisions/
├── dogfoods/
│   └── dogfood-001/
├── devlogs/
├── conversations/
└── content/
```

每个 Dogfood：

```text
docs/dogfoods/dogfood-001/
├── hypothesis.md
├── approved-plan.md
├── execution-packet.md
├── risk-register.md
├── test-plan.md
├── metrics.json
├── review.md
├── retrospective.md
└── buildlog-iteration.json
```

本地临时证据：

```text
.soloscale/runs/<run-id>/
├── task.json
├── route-decision.json
├── approval-receipt.json
├── events.jsonl
├── changed-files.txt
├── diff.patch
├── verification.log
├── verification.json
├── review.json
└── final-report.md
```

## 3. Issue Contract

Issue 必须明确：

- Goal
- Current problem
- Frozen decisions
- Required analysis
- Acceptance criteria
- Non-goals
- Stop conditions
- Evidence required
- Definition of Done

## 4. PR Contract

PR 必须包含：

```text
What changed
Why
What was not changed
Test evidence
Compatibility impact
Known limitations
Rollback
SoloScale run ID
Linked Issue
```

## 5. Commit Narrative

建议一个 Milestone 多个有意义的 Commit，而不是最后一个巨大 Commit：

```text
baseline
→ architecture / contract
→ implementation
→ hardening / failure semantics
→ evidence / documentation
→ release
```

每个 Commit 应回答：

```text
本次提交建立了什么可验证能力？
```

## 6. Chat 内容的处理

不要公开原始聊天。

三级处理：

```text
Raw Chat（私有）
→ Conversation Distillation
→ Verified Narrative
```

Distillation 只保存：

- Trigger
- New mental model
- Decisions
- Rejected alternatives
- Open questions
- Next experiment
- Reusable language

Verified Narrative 再加入：

- Commit
- Diff
- Test
- Screenshot
- Benchmark
- Failure
- Measured result

## 7. Public / Private 边界

Private Repo 可以保留：

- 详细中间实验
- 原始 metrics
- 内部 Prompt
- 本地运行日志（清理秘密后）
- 未成熟商业假设

Public 前必须清理：

- 绝对本地路径
- 用户名与 PII
- API key / Token / `.env`
- Session ID
- 内部聊天原文
- 未验证的性能、成本、Token、收入主张
- 私有 Figma / 文件路径
- 不兼容的 License

## 8. Release Gate

公开前必须：

```text
Clean-room audit PASS
Fresh independent review
CI green
License selected
Secrets/history scan clean
README claims match evidence
At least one real dogfood
No unresolved P0/P1
```
