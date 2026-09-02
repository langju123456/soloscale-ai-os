# Daily & Weekly Operating Runbook

## 1. 发散思维处理

任何新想法先进入 Inbox，不立即切换：

```text
Idea
→ Capture
→ Route
→ Prioritize
→ Schedule
```

记录格式：

```text
Idea:
Why now:
Expected value:
Track: SoloScale / Creator / Business / Learning
Next smallest experiment:
```

## 2. WIP 限制

同时最多：

```text
1 个 Codex Write Task
2 个 ChatGPT Thinking Tasks
1 个 Plugin Action
1 个 Human Approval Queue
```

禁止两个 Codex Thread 同时写同一个 Repo。

## 3. 每日启动（15–20 分钟）

1. 查看 Active Task
2. 查看上次 Next Action
3. 查看是否等待 Approval
4. 选择今天唯一 Primary Outcome
5. 决定哪些可并行

输出：

```text
Primary Outcome
Chat Task
Codex Task
Plugin Task
Human Gate
Stop Time
```

## 4. 日间流水

理想节奏：

```text
ChatGPT 提交高质量批处理规划
→ 等待期间 Codex 执行上一份已批准 Packet
→ Plugin 完成在线设计/Issue/Preview
→ 本地工具验证
→ Human 查看 Gate
```

不要一问一答地使用最慢推理档；一次请求尽量产出完整 Deliverable。

## 5. Codex Session 规则

每个 Session 开头：

```text
Read:
- Issue
- Approved Plan
- Execution Packet
- AGENTS.md

Do not:
- redo business analysis
- expand scope
- modify unrelated files
```

每个 Session 结束：

```text
Files changed
Commands
Tests
Deviations
Risks
Next action
```

## 6. 每日收尾（15 分钟）

- 保存 Git 状态
- 保存测试结果
- 更新 Issue / Run State
- 写 5 行 DevLog
- 把新想法移入 Backlog
- 明确明天第一步

## 7. 每周复盘

周复盘只回答：

1. 本周交付了什么？
2. 哪个步骤最慢？
3. 哪个步骤重复最多？
4. 哪个步骤应该自动化？
5. 哪个自动化不值得？
6. 哪个内容产生了真实关注或线索？
7. 下周只做哪三个 Outcome？

使用模板：

[`../templates/WEEKLY_REVIEW.md`](../templates/WEEKLY_REVIEW.md)
