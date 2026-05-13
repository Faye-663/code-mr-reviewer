# code-mr-reviewer

GitLab/CodeHub MR Review 助手，支持 IM 轮询和 Webhook 两种触发方式。

**IM 轮询路径**：WeLink CLI 轮询群历史消息，识别 `@Bot + GitLab MR URL`，clone 仓库到临时目录，fetch target/source 分支，checkout MR head 后调用 `opencode run` 生成 Markdown review 报告，最后上传报告文件并通过 WeLink CLI 发送群通知。

**Webhook 路径**：启动 HTTP 服务器，接收 CodeHub Merge Request Hook 事件，解析 payload 触发规则后自动运行 review，将报告以 MR Comment 形式回写到 CodeHub/GitLab。

## 项目优势

- **IM 轮询模式**：在 WeLink 群聊中通过 `@Bot` 实现 AI 自动检视，自动输出 CodeReview 报告并回复群聊。
- **Webhook 模式**：接收 CodeHub Merge Request Hook，MR 创建/重开/源分支有新提交时自动触发审查，报告以 MR Comment 形式回写。
- 在本地 clone 完整代码仓，对比 target/source 两个分支，可读取更多代码上下文与代码仓的 Skill，提升 Review 质量。

## 当前支持

- 生成 Markdown 检视报告，支持文件上传（WeLink OneBox）或 MR Comment 回写两种交付方式。
- 通过 HTTPS Token 访问 GitLab API（MR 元数据查询、项目信息查询、MR Comment 回写）。
- 支持 WeLink IM 轮询触发：`welink-cli im query-history-message`。
- 支持 CodeHub Webhook 触发：接收 Merge Request Hook，按 `action` + `update_reason` 判断触发策略。
- 支持 WeLink 群消息通知：先上传报告文件，再通过 `welink-cli im send-to-group` 发送文件名。
- 支持资源限制：最大变更文件数、最大 diff 行数、任务超时时间。
- 当前只支持一个 WeLink 群 ID，不做分布式调度。

## 依赖条件

- Python `>=3.11`。
- `uv`，用于安装和运行本项目的 Python 包。
- `git`，用于 clone 目标 GitLab 仓库、fetch MR 分支和生成 diff。
- 可访问目标 GitLab 的网络环境，以及具备读取 MR API 和 HTTPS clone 权限的 GitLab Token。
- `opencode` CLI：必须已安装并可在 PATH 中找到；如果不在 PATH 中，需要通过 `MR_REVIEWER_OPENCODE_COMMAND` 指定可执行命令。
- `welink-cli`：必须已安装并完成登录或授权，且当前账号需要能调用 `im query-history-message`、`im send-to-group` 和 `onebox file-upload`。
- LLM provider、model 和 API Key 不由本项目直接配置。本项目只调用 `opencode run`，具体 LLM 配置由目标机器上的 opencode 配置、登录状态、环境变量或 opencode 默认规则决定。如果 `healthcheck` 通过但 review 阶段失败，先在同一台机器上用独立的 `opencode run` 命令验证 LLM 配置。
- `opencode` 安装了 `codehub-mr-review` skill，见`.opencode`目录

## 启动步骤

1. 安装 Python 依赖并确认外部命令可用：

```powershell
uv sync
git --version
opencode --version
welink-cli --help
```

2. 配置环境变量：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，至少填入：

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

3. 执行健康检查：

```powershell
uv run mr-reviewer healthcheck
```

所有项都应为 `ok`。如果某项为 `missing`，先补齐对应命令或配置。

4. 单次验证 GitLab MR：

```powershell
uv run mr-reviewer run-once https://gitlab.example.com/team/project/merge_requests/7
```

这一步不依赖 WeLink，只验证 GitLab clone、diff 和 opencode review。当前 URL 解析以 `/merge_requests/` 为分隔符，示例使用不带 `/-/` 的形式；如果直接粘贴 GitLab Web 页面常见的 `/-/merge_requests/7`，当前版本会把 `-` 解析进项目路径。

实际 Git 流程：

1. clone target project 到任务临时目录。
2. fetch target branch。
3. 如果 source project 不同，添加 `source` remote。
4. fetch source branch。
5. checkout GitLab MR `diff_refs.head_sha`。
6. 用 `diff_refs.base_sha...diff_refs.head_sha` 生成 diff。
7. 在本地 repo 目录下调用 opencode，prompt 形如：`使用 codehub-mr-review skill 检视代码。MR URL: <mr-url>，Base SHA: <base-sha>，Head SHA: <head-sha>。代码仓在 <repo> 目录。`

5. 单轮验证 WeLink 轮询：

```powershell
uv run mr-reviewer poll --once
```

6. 常驻轮询：

```powershell
uv run mr-reviewer poll
```

7. 启动 Webhook 服务器（可选）：

```powershell
uv run mr-reviewer webhook --port 8080
```

启动后配置 CodeHub 项目的 Webhook URL 指向 `http://<host>:8080/`（生产环境建议前面放置 nginx/Caddy 反向代理以 terminate TLS）。触发策略：

| action | update_reason | 触发审查 |
|--------|---------------|----------|
| `open` | — | 是 |
| `reopen` | — | 是 |
| `update` | `source update` | 是（源分支有新提交） |
| `update` | `mr update` | 否（仅元数据变更） |
| `merge` / `close` / `stop` | — | 否 |

有冲突（`conflict: true`）的 MR 会被跳过。可配置 `MR_REVIEWER_ALLOWED_REPOS` 白名单限制审查范围。

## opencode skill 直接使用

除了现有 WeLink 自动轮询模式，也可以在 opencode 中按需直接使用 `.opencode/skills/gitlab-mr-review`。这个能力不替代现有 WeLink 自动轮询模式；它适合人工触发单个 GitLab MR 检视，并可把 Markdown 报告评论到现有 MR。

推荐在 opencode 中输入：

```text
使用 gitlab-mr-review skill 检视并评论这个 MR：
https://gitlab.example.com/team/project/merge_requests/7
```

该 skill 会调用内置脚本完成 clone/fetch/checkout、调用 `code-review skill` 生成报告，并在默认情况下通过 GitLab API 提交 MR comment。使用前需要配置：

```powershell
$env:GITLAB_BASE_URL = "https://gitlab.example.com"
$env:GITLAB_TOKEN = "your-gitlab-token"
# 可选：只生成本地报告，不提交 MR comment
$env:MR_REVIEW_SUBMIT_COMMENT = "false"
# .env 风格等价配置：MR_REVIEW_SUBMIT_COMMENT=false
```

可选环境变量：

- `OPENCODE_COMMAND`：opencode 可执行命令，默认 `opencode`。
- `MR_REVIEW_WORK_DIR`：临时 clone 和报告输出目录，默认系统临时目录下的 `gitlab-mr-review`。
- `MR_REVIEW_SUBMIT_COMMENT`：默认 `true`；设置为 `false` 时只输出本地 Markdown 报告路径。

opencode 的 provider/model 仍由 opencode 自身配置、登录状态和环境变量决定；该 skill 不覆盖模型配置。

## 配置

复制 `.env.example` 为 `.env`，按需配置：

- `MR_REVIEWER_GITLAB_BASE_URL`：GitLab 根地址，例如 `https://gitlab.example.com`。
- `MR_REVIEWER_GITLAB_TOKEN`：GitLab token，用于 MR API 和 HTTPS clone。
- `MR_REVIEWER_IM_POLL_COMMAND`：轮询 WeLink 群历史消息的基础命令，例如 `welink-cli im query-history-message --query-count 20`；程序会追加 `--group-id <MR_REVIEWER_WELINK_GROUP_ID>`。
- `MR_REVIEWER_IM_REPLY_COMMAND`：发送 WeLink 群通知的基础命令，例如 `welink-cli im send-to-group`；程序会追加 `--group-id <MR_REVIEWER_WELINK_GROUP_ID> --text <文件名通知>`。
- `MR_REVIEWER_WELINK_GROUP_ID`：当前唯一支持的 WeLink 群 ID，轮询历史消息和发送群通知都会使用这个值。
- `MR_REVIEWER_WELINK_ONEBOX_SPACE_ID`：WeLink OneBox 上传目标 `space-id`。
- `MR_REVIEWER_WELINK_ONEBOX_PARENT_ID`：WeLink OneBox 上传目标 `parent` 目录 ID。若 `space-id` 或 `parent` 不存在、无权限或未配置，程序会向群里提示 OneBox 上传失败，但不会把当前 review 任务标记为失败。
- `MR_REVIEWER_BOT_MENTION`：触发用的 bot mention，默认 `@Bot`。
- `MR_REVIEWER_BOT_ACCOUNT`：WeLink bot 账号 ID；配置后会用 `atAccountList` 精确判断是否 @ 了机器人。
- `MR_REVIEWER_ALLOWED_GROUPS`、`MR_REVIEWER_ALLOWED_USERS`、`MR_REVIEWER_ALLOWED_REPOS`：逗号分隔白名单；为空表示不限制。
- `MR_REVIEWER_OPENCODE_COMMAND`：opencode 可执行命令，默认 `opencode`。
- `MR_REVIEWER_OPENCODE_DEBUG`：是否以 debug 模式调用 opencode，默认 `true`。开启时实际使用 `opencode --print-logs --log-level DEBUG run <prompt>`。
- `MR_REVIEWER_OPENCODE_DIAGNOSTIC_DIR`：opencode 诊断输出目录；为空时不输出诊断文件。开启后每次调用会写入完整 `prompt.md`、实际 `cwd.txt`、脱敏 `command.txt`、`env-summary.json`、`stdout.md`、`stderr.log` 和 `returncode.txt`。
- `MR_REVIEWER_OPENCODE_PROMPT_TRANSPORT`：prompt 传输方式，默认 `argument`。设置为 `file` 时会写出 `prompt.md` 并调用 `opencode run --file <prompt.md> "请读取附件 prompt.md，并严格按其中内容执行代码审查。"`，用于验证命令行参数传输和文件附件传输的差异。
- opencode 的 provider 和 model 当前不由本项目传参控制；本项目只调用 opencode CLI，具体 provider/model 由目标机器上的 opencode 配置、登录状态、环境变量或 opencode 默认规则决定。
- `MR_REVIEWER_WORK_DIR`：任务临时目录；为空时使用系统临时目录下的 `code-review`。
- `MR_REVIEWER_STATE_PATH`：本地状态文件路径，用于记录已处理消息。
- `MR_REVIEWER_MAX_FILES`：最大变更文件数，默认 `50`。
- `MR_REVIEWER_MAX_DIFF_LINES`：最大 diff 行数，默认 `2000`。
- `MR_REVIEWER_TASK_TIMEOUT_SECONDS`：单个 review 任务超时，默认 `900`。
- `MR_REVIEWER_POLL_INTERVAL_SECONDS`：常驻轮询间隔，默认 `15`。
- `MR_REVIEWER_WEBHOOK_SECRET`：Webhook 校验密钥。为空时跳过校验（适合仅内网使用）。配置后要求请求 header 中携带匹配的密钥。
- `MR_REVIEWER_WEBHOOK_SECRET_HEADER`：携带 webhook 密钥的 HTTP header 名，默认 `X-CodeHub-Token`。
- `MR_REVIEWER_WEBHOOK_HOST`：Webhook 服务器绑定地址，默认 `127.0.0.1`。
- `MR_REVIEWER_WEBHOOK_PORT`：Webhook 服务器绑定端口，默认 `8080`。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT`：是否将 review 报告回写为 MR Comment，默认 `true`。设为 `false` 时只生成报告但不回写。

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

程序使用标准输出/错误日志，默认 `INFO` 级别。关键字段：

- `task`：单个 review 任务 ID。
- `message`：WeLink 消息 ID。
- `mr` / `repo` / `mr_iid`：GitLab MR 定位信息。
- `stage=im_poll`：开始调用 WeLink 历史消息查询。
- `stage=git` / `stage=im_poll` / `stage=im_send` / `stage=opencode`：会打印实际执行命令；Git token、WeLink 正文和 opencode prompt 会脱敏。opencode 日志会额外记录 `prompt_transport`、`prompt_chars`、`prompt_sha256`、`mr_url_present` 和 `diagnostic_path`。
- Windows 下如果 `welink-cli` 或 `opencode` 解析到 `.cmd`/`.bat`，程序会通过 `cmd.exe /d /c call "<cmd路径>" ...` 执行，避免 `subprocess` 直接调用批处理文件的兼容性问题。
- `status=messages_received`：本轮收到的消息数量。
- `reason=already_processed`：状态文件显示消息已处理。
- `reason=not_review_request`：消息不是 `@Bot + MR URL`。
- `stage=gitlab_fetch` / `stage=gitlab_ready`：获取 MR 元数据和仓库地址。
- `stage=diff_ready`：diff 已生成，会记录文件数和 diff 行数。
- `stage=opencode_review` / `stage=report_ready`：调用 AI review 并得到 Markdown。
- `stage=file_upload` / `stage=file_upload_result`：上传 Markdown 报告文件到 WeLink OneBox。
- `stage=file_upload_failed`：报告已生成，但 OneBox 上传失败；程序会继续向群里发送失败提示。
- `stage=im_reply` / `stage=im_send`：准备和执行 WeLink 群通知。
- `stage=cleanup`：清理任务临时目录。
- `stage=comment_posted`：MR Comment 回写成功。
- Webhook 路径日志使用 `webhook task=<task_id>` 前缀，包含 `mr=<project>/<iid>` 定位和 `status=started/success/failed` 状态。

日志不会输出 GitLab token、WeLink 原始正文和 opencode prompt。注意：当前 `poll` 成功路径会在 `stage=report_content` 输出完整 Markdown 报告正文；如果生产环境不允许日志包含报告正文，需要先调整代码。

## 当前限制与待确认

- WeLink CLI 不支持直接发送 Markdown 长文本；当前先上传 Markdown 报告文件，再向群里发送文件名通知。
- 当前 MR URL 解析器不兼容 GitLab 标准 Web URL 中的 `/-/merge_requests/` 分隔符。
- WeLink 历史消息是否需要基于 `maxMsgId` 增量查询；当前依赖本地状态文件去重。
- WeLink CLI 可以发送私聊消息，但当前只实现群聊回发。
- Webhook 模式下每个审查在后台线程执行，无内置队列或并发上限；高并发场景需要外部限流。
- Webhook 不支持 `conflict: true` 的 MR（冲突代码无法审查），需先解决冲突再重新触发。

## 排障

- `healthcheck` 显示 `opencode: missing`：确认 `opencode` 在 PATH 中，或设置 `MR_REVIEWER_OPENCODE_COMMAND`。
- opencode 运行时使用了错误 provider/model：先检查目标机器上的 opencode 配置和登录状态；本项目当前不会覆盖 provider/model。
- 群里提示 `OneBox 上传失败`：检查 `MR_REVIEWER_WELINK_ONEBOX_SPACE_ID`、`MR_REVIEWER_WELINK_ONEBOX_PARENT_ID` 是否存在，以及当前 WeLink 账号是否有该目录上传权限。该错误不会阻塞 review 任务完成，但报告文件不会进入 OneBox。
- `GitLab API request failed`：检查 `MR_REVIEWER_GITLAB_BASE_URL`、token 权限和 MR URL 域名是否一致。
- `git command failed`：检查 token 是否能 clone 目标仓库，以及 MR 的 base/head SHA 是否存在。
- 重复处理同一条消息：检查 `MR_REVIEWER_STATE_PATH` 是否可写、是否被删除。
- Webhook 服务器无法启动：检查绑定地址和端口是否被占用；若端口 < 1024 可能需要管理员权限。
- Webhook 请求返回 `403 invalid token`：检查 `MR_REVIEWER_WEBHOOK_SECRET` 与 CodeHub Webhook 配置的 Secret Token 是否一致，以及 `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 是否匹配。
- Webhook 请求返回 `400`：检查请求 Content-Type 是否为 `application/json`，body 是否为合法 JSON。
- Webhook 返回 `200 ignored`：检查 `object_kind` 是否为 `merge_request`、`action` 和 `update_reason` 是否在触发策略内、MR 是否有冲突、`MR_REVIEWER_ALLOWED_REPOS` 白名单是否匹配。
- Webhook 审查成功但 MR Comment 未出现：检查 `MR_REVIEWER_WEBHOOK_POST_COMMENT` 是否为 `true`、token 是否有该项目 MR 的写权限。
- 生产部署 HTTPS：当前 HTTP 服务器不支持 TLS，需要在前面放置 nginx/Caddy 等反向代理处理 TLS 终止。

## 验证命令

```powershell
uv run pytest
```

## 如何反馈

联系仓库 Owner
