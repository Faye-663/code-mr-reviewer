# Phase2 Webhook Inline Review 需求文档

## 1. 背景

Phase1 已完成 GitLab MR 自动 review 的基础闭环：GitLab webhook 触发后，Python 服务解析事件、准备本地 MR diff 上下文、调用 OpenCode 生成 Markdown review，并通过 GitLab notes API 将整段 Markdown 提交到 MR。

Phase2 先聚焦 webhook 入口的 review 质量收敛，再在阶段收尾时把 `run-once` 和 WeLink poll 同步迁移到同一套结构化 review 输出链路。核心目标是从“LLM 直接输出自由 Markdown”改为“LLM 输出结构化 finding，Python 负责校验、发布和 Markdown 渲染”。

## 2. 目标

- Webhook 模式下，MR 上只发布 inline comment。
- 下线 webhook 的 notes API 发布路径。
- OpenCode 输出结构化 finding，而不是自由 Markdown。
- Python 负责解析、校验、过滤和幂等控制。
- 只发布高置信、严重、可精确定位到 diff 行的 finding。
- 无法可靠定位的内容只写入本地报告，不发布到 MR。
- 重复 webhook 触发不得重复发布相同 finding 评论。
- 每次 webhook review 在本地保留完整 Markdown 代码检视报告。
- Phase2 收尾时，`run-once` 和 WeLink poll 也必须改为 LLM 返回 JSON、Python 渲染 Markdown，避免入口之间长期保留两套 review 输出协议。

## 3. 范围

### In Scope

- GitLab webhook 入口。
- OpenCode review 输出协议调整为 JSON。
- Finding parser。
- Finding validator。
- GitLab discussions API 发布 inline comment。
- GitLab MR 详情 API 读取 `diff_refs.base_sha` / `diff_refs.start_sha` / `diff_refs.head_sha`。
- 远端 marker 幂等检查。
- 本地 webhook JSON 监视报告增强。
- 本地 Markdown 代码检视报告。
- Phase2 收尾迁移 `run-once` 和 WeLink poll 的 Markdown 输出生成方式。
- README / webhook quickstart / design 文档同步。

### Out of Scope

- WeLink poll 的触发识别、白名单、OneBox 上传和群通知协议。
- GitLab notes API summary comment。
- `/-/merge_requests/` URL 兼容。
- Repo Review Profile。
- 历史缺陷 RAG。
- 多 Agent review。
- 完整 Review Context Package。
- 项目规则平台。
- 误报/采纳率统计闭环。

## 4. 功能需求

### R1. Webhook Inline-only 发布

Webhook review 完成后，只能通过 GitLab discussions API 发布 inline comment。

- 不再通过 notes API 提交整段 Markdown。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT=false` 时仍然不发布评论，只写本地报告。
- 无有效 inline finding 时，不发布 MR 评论。

### R2. OpenCode 输出结构化 JSON

OpenCode 必须输出 JSON。非法 JSON 不允许 fallback 到 Markdown 发布。

最小结构：

```json
{
  "findings": [
    {
      "rule_id": "SQL_PERFORMANCE",
      "severity": "major",
      "confidence": "HIGH",
      "old_path": "src/example.py",
      "new_path": "src/example.py",
      "old_line": -1,
      "new_line": 42,
      "title": "批量查询缺少数量限制",
      "evidence": "本次变更新增 IN 查询，但未限制集合大小。",
      "suggestion": "限制集合大小或拆批查询。"
    }
  ],
  "notes": [],
  "test_gaps": []
}
```

### R3. Finding 校验

Python 必须在发布前校验 finding。

必须校验：

- JSON 可解析。
- 必填字段存在。
- `severity` 使用 GitLab discussions API 枚举：`suggestion`、`minjor`、`major`、`fatal`。
- `confidence` 为允许值。
- `old_path` / `new_path` 属于本次 MR diff。
- `old_line` / `new_line` 能映射到 diff 中可评论行。
- `evidence` 非空。
- `suggestion` 非空。
- finding 满足发布阈值。

默认发布阈值：

- `severity in {fatal, major}`
- `confidence == HIGH`

### R4. 支持 GitLab old / new line inline position

Phase2 第一版使用 GitLab discussions API 的 position 行号语义：

- 新增行：`old_line = -1`，`new_line = <新增后的行号>`。
- 删除行：`old_line = <删除前的行号>`，`new_line = -1`。
- 修改行或上下文行：按 diff position map 中可构造的 `old_line` / `new_line` 组合发布。

无法构造合法 position 的 finding 不发布，只写本地报告。

构造 position 所需的 `base_sha`、`start_sha`、`head_sha` 必须来自 GitLab MR 详情 API 的 `diff_refs`，不能只用本地 `merge-base` 结果猜测。

### R5. Finding 幂等

发布前必须读取远端 MR discussions，查找 AI marker。

Marker 格式：

```markdown
<!-- ai-cr:finding:{project}:{mr_iid}:{head_sha}:{rule_id}:{old_path}:{new_path}:{old_line}:{new_line} -->
```

如果远端已存在相同 marker：

- 不重复发布。
- 本地报告记录为 `skipped_duplicate`。

### R6. 本地报告

每次 webhook review 生成：

- 机器可读 JSON 监视报告。
- 人类可读 Markdown 代码检视报告。

Markdown 报告由 Python 从结构化结果、校验结果和发布结果渲染，不由 OpenCode 直接生成。

Markdown 报告至少包含：

- MR 基础信息：repo、MR IID、source/target branch、base/head SHA。
- Review 结论：parse 状态、finding 总数、发布数量、过滤数量、重复跳过数量、失败数量。
- 已发布 inline findings。
- 被过滤 findings 及原因。
- 无法定位但值得记录的 findings。
- test gaps。
- notes。
- 未覆盖范围或失败原因。
- GitLab discussion / note id，若 API 返回可用。

### R7. 监视报告增强

Webhook 本地 JSON 监视报告需要记录：

- OpenCode JSON parse 状态。
- finding 总数。
- valid / invalid / filtered / posted / skipped_duplicate / failed 数量。
- 每个 finding 的处理结果。
- 成功发布后的 discussion id / note id。
- 非行级 notes / test_gaps 内容。
- 本地 Markdown 报告路径。
- parse failed / publish failed 的脱敏错误。

## 5. 非功能需求

- 不能泄露 GitLab token。
- 不把不可控 Markdown 直接发布到 MR。
- 测试覆盖核心 parser、validator、GitLab API client、webhook worker。
- 继续使用 UTF-8 no BOM。
- 保持现有 webhook secret、path、事件过滤、conflict skip 行为不退化。

## 6. 验收标准

- 有效 `fatal` / `major` + `HIGH` finding 会发布为 inline discussion comment。
- 重复 webhook 触发不会重复发布同一 finding。
- 非法 JSON 不发布 MR 评论，并写入本地报告。
- 无行号或无法定位的 finding 不发布 MR 评论。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT=false` 时不发布 comments，但仍生成本地报告。
- MR 上不出现整体 Markdown note。
- 本地始终生成 JSON 监视报告和 Markdown 代码检视报告。
- Phase2 收尾后，`run-once` 与 WeLink poll 不再依赖 LLM 自由 Markdown 输出；Markdown 由 Python 从结构化结果渲染。
- 全量测试通过：`uv run --group dev pytest`。
