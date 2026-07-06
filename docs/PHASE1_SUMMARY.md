# Phase1 总结

## 状态

本文档记录 `deng/code-doc-sync-audit-fixes` worktree 在 `fd5b307 test: cover review guardrail gaps` 时的 Phase1 状态。Phase1 已经完成 GitLab MR 自动 review 的基础闭环：系统可以通过 WeLink IM poll 或 GitLab webhook 接收触发事件，定位 MR，准备本地代码仓上下文，调用 opencode 生成 Markdown review 报告，并把结果写回对应入口的输出通道。

Phase2 开发应以本文档列出的能力、边界和限制为起点；未列为“已实现”的内容不应被默认视为可用能力。

## 已实现功能

### 触发入口与 CLI

- `healthcheck`：检查基础命令、GitLab 配置、WeLink poll/reply、OneBox 目录等配置是否可用，并输出 webhook endpoint 和 `comment_skill` 状态。
- `run-once <mr-url>`：人工触发单个 GitLab MR review，适合本地验证核心 review 流程。
- `poll [--once]`：轮询 WeLink 群历史消息，识别 review 请求，执行 review 后上传报告并通知群聊。
- `webhook`：启动 GitLab Merge Request Hook HTTP 服务，接收可处理事件并将 review 任务放入后台队列。

### WeLink IM Poll

- 解析 WeLink `query-history-message` 原始 JSON，读取 `respData.chatInfo` 并归一化消息字段。
- 通过文本中的 bot mention 或 `atAccountList` 精确识别机器人是否被 @。
- 支持 `MR_REVIEWER_ALLOWED_GROUPS`、`MR_REVIEWER_ALLOWED_USERS`、`MR_REVIEWER_ALLOWED_REPOS` 白名单过滤。
- 用本地 `StateStore` 记录已处理消息，避免重复处理同一条 IM 消息。
- 生成 Markdown review 报告后先写入临时文件，再调用 `welink-cli onebox file-upload` 上传到 OneBox。
- 群聊只发送报告文件名或上传失败提示，避免把完整 Markdown review 正文作为群消息发送。

### GitLab Webhook

- HTTP handler 校验请求 path、method 和可选 `X-Gitlab-Token` secret。
- 支持 GitLab MR `open`、`reopen`，以及 `update_reason == "source update"` 的 update 事件。
- 对不可处理事件返回 skipped，不触发 review。
- webhook 模式要求配置 `MR_REVIEWER_COMMENT_SKILL`；缺失时拒绝可处理事件或拒绝启动服务。
- 后台 `WebhookReviewQueue` 执行 review，避免 HTTP 请求阻塞完整 review 流程。
- 生成本地监视报告到 `MR_REVIEWER_REPORT_DIR`，包含任务状态、MR 定位、分支、base/head、changed files、opencode returncode、提交状态和 Markdown preview。

### Review Core

- 通过 GitLab API 获取 MR 元数据，并读取 target/source project 的 HTTPS clone URL。
- clone target repo 到任务临时目录，显式 fetch target branch 和 source branch。
- fork MR 场景下会添加 `source` remote 以获取 source project 分支。
- checkout MR head SHA 后生成 MR range diff。
- `run-once` 使用 GitLab MR `diff_refs.base_sha...diff_refs.head_sha`；webhook payload 只提供 head 时，使用 target branch 与 head 的 `merge-base...head`。
- prompt 只传 MR URL、Base SHA、Head SHA、Changed files 和本地 repo path，不把完整 diff 直接塞进 prompt。
- review 完成后清理任务临时目录。

### opencode 集成

- 默认使用 `code-review` skill；webhook 可通过 `MR_REVIEWER_COMMENT_SKILL` 指定评论型 skill。
- `MR_REVIEWER_OPENCODE_DEBUG` 默认开启，调用形式为 `opencode --print-logs --log-level DEBUG run ...`。
- 支持 `MR_REVIEWER_OPENCODE_DIAGNOSTIC_DIR`，输出 `prompt.md`、`cwd.txt`、脱敏 `command.txt`、`env-summary.json`、`stdout.md`、`stderr.log`、`returncode.txt`。
- 支持 `MR_REVIEWER_OPENCODE_PROMPT_TRANSPORT=argument|file`；非法 transport 会直接失败，避免进入不明确的调用路径。
- Windows 下如果 `opencode` 或 `welink-cli` 解析到 `.cmd`/`.bat`，会通过 `cmd.exe /d /c call` 包装执行。

### 资源保护

- `MR_REVIEWER_MAX_FILES` 限制 changed files 数量，默认 `50`。
- `MR_REVIEWER_MAX_DIFF_LINES` 限制 diff 行数，默认 `2000`。
- `MR_REVIEWER_TASK_TIMEOUT_SECONDS` 限制 opencode 单任务超时，默认 `900` 秒。

### opencode Skills

- `.opencode/skills/code-review`：面向已 checkout 临时 repo 的 GitLab MR review skill，要求只审查 Base SHA 到 Head SHA 的 MR range，不按本地未提交变更审查。
- `.opencode/skills/gitlab-mr-review`：面向人工在 opencode 中直接输入 MR URL 的端到端编排 skill，负责 clone/fetch/checkout、调用 `code-review`，并可按配置提交 MR comment。

## 架构边界

- Python 侧负责入口协议、配置读取、GitLab API、Git checkout/diff、opencode CLI 调用、WeLink 上传通知、webhook 队列和本地监视报告。
- opencode skill 负责实际 review 内容生成；Python 不解析 Markdown review 结论。
- webhook 模式下，MR comment 暂由 `MR_REVIEWER_COMMENT_SKILL` 指定的 opencode skill 脚本提交。Python 只能可靠记录 opencode 调用成败和本地监视报告，不能完全证明 GitLab comment 已提交成功。
- opencode provider、model 和 API Key 不由本项目传参控制，继续由目标机器上的 opencode 配置、登录状态、环境变量或 opencode 默认规则决定。

## 验证状态

当前 worktree 分支已有全量测试基线：

```powershell
uv run pytest
```

最近一次验证结果为 `52 passed`。测试覆盖范围包括：

- GitLab MR URL 解析、diff refs 选择、fork MR source repo 处理。
- Git clone/fetch/checkout、webhook `merge-base` fallback、资源限制超限失败。
- WeLink history JSON 解析、bot mention / `atAccountList` 触发、白名单过滤、OneBox 上传和群通知。
- webhook secret 校验、可处理事件过滤、`reopen` 事件、`COMMENT_SKILL_REQUIRED`、本地监视报告脱敏。
- opencode debug 参数、prompt 脱敏日志、diagnostic 输出、file transport 和非法 transport。
- `.opencode` skill 包存在性与关键 prompt 契约。

## 已知限制

- GitLab 标准 Web URL 中的 `/-/merge_requests/` 分隔符仍不兼容；当前 URL parser 以 `/merge_requests/` 为分隔符。
- WeLink 私聊回发未实现，目前只实现群聊通知。
- WeLink 增量拉取尚未使用 `maxMsgId`；当前依赖本地状态文件去重。
- WeLink CLI 不支持直接发送 Markdown 长文本，因此当前先上传 Markdown 文件，再发送文件名通知。
- `poll` 成功路径会在 `stage=report_content` 输出完整 Markdown 报告正文；生产环境若不允许日志包含报告内容，需要先调整。
- webhook comment 提交属于 opencode skill 外部副作用，Python 不能完全证明评论已提交。
- `healthcheck` 仍偏 IM/OneBox 全量检查，不是 webhook-only mode。

## Phase2 建议入口

- 决定 webhook comment 是否从 opencode skill 回收至 Python GitLab notes API，以便 Python 侧校验 comment 提交结果并记录 comment URL。
- 兼容 GitLab 标准 `/-/merge_requests/` URL，同时保留现有 `/merge_requests/` 示例兼容。
- 拆分 `healthcheck` mode，例如区分 IM poll、webhook-only 和 core review 检查。
- 收紧日志敏感内容边界，尤其是 `stage=report_content` 的完整 Markdown 输出。
- 设计 WeLink 增量轮询策略，明确是否使用 `maxMsgId`、状态文件迁移和失败重试语义。
- 评估 WeLink 私聊回发能力是否进入 Phase2，并明确它与群聊通知的配置、权限和失败处理差异。
