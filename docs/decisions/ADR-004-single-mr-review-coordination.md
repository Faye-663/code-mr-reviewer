# ADR-004: IM 与 webhook 共享单 MR ReviewRun 协调和交付

## Status

Accepted（Implemented）

## Date

2026-09-04

## Context

IM 单 MR 与 GitLab webhook 原本分别编排 Agent、报告与外部发布。多个仓库 webhook 可以同时到达，同一 MR 也可能被重复 webhook 或 IM/webhook 同时触发。若入口独立执行，会重复消耗 Agent、重复发布 GitLab finding、重复上传 OneBox；更严重的是，旧 Head 的慢任务可能在新提交后继续外发过期结论。

本期需要跨同机进程协调，但 webhook HTTP worker 仍是内存队列。ReviewSet 已有独立的多 MR 预检、ReviewSet ID、报告和发布语义，不应被单 MR 机制改变。

## Decision

- IM 单 MR 与 webhook 共享 `SingleMrReviewCoordinator`，以 `(project_path, mr_iid, head_sha)` 作为 ReviewKey。成功 run 可持续复用；`failed`、`interrupted`、`superseded` 不复用，同 SHA 的新 Trigger 创建下一 attempt。
- 使用 Python 标准库 SQLite，开启 WAL、`busy_timeout` 和事务 claim。数据库记录 ReviewRun、Trigger 与 delivery 审计，不保存足以恢复 webhook 队列的完整事件。全局只允许一个 running ReviewRun。
- Trigger 使用 IM message ID 或 GitLab `X-Gitlab-Event-UUID` 去重；缺少 webhook UUID 时回退到 payload 的项目、MR 和 Head。webhook 注册前读取 GitLab 当前 Head，避免延迟旧事件反向替代新版本。
- 新 Head 原子标记同 MR 旧未完成 run 的 `superseded_by`。不强杀正在运行的 Agent；review core 在 clone 前后、依赖准备后、plan 后、每次 Agent 返回后和交付前协作检查。
- review 成功后先原子写一组 ReviewRun 级 JSON/Markdown，再重新读取 GitLab Head。Head 不一致时两个 sink 均为 `skipped_stale`，不得外发。
- GitLab 与 OneBox 以 `(review_run_id, sink)` 分别 claim；所有 Trigger 的 sink 意图采用 OR 语义。GitLab 继续使用 finding marker 消除崩溃重试重复；OneBox 使用确定性文件名。明确失败只重试对应 sink，不重跑 Agent。
- OneBox 上传 lease 中断后状态为 `unknown`，不自动重试。当前 CLI 没有已验证的服务端幂等键，因此不能承诺严格 exactly-once。
- 默认值保持入口既有行为：IM `post_comment=false/upload_onebox=true`；webhook `post_comment=true/upload_onebox=false`。两个 sink 都关闭时仍生成本地报告。
- IM 开启 GitLab 发布且用户或仓库白名单为空时保留“空表示不限制”的兼容语义，但 healthcheck 与 poll 启动必须输出高风险 warning。
- ReviewSet、持久化队列、任务自动恢复、多主机协调和可配置并发不在本决策范围。

## Alternatives Considered

### 仅依赖 GitLab marker 和 OneBox 文件名去重

优点是改动小。缺点是无法避免重复 Agent 执行，也无法在新 Head 到达时协调旧任务停止；OneBox CLI 也没有可靠的原子“存在则不上传”能力。因此未选择。

### 只在单个进程内使用锁和字典

优点是实现简单。缺点是 IM poll 与 webhook 可能独立部署为两个进程，进程内锁不能协调；重启后也没有审计状态。因此未选择。

### 将完整 webhook 事件和队列持久化到 SQLite

优点是可在重启后自动恢复。缺点是需要定义 payload 版本、重放、死信和恢复顺序，显著扩大本期范围。本期只记录需求，不实现。

### 把 title、target branch、依赖目录和 Agent 配置纳入 ReviewKey

优点是输入变化都会重审。缺点是 key 和配置快照复杂度上升，且当前核心目标是按 MR 代码版本消重。接受同 SHA 下非代码输入变化仍复用成功结果的折衷。

## Consequences

- 多 repo、多 MR webhook 均可接收并按全局单 worker 顺序执行；同 Head 的 IM/webhook join 或 reuse，不重复调用 Agent。
- ReviewRun 是 review 结果与本地报告的唯一边界；Trigger 只保留来源、事件 ID 和 sink 意图。GitLab、OneBox 失败互不阻塞，部分失败为 `success_with_warnings`。
- 旧版本已完成的历史 GitLab discussion 或 OneBox 文件不会在新 Head 到达后撤回；新版本形成独立 ReviewRun。
- SQLite 文件必须位于所有相关同机进程共享且可写的位置。多主机部署仍可能重复执行和交付。
- HTTP `202 accepted` 只证明 Trigger 已注册到本进程内存队列；进程崩溃可能丢失尚未执行的任务。启动只把过期 lease 标为 `interrupted`，等待新 Trigger。
- 运维排障以 `review_run_id`、ReviewKey、attempt、Trigger 和两个 delivery 状态为主；当前不提供任务查询 API。
