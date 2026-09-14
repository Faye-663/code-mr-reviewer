# GitLab API 说明

本文记录主程序和仓库内便携式 `gitlab-mr-review` skill 实际使用的 GitLab/CodeHub 接口、消费字段、调用条件和写入副作用。它是 `gitlab_mr_api.txt` 原始样例整合后的长期事实源；实现行为仍以 `src/mr_reviewer/gitlab.py` 及各调用方为准。

标准 GitLab 接口可对照官方 [Merge requests API](https://docs.gitlab.com/api/merge_requests/)、[Projects API](https://docs.gitlab.com/api/projects/)、[Discussions API](https://docs.gitlab.com/api/discussions/) 和 [Notes API](https://docs.gitlab.com/api/notes/)。`/isource/...`、discussion `severity` 等 CodeHub 平台扩展以本文保存的项目契约为准，不能假定其它 GitLab 部署支持。

## 三类边界

### 入站 webhook

`mr-reviewer webhook` 暴露的是本项目 HTTP endpoint，不是 GitLab REST API。GitLab 把 Merge Request Hook payload 推送到 `MR_REVIEWER_WEBHOOK_HOST`、`PORT` 和 `PATH`；主程序从 payload 读取 project path、MR iid、title、分支、head SHA 及 target/source clone URL，再通过标准 MR API 确认当前 Head。传输去重优先使用 `X-Gitlab-Event-UUID`。

入站 webhook 本身不使用 `PRIVATE-TOKEN`。`MR_REVIEWER_WEBHOOK_SECRET` 通过单独的 secret header 校验，默认 header 为 `X-Gitlab-Token`。

可处理事件只有在当前 Head 查询和 SQLite Trigger 注册成功后才返回 `202 accepted`。注册所需的 GitLab API 或 SQLite 操作失败时返回 `503 WEBHOOK_REGISTRATION_FAILED`，让发送方按失败请求处理；响应不包含内部异常详情。

### 出站 REST API

主程序的 `GitLabClient` 以 `MR_REVIEWER_GITLAB_API_BASE_URL` 为完整 API root，只追加本文列出的 `/projects/...` 资源路径。它不从 MR Web URL 猜测额外 API 前缀。

所有真实 REST 请求都发送：

```http
PRIVATE-TOKEN: <MR_REVIEWER_GITLAB_TOKEN>
Accept: application/json
```

JSON POST 使用 `application/json; charset=utf-8`；普通 note POST 使用 `application/x-www-form-urlencoded; charset=utf-8`。每个请求超时为 30 秒，当前不自动重试。

### HTTPS Git

clone/fetch 不经过 `GitLabClient`。项目 API 或 webhook payload 提供 target/source HTTPS repository URL，`GitClient` 使用同一个 GitLab token 进行 clone/fetch/checkout。REST API 可用不代表 token 一定拥有仓库读取权限，反之亦然。

## 标识符与 URL

- `MR_REVIEWER_GITLAB_BASE_URL` 是 MR Web URL 的 host 校验边界，例如 `https://gitlab.example.com`。
- `MR_REVIEWER_GITLAB_API_BASE_URL` 是完整 REST API root，例如 `https://api.example.com/api/api/v4`；留空时回退为 `<GITLAB_BASE_URL>/api/v4`。
- `project_path` 来自 MR URL 或 webhook `path_with_namespace`，如 `team/service`。放入 API path 前使用 UTF-8 URL 编码，`/` 会编码为 `%2F`。
- `project_id` 必须来自 project API 或 MR 响应；不能从 URL 猜测。
- `iid` 是项目内 MR 编号，直接来自 MR URL 或 webhook payload；它不是全局 MR `id`。

当前 MR URL parser 以 `/merge_requests/` 为分隔符，不兼容 GitLab Web 页面常见的 `/-/merge_requests/` 路径。

## 工作模式调用矩阵

| 工作模式 | 元数据读取 | 发布前读取 | GitLab 写入 |
|---|---|---|---|
| `healthcheck` | 无；只检查配置和本地命令 | 无 | 无 |
| `run-once` | 标准 MR；按 target/source project id 各取 clone URL；映射命中的 Deep Review 还按 path 查询依赖项目 | 无 | 无 |
| IM 单 MR | 与 `run-once` 相同；ReviewRun 注册前再读取标准 MR 当前 Head | 交付前读取当前 Head；GitLab sink 分页读取 discussions | 由 IM sink 配置决定是否 POST inline discussion；OneBox 不属于 GitLab API |
| webhook 仅报告 | payload 解析后读取标准 MR 当前 Head；映射命中的 Deep Review 按 path 查询依赖项目 | 规范报告后再次读取当前 Head | 无 GitLab 写入；OneBox 可独立开启 |
| webhook 自动发布 | 与 webhook 仅报告相同 | 再读标准 MR `diff_refs`，分页读取 discussions 做 marker 去重 | 仅对满足门槛且可定位的 finding POST inline discussion；不回退 note |
| IM ReviewSet | 每个成员依次读取 project id、平台 isource MR、标准 MR、target/source clone URL | 只为至少一个可发布 target 的成员分页读取 discussions | 可定位 target POST discussion；未提供位置或合法位置不在 diff 时 POST note |
| 便携式 skill | 标准 MR；按 target/source project id 各取 clone URL | 无去重读取 | `MR_REVIEW_SUBMIT_COMMENT=true` 时 POST 一条普通 note |

IM/webhook 单 MR 在进入 `DiscussionPublisher` 后会先分页读取 discussions，即使最终没有 finding 被 POST。ReviewSet 只为存在 `publishable_inline` 或 `publishable_note` target 的成员读取 discussions；同一成员在一个 ReviewSet 发布阶段只读取一次。

## 接口目录

### 1. 获取标准 MR

```http
GET /projects/{url_encoded_project_path}/merge_requests/{iid}
```

主程序方法：

- `get_merge_request()`：`run-once`、IM 单 MR 和 ReviewSet 元数据准备。
- `get_mr_detail_for_discussion_position()`：IM/webhook 单 MR 注册前确认当前 Head，并在交付前重新读取权威 Head；GitLab sink 还要求完整 diff refs。

实际消费字段：

| 字段 | 调用方 | 用途 |
|---|---|---|
| `diff_refs.base_sha` 或 `diff_refs.start_sha` | `run-once`、IM 单 MR、便携式 skill | review range base；两者均缺失时失败。 |
| `diff_refs.head_sha` 或顶层 `sha` | `run-once`、IM/webhook 单 MR、便携式 skill | review range 或 ReviewRun 当前 head；两者均缺失时失败。 |
| `diff_refs.base_sha/start_sha/head_sha` | IM/webhook 单 MR 自动发布 | 构造 GitLab inline position；三者必须同时是非空字符串。 |
| `target_project_id`、`source_project_id` | 单 MR、ReviewSet、skill | 查询 target/source HTTPS clone URL。主程序要求两者有效。 |
| `target_branch`、`source_branch` | 单 MR、ReviewSet、skill | fetch/checkout 分支。 |
| `title` | 单 MR、skill | 选择 one-step 或 Deep Review。ReviewSet 不使用 title 路由。 |

读取失败会终止当前单 MR review；ReviewSet 在任何成员元数据失败时整组终止，不进入 Agent 或发布。单 MR 在 review 已完成但权威 refs 读取失败时写失败态本地报告，不发布 discussion。

### 2. 按 project path 获取项目

```http
GET /projects/{url_encoded_project_path}
```

主程序方法：`get_project()`。

用于 ReviewSet 和映射命中的单 MR Dependency Review：

- ReviewSet 只信任响应中的整数 `id` 作为 `project_id`，随后以该 ID 调用平台 isource MR 接口。MR URL 只提供 project path 和 iid，不能代替该查询或提供猜测的 project id。
- Dependency Review 对目录列出的每个依赖项目校验正整数 `id`、与目录完全相等的 `path_with_namespace`，以及非空 `http_url_to_repo`。clone URL 必须是没有用户名、密码、query 或 fragment 的 HTTPS URL。

响应不是 JSON object 或对应调用方所需字段无效时，ReviewSet 整组预检失败，或 Dependency Review 丢弃全部依赖上下文并降级为单仓 one-step。两者都不会使用部分项目元数据继续联合检视。

原始平台样例中的编码过程和成功响应：

```text
MR URL: https://api.example.com/d001010/code-mr-reviewer/merge_requests/10
project_path: d001010/code-mr-reviewer
URL encoded: d001010%2Fcode-mr-reviewer
```

```json
{
  "id": 12345,
  "name": "code-mr-reviewer",
  "path": "code-mr-reviewer",
  "path_with_namespace": "d00808710/code-mr-reviewer"
}
```

ReviewSet 当前只消费 `id`；Dependency Review 消费 `id`、`path_with_namespace` 和 `http_url_to_repo`。其它字段保留为平台响应上下文，不能替代这些必需字段。

### 3. 获取平台 isource MR 详情

```http
GET /projects/{project_id}/isource/merge_requests/{iid}
```

主程序方法：`get_review_set_merge_request()`。

这是 CodeHub 平台扩展，不是上游 GitLab 标准 endpoint。它只用于 IM ReviewSet，提供精确 refs 和需求关联：

```json
{
  "id": 48286491,
  "iid": 10,
  "project_id": 5713530,
  "merge_status": "unchecked",
  "sha": "160a832ad983cea7e2a9872f3421f5ee4c36b62c",
  "diff_refs": {
    "base_sha": "f6f51e5fb950d36908f4c35ee79a3e3e05668e4f",
    "head_sha": "160a832ad983cea7e2a9872f3421f5ee4c36b62c",
    "start_sha": "37b257aa52223fc6bd69d9fae56bc778b3451983"
  },
  "e2e_issues": [
    {
      "issue_num": "US20260714100001"
    }
  ]
}
```

信任边界：

- 响应 `project_id` 必须等于上一步 project API 返回值。
- 响应 `iid` 必须等于 MR URL 中的 iid。
- `diff_refs.base_sha`、`start_sha`、`head_sha` 必须都是非空字符串。
- `ReqID` 只读取 `e2e_issues[0].issue_num` trim 后的非空字符串；不搜索后续元素，也不读取相近字段。
- 所有 ReviewSet 成员的 ReqID 必须一致，否则整组拒绝。

原始平台样例记录成功状态为 HTTP 200、失败状态为 HTTP 500；当前客户端对任何 HTTP error 都统一按请求失败处理，不依赖固定失败状态码。

### 4. 按 project id 获取 clone URL

```http
GET /projects/{project_id}
```

主程序方法：`get_project_http_url()`。

消费唯一字段 `http_url_to_repo`，用于主 MR 或 ReviewSet 成员的 target/source HTTPS clone。单 MR 和 ReviewSet 都会分别查询 `target_project_id` 与 `source_project_id`；两者相同时当前实现仍会发起两次查询。字段为空时 review 终止。

Dependency Review 不调用这个按 id 查询的 helper；它直接使用上一节按 path 查询且完成信任边界校验的 `http_url_to_repo`。

### 5. 分页读取 MR discussions

```http
GET /projects/{url_encoded_project_path}/merge_requests/{iid}/discussions?per_page=100&page={page}
```

主程序方法：`list_mr_discussions()`。

客户端从 `page=1` 开始，每页固定请求 100 条；返回数量少于 100 时结束。每页响应必须是 JSON array。发布器遍历每个 discussion 的 `notes[*].body`，提取本项目生成的隐藏 HTML marker，用于避免单 MR 重放或 ReviewSet 重试产生重复评论。

读取失败时采用 fail-closed：

- IM/webhook 单 MR 不发布任何新 discussion，任务写失败态报告。
- ReviewSet 将该成员的候选 target 标记为 `duplicate_check_failed`，不向该成员发布；其它成员继续。

### 6. 创建 inline discussion

```http
POST /projects/{url_encoded_project_path}/merge_requests/{iid}/discussions
Content-Type: application/json; charset=utf-8
```

主程序方法：`post_mr_discussion()`。

当前 CodeHub 请求体：

```json
{
  "body": "检视意见内容",
  "severity": "suggestion",
  "position": {
    "base_sha": "f6f51e5fb950d36908f4c35ee79a3e3e05668e4f",
    "start_sha": "37b257aa52223fc6bd69d9fae56bc778b3451983",
    "head_sha": "160a832ad983cea7e2a9872f3421f5ee4c36b62c",
    "position_type": "text",
    "new_path": "README.md",
    "new_line": 264
  }
}
```

字段说明：

| 字段 | 当前契约 |
|---|---|
| `body` | Python 渲染的单个 finding 内容和不可见幂等 marker。 |
| `severity` | CodeHub 扩展枚举：`suggestion`、`minor`、`major`、`fatal`。 |
| `position.base_sha/start_sha/head_sha` | 来自权威 MR detail，不用本地 merge-base 替代。这些是应用主动提供的版本保护字段，而不是平台 JSON 必填字段；实测错误 SHA 会导致请求失败，因此服务继续完整发送。 |
| `position.position_type` | 固定 `text`。 |
| `position.new_path/new_line` | 新增行或上下文行只发送新侧路径和行号。 |
| `position.old_path/old_line` | 纯删除行只发送旧侧路径和行号。 |

原始平台样例的成功状态为 HTTP 201，响应示例：

```json
{
  "id": "314a0d092d1b73851a82ab23ad5d8147c626a68a",
  "notes": [
    {
      "id": 142680522
    }
  ]
}
```

主程序把 `id` 和首个 `notes[0].id` 写入本地发布结果。单条 POST 失败不会回滚已经发布的其它 finding：IM/webhook 单 MR 继续处理同 MR 的其它 finding；ReviewSet 继续其它 target，并将总状态转为带 warning 的成功。

原始平台样例记录失败状态为 HTTP 500；当前客户端对任意 HTTP error 都统一按该条发布失败处理，不依赖固定失败状态码。

### 7. 创建普通 MR note

```http
POST /projects/{url_encoded_project_path}/merge_requests/{iid}/notes
Content-Type: application/x-www-form-urlencoded; charset=utf-8

body=<comment body>
```

主程序方法：`post_mr_note()`。

主程序在单 MR、ReviewSet 和 dependency review 中按 finding 使用普通 note：

- target 没有提供 position。
- position 语法合法，但无法映射到责任 MR 的当前 diff。

未知成员、越界路径或非法行号不会回退 note，而是在结构化解析阶段隔离。普通 note 正文写明规范化请求位置和降级原因，并明确没有吸附邻近行；它与 inline discussion 使用同一个当前 Head/finding/位置幂等 marker。

便携式 skill 也使用该 endpoint，但语义不同：它在 `MR_REVIEW_SUBMIT_COMMENT=true` 时把契约校验后的 review object 重新序列化为纯 JSON，作为一条普通 MR note 提交。它不发布 inline discussion，也不读取 discussions 做 marker 去重。

## Webhook 调用顺序

1. 入站 payload 提供 project path、iid、分支、payload head 和 clone URL；使用 `X-Gitlab-Event-UUID` 作为首选 Trigger ID。
2. 在 HTTP 入队路径读取标准 MR 当前 Head，以当前值注册或加入 SQLite ReviewRun；陈旧 payload 不会反向替代新 Head。
3. owner 完成 clone/diff、Agent review 和结构化结果解析；joiner 等待，成功结果可被后续 Trigger 复用。
4. 原子写 ReviewRun 本地 JSON/Markdown，再读取标准 MR 确认 Head 未变化；变化则 GitLab/OneBox 都跳过。
5. `MR_REVIEWER_WEBHOOK_POST_COMMENT=false`：GitLab sink 记录 `disabled`，但前述 Head 读取仍会发生。
6. `MR_REVIEWER_AGENT_MODEL_NAME` 为空：GitLab sink 记录 `model_not_configured`，不读取 discussions、不调用写 API。
7. GitLab sink 严格取得 `base_sha/start_sha/head_sha`，基于本地 unified diff 验证规范位置和共享发布门槛。
8. 分页读取 discussions 提取 marker；精确位置 POST inline discussion，无法映射的位置按同一 marker POST 普通 note。

顶层解析失败、低于门槛或 finding 契约非法不会触发写 API。无法映射的位置会降级普通 note，但不会为了发布而借用邻近行。

## ReviewSet 调用顺序

对每个成员先完成全部元数据读取，再产生任何 clone：

1. `GET /projects/{project_path}` 取得 `project_id`。
2. `GET /projects/{project_id}/isource/merge_requests/{iid}` 验证 id/iid、取得精确 refs 和 ReqID。
3. `GET /projects/{project_path}/merge_requests/{iid}` 取得 target/source project id 与分支。
4. 按两个 project id 分别取得 target/source `http_url_to_repo`。

全部成员元数据和共同 ReqID 验证成功后才 clone、执行两阶段 Agent review。发布阶段先完成全量 evidence、target、position 和门槛校验：

- 发布开关关闭或模型名为空：不读取 discussions，不写 GitLab。
- 对存在发布候选的责任成员，每个成员分页读取一次 discussions。
- 可定位位置 POST discussion；无位置或合法但不在 diff 的位置 POST note。
- 某成员去重读取失败时只阻止该成员发布；单 target POST 失败不回滚其它结果。

## Dependency Review 调用顺序

只有完整 `【Deep-Review】` 或 `[Deep-Review]` marker 命中且部署侧目录为主项目配置了 1–3 个直接依赖时，才在主 MR checkout 之外准备依赖源码：

1. 对目录中排序后的每个依赖 path 调用 `GET /projects/{project_path}`。
2. 校验正整数 project id、精确 `path_with_namespace` 和无凭据 HTTPS clone URL。
3. 只 fetch 与主 MR `target_branch` 同名的 `refs/heads/<branch>`，解析并 detached checkout 得到的确定 commit SHA。
4. 所有依赖成功后才写入不含 token 或外部 URL 的 `dependency-review.json`，并执行固定 two-step 联合检视。

依赖分支缺失时不得 fallback source branch、默认分支、tag 或近似 ref；任一查询、分支或 checkout 失败时清理所有已准备依赖，并执行单仓 one-step。依赖仓没有 MR range，不调用其 MR/discussions/notes 接口，也不是评论目标。若主 MR finding 通过共享发布门槛，仍只向主 MR 发布。

## 便携式 skill

`.skill/gitlab-mr-review/scripts/review_gitlab_mr.py` 为了可独立复制，不导入主程序 `GitLabClient`，而是保留自包含 `GitLabApi`。它使用：

| 配置 | 用途 |
|---|---|
| `GITLAB_BASE_URL` | 必填，MR Web URL host 和默认 API root。 |
| `GITLAB_API_BASE_URL` | 可选，完整 REST API root；默认 `<GITLAB_BASE_URL>/api/v4`。 |
| `GITLAB_TOKEN` | 必填，REST 和 HTTPS clone。 |
| `MR_REVIEW_WORK_DIR` | 可选，默认系统临时目录下的 `gitlab-mr-review`。 |
| `MR_REVIEW_SUBMIT_COMMENT` | 默认 true；false 时只保留本地报告。 |
| `MR_REVIEWER_AGENT_TYPE`、`MR_REVIEWER_AGENT_COMMAND` | 选择 OpenCode/Claude Code adapter 和命令。 |

skill 的 GET/POST 同样使用 `PRIVATE-TOKEN`、JSON response 和 30 秒请求超时，但没有主程序的 fixture、INFO/DEBUG API 日志、discussions 分页读取、marker 去重、inline 发布或 ReviewSet note fallback。HTTP 错误只向调用方暴露状态码，终端错误会脱敏 token。

## 失败、日志与敏感信息

- token 为空时，主程序在发出 REST 请求前失败。
- HTTP error 在普通日志中只记录 method、path、状态码、耗时和错误内容长度；抛出的异常只包含状态码。
- 网络、JSON 解码或其它错误记录异常类型后原样向上抛出，由工作模式决定任务失败边界。
- `MR_REVIEWER_LOG_LEVEL=INFO` 只记录 method、path、状态、耗时和响应长度，不记录正文。
- `DEBUG` 在 `MR_REVIEWER_DEBUG_DIR/.../api` 保存脱敏后的 header、request body、response 或 error body；GitLab token、`PRIVATE-TOKEN`、Authorization 和 Basic 凭据会被替换。
- discussions 去重读取失败时不冒险写入；当前没有 REST 自动重试，因此外层重放必须依赖 marker 保证幂等。
- `MR_REVIEWER_TEST_GITLAB_RESPONSES` 只用于测试 fixture。匹配路径的 GET 会读取本地 JSON，不发真实请求；生产不得设置。

## 维护规则

新增或修改 GitLab API 调用时必须同步检查：

1. 本文的接口目录、消费字段、工作模式矩阵和失败策略。
2. [配置说明](CONFIGURATION.md) 中的 API root、token、发布开关与日志说明。
3. `README.md`、`DESIGN.md` 和对应 Quickstart 的用户可见行为。
4. API 契约测试、发布幂等测试和脱敏测试。

平台特有响应字段必须在信任边界显式校验，不能用相近字段、后续数组元素或本地推断静默补全。
