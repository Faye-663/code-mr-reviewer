# Webhook 快速开始

这份文档面向只使用 GitLab Merge Request webhook 的用户。你不需要配置 WeLink IM poll，也不需要 `welink-cli`。

## 适用场景

- GitLab 在 MR 打开、重新打开或 source branch 更新时主动回调本机服务。
- 服务收到 webhook 后在后台执行 two-step review：先生成 MR 概要，再把概要作为第二步 code review 上下文，并把两步结果写入 `MR_REVIEWER_REPORT_DIR`。
- Python 侧会把高置信、可定位的 finding 发布为 GitLab inline discussion；也可以关闭自动发布，只保留本地报告。

## 最小配置

先复制配置文件：

```powershell
Copy-Item .env.example .env
```

只使用 webhook 时，至少配置这些变量：

```env
MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com
MR_REVIEWER_GITLAB_API_BASE_URL=https://api.example.com/api/api/v4
MR_REVIEWER_GITLAB_TOKEN=your-gitlab-token

MR_REVIEWER_AGENT_TYPE=opencode
MR_REVIEWER_AGENT_COMMAND=opencode
MR_REVIEWER_LOG_LEVEL=OFF
MR_REVIEWER_DEBUG_DIR=log/debug

MR_REVIEWER_WEBHOOK_HOST=0.0.0.0
MR_REVIEWER_WEBHOOK_PORT=8080
MR_REVIEWER_WEBHOOK_PATH=/webhook/gitlab
MR_REVIEWER_WEBHOOK_SECRET=your-webhook-secret
MR_REVIEWER_WEBHOOK_SECRET_HEADER=X-Gitlab-Token
MR_REVIEWER_WEBHOOK_POST_COMMENT=true
MR_REVIEWER_REPORT_DIR=log/webhook-reports
```

说明：

- `MR_REVIEWER_GITLAB_BASE_URL` 用于 MR Web URL 校验；`MR_REVIEWER_GITLAB_API_BASE_URL` 是完整 REST API 根地址。后者为空时回退为 `<GitLab根地址>/api/v4`。
- `MR_REVIEWER_WEBHOOK_HOST` 是服务监听地址。本机自测可用 `127.0.0.1`；GitLab 从其他机器访问本机 IP 时，使用 `0.0.0.0` 或实际网卡 IP。
- `MR_REVIEWER_WEBHOOK_SECRET` 可为空；配置后会校验 `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 指定的请求头，默认是 `X-Gitlab-Token`。
- `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 可按平台调整，例如 CodeHub 使用 `X-CodeHub-Token` 时改成该值。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT=false` 时不会发布 inline discussion，只写本地 JSON 监视报告和 Markdown review 报告。
- `MR_REVIEWER_COMMENT_SKILL` 仍可选用于指定 review prompt skill；该 skill 必须只输出结构化 JSON，不要配置会自行提交评论的 skill。
- MR 概要只保存在本地 JSON/Markdown 报告中，不会发布到 GitLab；线上仅发布第二步产生且满足条件的 review finding。
- `MR_REVIEWER_LOG_LEVEL` 默认 `OFF`，不会输出项目日志或创建 debug 文件。设为 `INFO` 时只记录 API、Agent 调用元数据；设为 `DEBUG` 时会把脱敏后的请求、响应、prompt 和 Agent 输出写到 `MR_REVIEWER_DEBUG_DIR/YYYYMMDD/<task_id>/`。常规 webhook 审计仍使用 `MR_REVIEWER_REPORT_DIR`，它不受日志级别影响。

## 启动服务

```powershell
uv run mr-reviewer webhook
```

启动日志会包含：

```text
stage=webhook_server status=started host=0.0.0.0 port=8080 path=/webhook/gitlab
```

如果端口被占用，改 `MR_REVIEWER_WEBHOOK_PORT` 后重启。

## GitLab 配置

在 GitLab project 的 Webhooks 中配置：

```text
URL: http://本机IP:8080/webhook/gitlab
Secret token: your-webhook-secret
Trigger: Merge request events
```

注意：

- URL 中的 host 应该是 GitLab 能访问到的机器 IP、域名或反向代理地址，不是 `0.0.0.0`。
- 路径必须和 `MR_REVIEWER_WEBHOOK_PATH` 完全一致。默认是 `/webhook/gitlab`，不要写成 `/webhook/gitlab/`。
- 如果通过 nginx、Caddy 等反向代理转发，GitLab URL 使用代理域名，后端服务可以继续监听 `127.0.0.1`。

## 快速自测

服务启动后，可以先发一个非 MR 事件验证路径、方法和 secret 是否正确。这个请求不会触发 review，预期返回 `{"status":"skipped"}`。

```powershell
Invoke-WebRequest `
  -Method POST `
  -Uri "http://127.0.0.1:8080/webhook/gitlab" `
  -Headers @{ "X-Gitlab-Token" = "your-webhook-secret" } `
  -ContentType "application/json" `
  -Body '{"object_kind":"push"}'
```

如果使用最小 MR payload 自测，可处理事件会返回 `202 accepted`，随后后台任务会尝试 clone、diff、Agent 概要生成和 Agent review，并在 `MR_REVIEWER_REPORT_DIR` 写入同 stem 的 `.json` 监视报告和 `.md` review 报告；当 `MR_REVIEWER_WEBHOOK_POST_COMMENT=true` 时还会发布可定位 finding 的 inline discussion。

## 常见问题

- 返回 `404 NOT_FOUND`：请求路径和 `MR_REVIEWER_WEBHOOK_PATH` 不一致。默认路径是 `/webhook/gitlab`，尾部多一个 `/` 会返回 404。
- 访问 `http://本机IP:8080/webhook/gitlab` 连接失败：服务可能仍监听 `127.0.0.1`。把 `MR_REVIEWER_WEBHOOK_HOST` 改为 `0.0.0.0` 或实际网卡 IP 后重启。
- 返回 `401 WEBHOOK_TOKEN_MISSING`：已配置 `MR_REVIEWER_WEBHOOK_SECRET`，但请求没有 `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 指定的 header。
- 返回 `403 WEBHOOK_TOKEN_INVALID`：GitLab Secret token 和 `MR_REVIEWER_WEBHOOK_SECRET` 不一致。
- 返回 `200 skipped`：请求已到达服务，但事件不是可处理的 MR open、reopen 或 source update 事件。
- review 成功但 MR 没有 inline discussion：检查 `MR_REVIEWER_WEBHOOK_POST_COMMENT` 是否为 `true`，`MR_REVIEWER_GITLAB_TOKEN` 是否有读取 MR diff 与提交 discussion 的权限，并查看本地 `.json`/`.md` 报告中的 finding 是否被过滤、无法定位或判定为重复。
- 本地报告失败：查看 JSON/Markdown 中的 `failure_stage`。`summary` 表示第一步概要失败且未进入 review；`review` 表示第二步失败，报告仍会保留已生成的概要。
