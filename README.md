# code-mr-reviewer

IM 驱动的 GitLab MR Review 助手。MVP 流程是：WeLink CLI 轮询群历史消息，识别 `@Bot + GitLab MR URL`，clone GitLab target 仓库到临时目录，显式拉取 target/source 两个分支，checkout MR head 后在该本地 repo 目录调用 `opencode run` 输出 Markdown review 报告，最后通过 WeLink CLI 回发群消息。传给 opencode 的 prompt 只描述 review 任务、检视范围和本地代码仓目录，不内嵌完整 git diff。

## 当前支持

- 只生成 Markdown 检视报告。
- 只支持 GitLab MR URL。
- 只通过 HTTPS Token 访问 GitLab。
- 支持 WeLink 群历史消息轮询：`welink-cli im query-history-message`。
- 支持 WeLink 群消息回发：`welink-cli im send-to-group`。
- 支持 clone target repo，并显式 fetch `target_branch` 与 `source_branch`。
- 支持 fork MR：当 `source_project_id != target_project_id` 时，会添加 `source` remote 并从 source repo 拉取 source branch。
- 支持本地 JSON 状态文件去重，避免重复处理同一条 IM 消息。
- 支持资源限制：最大变更文件数、最大 diff 行数、任务超时时间。
- 不提交 MR 评论，不做分布式调度。

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
MR_REVIEWER_IM_POLL_COMMAND=welink-cli im query-history-message --group-id 1234567891011 --query-count 20
MR_REVIEWER_IM_REPLY_COMMAND=welink-cli im send-to-group
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
uv run mr-reviewer run-once https://gitlab.example.com/team/project/-/merge_requests/7
```

这一步不依赖 WeLink，只验证 GitLab clone、diff 和 opencode review。

实际 Git 流程：

1. clone target project 到任务临时目录。
2. fetch target branch。
3. 如果 source project 不同，添加 `source` remote。
4. fetch source branch。
5. checkout GitLab MR `diff_refs.head_sha`。
6. 用 `diff_refs.base_sha...diff_refs.head_sha` 生成 diff。
7. 在本地 repo 目录下调用 opencode，prompt 形如：`使用 mr-review skill 检视代码。检视范围：feature 到 dev 的差异。代码仓在 <repo> 目录。`

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
- `MR_REVIEWER_IM_POLL_COMMAND`：轮询 WeLink 群历史消息的命令，例如 `welink-cli im query-history-message --group-id 1234567891011 --query-count 20`。
- `MR_REVIEWER_IM_REPLY_COMMAND`：回发 WeLink 群消息的基础命令，例如 `welink-cli im send-to-group`；程序会追加 `--group-id <groupId> --text <Markdown>`。
- `MR_REVIEWER_BOT_MENTION`：触发用的 bot mention，默认 `@Bot`。
- `MR_REVIEWER_BOT_ACCOUNT`：WeLink bot 账号 ID；配置后会用 `atAccountList` 精确判断是否 @ 了机器人。
- `MR_REVIEWER_ALLOWED_GROUPS`、`MR_REVIEWER_ALLOWED_USERS`、`MR_REVIEWER_ALLOWED_REPOS`：逗号分隔白名单；为空表示不限制。
- `MR_REVIEWER_OPENCODE_COMMAND`：opencode 可执行命令，默认 `opencode`。
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
        "content": "@Bot https://gitlab.example.com/team/project/-/merge_requests/7",
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

回发时实际执行：

```powershell
welink-cli im send-to-group --group-id "619850427" --text "# Review..."
```

## 日志

程序使用标准输出/错误日志，默认 `INFO` 级别。关键字段：

- `task`：单个 review 任务 ID。
- `message`：WeLink 消息 ID。
- `mr` / `repo` / `mr_iid`：GitLab MR 定位信息。
- `stage=im_poll`：开始调用 WeLink 历史消息查询。
- `stage=git` / `stage=im_poll` / `stage=im_send` / `stage=opencode`：会打印实际执行命令；Git token、WeLink 正文和 opencode prompt 会脱敏。
- Windows 下如果 `welink-cli` 或 `opencode` 解析到 `.cmd`/`.bat`，程序会通过 `cmd.exe /d /s /c` 执行，避免 `subprocess` 直接调用批处理文件的兼容性问题。
- `status=messages_received`：本轮收到的消息数量。
- `reason=already_processed`：状态文件显示消息已处理。
- `reason=not_review_request`：消息不是 `@Bot + MR URL`。
- `stage=gitlab_fetch` / `stage=gitlab_ready`：获取 MR 元数据和仓库地址。
- `stage=diff_ready`：diff 已生成，会记录文件数和 diff 行数。
- `stage=opencode_review` / `stage=report_ready`：调用 AI review 并得到 Markdown。
- `stage=im_reply` / `stage=im_send`：准备和执行 WeLink 群回发。
- `stage=cleanup`：清理任务临时目录。

日志不会输出 GitLab token，也不会输出完整报告正文，只记录长度和定位字段。

## 当前不确定点

- WeLink CLI 是否支持 Markdown 长文本、最大长度是多少；当前直接通过 `--text` 发送完整报告。
- WeLink 历史消息是否需要基于 `maxMsgId` 增量查询；当前依赖本地状态文件去重。
- `welink-cli im send-to-user` 是否需要用于私聊回执；当前只回发群组。
- GitLab token 的权限范围需要在真实环境确认；至少需要 MR API 读取和仓库 HTTPS clone 权限。
- `opencode run` 的模型配置、鉴权和成本限制由本机 opencode 配置负责，当前项目只做健康检查和调用。
- Git for Windows Credential Manager 在部分环境会弹出账号密码窗口；当前 clone 命令会禁用 Git credential helper 和 GCM 交互，并通过 Git 环境配置注入 HTTPS token，不再依赖 `.cmd` askpass wrapper。

## 排障

- `healthcheck` 显示 `opencode: missing`：确认 `opencode` 在 PATH 中，或设置 `MR_REVIEWER_OPENCODE_COMMAND`。
- `GitLab API request failed`：检查 `MR_REVIEWER_GITLAB_BASE_URL`、token 权限和 MR URL 域名是否一致。
- `git command failed`：检查 token 是否能 clone 目标仓库，以及 MR 的 base/head SHA 是否存在。
- Git 弹出用户名/密码窗口：确认运行的是包含当前修复的版本；本项目内部 clone 不会调用交互式凭据窗口。如果弹窗仍出现，通常是外部脚本或旧安装版本绕过了 `mr_reviewer.git.GitClient`。
- 中文或 emoji 乱码：确认运行的是当前版本；WeLink、Git、opencode 子进程都显式使用 UTF-8 解码，并对非法字节使用替换策略保留日志可读性。
- `IM poll command failed`：单独运行 `MR_REVIEWER_IM_POLL_COMMAND`，确认 stdout 是合法 JSON。
- `IM reply command failed`：单独运行 `welink-cli im send-to-group --group-id <id> --text "test"`。
- 重复处理同一条消息：检查 `MR_REVIEWER_STATE_PATH` 是否可写、是否被删除。

## 验证命令

```powershell
uv run pytest
```
