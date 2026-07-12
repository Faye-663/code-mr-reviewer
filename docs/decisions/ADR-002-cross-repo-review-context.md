# ADR-002: 跨仓 MR 与内部依赖采用显式 ReviewSet 和精确源码上下文

## Status

Proposed（已形成推荐方向，功能尚未实施，`ReqID` API 契约待补充）

## Date

2026-07-12

## Context

当前 review core 每次只 clone 一个 GitLab MR 仓库，并要求 Agent 基于一个 base/head range 输出 finding。这一边界适合大多数单仓变更，但无法可靠处理：

1. 多个不同仓库的 MR 共同实现同一需求，契约只有组合后才能验证。
2. 单个 MR 的改动依赖同组织 SDK 的类型、空值、枚举、序列化或异常语义，需要读取其实际依赖版本源码。

简单扩大 Agent 的搜索范围不能解决问题。跨仓结论必须知道哪些 MR 属于同一变更集、依赖实际解析为何版本、源码对应哪个不可变 ref，以及 finding 应回写哪个 MR。

本决策优先保证 review 证据质量和可复现性，其次考虑时延和 clone 成本。首期只提供建议，不作为合并门禁。

## Decision

### 1. 多 MR 使用显式 ReviewSet

- ReviewSet 只由一条 WeLink IM 消息中的 2–3 个不同项目 MR URL 显式创建。
- 系统读取 GitLab MR 详情中的非空 `ReqID`，仅在全部相同时继续；缺失或不一致时拒绝整个请求。
- webhook 保持单 MR 事件处理，不增加聚合状态、等待窗口或“变更集完整”推断。
- 联合检视固定 two-step：先建立跨仓审查计划，再重新验证所有成员 diff；覆盖每个 MR 自身问题和组合问题。
- 生成一个聚合报告。HIGH major/fatal finding 按 targets 回写责任 MR：可定位时使用 inline discussion，否则使用普通 comment。

`ReqID` 的真实 API 字段路径、类型与空值语义是本 ADR 转为 Accepted 和开始生产实现前必须补齐的外部契约。

### 2. 单 MR 使用确定性的 Dependency Context Resolver

- IM 与 webhook 的单 MR review core 都可补充内部依赖源码上下文，但不改变现有 title one-step/two-step 路由。
- 首期只支持可静态确定的 Maven 子集，只选择 changed module 的直接内部依赖，最多 3 个。
- 部署侧中央 JSON 目录提供 `GAV -> GitLab project + tag template + package prefixes` 映射。
- resolver 必须 clone 实际版本对应的精确 tag，并记录 commit SHA；不允许 fallback 到默认分支。
- 无法解析、映射或 clone 时继续单仓 review，但明确标为 degraded 并列出未验证风险。

### 3. 由 Python 控制信任边界和发布

- Python 负责 MR/ReqID 校验、checkout、manifest、结果 schema、diff position、marker 和发布目标。
- Agent 只能把 manifest 中的成员/依赖作为证据，不能自行增加 repo、URL、SHA 或评论目标。
- 仓库内容包括 Agent 指令文件均视为待审查数据，不能覆盖自动 review 的系统/skill/output 契约。
- ReviewSet 任一准备、Agent 或结构化校验阶段失败时不产生部分评论。

## Alternatives Considered

### 方案 A：每个 MR 独立 review，不做联合检视

- 优点：完全复用现有流程，调用和故障边界简单。
- 缺点：无法验证未合入配套变更的组合契约，可能同时产生漏报和错误归因。
- 未选择原因：不能解决多 MR 同一需求这一核心问题。

### 方案 B：webhook 自动按需求聚合多个 MR

- 优点：无需用户发送联合请求，自动化程度高。
- 缺点：单个 webhook 无法证明变更集已经完整；需要持久化聚合、等待窗口、更新/撤销、重复触发和超时策略。
- 未选择原因：首期复杂度高且容易在缺少成员时给出误导结论。IM 消息能明确表达一次封闭的 ReviewSet。

### 方案 C：clone 所有内部依赖仓库

- 优点：Agent 理论上可读取更多源码。
- 缺点：大型服务依赖数量无界，默认分支可能与实际制品版本不一致，网络、磁盘和上下文噪声显著增加。
- 未选择原因：更多代码不等于更正确的证据；必须先确定版本和相关性。

### 方案 D：通过 MCP 下载 JAR 并反编译

- 优点：缺少源码或源码映射时仍可能读取公开字节码行为。
- 缺点：MCP 只解决传输，不能证明版本选择正确；反编译丢失源码信息，也增加工具和安全边界。
- 未选择原因：组织源码和制品均可访问，首期应先建立精确源码映射；JAR 反编译明确不在范围内。

### 方案 E：优先使用 sources JAR

- 优点：比完整 clone 轻量，并与制品版本绑定。
- 缺点：通常缺少测试、构建配置和历史上下文，需要额外制品下载路径。
- 未选择原因：首期限制最多 3 个依赖，选择精确 tag clone 能用更简单的单一来源满足所需上下文。

### 方案 F：由 Agent 自行发现和下载相关仓库

- 优点：确定性代码较少，可利用 Agent 推理相关性。
- 缺点：结果不可复现，可能越权读取仓库、选错版本或受仓库内提示影响。
- 未选择原因：访问范围、版本、证据和发布目标必须由 Python 校验和审计。

### 方案 G：运行 Maven/Gradle 解析完整依赖图

- 优点：能覆盖 parent、BOM、profile 和 Gradle 动态逻辑，解析结果更接近真实构建。
- 缺点：Gradle 和 Maven extension/plugin 配置可能执行 MR 中的任意代码，需要隔离运行环境和凭据治理。
- 未选择原因：首期明确不执行被检视仓库代码；超出静态 Maven 子集时选择降级。

## Consequences

### Positive

- 跨仓 finding 有明确的成员、版本、源码 ref、证据和责任 MR。
- 联合任务通过显式 IM 请求避免 webhook 聚合时机的不确定性。
- 单 MR 在上下文缺失时仍可提供原有 review 价值，同时不会伪装为已完成跨仓验证。
- 现有单 MR title 路由、MR range 和 finding 契约可以保持兼容。

### Negative

- ReviewSet 引入新的 domain model、prompt/result schema、聚合报告和多目标发布逻辑。
- 联合任务固定两次 Agent 调用并最多 clone 3 个 MR，时延和失败面高于单 MR。
- 单 MR 依赖质量取决于中央 GAV 目录和 release tag 约定的准确性。
- 静态 Maven 子集无法覆盖动态 profile、远程 BOM、Gradle 和运行时组合，只能显式降级。
- 多仓 Agent workspace 扩大了提示注入和文件访问面，必须先通过 adapter spike。

### Operational constraints

- 首期结果仅建议，不作为合并门禁。
- 不增加持久 clone cache；先采集 clone 耗时、上下文完整率和复用需求。
- 不得在没有精确 tag 时 fallback 默认分支。
- 不得静默忽略未解析或未入选的内部依赖。
- 实施完成后将稳定行为折回 README、DESIGN 和入口指南，删除临时 requirements/implementation plan；ADR 保留。

## Future Decisions

以下能力必须通过新的 ADR 补充或 supersede 本决策：

- CI 生成 resolved dependency manifest，替代或补充静态 Maven resolver。
- 在隔离环境执行 Maven/Gradle dependency resolution、编译或测试。
- webhook 自动聚合关联 MR。
- JAR/sources JAR、开源三方件或供应链安全分析。
- 将跨仓 review 结果接入合并门禁。
