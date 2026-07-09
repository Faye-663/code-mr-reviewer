# Phase2 Webhook Inline Review 实施计划

## 1. 实施原则

- 先写清测试契约，再在同一逻辑 commit 内提交最小实现，避免把预期失败测试单独提交到分支。
- 每个 commit 保持可运行。
- 混合 code / tests / docs 时分批提交。
- 每个逻辑 commit 通过测试后，进入下一 commit 前做 docs-sync 检查。
- Phase2 前半段只改 webhook 发布链路；Phase2 收尾再把 `run-once` 和 WeLink poll 同步迁移到 LLM JSON + Python Markdown renderer。
- 不修改 WeLink poll 的触发识别、白名单、OneBox 上传和群通知协议。
- 不引入 repo profile、RAG、多 Agent。

## 2. Commit Plan

### Commit 1: `feat: add structured review result contract`

边界：

- 新增结构化 review result model 和 JSON parser。
- 结构化 schema 使用 GitLab discussions API severity 枚举：`suggestion`、`minjor`、`major`、`fatal`。
- finding 使用 `old_path` / `new_path` / `old_line` / `new_line` 表达 position，不再使用内部 `BLOCKER` / `MAJOR` / `line_type` 作为发布契约。
- 不接入 webhook worker，不发布 GitLab discussions。

完成标志：

- 合法 JSON 可解析为内部结构。
- 非法 JSON 返回 parse failed，不 fallback 到 Markdown 发布。
- 缺必填字段、非法 severity、非法 confidence 可被 parser 或 validator 前置发现。
- 当前测试全量通过。

下次开始条件：

- parser/model 已能稳定表达后续 validator 所需字段。
- 没有入口行为变化。

涉及文件：

- `tests/test_core.py`
- 可新增 `tests/test_review_result.py`
- 可新增 parser/model module

验证：

```powershell
uv run --group dev pytest
```

### Commit 2: `feat: validate inline positions from mr detail`

边界：

- 新增 GitLab MR 详情 API 读取，使用 `diff_refs.base_sha` / `diff_refs.start_sha` / `diff_refs.head_sha` 构造 discussion position。
- 新增 diff position map。
- 新增 finding validator。
- 支持新增行 `old_line=-1`、删除行 `new_line=-1`。
- 不发布 discussions。

完成标志：

- 可分类 `publishable` / `filtered` / `invalid` / `monitor_only`。
- 无行号、错文件、错行号不会进入 `publishable`。
- 默认阈值只允许 `fatal` / `major` + `HIGH`。
- position 中的 SHA 来自 MR 详情 API，而不是本地 `merge-base` 猜测。

下次开始条件：

- validator 输出能直接交给 publisher。
- MR 详情 API 失败路径已有测试覆盖。

涉及文件：

- `src/mr_reviewer/gitlab.py`
- `src/mr_reviewer/git.py`
- `src/mr_reviewer/reviewer.py`
- 新增 validator module
- tests

验证：

```powershell
uv run --group dev pytest
```

### Commit 3: `feat: publish webhook findings as gitlab discussions`

边界：

- 新增 GitLab discussions GET/POST。
- Webhook worker 改为 inline publisher。
- 下线 webhook notes API 调用。
- 增加 marker 幂等。
- 只改 webhook 发布链路，不迁移 `run-once` / WeLink 输出协议。

完成标志：

- 有效 finding 发布 inline discussion。
- 已存在 marker 时跳过。
- `MR_REVIEWER_WEBHOOK_POST_COMMENT=false` 不发布。
- GitLab discussions POST body 使用 API severity 枚举。
- 发布结果写入监视报告。

下次开始条件：

- webhook 成功、重复、禁用、单条发布失败路径都有测试。
- webhook 不再调用 `post_mr_note`。

涉及文件：

- `src/mr_reviewer/gitlab.py`
- `src/mr_reviewer/webhook.py`
- tests

验证：

```powershell
uv run --group dev pytest
```

### Commit 4: `feat: render webhook markdown review reports`

边界：

- 新增 Markdown renderer。
- 每次 webhook review 生成 `.md`。
- JSON 监视报告记录 `markdown_report_path`。
- 不把 Markdown 发到 MR。

完成标志：

- 成功、parse failed、publish failed 都有本地 Markdown 报告。
- Markdown 包含结构化 finding、过滤原因、发布结果、notes、test gaps 和失败原因。
- Markdown 报告路径写入 JSON 监视报告。

下次开始条件：

- webhook 本地报告链路完整，后续可复用 renderer 迁移其它入口。

涉及文件：

- `src/mr_reviewer/webhook.py`
- 新增 markdown report renderer module
- tests

验证：

```powershell
uv run --group dev pytest
```

### Commit 5: `feat: migrate run-once and welink to structured reports`

边界：

- `run-once` 改为 LLM 返回 JSON、Python 渲染 Markdown 后输出。
- WeLink poll 改为 LLM 返回 JSON、Python 渲染 Markdown 后上传 OneBox。
- 复用前面 webhook 的 parser、validator 和 Markdown renderer。
- 不改变 WeLink poll 的触发识别、白名单、OneBox 上传和群通知协议。

完成标志：

- `run-once` / WeLink 不再依赖 LLM 自由 Markdown 输出。
- 结构化 parse failed 时能生成可读失败报告。
- 现有 WeLink 输出通道仍上传 Markdown 文件。

下次开始条件：

- 所有入口已统一 review 输出协议，文档可以声明 Phase2 最终行为。

涉及文件：

- `src/mr_reviewer/reviewer.py`
- `src/mr_reviewer/cli.py`
- `src/mr_reviewer/welink.py`
- `src/mr_reviewer/webhook.py`
- `.opencode/skills/code-review/SKILL.md`
- tests

验证：

```powershell
uv run --group dev pytest
```

### Commit 6: `docs: document phase2 structured review behavior`

边界：

- 只改文档。

完成标志：

- README、Webhook Quick Start、DESIGN 说明：
  - MR 只发 inline comment。
  - LLM 输出 JSON，Python 渲染 Markdown。
  - `run-once` 和 WeLink poll 使用同一结构化输出协议。
  - 本地保留 Markdown 报告。
  - notes API 不再用于 webhook。
  - JSON/Markdown 报告路径和失败策略。

下次开始条件：

- docs-sync 检查确认 README、quickstart、design 和 Phase2 文档没有行为冲突。

涉及文件：

- `README.md`
- `docs/WEBHOOK_QUICKSTART.md`
- `docs/DESIGN.md`
- 可选更新 `docs/PHASE2_WEBHOOK_INLINE_REVIEW_REQUIREMENTS.md`
- 可选更新 `docs/PHASE2_WEBHOOK_INLINE_REVIEW_DESIGN.md`
- 可选更新 `docs/PHASE2_WEBHOOK_INLINE_REVIEW_IMPLEMENTATION_PLAN.md`

验证：

```powershell
uv run --group dev pytest
git diff --check
```

## 3. 整体验收

- [ ] webhook review 不调用 notes API。
- [ ] webhook 有效 finding 发布到 GitLab inline discussion。
- [ ] 重复 webhook 不重复发布相同 finding。
- [ ] 非法 JSON 不发布 MR 评论。
- [ ] 无可靠行定位的内容只写本地报告。
- [ ] `MR_REVIEWER_WEBHOOK_POST_COMMENT=false` 行为保持有效。
- [ ] MR 上不出现整体 Markdown note。
- [ ] 本地生成 `.json` 监视报告和 `.md` 代码检视报告。
- [ ] `run-once` 和 WeLink poll 使用 LLM JSON + Python Markdown renderer。
- [ ] README / docs 与代码行为一致。
- [ ] 全量测试通过。

## 4. 风险与缓解

- Risk: GitLab discussions position 校验复杂，容易出现 500。
  - Mitigation: validator 先严格校验 diff old/new line；发布失败写监视报告，不 fallback 到 notes。
- Risk: OpenCode 输出 JSON 不稳定。
  - Mitigation: prompt 强约束 JSON；parse failed 不发布；后续再考虑一次修复重试。
- Risk: 全部改为 inline 后没有 MR summary。
  - Mitigation: Phase2 明确接受该取舍；summary/test gaps 只进本地报告。
- Risk: `run-once` / WeLink 迁移过早会扩大单次变更范围。
  - Mitigation: 先完成 webhook inline 闭环，再用独立 commit 复用 renderer 迁移其它入口。
- Risk: 远端 marker 读取失败导致无法幂等。
  - Mitigation: GET discussions failed 时不发布新评论，避免刷屏。
