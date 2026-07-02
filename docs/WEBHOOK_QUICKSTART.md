# Webhook 快速开始

这份文档面向只使用 GitLab Merge Request webhook 的用户。你不需要配置 WeLink IM poll，也不需要 `welink-cli`。

## 适用场景

- GitLab 在 MR 打开、重新打开或 source branch 更新时主动回调本机服务。
- 服务收到 webhook 后在后台执行 review，并把本地监视报告写入 `MR_REVIEWER_REPORT_DIR`。
- MR 评论暂时由 `MR_REVIEWER_COMMENT_SKILL` 指定的 opencode skill 脚本提交；Python 侧负责启动 review、记录成功或失败报告。

## 最小配置

先复制配置文件：

```powershell
Copy-Item .env.example .env
```

只使用 webhook 时，至少配置这些变量：

```env
MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com
MR_REVIEWER_GITLAB_TOKEN=your-gitlab-token

MR_REVIEWER_OPENCODE_COMMAND=opencode
MR_REVIEWER_COMMENT_SKILL=gitlab-mr-review

MR_REVIEWER_WEBHOOK_HOST=0.0.0.0
MR_REVIEWER_WEBHOOK_PORT=8080
MR_REVIEWER_WEBHOOK_PATH=/webhook/gitlab
MR_REVIEWER_WEBHOOK_SECRET=your-webhook-secret
MR_REVIEWER_REPORT_DIR=log/webhook-reports
```

如果使用仓库内置的 `gitlab-mr-review` 作为 `MR_REVIEWER_COMMENT_SKILL`，还需要在启动 webhook 的同一个 shell 中设置该 skill 读取的环境变量：

```powershell
$env:GITLAB_BASE_URL = "https://gitlab.example.com"
$env:GITLAB_TOKEN = "your-gitlab-token"
```

说明：

- `MR_REVIEWER_WEBHOOK_HOST` 是服务监听地址。本机自测可用 `127.0.0.1`；GitLab 从其他机器访问本机 IP 时，使用 `0.0.0.0` 或实际网卡 IP。
- `MR_REVIEWER_WEBHOOK_SECRET` 可为空；配置后会校验 GitLab 请求头 `X-Gitlab-Token`。
- `MR_REVIEWER_COMMENT_SKILL` 是 webhook 模式必填项。缺失时服务会拒绝启动，或对可处理事件返回 `COMMENT_SKILL_REQUIRED`。
- `.env` 中的 `MR_REVIEWER_GITLAB_BASE_URL` / `MR_REVIEWER_GITLAB_TOKEN` 只供 Python 侧读取；opencode 子进程中的 `gitlab-mr-review` skill 需要继承 shell 环境里的 `GITLAB_BASE_URL` / `GITLAB_TOKEN`。

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

如果使用最小 MR payload 自测，可处理事件会返回 `202 accepted`，随后后台任务会尝试 clone、diff、opencode review，并在 `MR_REVIEWER_REPORT_DIR` 写入监视报告。

## 常见问题

- 返回 `404 NOT_FOUND`：请求路径和 `MR_REVIEWER_WEBHOOK_PATH` 不一致。默认路径是 `/webhook/gitlab`，尾部多一个 `/` 会返回 404。
- 访问 `http://本机IP:8080/webhook/gitlab` 连接失败：服务可能仍监听 `127.0.0.1`。把 `MR_REVIEWER_WEBHOOK_HOST` 改为 `0.0.0.0` 或实际网卡 IP 后重启。
- 返回 `401 WEBHOOK_TOKEN_MISSING`：已配置 `MR_REVIEWER_WEBHOOK_SECRET`，但请求没有 `X-Gitlab-Token` header。
- 返回 `403 WEBHOOK_TOKEN_INVALID`：GitLab Secret token 和 `MR_REVIEWER_WEBHOOK_SECRET` 不一致。
- 启动时报 `MR_REVIEWER_COMMENT_SKILL is required for webhook mode`：补齐 `MR_REVIEWER_COMMENT_SKILL`。
- 返回 `200 skipped`：请求已到达服务，但事件不是可处理的 MR open、reopen 或 source update 事件。
