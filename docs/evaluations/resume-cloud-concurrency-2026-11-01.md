# Resume Cloud 并发与可观测：2026-11-01 实测交付

代码基线 `787264fa783d19a910052642627e8f7ab2798561`，最终被测代码
`651112bad4b73828fc8b6cd1d38bf115085fdc21`，两次运行时工作树均干净。
后续提交只补充本报告和证据。每个报告 JSON 包含源码 SHA-256；目录保留不含内容的
实际调用区间、任务元数据、终态审计和 worker 指标，便于复算峰值和计数。

## 测量范围

本机 macOS arm64、Python 3.12.13、Docker 29.6.1、Compose 中的 PostgreSQL 16。
2 个真实 API 子进程、4 个业务客户端、2 个 worker 子进程，每进程 2 个槽位。
假网关配置延迟 0.15 秒，执行真实 AI 简历管线；未访问付费模型。
24 个基础任务各发起 3 次并发请求，另有超限突发、冲突请求及两个缓存对照任务。
25% 是假错误率配置，实际错误数量以观察结果为准。缓存对照阶段错误率固定为 0。

| 实际测量 | 正常场景 | 配置 25% 错误场景 |
|---|---:|---:|
| POST /tasks 请求 | 91 | 91 |
| HTTP 201 / 409 / 429 | 82 / 1 / 8 | 82 / 1 / 8 |
| 幂等重放 | 48 | 48 |
| 唯一任务 | 34 | 34 |
| 成功 / 失败 | 34 / 0 | 25 / 9 |
| 实际模型调用 | 33 | 33 |
| 批次模型错误 | 0 | 9 |
| 每 worker 并发峰值 | 2、2 | 2、2 |
| 总模型并发峰值 / 配置上界 | 4 / 4 | 4 / 4 |
| 重复终态事件 | 0 | 0 |
| 缓存命中新增模型调用 | 0 | 0 |
| 换客户端新增模型调用 | 1 | 1 |
| 排队指标样本 / 模型指标样本 | 34 / 33 | 34 / 33 |
| 缓存查找 / 命中 | 34 / 1 | 34 / 1 |
| 创建请求 p95（秒） | 0.05403143749572337 | 0.0741769999731332 |
| 排队等待 p95（秒） | 1.86957295 | 1.8588783 |
| 任务端到端 p95（秒） | 2.18574695 | 2.17652455 |
| 模型耗时 p95（秒） | 0.156511225 | 0.1555001164 |

正常场景：[完整报告](resume-cloud-concurrency-2026-11-01/success/report.md) ·
[机器可读证据](resume-cloud-concurrency-2026-11-01/success/report.json)。
错误场景：[完整报告](resume-cloud-concurrency-2026-11-01/errors/report.md) ·
[机器可读证据](resume-cloud-concurrency-2026-11-01/errors/report.json)。
两个场景全部 26 项断言通过。每个任务只尝试一次，错误未重试。
终态触发器还经过一次 savepoint 内的重复完成探针，确认能捕捉重复事件后回滚，
该探针不计入上表的任务或已提交终态事件。

## 验证结果

| 命令 / 证据 | 结果 |
|---|---|
| 改动前完整 pytest | 720 passed, 10 skipped，28.32 秒 |
| 改动前专用 PG 定向测试 | 31 passed |
| 改动前不变量探针 | 9 项通过，见 baseline-invariants.json |
| 最终专用 PG 完整 `pytest -q` | 779 passed, 1 skipped, 1 warning，37.08 秒；exit 0 |
| `ruff check .` | 通过；exit 0 |
| `MYPYPATH=src mypy --explicit-package-bases src tests scripts/evaluate_resume_concurrency.py` | 165 source files 通过；strict=true；exit 0 |
| `python -m build` | wheel / sdist 成功；exit 0 |
| Docker `cloud,observability` 构建 | 成功；exit 0 |
| 镜像内控制台 span 冒烟 | 成功；UID 10001，OTel SDK 1.45.2；exit 0 |
| 跨进程 trace 验收 | 独立 Python worker 继承 API trace；新建、重放、冲突、管线、模型、缓存终态、过期转人工均通过 |

唯一 skipped 为缺失可选 `buildlog.models` 的既有 EvidenceHub 测试；云端 PG 测试均执行。
warning 为当前 Starlette 对 httpx 测试适配器的弃用提示。
Pydantic 2.13.5 用于复现严格检查；2.14.2 在既有 resume_docx 代码触发 TypeForm
类型错误，尚未修改全仓库依赖范围。详细版本见 [runtime.json](resume-cloud-concurrency-2026-11-01/runtime.json)。

并发回归还覆盖：同键并发唯一、令牌桶突发/补充/客户端隔离/时钟回退、槽位满时不领取、
错误 token 与过期租约拒绝写入、缓存原 TTL/版本/分区、缓存清理与刷新真实行锁竞争，
以及恢复回调必须在另一连接可见已提交状态后执行。SDK 默认关闭时不加载 SDK/exporter，
FastAPI 自动遥测关闭。导出器测试检查 sentinel 内容不进入日志/span。

## 五条可用的面试讲法

1. “先用数据库唯一键保证请求幂等，再在同一事务中消耗共享令牌桶。两台 API 收到
   48 次重放仍只有一份任务；超限的 8 次请求返回 429，不留下任务。”
   对应 `resume_cloud_repository.py:95`、`:180`，API 错误映射 `resume_cloud_api.py:127`。
2. “领取前先占本进程槽位，数据库用 SKIP LOCKED 分配任务，完成、失败和续租必须同时
   匹配 token 与有效租约。2 个 worker 各限 2，实测各峰值 2、总峰值 4，重复完成为 0。”
   对应 `resume_cloud_service.py:232`、`resume_cloud_repository.py:231`、`:248`、`:326`、`:346`。
3. “不会因为超时就盲目重试模型；SDK 最大重试仍为 0。已中断任务租约过期转人工，
   不能让另一 worker 自动重放可能已计费的调用。”
   对应 `model_gateway.py:37`、`resume_cloud_repository.py:356`、`test_resume_cloud_observability.py:257`。
4. “缓存键包含客户端、原文/JD 哈希、模式、实际模型身份与源码指纹，命中不延长 TTL。
   实测同客户端命中新增调用 0，换客户端新增调用 1。”
   对应 `resume_cloud_cache.py:29`、`resume_cloud_service.py:213`、`resume_cloud_repository.py:296`。
5. “API 把 traceparent 写进任务，worker 从数据库接上。先看排队直方图，再看模型耗时和
   并发峰值，最后定位管线 span；日志和 trace 不放简历、JD、输出和原始异常消息。”
   对应 `resume_cloud_api.py:127`、`resume_cloud_service.py:253`、
   `resume_cloud_observability.py:77`、`:110`、`resume_cloud_metrics.py:142`。

行号对应上述被测代码提交。可以从 GitHub 该提交逐行核查；本报告不会把配置上限冒充
生产吞吐能力，也不会把测试通过冒充真实供应商可用性。

## 明确边界与后续工作

- X-Client-ID 是可信调用方分区，不是多租户认证；共享 token 持有人可冒用 ID。
- 模型上限和指标均按进程；跨机器全局额度、指标持久化/聚合需后续规划。
- 未做真实付费模型、外部 OTLP collector 联通或生产容量测试；OTLP 构造和内存导出有测试。
- 并发缓存 miss 不合并；原请求可安全地分别生成。没有增加模型自动重试或上游 429 重排。
- 本次没有生产迁移、部署或合并 main；测试数据库/卷保留供核查。

## 提交与文件清单

下表来自 Git 实际提交，不合并或重写已经保存的历史。A–F 主体分别是
`50cef22`、`57c70fe`、`9c1fd2e`、`8f1174b`、`00aef5f`、`40d77ab`，
后续小提交修复审查发现并补齐证据。`8f1174b` 同时纠正早期迁移对 legacy client
分区的推断：旧记录 NULL 保持兼容且永久不缓存。

| 提交 | 说明 | 修改文件 |
|---|---|---|
| [50cef22](https://github.com/langju123456/soloscale-ai-os/commit/50cef22c28d3df6768882eef5503ef9f56bd85a8) | feat(resume-cloud): add shared client rate limits | `docs/decisions/ADR-0008-resume-cloud-client-rate-limits.md`, `migrations/resume_cloud/0002_rate_limits.sql`, `scripts/migrate_resume_cloud.py`, `src/soloscale/resume_cloud_api.py`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `tests/test_resume_cloud_integration.py` |
| [e7d5ded](https://github.com/langju123456/soloscale-ai-os/commit/e7d5dedf39f85c95ba95b9ec3a6548c90c6f0ede) | fix(resume-cloud): fence rate limiter clock skew | `docs/decisions/ADR-0008-resume-cloud-client-rate-limits.md`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `tests/test_resume_cloud_integration.py` |
| [1ebf1ee](https://github.com/langju123456/soloscale-ai-os/commit/1ebf1eede0b9b2688c92e3a3eae7402044f97dc1) | fix(resume-cloud): disambiguate token bucket result | `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py` |
| [57c70fe](https://github.com/langju123456/soloscale-ai-os/commit/57c70fed0f5534b5038aa0cff83cf769bb73542a) | feat(resume-cloud): bound worker model concurrency | `docs/decisions/ADR-0009-resume-cloud-model-concurrency.md`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `src/soloscale/resume_cloud_worker.py`, `tests/test_resume_cloud_integration.py` |
| [39a178d](https://github.com/langju123456/soloscale-ai-os/commit/39a178da5d203dd488fe106651b29259548b9313) | fix(resume-cloud): claim only after worker slot | `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `tests/test_resume_cloud_integration.py` |
| [19aab4d](https://github.com/langju123456/soloscale-ai-os/commit/19aab4dabe07c509b88ff1d931556c4f239ab75c) | fix(resume-cloud): make idempotent rate limits transactional | `docs/decisions/ADR-0008-resume-cloud-client-rate-limits.md`, `docs/decisions/ADR-0009-resume-cloud-model-concurrency.md`, `scripts/migrate_resume_cloud.py`, `src/soloscale/resume_cloud_api.py`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `src/soloscale/resume_cloud_worker.py`, `tests/test_resume_cloud_integration.py` |
| [9c1fd2e](https://github.com/langju123456/soloscale-ai-os/commit/9c1fd2ee3c119cdc9f728372f827b601fa6d05b3) | feat(resume-cloud): cache successful results | `docs/decisions/ADR-0010-resume-cloud-result-cache.md`, `migrations/resume_cloud/0003_result_cache.sql`, `scripts/migrate_resume_cloud.py`, `src/soloscale/resume_cloud_cache.py`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `tests/test_resume_cloud_cache.py`, `tests/test_resume_cloud_integration.py` |
| [cead7cc](https://github.com/langju123456/soloscale-ai-os/commit/cead7cc5e8ad2e4827eda17899b4532eb9aca0a6) | fix(resume-cloud): preserve earliest cache expiry | `src/soloscale/resume_cloud_repository.py` |
| [7254675](https://github.com/langju123456/soloscale-ai-os/commit/7254675c93dce0fd9bbaab3b62fd24574d3c5a77) | fix(resume-cloud): preserve legacy idempotency partition | `migrations/resume_cloud/0003_result_cache.sql`, `tests/test_resume_cloud_integration.py` |
| [8f1174b](https://github.com/langju123456/soloscale-ai-os/commit/8f1174b65522360f74772b939c78d36cde2c8230) | feat(resume-cloud): add optional trace propagation | `Dockerfile.resume-cloud`, `compose.resume-cloud.yaml`, `docs/decisions/ADR-0011-resume-cloud-optional-tracing.md`, `migrations/resume_cloud/0003_result_cache.sql`, `migrations/resume_cloud/0004_traceparent.sql`, `pyproject.toml`, `scripts/migrate_resume_cloud.py`, `src/soloscale/resume_cloud_api.py`, `src/soloscale/resume_cloud_cache.py`, `src/soloscale/resume_cloud_observability.py`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `tests/test_resume_cloud_integration.py` |
| [4aa80cf](https://github.com/langju123456/soloscale-ai-os/commit/4aa80cf20984e3f9b49ee3d922ee69839994bdb5) | feat(resume-cloud): trace canonical pipeline stages | `compose.resume-cloud.yaml`, `docs/decisions/ADR-0010-resume-cloud-result-cache.md`, `src/soloscale/resume_cloud_observability.py`, `src/soloscale/resume_cloud_service.py`, `src/soloscale/resume_docx.py`, `src/soloscale/resume_gateway_boundary.py` |
| [5901020](https://github.com/langju123456/soloscale-ai-os/commit/590102040271c6e084c040c9f631383e1c36c80a) | fix(resume-cloud): complete trace propagation | `docs/decisions/ADR-0011-resume-cloud-optional-tracing.md`, `pyproject.toml`, `src/soloscale/model_gateway.py`, `src/soloscale/resume_cloud_api.py`, `src/soloscale/resume_cloud_observability.py`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `src/soloscale/resume_cloud_worker.py`, `tests/test_resume_cloud_cache.py`, `tests/test_resume_cloud_integration.py` |
| [00aef5f](https://github.com/langju123456/soloscale-ai-os/commit/00aef5fc015e0076d095d09851de9bf02ebd2ca6) | feat(resume-cloud): add process metrics | `compose.resume-cloud.yaml`, `docs/decisions/ADR-0012-resume-cloud-metrics.md`, `migrations/resume_cloud/0005_metrics_queue_timing.sql`, `scripts/migrate_resume_cloud.py`, `src/soloscale/resume_cloud_api.py`, `src/soloscale/resume_cloud_metrics.py`, `src/soloscale/resume_cloud_observability.py`, `src/soloscale/resume_cloud_repository.py`, `src/soloscale/resume_cloud_service.py`, `src/soloscale/resume_cloud_worker.py`, `tests/test_resume_cloud_integration.py`, `tests/test_resume_cloud_metrics.py` |
| [98e9643](https://github.com/langju123456/soloscale-ai-os/commit/98e9643b75dcd8b8c3cdf1177053b4dd0ca2efe2) | fix(resume-cloud): export metrics gauges safely | `compose.resume-cloud.yaml`, `docs/decisions/ADR-0012-resume-cloud-metrics.md`, `src/soloscale/model_gateway.py`, `src/soloscale/resume_cloud_metrics.py`, `src/soloscale/resume_cloud_observability.py`, `tests/test_resume_cloud_metrics.py` |
| [40d77ab](https://github.com/langju123456/soloscale-ai-os/commit/40d77ab85144a07ff941a70feeab0dcb9abe26ef) | test: add resume cloud concurrency evaluator | `docs/decisions/ADR-0013-resume-cloud-concurrency-evaluation.md`, `scripts/evaluate_resume_concurrency.py`, `tests/test_resume_concurrency_evaluation.py` |
| [4421e23](https://github.com/langju123456/soloscale-ai-os/commit/4421e23823d206561f38abc023749e6985da0107) | test(resume-cloud): prove concurrent model calls and cache isolation | `docs/decisions/ADR-0013-resume-cloud-concurrency-evaluation.md`, `scripts/evaluate_resume_concurrency.py`, `tests/test_resume_concurrency_evaluation.py` |
| [1067f19](https://github.com/langju123456/soloscale-ai-os/commit/1067f194ee20ef48ecb2e0970160c53c7dbb0526) | test(resume-cloud): expose loopback Postgres for reproducible evaluation | `compose.resume-cloud-eval.yaml`, `docs/decisions/ADR-0013-resume-cloud-concurrency-evaluation.md`, `scripts/evaluate_resume_concurrency.py` |
| [7e55444](https://github.com/langju123456/soloscale-ai-os/commit/7e554449450ab44ce32be97c38c26c0b640abf04) | test(resume-cloud): prove cross-process trace and privacy boundaries | `compose.resume-cloud.yaml`, `docs/decisions/ADR-0011-resume-cloud-optional-tracing.md`, `src/soloscale/resume_cloud_api.py`, `tests/test_resume_cloud_observability.py` |
| [7bd6011](https://github.com/langju123456/soloscale-ai-os/commit/7bd60111988d00322ba47b52df6aa70ede80514d) | test(resume-cloud): verify metrics endpoints and cache cleanup races | `docs/decisions/ADR-0012-resume-cloud-metrics.md`, `src/soloscale/resume_cloud_observability.py`, `tests/test_resume_cloud_integration.py`, `tests/test_resume_cloud_metrics.py` |
| [651112b](https://github.com/langju123456/soloscale-ai-os/commit/651112bad4b73828fc8b6cd1d38bf115085fdc21) | test(resume-cloud): reconcile measured metrics with independent audit | `README.md`, `docs/decisions/ADR-0008-resume-cloud-client-rate-limits.md`, `docs/decisions/ADR-0013-resume-cloud-concurrency-evaluation.md`, `scripts/evaluate_resume_concurrency.py` |
