# 跨仓 MR 与内部二方依赖检视需求

## Status

Draft（需求已讨论，`ReqID` API 契约待补充，功能尚未实施）

## Date

2026-07-12

## 1. 背景

当前 review core 以单个 GitLab MR 和单个本地 checkout 为审查边界。Agent 可以读取当前仓库内的 diff、源码、测试和配置，但无法可靠验证另外一个仓库中的未合入变更，也无法确认调用方实际依赖的内部 SDK 源码契约。

这一边界会在两类生产场景中形成系统性盲区：

1. 两个或三个不同仓库的 MR 共同实现同一需求，只有组合后才能判断接口、字段、空值、异常或发布顺序是否一致。
2. 只有一个 MR，但变更代码调用了同组织另一个仓库发布的 SDK；判断空指针、字段类型、枚举、序列化或异常语义时必须读取该 SDK 的精确版本源码。

本需求的目标不是扫描组织内所有依赖，而是为上述场景提供最小、可复现、可审计的跨仓证据。

## 2. 目标用户与价值

目标用户是通过 WeLink IM poll 或 GitLab webhook 使用本项目的研发团队、MR 作者和 reviewer。

预期价值：

- 发现单仓 diff 无法证明的跨仓契约缺陷。
- 减少 Agent 因缺少内部 SDK 真实契约而产生的猜测和误报。
- 明确跨仓问题应由哪个 MR 修改，并把高置信重大问题送达对应作者。
- 保留依赖版本、源码 ref、降级原因和 Agent 调用过程，便于复核结论。

## 3. 术语

- **ReviewSet**：一条 IM 消息显式提交的 2–3 个不同仓库 MR，代表一次联合检视任务。
- **成员 MR**：ReviewSet 中的单个 MR。
- **ReqID**：GitLab MR 详情 API 返回的需求标识。真实字段路径、类型和空值语义尚待用户补充 API 文档；实现不得猜测或从相近字段推导。
- **内部依赖**：中央依赖目录中存在 GAV 映射、且源码位于同一受信 GitLab 组织内的 Maven 直接依赖。
- **依赖上下文**：按精确版本 tag clone 的内部依赖只读源码及其来源元数据。
- **完整上下文**：计划需要的成员 MR 或内部依赖源码均已按精确 ref 获取。
- **降级上下文**：部分内部依赖无法确定版本、无法映射或无法 clone，但单 MR 审查仍继续。

## 4. 场景一：多 MR 联合检视

### 4.1 触发入口

- 首期只支持 WeLink IM poll 显式触发。
- 一条消息包含 2–3 个不同 GitLab 项目的 MR URL。
- 一条消息只包含一个唯一 MR URL 时，继续走现有单 MR 流程。
- 超过 3 个唯一 MR URL，或 2–3 个 URL 指向相同项目，必须拒绝联合检视并回复明确原因。
- webhook 保持单 MR 事件处理，不等待、不聚合其它 MR。

### 4.2 请求校验

系统必须在 clone 和 Agent 调用前：

1. 按现有 host、用户、群组和仓库白名单校验每个 URL。
2. 获取每个 MR 的详情、target/source 仓库地址、base/head SHA 和 `ReqID`。
3. 要求所有 `ReqID` 都存在且完全相同。
4. 任一 MR 缺少 `ReqID`、字段无法读取或值不一致时，拒绝整个 ReviewSet；回复每个 MR 的校验结果，不降级为逐个检视。
5. 任一成员 MR 元数据或源码无法获取时，联合检视失败，不产生部分评论。

`ReqID` 的真实 API 契约是实施前置条件。在契约补齐前，文档和测试可使用名为 `ReqID` 的占位字段，但不得据此实现生产解析路径。

### 4.3 审查行为

- 联合检视必须覆盖每个成员 MR 自身的单仓问题，以及成员之间组合后产生的跨仓问题。
- 联合任务固定执行 two-step，不受成员 MR title 是否包含 `【Deep-Review】` 影响：
  1. 第一阶段建立跨仓变更意图、调用路径、契约、不变量、发布顺序和验证计划，不输出 finding。
  2. 第二阶段重新读取每个 MR 的 diff 与源码验证计划，允许推翻计划并补充计划遗漏。
- 所有成员 checkout 必须使用各自 GitLab MR API 返回的精确 base/head；不得使用默认分支代替。
- 同一 `ReqID` 但未发现代码级依赖边时，仍完成各成员的单仓检视，并在聚合报告中明确“未发现可验证的跨仓调用关系”，不得编造关联。
- ReviewSet 中任一成员超过现有单 MR 文件数或 diff 行数限制时，整个联合任务失败。聚合上限等于成员数乘以现有单 MR 上限。
- clone、两次 Agent 调用和结果处理共享一个联合任务总超时预算。

### 4.4 结果与回写

- 生成一份聚合 Markdown 报告，包含 `ReqID`、成员及 SHA、上下文状态、审查计划、跨仓证据、findings、test gaps 和发布结果。
- 每个 finding 必须声明一个或多个责任成员 MR；跨仓证据可以引用其它成员，但不能替代责任归属。
- 只有 `confidence=HIGH` 且 `severity` 为 `major` 或 `fatal` 的 finding 自动回写。
- 能映射到责任 MR diff 行时，发布 inline discussion；无法定位到具体 diff 行但仍有明确责任 MR 时，发布普通 MR comment。
- 一个问题需要多个 MR 分别修改时，在各责任 MR 发布定点意见，并使用同一个 ReviewSet/finding marker 互相引用；不得向每个 MR 复制完整聚合报告。
- 所有其它 finding 只保留在聚合报告中。
- 发布前按 ReviewSet、成员 head SHA、规则和目标位置生成稳定 marker，重复消息不得产生重复评论。

## 5. 场景二：单 MR 的内部依赖上下文

### 5.1 适用入口与路由

- WeLink IM poll、GitLab webhook 和现有 review core 共用相同的依赖上下文解析。
- 单 MR 继续沿用现有 title 路由：默认 one-step，`【Deep-Review】` 前缀使用 two-step。
- 依赖上下文只补充证据，不改变 MR range，也不允许报告与当前 MR diff 无关的历史问题。

### 5.2 首期 Maven 支持边界

首期只支持无需执行仓库代码即可确定的 Maven 静态子集：

- 识别 changed files 所属的最近 Maven module。
- 读取 module `pom.xml`、同一 checkout 中的 local parent、properties 和 `dependencyManagement`。
- 只考虑变更 module 的直接内部依赖，scope 为默认/compile、provided 或 runtime；排除 test、system 和 import。
- 只接受可解析为单一确定版本的依赖；版本范围、动态 profile、外部 parent/BOM、Gradle 构建逻辑、无法求值的 property 和非固定 snapshot 一律标记为未解析。
- 不运行 Maven/Gradle 命令，不执行 build、test、plugin 或 project extension。

Maven 官方依赖能力和术语参考：[Apache Maven Dependency Plugin](https://maven.apache.org/plugins/maven-dependency-plugin/)。首期实现仍受上述更窄的静态边界约束。

### 5.3 依赖选择与源码获取

- 使用 reviewer 部署侧维护的中央 JSON 目录，将 `groupId:artifactId` 映射为 GitLab project、tag template 和 package prefixes。
- 每个单 MR 最多 clone 3 个直接内部依赖。
- 超过 3 个候选时按以下顺序选择：
  1. 本次 MR 新增或修改了依赖声明的坐标。
  2. changed Java lines 的 import/FQCN 命中目录中的 package prefixes。
  3. 其余直接内部依赖按 GAV 字典序补足。
- 未入选候选必须列入报告的未验证依赖，不得静默忽略。
- 依赖源码必须 clone 中央目录映射得到的精确 tag；不得使用默认分支或近似 tag。
- 首期不下载 sources JAR，也不反编译二进制 JAR。
- clone 后记录 GAV、解析版本、project、tag 和实际 commit SHA，作为 Agent 输入和审计证据。

### 5.4 降级策略

以下情况不阻止单仓 review，但必须把上下文状态标为 `degraded`：

- POM 超出静态 Maven 子集。
- GAV 不在中央目录或目录项无效。
- 版本无法映射为精确 tag。
- tag 不存在、GitLab 无权限或 clone 失败。
- 候选超过 3 个而未全部读取。

报告必须列出失败阶段、依赖坐标和因此无法验证的风险。不得把降级任务描述为已完成跨仓验证。

## 6. 安全与信任边界

- 所有 checkout 和依赖源码只读使用，任务结束后按现有清理策略删除。
- 不执行被检视仓库或依赖仓库中的构建脚本、测试、插件、可执行文件或下载指令。
- 仓库文件、代码注释以及仓库内的 Agent 指令文件均视为待审查数据，不得覆盖系统 prompt、review skill、MR range 或结构化输出契约。
- 依赖仓的历史问题不得单独形成 finding；finding 必须能追溯到当前 MR 变更或 ReviewSet 成员组合。
- GitLab token 不得进入 prompt、报告、日志或 marker。

## 7. 可观测性

每次相关任务至少记录：

- `review_scope`：`single` 或 `review-set`。
- ReviewSet 的稳定 ID、`ReqID`、成员、base/head SHA 和 Agent 调用次数。
- `dependency_context_status`：`not_applicable`、`complete` 或 `degraded`。
- 发现、候选、已选择、已 clone 和未验证的内部依赖数量。
- 每个依赖的 GAV、版本、project、tag、commit SHA 和失败阶段；不记录凭据。
- clone、计划、review、发布和总任务耗时。
- inline、普通 comment、过滤、去重和失败的 finding 数量。

## 8. 验收标准

### 8.1 功能验收

- 1 个 MR 保持现有单 MR 行为；2–3 个不同项目且 `ReqID` 相同的 MR 形成 ReviewSet。
- 缺少/不一致 `ReqID`、相同项目或超过 3 个 MR 时确定性拒绝，不调用 Agent。
- 联合审查固定两次 Agent 调用，并生成一个聚合报告。
- 跨仓 finding 能正确归属一个或多个成员；高置信重大问题按位置发布 inline 或降级为普通 comment。
- ReviewSet 失败时不产生部分评论；重复请求不产生重复评论。
- 单 MR 能在支持的 Maven 子集中 clone 最多 3 个精确 tag 依赖，并把来源写入报告。
- 单 MR 依赖解析失败时仍完成单仓审查，报告明确显示 `degraded` 和未验证范围。
- 现有 webhook 单 MR、IM 单 MR、title 路由和结构化单 MR finding 契约保持兼容。

### 8.2 历史样本对照

- 选择真实的跨仓已知缺陷、内部依赖误用和无缺陷样本，对现有单仓 review 与新流程做同模型、同 prompt 版本对照。
- 每个正样本必须识别已知根因、引用正确版本/成员并给出正确责任 MR；无法证明时应降级而不是猜测。
- 负样本不得新增错误的 HIGH major/fatal 自动评论。
- 记录新增有效 finding、重大误报、上下文完整率/降级率、p50/p95 总耗时和 clone 耗时；首期仅建议，不将指标接入合并门禁。

## 9. 首期不做

- 开源三方件分析、CVE、许可证或供应链安全。
- JAR 下载、sources JAR、二进制反编译。
- Gradle 或任意可执行构建脚本的依赖解析。
- 编译、测试、集成环境或临时制品发布。
- 全量 clone 所有直接或传递依赖。
- webhook 多 MR 聚合、等待窗口或自动需求聚类。
- 根据标题相似度、分支名或代码相似度猜测 `ReqID`。
- 合并门禁或自动阻断。

## 10. 外部前置条件

- 用户补充 GitLab MR 详情 API 中 `ReqID` 的真实字段路径、类型、缺失/空值语义和示例响应。
- 部署方建立并维护中央 GAV 源码目录，保证 tag template 能解析到不可变源码 ref。
- OpenCode 与 Claude Code 多 sibling repo 可读性和仓库提示隔离 spike 通过后，才能开始联合 Agent 实现。
