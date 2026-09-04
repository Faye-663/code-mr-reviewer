# Webhook 快速开始

这份文档面向只使用 GitLab Merge Request webhook 的用户。你不需要配置 WeLink IM poll，也不需要 `welink-cli`。

## 适用场景

- GitLab 在 MR 打开、重新打开或 source branch 更新时主动回调本机服务。
- 服务收到 webhook 后按 MR title 与可选项目依赖目录路由：title 去除前导空白后以 `【Deep-Review】` 或 `[Deep-Review]` 开头时（忽略大小写）请求 Deep Review，混合括号不匹配；普通 MR 固定单仓 one-step；Deep Review 无依赖映射时单仓 two-step，完整准备 1–3 个依赖时联合 two-step；目录、数量或准备异常时降级为单仓 one-step。所有路径都把结果写入 `MR_REVIEWER_REPORT_DIR`，仅修改 title 不触发 review。
- Python 侧会把满足共享发布门槛且可定位的 finding 发布为 GitLab inline discussion；也可以独立配置 GitLab 与 OneBox sink。webhook 与 IM 同时触发同一 Head 时，成功 review、GitLab findings 和 OneBox 文件都按 ReviewRun 复用。

## 最小配置

完整的配置加载规则、模式矩阵和变量参考见 [配置说明](CONFIGURATION.md)。本节只列出 webhook 的最小部署示例。

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
MR_REVIEWER_AGENT_MODEL_NAME=GLM5
MR_REVIEWER_LOG_LEVEL=OFF
MR_REVIEWER_DEBUG_DIR=log/debug

MR_REVIEWER_WEBHOOK_HOST=0.0.0.0
MR_REVIEWER_WEBHOOK_PORT=8080
MR_REVIEWER_WEBHOOK_PATH=/webhook/gitlab
MR_REVIEWER_WEBHOOK_SECRET=your-webhook-secret
MR_REVIEWER_WEBHOOK_SECRET_HEADER=X-Gitlab-Token
MR_REVIEWER_WEBHOOK_POST_COMMENT=true
MR_REVIEWER_WEBHOOK_UPLOAD_ONEBOX=false
MR_REVIEWER_PUBLISH_MIN_SEVERITY=minor
MR_REVIEWER_PUBLISH_MIN_CONFIDENCE=HIGH
MR_REVIEWER_REPORT_DIR=log/webhook-reports
MR_REVIEWER_COORDINATION_DB_PATH=log/review-coordination.sqlite3

# 可选：只读项目依赖目录；生产首次验证可先不配置或关闭评论开关。
MR_REVIEWER_REPOSITORY_DEPENDENCY_CATALOG=
```

说明：

- `MR_REVIEWER_GITLAB_BASE_URL` 用于 MR Web URL 校验；`MR_REVIEWER_GITLAB_API_BASE_URL` 是完整 REST API 根地址。后者为空时回退为 `<GitLab根地址>/api/v4`。
- `MR_REVIEWER_WEBHOOK_HOST` 是服务监听地址。本机自测可用 `127.0.0.1`；GitLab 从其他机器访问本机 IP 时，使用 `0.0.0.0` 或实际网卡 IP。
- `MR_REVIEWER_WEBHOOK_SECRET` 可为空；配置后会校验 `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 指定的请求头，默认是 `X-Gitlab-Token`。
- `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 可按平台调整，例如 CodeHub 使用 `X-CodeHub-Token` 时改成该值。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT=false` 时不会发布 inline discussion，只写本地 JSON 监视报告和 Markdown review 报告。
- `MR_REVIEWER_WEBHOOK_UPLOAD_ONEBOX=false` 是默认值；设为 true 后 webhook 也会请求 OneBox 上传，并需要配置 `WELINK_ONEBOX_SPACE_ID` 与 `WELINK_ONEBOX_PARENT_ID`。同一 ReviewRun 只上传一个确定性文件。
- `MR_REVIEWER_COORDINATION_DB_PATH` 是 IM/webhook 的共享单机 SQLite。多个进程必须配置为同一路径；数据库不保存足以恢复 worker 队列的完整事件。
- `MR_REVIEWER_REPOSITORY_DEPENDENCY_CATALOG` 可选，只在两种完整 Deep Review marker 的单 MR 中读取。配置 1–3 个直接依赖且全部同名 target branch checkout 成功时才执行联合检视；任何不完整上下文都整组降级，不使用部分依赖。
- `MR_REVIEWER_PUBLISH_MIN_SEVERITY` 与 `MR_REVIEWER_PUBLISH_MIN_CONFIDENCE` 同时用于 webhook 和 ReviewSet。默认发布 `minor` 及以上且 `confidence=HIGH` 的 finding；severity 顺序为 `suggestion < minor < major < fatal`，confidence 顺序为 `LOW < MEDIUM < HIGH`。非法枚举值会导致启动失败，这两个门槛不会过滤本地报告 findings。
- `MR_REVIEWER_COMMENT_SKILL` 仍可选用于指定单仓 review prompt skill；依赖联合检视固定使用 `dependency-code-review`，不受该配置覆盖。skill 必须只输出结构化 JSON，不要配置会自行提交评论的 skill。
- `MR_REVIEWER_AGENT_MODEL_NAME` 是 webhook inline discussion 的展示模型名。它为空时，worker 只写本地报告并标记 `model_not_configured`，不会提交 GitLab discussion；不会从 Agent 输出推断模型名。
- 单仓或依赖联合 Deep Review 的审查计划只保存在本地 JSON/Markdown 报告中，不会发布到 GitLab；one-step 不生成计划。依赖仓只能提供 evidence，线上 finding 仍只能发布到主 MR。
- `MR_REVIEWER_LOG_LEVEL` 默认 `OFF`，不会输出项目日志或创建 debug 文件。设为 `INFO` 时只记录 API、Agent 调用元数据；设为 `DEBUG` 时会把脱敏后的请求、响应、prompt 和 Agent 输出写到 `MR_REVIEWER_DEBUG_DIR/YYYYMMDD/<task_id>/`。常规 webhook 审计仍使用 `MR_REVIEWER_REPORT_DIR`，它不受日志级别影响。
- review/review-plan/deep-review/dependency-review prompt 只使用本项目随 Git 发布的包内模板，不支持部署侧覆盖。webhook JSON 审计报告会记录实际使用阶段的模板 ID 与内容哈希版本；DEBUG 的 Agent `request.json` 也会记录对应版本。启用依赖联合检视前，需要在所选 Agent 中安装仓库 `.skill/dependency-code-review`。

Agent 的 `old_line` / `new_line` 不是范围起止行。新增行必须使用 `old_line=-1, new_line=N`，删除行使用 `old_line=N, new_line=-1`，未修改的上下文行同时提供同一位置匹配的两侧行号。对于更新文件中的同号替换行，若 Agent 误报 `old_line=new_line=N`，且 diff 两侧精确存在旧侧删除行和新侧新增行，Python 会规范为新侧位置；该容错不适用于新文件或范围式行号。其它非法或自相矛盾的组合不会发布；合法但不在当前 diff 的 finding 只保留在本地报告，webhook 不会改用邻近行或普通 note。

启动前可以运行 `uv run mr-reviewer healthcheck`；输出中的 `publish_min_severity` 与 `publish_min_confidence` 是实际生效门槛。当前 healthcheck 是全局检查，会同时要求 WeLink poll/reply、群和 OneBox 配置；只部署 webhook 时，这些缺失项会让命令返回非零，但不表示 webhook 最小配置本身不可运行。

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

如果使用最小 MR payload 自测，可处理事件会返回：

```json
{
  "status": "accepted",
  "event_id": "...",
  "repo": "team/project",
  "mr_iid": 7,
  "review_run_id": "review-...",
  "disposition": "created"
}
```

`disposition` 可能为 `created`、`joined`、`reused` 或 `duplicate`。服务优先使用 `X-Gitlab-Event-UUID` 判断 webhook 传输是否重复；缺少该 header 时回退到项目、MR 与 payload Head 组成的事件 ID。返回 `202` 只表示已注册到内存 worker 队列，不代表任务已经完成；当前不提供查询 API。

后台任务执行前会重新读取 GitLab 当前 Head，避免延迟到达的旧 payload 反向替代新版本。成功 review 在 `MR_REVIEWER_REPORT_DIR` 写入一组 ReviewRun 级 `.json`/`.md`；发布前再次检查当前 Head。Head 变化时本地报告保留，GitLab 与 OneBox 均记为 `skipped_stale`。首次生产验证建议先关闭 GitLab 开关。

## 并发与恢复边界

- 多仓、多 MR webhook 可以同时接收，但当前全局一次只执行一个 ReviewRun，其余任务驻留进程内队列。
- 同一 Head 的重复 webhook 或 IM/webhook 并发只调用一次 Agent；后到 Trigger 加入运行中任务，或复用已成功结果并补做缺失/失败 sink。
- 新 Head 注册后立即把同 MR 旧未完成 run 标为过期。旧 Agent 子进程不会被强杀，但会在 clone、依赖准备、plan、每次 Agent 返回及交付前停止进入下一阶段。
- webhook 队列不持久化。进程在 `202` 后退出可能丢任务；过期 lease 启动时只标为 `interrupted`，等待新 Trigger，不自动恢复。
- OneBox 明确失败允许后续 Trigger 重试；上传期间进程中断会标为 `unknown`，不自动重试，以降低重复文件风险。

## 常见问题

- 返回 `404 NOT_FOUND`：请求路径和 `MR_REVIEWER_WEBHOOK_PATH` 不一致。默认路径是 `/webhook/gitlab`，尾部多一个 `/` 会返回 404。
- 访问 `http://本机IP:8080/webhook/gitlab` 连接失败：服务可能仍监听 `127.0.0.1`。把 `MR_REVIEWER_WEBHOOK_HOST` 改为 `0.0.0.0` 或实际网卡 IP 后重启。
- 返回 `401 WEBHOOK_TOKEN_MISSING`：已配置 `MR_REVIEWER_WEBHOOK_SECRET`，但请求没有 `MR_REVIEWER_WEBHOOK_SECRET_HEADER` 指定的 header。
- 返回 `403 WEBHOOK_TOKEN_INVALID`：GitLab Secret token 和 `MR_REVIEWER_WEBHOOK_SECRET` 不一致。
- 返回 `200 skipped`：请求已到达服务，但事件不是可处理的 MR open、reopen 或 source update 事件。
- 返回 `202` 且 `disposition=duplicate`：同一个 webhook 事件已经注册，不会再次入队。`reused` 表示复用同 Head 的成功审查，不表示重复上传或重复发布。
- review 成功但 MR 没有 inline discussion：检查 `MR_REVIEWER_WEBHOOK_POST_COMMENT` 是否为 `true`，`MR_REVIEWER_GITLAB_TOKEN` 是否有读取 MR diff 与提交 discussion 的权限，并查看本地 `.json`/`.md` 报告中的 finding 是否被过滤、无法定位或判定为重复。
- `healthcheck` 显示 `repository_dependency_catalog: invalid`：检查 path、文件权限、JSON 和严格 schema。无效目录不会影响普通 MR，但 Deep Review 会降级为单仓 one-step。
- 报告显示“未执行依赖联合检视”：查看 `dependency_degradation_reason`、`dependency_failed_project`、`requested_review_mode` 与实际 `review_mode`；超过 3 个依赖、同名 target branch 缺失或任一 checkout 失败都不会做部分联合检视。
- 本地报告失败：查看 JSON/Markdown 中的 `failure_stage`。`review_plan`/`review` 表示单仓 Deep 两阶段；`dependency_review_plan`/`dependency_review` 表示依赖联合两阶段，报告会保留已准备依赖的 project、branch、commit SHA 和耗时。
