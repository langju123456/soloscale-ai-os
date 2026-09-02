# Quick Start：现在立即执行什么

不要同时启动所有路线。先把工作拆成三个并行 Lane：

```text
Lane A — SoloScale Dogfood
完成一个真实 Issue → Plan → Codex → Test → PR → Fresh Review → BuildLog 闭环

Lane B — Creator Narrative
把 SoloScale 现有真实证据整理成第一批文字内容与视频脚本

Lane C — Creator Video Factory
由 Codex 建立本地 Remotion 视频模板，不接付费 API，不自动发布
```

## 当前四个最小动作

### Action 1：把本手册提交到 SoloScale Repo

下载本包后，把路径交给 Codex，使用：

- Prompt：`CODEX-DOCS-001`
- 文件：[`../prompts/CODEX_ACTION_PROMPTS.md`](../prompts/CODEX_ACTION_PROMPTS.md)

目标目录建议：

```text
docs/operating-manual/
```

这一动作只允许修改文档，不允许改 `src/` 或测试行为。

---

### Action 2：建立 Dogfood #1 的 GitHub Issue

在普通 ChatGPT 中调用 GitHub Plugin：

- Prompt：`PLUGIN-GH-001`
- 文件：[`../prompts/PLUGIN_ACTION_PROMPTS.md`](../prompts/PLUGIN_ACTION_PROMPTS.md)

目标任务：

```text
Add source-grounded citations to AI-Research-Assistant-LangJu-Edition
```

---

### Action 3：由 ChatGPT 完成只读规划

在新的 ChatGPT 对话中运行：

- Prompt：`CHAT-PLAN-001`
- 然后：`CHAT-PACKET-001`
- 文件：[`../prompts/CHATGPT_ACTION_PROMPTS.md`](../prompts/CHATGPT_ACTION_PROMPTS.md)

最终应产出：

```text
approved-plan.md
risk-register.md
test-plan.md
codex-execution-packet.md
```

不要在这一阶段让 ChatGPT 输出整套代码。

---

### Action 4：Codex 按执行包实现

在目标 Repo 中先运行：

- `CODEX-REPO-001`：只读检查计划与真实仓库是否匹配
- 得到确认后运行 `CODEX-IMPLEMENT-001`
- 完成后运行 `CODEX-VERIFY-001`

不得把整个长对话重新喂给 Codex。Codex 只接收：

```text
GitHub Issue
Approved Plan
Codex Execution Packet
Target Repository
```

## 第一周结束条件

```text
✓ SoloScale 操作手册进入 GitHub
✓ Dogfood #1 Issue 存在
✓ Approved Plan 存在
✓ Codex Feature Branch 存在
✓ 本地测试与 CI 通过
✓ 新 ChatGPT 对话完成独立审查
✓ 无未解决 P0/P1
✓ BuildLog 成功消费真实 Evidence
✓ 至少形成 1 篇 LinkedIn 草稿、1 条 X Thread、3 个短视频脚本
```

## 不要立即做

- 不先做云端 Dashboard
- 不先接六七个 Agent
- 不先接所有视频付费 API
- 不自动发布社交内容
- 不把聊天原文直接公开
- 不宣称节省了多少 Token，除非真实记录过
- 不同时让多个 Codex 会话写同一个 Repo
