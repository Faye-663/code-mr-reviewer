# 配置说明

本文是主程序 `mr-reviewer` 的完整配置参考。`.env.example` 保持为可直接复制的部署模板；README 和各模式的 Quickstart 只保留最小示例。仓库内便携式 `gitlab-mr-review` skill 使用另一组环境变量，其配置见 README 的“Agent skill 直接使用”和 [GitLab API 说明](GITLAB_API.md)。

## 加载规则

`Config.from_env()` 启动时读取当前工作目录下的 `.env`，然后按以下顺序解析每个 `MR_REVIEWER_*` 配置：

1. 当前进程环境变量。
2. `.env` 中的同名变量。
3. 代码内置默认值。

进程环境变量即使显式设为空字符串，也会覆盖 `.env` 中的非空值，并回落到该配置的代码默认值。路径型相对值按启动进程的当前工作目录解析。

当前 dotenv loader 是简化实现：

- 忽略空行、以 `#` 开头的整行注释，以及不含 `=` 的行。
- 只按第一个 `=` 分隔 key/value，并去除首尾空白和简单的单、双引号。
- 不执行 shell，不支持 `export`、变量插值、多行值或行尾注释语义。
- 布尔值只有 `1`、`true`、`yes`、`on`（忽略大小写）表示 true，其余值均表示 false。
- 整数配置无法转换时启动失败；非法日志级别、severity 或 confidence 也会在启动阶段失败。

## 工作模式配置矩阵

表中“必需”表示该模式要完成对应工作必须有有效值；“条件”表示只在启用相关能力时需要；“可选”表示有默认值或用于收紧行为；“不使用”表示该模式不消费该配置。“检查”表示 `healthcheck` 会把缺失值计入退出码。

| 配置能力 | `healthcheck` | `run-once` | IM 单 MR | IM ReviewSet | webhook 仅报告 | webhook 自动发布 |
|---|---|---|---|---|---|---|
| GitLab Web 根地址 | 检查 | 必需 | 必需 | 必需 | 必需 | 必需 |
| GitLab REST API 根地址 | 检查 | 必需，可由 Web 根地址派生 | 必需，可派生 | 必需，可派生 | 必需，用于确认当前 Head | 必需，可派生 |
| GitLab token | 检查 | 必需 | 必需 | 必需 | 必需，用于 HTTPS clone | 必需 |
| Git 与 Agent 可执行命令 | 检查 | 必需 | 必需 | 必需 | 必需 | 必需 |
| Agent 展示模型名 | 不检查 | 不使用 | 条件：IM 发布 GitLab 评论 | 条件：发布 GitLab 评论 | 不使用 | 必需 |
| WeLink poll/reply、群和 OneBox | 检查 | 不使用 | poll/reply 必需，OneBox 条件使用 | 必需 | OneBox 开启时条件使用 | OneBox 开启时条件使用 |
| bot mention/account 与白名单 | 不检查 | 不使用 | 可选 | 可选 | `ALLOWED_REPOS` 可选 | `ALLOWED_REPOS` 可选 |
| ReviewSet 发布开关 | 展示 | 不使用 | 不使用 | 可选 | 不使用 | 不使用 |
| 单 MR 四个入口/sink 开关 | 展示 | 不使用 | 使用 IM 两项 | 不使用 | 使用 webhook 两项 | 使用 webhook 两项 |
| webhook 监听与 secret | 展示 | 不使用 | 不使用 | 不使用 | 必需/可选 | 必需/可选 |
| 发布 severity/confidence 门槛 | 展示并校验 | 启动时校验 | 启动时校验 | 发布时使用 | 启动时校验 | 发布时使用 |
| 项目依赖目录 | 展示并校验 | Deep Review 条件使用 | Deep Review 条件使用 | 不使用 | Deep Review 条件使用 | Deep Review 条件使用 |
| work/state/report/协调路径 | 展示协调路径 | `WORK_DIR` | `WORK_DIR`、`STATE_PATH`、`REPORT_DIR`、`COORDINATION_DB_PATH` | `WORK_DIR`、`STATE_PATH` | `WORK_DIR`、`REPORT_DIR`、`COORDINATION_DB_PATH` | 同左 |
| 资源限制和超时 | 不检查 | 使用 | 使用 | 使用 | 使用 | 使用 |
| 日志和 debug 目录 | 不检查 | 可选 | 可选 | 可选 | 可选 | 可选 |

`healthcheck` 当前是全局检查，不按准备运行的模式裁剪：它总会检查 Git、Agent、GitLab、WeLink poll/reply、群 ID 和 OneBox 目录。只部署 webhook 时，即使 webhook 的实际最小配置已经完整，缺少 WeLink 配置仍会使 `healthcheck` 返回非零；输出中的 webhook endpoint、secret、发布开关和门槛仍可用于人工核对。

## GitLab

| 配置 | 默认值 | 适用模式 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_GITLAB_BASE_URL` | 空 | 所有 review 模式 | GitLab Web 根地址，用于校验 MR URL host、解析 project path，并作为默认 API root 的来源；启动后移除尾部 `/`。 |
| `MR_REVIEWER_GITLAB_API_BASE_URL` | `<GITLAB_BASE_URL>/api/v4` | 需要 REST API 的模式 | 完整 REST API 根地址。适配独立 API 域名或额外前缀时必须显式设置；启动后移除尾部 `/`。 |
| `MR_REVIEWER_GITLAB_TOKEN` | 空 | 所有 review 模式 | 作为 REST `PRIVATE-TOKEN`，也用于 HTTPS clone/fetch。不得写入 prompt、普通日志或报告。 |

Web 根地址、API 根地址和各模式的具体接口调用范围见 [GitLab API 说明](GITLAB_API.md)。

## 项目依赖目录

| 配置 | 默认值 | 适用模式 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_REPOSITORY_DEPENDENCY_CATALOG` | 空 | `run-once`、IM 单 MR、webhook 的 Deep Review | 指向部署侧只读 UTF-8 JSON。空值或当前主项目无映射时维持单仓 Deep Review；普通单仓 one-step 和 ReviewSet 不读取。 |

目录使用 `schema_version=1` 和 `repositories[]`，每项包含唯一、非空的 `project_path` 以及去重、非自身的直接 `dependencies[]`。目录只表达项目关系，不解析 Maven/POM/GAV/version，也不递归展开依赖仓自己的映射。

title 去除前导空白后，只有以完整 `【Deep-Review】` 或 `[Deep-Review]` 开头（忽略大小写）的单 MR 才读取目录。映射包含 1–3 个依赖且全部同名 target branch 准备成功时执行依赖联合 two-step；超过 3 个、目录不可读/schema 非法或任一依赖准备失败时，丢弃全部依赖上下文并降级为单仓 one-step。

`healthcheck` 在未配置时显示 `optional`；配置有效时显示路径和项目数；不可读或 schema 非法时显示稳定 reason code 并返回非零。具体 project 查询、HTTPS clone URL 校验和分支审计边界见 [GitLab API 说明](GITLAB_API.md)。

## WeLink IM

| 配置 | 默认值 | 适用模式 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_IM_POLL_COMMAND` | 空 | `poll` | 查询群历史消息的基础命令；程序追加 `--group-id <WELINK_GROUP_ID>`。 |
| `MR_REVIEWER_IM_REPLY_COMMAND` | 空 | `poll` | 发送群通知的基础命令；程序追加 `--group-id ... --text ...`。 |
| `MR_REVIEWER_WELINK_GROUP_ID` | 空 | `poll` | 当前唯一轮询和通知目标群。 |
| `MR_REVIEWER_WELINK_ONEBOX_SPACE_ID` | 空 | `poll` | 聚合/单 MR Markdown 上传目标 `space-id`。 |
| `MR_REVIEWER_WELINK_ONEBOX_PARENT_ID` | 空 | `poll` | OneBox 目标目录 ID；与 `SPACE_ID` 必须同时有效。上传失败会通知群，但不会把已完成的 review 改为失败。 |
| `MR_REVIEWER_BOT_MENTION` | `@Bot` | `poll` | 文本触发标记。配置 `BOT_ACCOUNT` 后仍保留文本匹配能力。 |
| `MR_REVIEWER_BOT_ACCOUNT` | 空 | `poll` | 用 `atAccountList` 精确判断是否 @ 机器人。 |
| `MR_REVIEWER_ALLOWED_GROUPS` | 空集合 | `poll` | 逗号分隔群 ID 白名单；空表示不限制。 |
| `MR_REVIEWER_ALLOWED_USERS` | 空集合 | `poll` | 逗号分隔发送者白名单；空表示不限制。 |
| `MR_REVIEWER_ALLOWED_REPOS` | 空集合 | IM 与 webhook | 逗号分隔的 GitLab `path_with_namespace` 白名单；空表示不限制。 |

当 `MR_REVIEWER_IM_POST_COMMENT=true` 且 `ALLOWED_USERS` 或 `ALLOWED_REPOS` 为空时，IM 请求可在对应维度触发不受限的 GitLab 写入。该组合按已接受的兼容语义继续运行，但 `healthcheck` 与 poll 启动日志会输出高风险 warning，不会把 warning 计入非零退出码。

## Agent

| 配置 | 默认值 | 允许值/空值 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_AGENT_TYPE` | `opencode` | `opencode`、`claude-code` | 其它值启动失败。 |
| `MR_REVIEWER_AGENT_COMMAND` | 按类型选择 `opencode` 或 `claude` | 可执行命令及参数 | 非空时优先于旧 `OPENCODE_COMMAND`。命令解析后首个可执行文件必须可用。 |
| `MR_REVIEWER_AGENT_MODEL_NAME` | 空 | 任意展示名称 | 用于 IM/webhook 单 MR inline discussion 和 ReviewSet GitLab 评论。为空时仍生成报告，但不发布 GitLab 评论；不会从 Agent 输出猜测。 |
| `MR_REVIEWER_COMMENT_SKILL` | 空（有效默认 `code-review`） | skill 名称 | 指定自动入口的单仓 review prompt skill；依赖联合检视固定使用 `dependency-code-review`，不受该配置覆盖。skill 必须只返回结构化 JSON，不得自行发布评论。 |

Agent 的 provider、API Key、实际模型和登录状态由 OpenCode 或 Claude Code 自身管理，本项目只选择 adapter、命令和 GitLab 评论中的展示名。review/review-plan/deep-review prompt 使用包内版本化模板，不支持部署侧覆盖。OpenCode adapter 使用 `--format json`，Claude Code adapter 要求 Claude Code `2.1.214` 或更高版本并使用 `--output-format stream-json --verbose`；两者都遍历顶层会话的完整文本事件、排除工具输出和转发的子 Agent 文本，并把所有文本交给唯一契约有效 JSON 的 fail-closed 校验，不要求结果出现在最后一条消息。

## GitLab 发布策略

| 配置 | 默认值 | 允许值 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_IM_POST_COMMENT` | `false` | 布尔值 | 控制 IM 单 MR 是否请求 GitLab inline discussion；不影响 ReviewSet。 |
| `MR_REVIEWER_IM_UPLOAD_ONEBOX` | `true` | 布尔值 | 控制 IM 单 MR 是否请求 OneBox 上传。 |
| `MR_REVIEWER_WEBHOOK_POST_COMMENT` | `true` | 布尔值 | 控制 webhook 单 MR 是否请求 GitLab inline discussion。 |
| `MR_REVIEWER_WEBHOOK_UPLOAD_ONEBOX` | `false` | 布尔值 | 控制 webhook 单 MR 是否请求 OneBox 上传。开启时需要有效的 OneBox 配置。 |
| `MR_REVIEWER_REVIEW_SET_POST_COMMENT` | `true` | 布尔值 | 只控制 IM ReviewSet 的 inline discussion/普通 note；false 时仍生成并上传聚合报告。生产首次验证建议先设为 false。 |
| `MR_REVIEWER_PUBLISH_MIN_SEVERITY` | `minor` | `suggestion`、`minor`、`major`、`fatal` | IM/webhook 单 MR 与 ReviewSet 共用；顺序从低到高。非法值在 `Config` 初始化时失败。 |
| `MR_REVIEWER_PUBLISH_MIN_CONFIDENCE` | `HIGH` | `LOW`、`MEDIUM`、`HIGH` | IM/webhook 单 MR 与 ReviewSet 共用；非法值在启动时失败。 |

两个门槛只控制 GitLab 发布候选，不过滤本地 JSON、Markdown 或 ReviewSet 聚合报告中的 findings。单 MR 四个入口/sink 开关彼此独立；同一 `(project_path, mr_iid, head_sha)` ReviewRun 中，只要任一 Trigger 请求某 sink，该 sink 即可执行一次。两个入口都请求 GitLab 时仍按 marker 最多发布一次；都请求 OneBox 时只上传同一个逻辑文件。GitLab 开关为 true 但 `AGENT_MODEL_NAME` 为空时仍不发布。两个单 MR sink 都关闭时，review 仍成功并生成本地报告。

## Webhook

| 配置 | 默认值 | 适用方式 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_WEBHOOK_HOST` | `127.0.0.1` | `webhook` | HTTP 监听地址。本机自测用 `127.0.0.1`；接受其它机器直连时用 `0.0.0.0` 或实际网卡 IP。 |
| `MR_REVIEWER_WEBHOOK_PORT` | `8080` | `webhook` | HTTP 监听端口，必须是整数。 |
| `MR_REVIEWER_WEBHOOK_PATH` | `/webhook/gitlab` | `webhook` | 精确匹配路径；默认配置不接受尾部 `/`。 |
| `MR_REVIEWER_WEBHOOK_SECRET` | 空 | `webhook` | 空值允许请求但输出 warning；非空时校验指定 header。 |
| `MR_REVIEWER_WEBHOOK_SECRET_HEADER` | `X-Gitlab-Token` | `webhook` | secret header 名，可按平台改成 `X-CodeHub-Token` 等实际值。 |
| `MR_REVIEWER_REPORT_DIR` | `log/webhook-reports` | IM/webhook 单 MR | 每个 ReviewRun attempt 的规范 JSON 与 Markdown 报告目录，不受日志级别影响；默认目录名为兼容旧部署而保留。 |
| `MR_REVIEWER_COORDINATION_DB_PATH` | `log/review-coordination.sqlite3` | IM/webhook 单 MR | 单机 SQLite 协调与审计文件。两个进程必须共享此路径；使用 WAL、lease 和事务 claim，不保存可恢复 webhook 队列。 |

成功 ReviewRun 按 ReviewKey 复用；ReviewKey 只包含 `project_path + mr_iid + head_sha`。相同 SHA 下 title、target branch、依赖目录或 Agent 配置发生变化不会自动重审。失败、中断或过期 run 不复用，同 SHA 的新 Trigger 会创建下一 attempt。全局同时只运行一个 ReviewRun；队列仍驻留内存，进程退出后不会自动恢复。

完整部署、自测和故障排查见 [Webhook 快速开始](WEBHOOK_QUICKSTART.md)。

## 日志与诊断

| 配置 | 默认值 | 允许值/空值 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_LOG_LEVEL` | `OFF` | `OFF`、`INFO`、`DEBUG` | `INFO` 记录调用元数据；`DEBUG` 额外启用 Agent debug 参数并写入脱敏诊断内容。其它值启动失败。 |
| `MR_REVIEWER_DEBUG_DIR` | `log/debug` | 路径 | DEBUG 根目录，按日期、任务和 `api`/`agent`/`im` 分类。非 DEBUG 模式不创建。 |
| `MR_REVIEWER_AGENT_DEBUG` | 空/false | 兼容布尔值 | 仅在未设置非空 `LOG_LEVEL` 时生效；true 映射为 `DEBUG`，否则为 `OFF`。 |
| `MR_REVIEWER_AGENT_DIAGNOSTIC_DIR` | 空 | 兼容路径 | `DEBUG_DIR` 为空时作为诊断目录 fallback。 |

日志不会输出 GitLab token、Authorization/Basic 凭据、WeLink 原始正文、Agent prompt 或完整报告。DEBUG 文件会保存更多脱敏内容，应继续按敏感诊断产物管理。

## 工作目录、状态与资源限制

| 配置 | 默认值 | 适用模式 | 行为与关联 |
|---|---|---|---|
| `MR_REVIEWER_WORK_DIR` | 系统临时目录下的 `code-review` | 所有 review | 每个任务的临时 clone/workspace 根目录；空值回落到默认值。 |
| `MR_REVIEWER_STATE_PATH` | `.mr-reviewer-state.json` | `poll` | 已处理 IM message ID 的本地状态文件。每个新 entry 还记录 `notifications.accepted` / `notifications.terminal` 的 `succeeded`、`failed` 或 `not_applicable`；旧 entry 无需迁移。删除或不可写会影响去重。 |
| `MR_REVIEWER_REPORT_DIR` | `log/webhook-reports` | IM/webhook 单 MR | ReviewRun 级 JSON/Markdown；每个 attempt 一组，不按 Trigger 复制。 |
| `MR_REVIEWER_COORDINATION_DB_PATH` | `log/review-coordination.sqlite3` | IM/webhook 单 MR | 单机共享协调状态。过期 review lease 启动时标记 `interrupted`，等待新 Trigger，不自动恢复。 |
| `MR_REVIEWER_MAX_FILES` | `50` | 所有 review | 单个成员允许的最大 changed files 数。 |
| `MR_REVIEWER_MAX_DIFF_LINES` | `2000` | 所有 review | 单个成员允许的最大 diff 行数。 |
| `MR_REVIEWER_TASK_TIMEOUT_SECONDS` | `900` | 所有 review | 单仓 one-step、单仓/依赖联合 Deep Review 或 ReviewSet 共享的任务总时间预算；two-step 的两次 Agent 调用共享该预算。 |
| `MR_REVIEWER_POLL_INTERVAL_SECONDS` | `15` | 常驻 `poll` | 两轮 WeLink 查询间隔；`poll --once` 不等待下一轮。 |

## 旧兼容配置

这些配置仍由 loader 读取，但新部署应使用对应的通用 `AGENT_*`、`LOG_LEVEL` 和 `DEBUG_DIR`：

| 旧配置 | 当前兼容行为 |
|---|---|
| `MR_REVIEWER_OPENCODE_COMMAND` | 仅 `AGENT_TYPE=opencode` 且 `AGENT_COMMAND` 为空时作为命令 fallback；默认 `opencode`。 |
| `MR_REVIEWER_OPENCODE_DEBUG` | 仅通用 `AGENT_DEBUG` 为空且类型为 OpenCode 时作为 debug fallback。 |
| `MR_REVIEWER_OPENCODE_DIAGNOSTIC_DIR` | 通用诊断目录为空且类型为 OpenCode 时作为目录 fallback。 |
| `MR_REVIEWER_OPENCODE_PROMPT_TRANSPORT` | 仍被解析并保存为 `argument`/用户提供的小写值，但当前 AgentRunner 执行路径不消费它；不要把它当作有效部署能力。 |

优先级汇总：

- 命令：`AGENT_COMMAND` > OpenCode 模式下的 `OPENCODE_COMMAND` > 按 Agent 类型的默认命令。
- 日志级别：非空 `LOG_LEVEL` > `AGENT_DEBUG` > OpenCode 模式下的 `OPENCODE_DEBUG` > `OFF`。
- debug 目录：`DEBUG_DIR` > `AGENT_DIAGNOSTIC_DIR` > OpenCode 模式下的 `OPENCODE_DIAGNOSTIC_DIR` > `log/debug`。

## 测试专用配置

`MR_REVIEWER_TEST_GITLAB_RESPONSES` 指向 GitLab fixture JSON，由测试工作流注入 `GitLabClient`。它会绕过匹配路径的真实 GET 请求，不属于生产接口或受支持的部署配置，生产环境不得设置。
