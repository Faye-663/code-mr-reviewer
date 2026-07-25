# GitLab Discussion 格式优化

## Problem Statement

如何让普通单 MR 与 ReviewSet 的 AI discussion 在 GitLab diff 页面中更容易被 reviewer 快速判断，同时让 MR 作者紧接着获得可执行的修复建议，并避免重复平台已经展示的 severity 信息。

## Recommended Direction

采用“证据优先、元数据折叠”的统一信息层级：

1. 标题保留 `🤖 AI Review` 来源标识，直接描述问题，不重复显示 severity；ReviewSet 额外标明类型。
2. 展开显示“判断依据”，帮助 reviewer 判断 finding 是否可信。
3. 展开显示“影响”，说明问题为何值得处理。
4. 展开显示“建议”；存在可靠的具体代码方案时使用带语言标识的普通 fenced code block。
5. 将 confidence、rule、模型名及 ReviewSet issue 等审查元数据放入 `<details>` 折叠区。
6. 幂等 marker 继续作为不可见 HTML comment 放在正文末尾。

普通单 MR inline discussion 不重复展示 GitLab 已提供的文件和行号。ReviewSet discussion 使用相同骨架，但“判断依据”按成员列出证据来源。普通 note 没有合法位置时不伪造文件或行号。

## Key Assumptions to Validate

- [x] 当前 GitLab 版本能在 discussion 中正确渲染 `<details>`。
- [x] fenced code block 在 discussion 中正确换行并显示语法高亮。
- [x] GitLab 平台展示的 severity 足够醒目，标题无需重复。
- [x] HTML comment marker 在渲染结果中不可见。
- [x] “判断依据 → 影响 → 建议”的展开层级符合人工验证预期。
- [ ] Agent 能稳定做到“只有存在可靠的具体代码方案时才输出代码块”，不会为了满足格式生成不可靠的示例代码。

## MVP Scope

- 同时调整普通单 MR 与 ReviewSet 的 discussion renderer。
- 删除正文标题中的 severity 和模型名；保留提交给 GitLab API 的 severity 字段。
- 单 MR discussion 展示当前结构化 finding 中已有的 evidence。
- 统一使用“判断依据”“影响”“建议”三个展开区块。
- suggestion 中存在 fenced code block 时原样交给 GitLab Markdown 渲染。
- 添加折叠的审查元数据区。
- ReviewSet 证据按成员、文件和行号分项展示。
- 保持 marker 内容、位置和去重行为不变。
- 用 UT 覆盖两类 renderer、折叠元数据和多行代码块。

## Not Doing

- 不自动生成 GitLab `suggestion` block — 当前契约没有精确替换范围和 replacement code，无法安全支持一键应用。
- 不使用 GitLab Alert 作为默认 severity 展示 — 平台已经显示 severity。
- 不修改 finding severity、confidence、发布门槛或 JSON Schema — 本次只优化展示格式和 suggestion 内容指导。
- 不修改 Agent review 的判断逻辑 — 仅允许建议字段在适合时包含普通 Markdown 代码块。
- 不为每种 severity 维护不同正文模板 — 避免重复平台能力和增加格式分支。
- 不把完整本地报告复制进 discussion — 保持 discussion 聚焦单个可行动 finding。

## Open Questions

- 生产样本中，Agent 是否能稳定选择正确的代码块语言并保持 JSON 转义合法？
- 真实 finding 累积后，“判断依据”和“影响”是否存在可进一步合并的重复表达？
