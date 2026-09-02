# Metrics, Budget & Approval Gates

## 1. SoloScale Workflow Metrics

每个 Run 记录：

```text
wall_clock_start
wall_clock_end
chat_planning_turns
codex_implementation_turns
verification_cycles
repair_cycles
human_interventions
manual_handoffs
files_inspected
files_changed
commands_run
tests_passed
tests_failed
review_p0
review_p1
review_p2
evidence_missing_fields
codex_usage_observed
api_cost_observed
```

不得估算不可观察值。

## 2. Creator Metrics

每条内容记录：

```text
content_id
source_run_id
platform
format
hook
cta
offer_id
impressions
3_second_hold
average_watch_time
completion_rate
saves
shares
comments
profile_visits
link_clicks
github_clicks
email_signups
qualified_leads
calls_booked
purchases
revenue
```

核心商业指标：

```text
Qualified Lead Rate
Lead-to-Sale Rate
Revenue per Content Asset
Revenue per 1,000 Impressions
Revenue per Build Iteration
```

## 3. Budget Gates

### Gate B0 — 免费验证

在 10 条视频之前：

- 不买多个视频 Pro Plan
- 不接多个付费 API
- 不搭 Cloud Render
- 不做自动排程系统

### Gate B1 — 测量瓶颈

只有以下瓶颈真实出现才升级：

```text
Avatar 录制耗时
Voice 重录耗时
Long-to-short 剪辑耗时
Render 本地性能
排程管理
Analytics 汇总
```

### Gate B2 — 月度 SaaS 上限

设一个固定月度试验上限。任何新增订阅必须回答：

```text
它替代了什么人工步骤？
每月节省多少可观察时间？
是否已有 3 次以上重复使用？
是否可以月付先验证？
```

### Gate B3 — API

只有实时、重复、无人值守价值明确时接 API。

## 4. Human Gates

必须人工批准：

- Plan Approval
- 新生产依赖
- 公共 API breaking change
- DB schema / migration
- Auth / permission
- 购买付费工具
- Push / Merge
- Vercel Production deploy
- Stripe 价格和退款条款
- X / LinkedIn / YouTube 等公开发布
- 真实用户数据
- 任何不可逆动作

## 5. Stop Conditions

立即停止自动流程：

- 计划与 Repo 冲突
- Scope 扩张
- 秘密或生产凭据被要求
- 连续两轮修复仍失败
- 超过文件变更上限
- 测试无法运行
- Evidence 不足却要求宣称结果
- 需要花钱但未审批
- 需要公开发布但未审批

## 6. Claim Policy

公开内容只能使用：

```text
Verified
Measured
Observed
Explicitly labeled hypothesis
```

不能使用：

```text
感觉提高 10 倍
估计节省 80% Token
自动化已经完全稳定
大规模生产可用
```

除非有真实定义、样本和证据。
