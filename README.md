# code-mr-reviewer

GitLab MR Review 助手。项目支持两种触发入口：

- **WeLink IM poll**：轮询 WeLink 群历史消息，识别 `@Bot + GitLab MR URL` 后调用已配置的 Agent 先生成 MR 概要、再生成结构化 review JSON，由 Python 渲染包含概要与 review 的 Markdown 报告，并把报告文件上传到 WeLink OneBox 后通知群聊。
- **GitLab webhook**：接收 GitLab Merge Request Hook，后台执行 review，把 JSON 监视报告和 Markdown review 报告写入 `MR_REVIEWER_REPORT_DIR`，并可由 Python 侧发布 GitLab inline discussion。只使用 webhook 的用户可直接阅读 [Webhook 快速开始](docs/WEBHOOK_QUICKSTART.md)。

两种入口共用同一条 two-step review 流程：clone GitLab target 仓库到临时目录，拉取 target/source 分支，checkout MR head；第一次调用 OpenCode 或 Claude Code 输出结构化 MR 概要，第二次把该概要作为上下文生成结构化 review JSON，再由 Python 校验、发布或渲染 Markdown。概要只写入本地报告，不作为线上 comment/discussion 发布。

## 项目优势

- 在本地 clone 完整代码仓，对比 target/source 两个分支，可以读取更多代码上下文与代码仓的 skill，提升 review 质量。
- 只通过 HTTPS Token 访问 GitLab。
- 支持资源限制：最大变更文件数、最大 diff 行数、任务超时时间。
- Agent 的 provider、model 和 API Key 由目标机器上的 OpenCode 或 Claude Code 配置、登录状态和环境变量决定，本项目不直接配置模型。

## 依赖条件

- Python `>=3.11`。
- `uv`，用于安装和运行本项目的 Python 包。
- `git`，用于 clone 目标 GitLab 仓库、fetch MR 分支和生成 diff。
- 可访问目标 GitLab 的网络环境，以及具备读取 MR API 和 HTTPS clone 权限的 GitLab Token。
- OpenCode 或 Claude Code CLI：必须已安装并可在 PATH 中找到；也可以通过 `MR_REVIEWER_AGENT_COMMAND` 指定可执行命令。
- 所选 Agent 已安装 `code-review` skill。仓库内的中立源文件位于 `.skill`，使用方需要复制到自己的 Agent skill 配置目录。
- 使用 WeLink IM poll 时，还需要 `welink-cli` 已安装并完成登录或授权，且当前账号需要能调用 `im query-history-message`、`im send-to-group` 和 `onebox file-upload`。

## Quick Start

1. 安装 Python 依赖并确认基础外部命令可用：

```powershell
uv sync
git --version
opencode --version
# 如果使用 Claude Code，改为：claude --version
```

2. 复制配置文件：

```powershell
Copy-Item .env.example .env
```

### GitLab webhook

只接 GitLab webhook 时，按 [Webhook 快速开始](docs/WEBHOOK_QUICKSTART.md) 配置并启动：

```powershell
uv run mr-reviewer webhook
```

GitLab 中配置的 URL 示例：

```text
http://本机IP:8080/webhook/gitlab
```

路径需要和 `MR_REVIEWER_WEBHOOK_PATH` 完全一致，不要追加尾部 `/`。

### WeLink IM poll

编辑 `.env`，至少填入：

```env
MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com
MR_REVIEWER_GITLAB_TOKEN=your-gitlab-token
MR_REVIEWER_IM_POLL_COMMAND=welink-cli im query-history-message --query-count 20
MR_REVIEWER_IM_REPLY_COMMAND=welink-cli im send-to-group
MR_REVIEWER_WELINK_GROUP_ID=group-example
MR_REVIEWER_WELINK_ONEBOX_SPACE_ID=space-example
MR_REVIEWER_WELINK_ONEBOX_PARENT_ID=parent-example
MR_REVIEWER_BOT_MENTION=@Bot
MR_REVIEWER_BOT_ACCOUNT=bot-example
```

执行健康检查：

```powershell
uv run mr-reviewer healthcheck
```

单次验证 GitLab MR：

```powershell
uv run mr-reviewer run-once https://gitlab.example.com/team/project/merge_requests/7
```

单轮验证 WeLink 轮询：

```powershell
uv run mr-reviewer poll --once
```

常驻轮询：

```powershell
uv run mr-reviewer poll
```

## Review 流程

实际 Git 流程：

1. clone target project 到任务临时目录。
2. fetch target branch。
3. 如果 source project 不同，添加 `source` remote。
4. fetch source branch。
5. checkout GitLab MR head SHA。
6. 生成 MR range diff：`run-once` 使用 GitLab MR `diff_refs.base_sha...diff_refs.head_sha`；webhook payload 只提供 head 时，使用 target branch 与 head 的 `merge-base...head`。
7. 第一次调用已配置 Agent，只生成严格结构化的 MR 概要 JSON：`overview`、`change_areas`、`behavior_changes`、`risk_areas`、`test_changes`。
8. 第二次调用同一 Agent，把第一步概要作为上下文，要求所选 `code-review` skill 输出严格结构化 review JSON。两次调用共享 `MR_REVIEWER_TASK_TIMEOUT_SECONDS` 的总时间预算。
9. Python 校验两步输出。本地 JSON/Markdown 报告同时保留概要与 review；GitLab 线上只发布 review finding，不发布概要。

当前 `run-once` URL 解析以 `/merge_requests/` 为分隔符，示例使用不带 `/-/` 的形式；如果直接粘贴 GitLab Web 页面常见的 `/-/merge_requests/7`，当前版本会把 `-` 解析进项目路径。

## Agent skill 直接使用

除了现有自动入口，也可以把 `.skill/gitlab-mr-review` 和 `.skill/code-review` 复制到 OpenCode 或 Claude Code 的 skill 配置目录后按需使用。这个能力不替代现有 WeLink 自动轮询模式；它适合人工触发单个 GitLab MR 检视，并可把 Markdown 报告评论到现有 MR。

推荐在所选 Agent 中输入：

```text
使用 gitlab-mr-review skill 检视并评论这个 MR：
https://gitlab.example.com/team/project/merge_requests/7
```

该 skill 会调用内置脚本完成 clone/fetch/checkout，先生成 MR 概要，再把概要交给 `code-review skill` 生成 review。完整概要与 review 写入本地报告；默认提交到 GitLab MR comment 的只有 review 正文。使用前需要配置：

```powershell
$env:GITLAB_BASE_URL = "https://gitlab.example.com"
$env:GITLAB_API_BASE_URL = "https://api.example.com/api/api/v4"
$env:GITLAB_TOKEN = "your-gitlab-token"
# 可选：只生成本地报告，不提交 MR comment
$env:MR_REVIEW_SUBMIT_COMMENT = "false"
# .env 风格等价配置：MR_REVIEW_SUBMIT_COMMENT=false
```

可选环境变量：

- `MR_REVIEWER_AGENT_TYPE`：`opencode` 或 `claude-code`，默认 `opencode`。
- `MR_REVIEWER_AGENT_COMMAND`：Agent 可执行命令；为空时根据类型使用 `opencode` 或 `claude`。
- `MR_REVIEW_WORK_DIR`：临时 clone 和报告输出目录，默认系统临时目录下的 `gitlab-mr-review`。
- `MR_REVIEW_SUBMIT_COMMENT`：默认 `true`；设置为 `false` 时只输出本地 Markdown 报告路径。

Agent 的 provider/model 仍由 OpenCode 或 Claude Code 自身配置、登录状态和环境变量决定；该 skill 不覆盖模型配置。

## 配置

复制 `.env.example` 为 `.env`，按需配置：

- `MR_REVIEWER_GITLAB_BASE_URL`：GitLab 根地址，例如 `https://gitlab.example.com`。
- `MR_REVIEWER_GITLAB_API_BASE_URL`：完整 GitLab REST API 根地址，例如 CodeHub 的 `https://api.example.com/api/api/v4`。为空时回退为 `<MR_REVIEWER_GITLAB_BASE_URL>/api/v4`。
- `MR_REVIEWER_GITLAB_TOKEN`：GitLab token，用于 MR API 和 HTTPS clone。
- `MR_REVIEWER_IM_POLL_COMMAND`：轮询 WeLink 群历史消息的基础命令，例如 `welink-cli im query-history-message --query-count 20`；程序会追加 `--group-id <MR_REVIEWER_WELINK_GROUP_ID>`。
- `MR_REVIEWER_IM_REPLY_COMMAND`：发送 WeLink 群通知的基础命令，例如 `welink-cli im send-to-group`；程序会追加 `--group-id <MR_REVIEWER_WELINK_GROUP_ID> --text <文件名通知>`。
- `MR_REVIEWER_WELINK_GROUP_ID`：当前唯一支持的 WeLink 群 ID，轮询历史消息和发送群通知都会使用这个值。
- `MR_REVIEWER_WELINK_ONEBOX_SPACE_ID`：WeLink OneBox 上传目标 `space-id`。
- `MR_REVIEWER_WELINK_ONEBOX_PARENT_ID`：WeLink OneBox 上传目标 `parent` 目录 ID。若 `space-id` 或 `parent` 不存在、无权限或未配置，程序会向群里提示 OneBox 上传失败，但不会把当前 review 任务标记为失败。
- `MR_REVIEWER_BOT_MENTION`：触发用的 bot mention，默认 `@Bot`。
- `MR_REVIEWER_BOT_ACCOUNT`：WeLink bot 账号 ID；配置后会用 `atAccountList` 精确判断是否 @ 了机器人。
- `MR_REVIEWER_ALLOWED_GROUPS`、`MR_REVIEWER_ALLOWED_USERS`、`MR_REVIEWER_ALLOWED_REPOS`：逗号分隔白名单；为空表示不限制。
- `MR_REVIEWER_AGENT_TYPE`：`opencode` 或 `claude-code`，默认 `opencode`。
- `MR_REVIEWER_AGENT_COMMAND`：Agent 可执行命令；为空时根据类型使用 `opencode` 或 `claude`。
- `MR_REVIEWER_LOG_LEVEL`：全局日志级别，取值 `OFF`、`INFO`、`DEBUG`，默认 `OFF`。`INFO` 只记录 API、Agent 和 WeLink 调用的任务、操作、耗时、状态码/返回码和内容长度；`DEBUG` 额外开启 Agent CLI 的 debug 参数并保存脱敏的本地诊断内容。
- `MR_REVIEWER_DEBUG_DIR`：`DEBUG` 本地诊断根目录，默认 `log/debug`。按 `YYYYMMDD/<task_id>/api`、`agent`、`im` 分目录保存；Agent 调用包含 `prompt.md`、`request.json`、`stdout.md`、`stderr.log`、`result.json`，API 内容写入独立 JSON。所有文件都会脱敏 GitLab token、`PRIVATE-TOKEN`、Authorization 和 Basic 凭据。
- `MR_REVIEWER_AGENT_DEBUG`、`MR_REVIEWER_AGENT_DIAGNOSTIC_DIR`：旧兼容配置。仅在未设置新变量时生效：`AGENT_DEBUG=true` 映射为 `MR_REVIEWER_LOG_LEVEL=DEBUG`，旧诊断目录映射为 `MR_REVIEWER_DEBUG_DIR`。
- `MR_REVIEWER_COMMENT_SKILL`：可选。配置后 prompt 会显式指定该 Agent skill 检视 MR；未配置时使用默认 `code-review` skill。自动入口要求该 skill 只输出结构化 JSON，不应自行提交评论。
- 旧 `MR_REVIEWER_OPENCODE_COMMAND`、`MR_REVIEWER_OPENCODE_DEBUG` 和 `MR_REVIEWER_OPENCODE_DIAGNOSTIC_DIR` 仅在 OpenCode 模式且未配置对应通用变量时作为兼容 fallback。
- `MR_REVIEWER_WEBHOOK_HOST`、`MR_REVIEWER_WEBHOOK_PORT`、`MR_REVIEWER_WEBHOOK_PATH`：webhook 服务监听地址，默认 `127.0.0.1:8080/webhook/gitlab`。本机自测使用 `127.0.0.1`；GitLab 从其他机器访问 `http://本机IP:8080/webhook/gitlab` 时，需要把 host 改为 `0.0.0.0` 或实际网卡 IP。路径是精确匹配，`/webhook/gitlab/` 会返回 404。完整配置见 [Webhook 快速开始](docs/WEBHOOK_QUICKSTART.md)。
- `MR_REVIEWER_WEBHOOK_SECRET`：可选。配置后校验 webhook secret header；未配置时允许请求但会输出 warning 日志。
- `MR_REVIEWER_WEBHOOK_SECRET_HEADER`：webhook secret header 名，默认 `X-Gitlab-Token`。CodeHub 等平台可改为实际 header，例如 `X-CodeHub-Token`。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT`：是否由 Python 侧发布 GitLab inline discussion，默认 `true`；设为 `false` 时只写本地 JSON 和 Markdown 报告。
- `MR_REVIEWER_REPORT_DIR`：webhook 本地监视报告目录，默认 `log/webhook-reports`。
- Agent 的 provider 和 model 当前不由本项目传参控制；具体 provider/model 由目标机器上的 OpenCode 或 Claude Code 配置、登录状态和环境变量决定。
- `MR_REVIEWER_WORK_DIR`：任务临时目录；为空时使用系统临时目录下的 `code-review`。
- `MR_REVIEWER_STATE_PATH`：本地状态文件路径，用于记录已处理消息。
- `MR_REVIEWER_MAX_FILES`：最大变更文件数，默认 `50`。
- `MR_REVIEWER_MAX_DIFF_LINES`：最大 diff 行数，默认 `2000`。
- `MR_REVIEWER_TASK_TIMEOUT_SECONDS`：单个 review 任务超时，默认 `900`。
- `MR_REVIEWER_POLL_INTERVAL_SECONDS`：常驻轮询间隔，默认 `15`。

默认 `MR_REVIEWER_LOG_LEVEL=OFF` 时不输出项目日志，也不会创建 `MR_REVIEWER_DEBUG_DIR`。`MR_REVIEWER_REPORT_DIR` 保存的是 webhook 业务审计结果、概要、finding 发布状态和失败阶段，不受日志级别影响；它与仅用于排障的 debug 目录职责不同。

WeLink poll 命令 stdout 需要返回 `query-history-message` 的原始 JSON，程序会读取 `respData.chatInfo`：

```json
{
  "respData": {
    "chatInfo": [
      {
        "at": true,
        "atAccountList": ["bot-example"],
        "content": "@Bot https://gitlab.example.com/team/project/merge_requests/7",
        "contentType": "TEXT_MSG",
        "groupId": "group-example",
        "msgId": 88863928388808372,
        "sender": "user-example",
        "serverSendTime": 1777278567776
      }
    ]
  },
  "resultCode": "0"
}
```

回发前会先上传 Markdown 报告文件，随后实际发送群通知：

```powershell
welink-cli onebox file-upload --space-id "<space-id>" --parent "<parent-id>" "<report-file>.md"
welink-cli im send-to-group --group-id "group-example" --text "代码审查报告已上传到 WeLink OneBox，群空间Review目录下: <report-file>.md"
```

## 日志

程序默认关闭项目日志。设置 `MR_REVIEWER_LOG_LEVEL=INFO` 后才输出标准日志；设置为 `DEBUG` 时会额外写入本地脱敏诊断文件。关键字段：

- `task`：单个 review 任务 ID。
- `message`：WeLink 消息 ID。
- `mr` / `repo` / `mr_iid`：GitLab MR 定位信息。
- `stage=webhook_server`：webhook 服务已启动，会记录 `host`、`port`、`path`。
- `stage=webhook_review` / `stage=webhook_report`：webhook 后台 review、本地 JSON 监视报告和 Markdown 报告写入。
- webhook 本地监视报告会记录 `submission_owner=python`、第一步 `summary`、`failure_stage`、结构化解析状态、finding 处理结果、`markdown_report_path` 以及 `posted`、`disabled`、`failed` 等 inline discussion 发布状态。
- `stage=im_poll`：开始调用 WeLink 历史消息查询。
- `stage=gitlab_api` / Agent / `stage=im_*`：记录调用方法、状态、耗时、返回码和内容长度，不记录请求或响应正文。完整且脱敏的内容只在 `DEBUG` 本地目录中保存。
- Windows 下如果 `welink-cli` 或 Agent command 解析到 `.cmd`/`.bat`，程序会通过 `cmd.exe /d /c call "<cmd路径>" ...` 执行。OpenCode 的完整 prompt 通过 UTF-8 文件附件传递，Claude Code 通过 stdin 传递，避免多行 argv 被截断。
- `status=messages_received`：本轮收到的消息数量。
- `reason=already_processed`：状态文件显示消息已处理。
- `reason=not_review_request`：消息不是 `@Bot + MR URL`。
- `stage=gitlab_fetch` / `stage=gitlab_ready`：获取 MR 元数据和仓库地址。
- `stage=diff_ready`：diff 已生成，会记录文件数和 diff 行数。
- `stage=summary`：第一次调用 Agent 并校验结构化 MR 概要。
- `stage=opencode_review` / `stage=report_ready`：把概要作为上下文调用 Agent review 并得到结构化 JSON；`run-once` 和 WeLink poll 会继续由 Python 渲染为 Markdown。
- `stage=file_upload` / `stage=file_upload_result`：上传 Markdown 报告文件到 WeLink OneBox。
- `stage=file_upload_failed`：报告已生成，但 OneBox 上传失败；程序会继续向群里发送失败提示。
- `stage=im_reply` / `stage=im_send`：准备和执行 WeLink 群通知。
- `stage=cleanup`：清理任务临时目录。

控制台日志不会输出 GitLab token、WeLink 原始正文、Agent prompt 或完整 Markdown 报告；需要排障时使用 `DEBUG` 的本地脱敏产物。

## 当前限制与待确认

- WeLink CLI 不支持直接发送 Markdown 长文本；当前先上传 Markdown 报告文件，再向群里发送文件名通知。
- 当前 URL 解析器不兼容 GitLab 标准 Web URL 中的 `/-/merge_requests/` 分隔符。
- WeLink 历史消息是否需要基于 `maxMsgId` 增量查询；当前依赖本地状态文件去重。
- WeLink CLI 可以发送私聊消息，但当前只实现群聊回发。
- webhook 不再提交整段 Markdown note；无法发布为 inline discussion 的 finding 只保留在本地 JSON 和 Markdown 报告中。

## 排障

- `healthcheck` 显示 `agent: missing`：确认所选 Agent 在 PATH 中，或设置 `MR_REVIEWER_AGENT_COMMAND`。
- Agent 运行时使用了错误 provider/model：检查目标机器上的 OpenCode 或 Claude Code 配置和登录状态；本项目不会覆盖 provider/model。
- 群里提示 `OneBox 上传失败`：检查 `MR_REVIEWER_WELINK_ONEBOX_SPACE_ID`、`MR_REVIEWER_WELINK_ONEBOX_PARENT_ID` 是否存在，以及当前 WeLink 账号是否有该目录上传权限。该错误不会阻塞 review 任务完成，但报告文件不会进入 OneBox。
- `GitLab API request failed`：检查 `MR_REVIEWER_GITLAB_API_BASE_URL` 是否包含完整 API 前缀、token 权限是否正确；MR Web URL 校验仍使用 `MR_REVIEWER_GITLAB_BASE_URL`。
- GitLab webhook 返回 404：先确认请求 URL 没有尾部 `/`，并确认服务启动日志里的 `host`、`port`、`path` 与 GitLab 配置一致。如果 GitLab 配置的是 `http://本机IP:8080/webhook/gitlab`，不要使用默认 `MR_REVIEWER_WEBHOOK_HOST=127.0.0.1`，改为 `0.0.0.0` 或实际网卡 IP 后重启。
- `git command failed`：检查 token 是否能 clone 目标仓库，以及 MR 的 base/head SHA 是否存在。
- 重复处理同一条消息：检查 `MR_REVIEWER_STATE_PATH` 是否可写、是否被删除。

## 验证命令

```powershell
uv run pytest
```

## 如何反馈

联系仓库 Owner
