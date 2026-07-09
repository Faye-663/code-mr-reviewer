# Phase2 Webhook Inline Review 技术设计文档

## 1. 当前架构

当前 webhook 流程：

```text
GitLab webhook
 -> parse payload
 -> enqueue background review
 -> clone/fetch/checkout MR range
 -> OpenCode run
 -> Markdown ReviewReport
 -> GitLab notes API post_mr_note
 -> local monitor report
```

当前主要限制：

- OpenCode 输出是自由 Markdown。
- Python 不理解 finding 结构。
- 无 finding 校验。
- webhook 通过 notes API 发布 summary comment。
- `comment_url` 固定为空。
- 重复 webhook 可能重复发评论。
- 本地监视报告是 JSON preview，不是完整 Markdown 检视报告。

## 2. 目标架构

Phase2 webhook 流程：

```text
GitLab webhook
 -> parse payload
 -> enqueue background review
 -> clone/fetch/checkout MR range
 -> fetch MR detail diff_refs
 -> collect diff position map
 -> OpenCode run structured review prompt
 -> parse JSON review result
 -> validate findings
 -> fetch existing discussions markers
 -> publish valid inline discussions
 -> write JSON monitor report
 -> render local Markdown review report
```

Phase2 收尾后，`run-once` 和 WeLink poll 也复用同一套结构化 review 结果和 Markdown renderer。区别只保留在入口协议与输出通道：webhook 发布 GitLab inline discussion，`run-once` 输出本地 Markdown，WeLink 上传 Python 渲染后的 Markdown 文件。

## 3. 核心模块

### Review Result Model

新增结构化 review result model，表示 OpenCode JSON 输出。

核心对象：

- `StructuredReviewResult`
- `ReviewFinding`
- `FindingConfidence`
- `FindingPosition`

允许值：

```text
severity: suggestion, minjor, major, fatal
confidence: HIGH, MEDIUM, LOW
```

`severity` 直接使用 GitLab discussions API 的枚举。`minjor` 保留 API 文档中的原始拼写，不在本项目内改写为 `minor`。

### Finding Parser

职责：

- 从 OpenCode stdout 解析 JSON。
- 校验顶层结构。
- 将原始 dict 转为内部类型。
- 返回 parse error 给 webhook worker。

不做：

- 不判断 line 是否有效。
- 不判断是否应该发布。
- 不访问 Git 或 GitLab。

### Diff Position Map

职责：

- 从 MR diff 构建可评论行集合。
- 支持 GitLab API 的 `new_line` 和 `old_line`。
- 为 GitLab discussions API 构造 position。
- 用 MR 详情 API 的 `diff_refs` 填充 `base_sha`、`start_sha`、`head_sha`。
- 从 diff header 解析 `old_path` / `new_path`，避免 rename/delete 场景只靠单个文件名推断。

需要的 position 字段：

```json
{
  "base_sha": "...",
  "start_sha": "...",
  "head_sha": "...",
  "position_type": "text",
  "new_path": "...",
  "old_path": "...",
  "new_line": 42,
  "old_line": -1,
  "ignore_whitespace_change": false
}
```

行号规则：

- 新增行：`old_line = -1`，`new_line` 为新增后的行号。
- 删除行：`old_line` 为删除前的行号，`new_line = -1`。
- 其它可评论行：使用 diff position map 中能被 GitLab API 接受的 `old_line` / `new_line` 组合。

`base_sha`、`start_sha`、`head_sha` 的权威来源是 GitLab MR 详情 API：

```text
GET /api/v4/projects/{project_id}/isource/merge_requests/{iid}
```

响应中的 `diff_refs.base_sha`、`diff_refs.start_sha`、`diff_refs.head_sha` 用于 discussion position。本地 `merge-base` 仍可用于 clone/diff fallback，但不能替代 API `diff_refs` 生成 inline position。

### Finding Validator

职责：

- 校验 finding 是否能发布。
- 将 finding 分类为：
  - `publishable`
  - `filtered`
  - `invalid`
  - `monitor_only`

默认规则：

- 只发布 `fatal` / `major` + `HIGH`。
- 文件必须在 diff 中。
- `old_line` / `new_line` 必须能映射到 diff old/new line。
- evidence / suggestion 必须非空。

### GitLab Discussions Client

在 `GitLabClient` 中新增：

- `get_mr_detail_for_discussion_position(target) -> dict`
- `list_mr_discussions(target) -> list[dict]`
- `post_mr_discussion(target, body, severity, position) -> dict`

保留 `post_mr_note`，但 webhook 不再调用。

API 参考 `gitlab_mr_api.txt`：

```text
GET /api/v4/projects/{project_id}/isource/merge_requests/{iid}
```

```text
POST /api/v4/projects/{id}/merge_requests/{noteable_id}/discussions
```

### Comment Publisher

职责：

- 读取远端 discussions。
- 提取已有 marker。
- 对 publishable finding 做去重。
- 发布 inline discussion。
- 返回每个 finding 的发布结果。

Body 格式：

```markdown
**[major][HIGH][SQL_PERFORMANCE] 批量查询缺少数量限制**

证据：本次变更新增 IN 查询，但未限制集合大小。

建议：限制集合大小或拆批查询。

<!-- ai-cr:finding:team/project:7:headsha:SQL_PERFORMANCE:src/example.py:src/example.py:-1:42 -->
```

### Markdown Report Renderer

职责：

- 从结构化 review、校验结果、发布结果渲染本地 `.md`。
- 不依赖 OpenCode 自由 Markdown。
- 输出路径写回 JSON 监视报告。

报告路径与 JSON 监视报告同目录、同 stem：

```text
log/webhook-reports/20260709T120000Z-team_project-mr-7-webhook-abc123.json
log/webhook-reports/20260709T120000Z-team_project-mr-7-webhook-abc123.md
```

## 4. 数据流

```text
ReviewService.review_target()
 -> OpenCodeRunner.run_review()
 -> StructuredReviewParser.parse()
 -> FindingValidator.validate()
 -> DiscussionPublisher.publish()
 -> render_markdown_review_report()
 -> write_webhook_monitor_report()
```

`ReviewReport` 需要扩展或替换为可承载：

- raw output
- parse status
- structured result
- finding validation results
- publish results
- local Markdown report path

## 5. 失败策略

- OpenCode command failed：沿用现有 failed monitor report，并生成失败态 Markdown 报告。
- JSON parse failed：不发布 MR 评论，写 `parse_failed` JSON 和 Markdown 报告。
- Validator 全部过滤：不发布 MR 评论，写成功态本地报告。
- GET discussions failed：不发布新评论，写 `publish_failed`，避免重复刷屏。
- 单条 discussion POST failed：记录该 finding failed，继续处理其它 finding。
- Markdown report write failed：任务标记 failed，因为本地 Markdown 报告是 Phase2 必需产物。
- report write failed：沿用现有 worker 保护逻辑，不让 worker 线程退出。

## 6. 兼容性

- Phase2 前半段允许 CLI `run-once` 和 WeLink poll 暂时继续使用现有 Markdown 输出，避免把 webhook inline 发布和入口迁移混在同一提交中。
- Phase2 收尾必须迁移 CLI `run-once` 和 WeLink poll：LLM 返回 JSON，Python 从结构化结果渲染 Markdown。迁移后不再依赖 LLM 自由 Markdown 作为任一入口的长期输出协议。
- WeLink poll 的触发识别、白名单、OneBox 上传和群通知协议不在 Phase2 改动范围内。
- Webhook 配置继续使用现有：
  - `MR_REVIEWER_WEBHOOK_POST_COMMENT`
  - `MR_REVIEWER_REPORT_DIR`
  - webhook secret/path/host/port
- webhook worker 不再调用 `post_mr_note`。

## 7. 文档影响

必须更新：

- `README.md`
- `docs/WEBHOOK_QUICKSTART.md`
- `docs/DESIGN.md`

建议更新：

- `docs/PHASE1_SUMMARY.md` 或新增 Phase2 文档，避免 Phase1 状态和 Phase2 目标混淆。
