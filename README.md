# code-mr-reviewer

IM 驱动的 GitLab MR Review 助手。MVP 流程是：WeLink CLI 轮询群历史消息，识别 `@Bot + GitLab MR URL`，clone GitLab target 仓库到临时目录，显式拉取 target/source 两个分支，checkout MR head 后在该本地 repo 目录调用 `opencode run` 输出 Markdown review 报告，最后上传 Markdown 报告文件，并通过 WeLink CLI 向群里发送文件名通知。传给 opencode 的 prompt 只描述 review 任务、检视范围和本地代码仓目录，不内嵌完整 git diff。

## 当前支持

- 只生成 Markdown 检视报告，并通过文件上传方式交付。
- 只支持 GitLab MR URL。
- 只通过 HTTPS Token 访问 GitLab。
- 支持 WeLink 群历史消息轮询：`welink-cli im query-history-message`。
- 支持 WeLink 群消息通知：先上传报告文件，再通过 `welink-cli im send-to-group` 发送文件名。
- 支持 clone target repo，并显式 fetch `target_branch` 与 `source_branch`。
- 支持 fork MR：当 `source_project_id != target_project_id` 时，会添加 `source` remote 并从 source repo 拉取 source branch。
- 支持本地 JSON 状态文件去重，避免重复处理同一条 IM 消息。
- 支持资源限制：最大变更文件数、最大 diff 行数、任务超时时间。
- 当前只支持一个 WeLink 群 ID，不提交 MR 评论，不做分布式调度。

## 启动步骤

1. 安装运行依赖：

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
MR_REVIEWER_WELINK_GROUP_ID=1234567891011
MR_REVIEWER_BOT_MENTION=@Bot
MR_REVIEWER_BOT_ACCOUNT=l00808734
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
7. 在本地 repo 目录下调用 opencode，prompt 形如：`使用 code-review skill 检视代码。检视范围：feature 到 dev 的差异。代码仓在 <repo> 目录。`

5. 单轮验证 WeLink 轮询：

```powershell
uv run mr-reviewer poll --once
```

6. 常驻轮询：

```powershell
uv run mr-reviewer poll
```

## 配置

复制 `.env.example` 为 `.env`，按需配置：

- `MR_REVIEWER_GITLAB_BASE_URL`：GitLab 根地址，例如 `https://gitlab.example.com`。
- `MR_REVIEWER_GITLAB_TOKEN`：GitLab token，用于 MR API 和 HTTPS clone。
- `MR_REVIEWER_IM_POLL_COMMAND`：轮询 WeLink 群历史消息的基础命令，例如 `welink-cli im query-history-message --query-count 20`；程序会追加 `--group-id <MR_REVIEWER_WELINK_GROUP_ID>`。
- `MR_REVIEWER_IM_REPLY_COMMAND`：发送 WeLink 群通知的基础命令，例如 `welink-cli im send-to-group`；程序会追加 `--group-id <MR_REVIEWER_WELINK_GROUP_ID> --text <文件名通知>`。
- `MR_REVIEWER_WELINK_GROUP_ID`：当前唯一支持的 WeLink 群 ID，轮询历史消息和发送群通知都会使用这个值。
- `MR_REVIEWER_BOT_MENTION`：触发用的 bot mention，默认 `@Bot`。
- `MR_REVIEWER_BOT_ACCOUNT`：WeLink bot 账号 ID；配置后会用 `atAccountList` 精确判断是否 @ 了机器人。
- `MR_REVIEWER_ALLOWED_GROUPS`、`MR_REVIEWER_ALLOWED_USERS`、`MR_REVIEWER_ALLOWED_REPOS`：逗号分隔白名单；为空表示不限制。
- `MR_REVIEWER_OPENCODE_COMMAND`：opencode 可执行命令，默认 `opencode`。
- `MR_REVIEWER_OPENCODE_DEBUG`：是否以 debug 模式调用 opencode，默认 `true`。开启时实际使用 `opencode --print-logs --log-level DEBUG run <prompt>`。
- opencode 的 provider 和 model 当前不由本项目传参控制；本项目只调用 opencode CLI，具体 provider/model 由目标机器上的 opencode 配置、登录状态、环境变量或 opencode 默认规则决定。
- OneBox 上传的 `space-id=16220079` 和 `parent=763` 当前写在代码中，不由环境变量配置。
- `MR_REVIEWER_WORK_DIR`：任务临时目录；为空时使用系统临时目录下的 `mr-review`。
- `MR_REVIEWER_STATE_PATH`：本地状态文件路径，用于记录已处理消息。
- `MR_REVIEWER_MAX_FILES`：最大变更文件数，默认 `50`。
- `MR_REVIEWER_MAX_DIFF_LINES`：最大 diff 行数，默认 `2000`。
- `MR_REVIEWER_TASK_TIMEOUT_SECONDS`：单个 review 任务超时，默认 `900`。
- `MR_REVIEWER_POLL_INTERVAL_SECONDS`：常驻轮询间隔，默认 `15`。

WeLink poll 命令 stdout 需要返回 `query-history-message` 的原始 JSON，程序会读取 `respData.chatInfo`：

```json
{
  "respData": {
    "chatInfo": [
      {
        "at": true,
        "atAccountList": ["l00808734"],
        "content": "@Bot https://gitlab.example.com/team/project/merge_requests/7",
        "contentType": "TEXT_MSG",
        "groupId": 619850427,
        "msgId": 88863928388808372,
        "sender": "d00808710",
        "serverSendTime": 1777278567776
      }
    ]
  },
  "resultCode": "0"
}
```

回发前会先上传 Markdown 报告文件，随后实际发送群通知：

```powershell
welink-cli onebox file-upload --space-id 16220079 --parent 763 "<report-file>.md"
welink-cli im send-to-group --group-id "619850427" --text "代码审查报告已上传到 WeLink OneBox，群空间Review目录下: <report-file>.md"
```

## 日志

程序使用标准输出/错误日志，默认 `INFO` 级别。关键字段：

- `task`：单个 review 任务 ID。
- `message`：WeLink 消息 ID。
- `mr` / `repo` / `mr_iid`：GitLab MR 定位信息。
- `stage=im_poll`：开始调用 WeLink 历史消息查询。
- `stage=git` / `stage=im_poll` / `stage=im_send` / `stage=opencode`：会打印实际执行命令；Git token、WeLink 正文和 opencode prompt 会脱敏。
- Windows 下如果 `welink-cli` 或 `opencode` 解析到 `.cmd`/`.bat`，程序会通过 `cmd.exe /d /c call "<cmd路径>" ...` 执行，避免 `subprocess` 直接调用批处理文件的兼容性问题。
- `status=messages_received`：本轮收到的消息数量。
- `reason=already_processed`：状态文件显示消息已处理。
- `reason=not_review_request`：消息不是 `@Bot + MR URL`。
- `stage=gitlab_fetch` / `stage=gitlab_ready`：获取 MR 元数据和仓库地址。
- `stage=diff_ready`：diff 已生成，会记录文件数和 diff 行数。
- `stage=opencode_review` / `stage=report_ready`：调用 AI review 并得到 Markdown。
- `stage=file_upload` / `stage=file_upload_result`：上传 Markdown 报告文件到 WeLink OneBox。
- `stage=im_reply` / `stage=im_send`：准备和执行 WeLink 群通知。
- `stage=cleanup`：清理任务临时目录。

日志不会输出 GitLab token、WeLink 原始正文和 opencode prompt。注意：当前 `poll` 成功路径会在 `stage=report_content` 输出完整 Markdown 报告正文；如果生产环境不允许日志包含报告正文，需要先调整代码。

## 当前限制与待确认

- WeLink CLI 不支持直接发送 Markdown 长文本；当前先上传 Markdown 报告文件，再向群里发送文件名通知。
- OneBox 目录参数当前硬编码，暂不支持按环境切换空间或目录。
- 当前 URL 解析器不兼容 GitLab 标准 Web URL 中的 `/-/merge_requests/` 分隔符。
- WeLink 历史消息是否需要基于 `maxMsgId` 增量查询；当前依赖本地状态文件去重。
- WeLink CLI 可以发送私聊消息，但当前只实现群聊回发。

## 排障

- `healthcheck` 显示 `opencode: missing`：确认 `opencode` 在 PATH 中，或设置 `MR_REVIEWER_OPENCODE_COMMAND`。
- opencode 运行时使用了错误 provider/model：先检查目标机器上的 opencode 配置和登录状态；本项目当前不会覆盖 provider/model。
- `GitLab API request failed`：检查 `MR_REVIEWER_GITLAB_BASE_URL`、token 权限和 MR URL 域名是否一致。
- `git command failed`：检查 token 是否能 clone 目标仓库，以及 MR 的 base/head SHA 是否存在。
- 重复处理同一条消息：检查 `MR_REVIEWER_STATE_PATH` 是否可写、是否被删除。

## 验证命令

```powershell
uv run pytest
```
