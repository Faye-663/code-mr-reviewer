# ADR-001: GitLab MR 审查采用 one-step 还是 two-step

## Status

Accepted（已实施）

## Date

2026-07-11

## Context

当前 review core 对所有 GitLab MR 固定执行两次 Agent 调用：第一步生成结构化 MR 概要，第二步将该概要作为上下文生成结构化 review JSON。两步共享同一任务超时预算；概要只写入本地报告，不发布到 MR。

该设计增加了一次模型调用、延迟和一个会阻断第二步的失败点。现有第一步产物主要包含变更概述、变更区域、行为变化、风险区域和测试变化；第二步仍必须自行读取 `Base SHA...Head SHA` diff、源码、测试和配置，才能得出可发布的 finding。对普通 MR，第一步通常不能提供第二步无法自行获取的新事实，并可能造成锚定偏差。

项目入口是 webhook 和 IM poll，不适合在每次任务执行时要求人工交互选择模式。决策优先级为代码审查质量，其次是时延，最后是 Token 成本；如果复杂 MR 的 two-step 能提升审查质量，可以接受约两倍的时延和 Token 消耗。

## Decision

采用“普通 MR one-step，显式 Deep Review MR two-step”的模式。

### 路由规则

- 默认使用 one-step，直接基于 MR 范围和 `code-review` skill 生成结构化 review JSON。
- MR title 去除前导空白后，以 `【Deep-Review】` 开头时使用 two-step；前缀匹配忽略大小写。
- 初始版本不根据文件数、diff 行数、目录数量或风险关键词自动判断复杂度；`【Deep-Review】` 是进入 two-step 的唯一条件。
- webhook 与 IM poll 使用相同的 title 路由规则。
- 仅修改 MR title 不触发新的 webhook review。下一次 open、reopen 或 source update 事件发生时，使用事件中的最新 title 决定模式。IM poll 在处理请求时使用 GitLab MR API 返回的最新 title。

### one-step 职责

- Agent 直接审查 `Base SHA...Head SHA` 的 diff、源码、测试和配置，并输出结构化 review JSON。
- Python 使用 Git 元数据和 findings 生成本地报告中的 Discoveries；不为了生成展示性概要增加一次前置 Agent 调用。

### two-step 职责

- 第一步生成“审查计划”，而不是仅供展示的 MR 摘要，也不输出 finding。
- 审查计划应包含变更意图、关键文件或调用路径及其验证理由、外部契约、状态不变量、事务或异步边界、测试风险和待确认假设。
- 第二步把审查计划视为待验证线索，不得当作事实。它必须以 MR diff、本地源码、测试和配置重新验证，可以推翻第一步结论，并必须覆盖计划未列出的变更。
- 第一阶段输出只进入第二阶段上下文和本地审计/报告，不发布到 GitLab MR。

### 可观测性

本地审计报告和必要的 INFO 调用元数据应记录：

- `review_mode`：`one-step` 或 `two-step`；
- `routing_reason`：默认路由或 title 前缀；
- 命中 two-step 时使用的规范化 marker；
- 实际 Agent 调用次数、两个阶段各自的模板版本、耗时和执行状态。

## Alternatives Considered

### 方案 A：所有 MR 固定 one-step

- 优点：一次调用，延迟和失败面更低；没有第一步对第二步造成的锚定偏差；输出契约更简单。
- 缺点：复杂、跨层或高风险 MR 无法显式获得“先建立变更地图，再验证”的额外分析阶段。
- 未选择原因：项目把审查质量放在首位，并接受复杂 MR 为质量支付额外时延和 Token。

### 方案 B：所有 MR 固定 two-step

- 优点：实现和执行路径统一；本地报告天然包含概述。
- 缺点：普通 MR 的第一步通常不提供新增价值，却固定增加成本、延迟和阶段性失败；摘要可能让第二步产生锚定偏差。
- 未选择原因：额外调用不应成为所有 MR 的固定成本。

### 方案 C：自动判断复杂 MR 并选择 two-step

- 优点：无需维护者修改 title；理论上可以自动覆盖跨层、高风险和多主题变更。
- 缺点：文件数、diff 行数和关键词都不能稳定代表审查复杂度；误路由难解释，也缺少足够评测数据来确定阈值。
- 未选择原因：初始版本优先采用明确、可审计的 title 路由，积累真实质量数据后再评估自动路由。

## Two-step 的必要性与预期价值

two-step 仅在第一步能够降低第二步的真实认知负担时成立。例如，订单取消同时修改订单状态、库存回补事件和异步消费者时，第一步可标出 `OrderCancelService -> InventoryEventPublisher -> InventoryConsumer` 链路，并要求第二步验证：

- 订单事务提交失败时不能发布事件；
- 重复投递不得重复回补库存；
- 消费者更新库存状态时必须保持幂等。

第二步仍必须用 MR diff、源码和测试验证这些线索。对于只修改一个局部算法、机械重命名、生成文件、格式化或依赖锁文件的 MR，第一步通常无法形成有价值的跨边界验证路线，应保持 one-step。

## Validation After Implementation

- 使用普通、跨层、高风险、重构和机械变更等代表性 MR 样本，比较有效 finding、人工确认的误报/漏报、总耗时、调用次数和阶段失败率。
- 验证 Deep Review 的第一步计划是否带来可复核的新增覆盖，而非重复摘要或增加锚定偏差。
- 持续记录 `【Deep-Review】` 的使用频率和 two-step 的质量收益；没有数据支持前，不增加自动复杂度路由。
- 如果未来引入自动路由，应通过新的 ADR 补充或 supersede 本决策，明确阈值、例外、优先级和回滚方式。

## Consequences

- 本 ADR 接受的是目标行为；当前代码仍是固定 two-step，实现必须作为后续独立任务完成。
- webhook 需要从 payload 保存 MR title；IM poll/review core 需要保留 MR API 返回的 title，并在进入 Agent 调用前统一计算路由结果。
- one-step 与 two-step 需要不同的模板和解析流程；two-step 第一阶段现有 summary 契约需要替换为审查计划契约。
- title 前缀允许 MR 作者显式请求更深审查，并会增加调用时延和 Token；该成本是本决策接受的质量取舍。
- 仅修改 title 不触发 review，使用者必须在创建 MR 前添加前缀，或等待下一次代码 push、reopen 等既有触发事件。
- 实施时必须同步 README、webhook 文档、skill 描述、prompt 模板、测试和运行审计字段。
