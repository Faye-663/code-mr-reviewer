# 跨仓 MR 与内部二方依赖检视需求

## Status

Active（场景一 Implemented；场景二 Draft）

## Date

2026-07-14

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
- 保留依赖项目、目标分支、实际 commit SHA、降级原因和 Agent 调用过程，便于复核结论。

## 3. 术语

- **ReviewSet**：一条 IM 消息显式提交的 2–3 个不同仓库 MR，代表一次联合检视任务。
- **成员 MR**：ReviewSet 中的单个 MR。
- **ReqID**：先按 MR URL 中的 project path 查询项目信息取得 `project_id`，再以 URL 中的 MR `iid` 调用 `GET /projects/{project_id}/isource/merge_requests/{iid}`，读取响应中 `e2e_issues[0].issue_num` 的值。该值必须是去除首尾空白后的非空字符串；实现只读取数组首元素，不从相近字段推导，也不校验后续元素。
- **项目依赖**：中央项目依赖目录为当前主项目显式列出的同一受信 GitLab 组织内的直接依赖项目。
- **依赖上下文**：依赖项目与主 MR `target_branch` 同名分支在任务开始时对应的只读源码及其来源元数据。
- **完整上下文**：计划需要的成员 MR 或内部依赖源码均已按精确 ref 获取。
- **降级上下文**：项目依赖目录或任一依赖源码无法完整准备，联合检视未执行，任务改为单仓 one-step。

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
2. 按 MR URL 的 project path 获取项目信息和 `project_id`，再用 `project_id` 与 URL 中的 `iid` 获取 isource MR 的 base/start/head SHA 和 `ReqID`；target/source 分支及项目地址继续通过现有标准 MR/project API 获取。
3. 要求所有 `ReqID` 都存在且完全相同。
4. 任一 MR 缺少 `ReqID`、字段无法读取或值不一致时，拒绝整个 ReviewSet；向当前群发送稳定原因码对应的安全短文案，不泄漏原始响应或异常，也不降级为逐个检视。
5. 任一成员 MR 元数据或源码无法获取时，联合检视失败，不产生部分评论。

MR 详情的生产接口固定为 `GET /projects/{project_id}/isource/merge_requests/{iid}`，其中 `project_id` 来自 project path 查询，`iid` 来自 MR URL。`ReqID` 的生产解析路径固定为 `e2e_issues[0].issue_num`。`e2e_issues` 缺失、不是数组、数组为空、首元素不是对象、`issue_num` 缺失、不是字符串或去除首尾空白后为空时，均视为该成员缺少有效 `ReqID`。

### 4.3 审查行为

- 联合检视必须覆盖每个成员 MR 自身的单仓问题，以及成员之间组合后产生的跨仓问题。
- 联合任务固定执行 two-step，不受成员 MR title 是否包含 `【Deep-Review】` 影响：
  1. 第一阶段建立跨仓变更意图、调用路径、契约、不变量、发布顺序和验证计划，不输出 finding。
  2. 第二阶段重新读取每个 MR 的 diff 与源码验证计划，允许推翻计划并补充计划遗漏。
- 所有成员 checkout 必须使用各自 GitLab MR API 返回的精确 base/head；不得使用默认分支代替。
- 同一 `ReqID` 但未发现代码级依赖边时，仍完成各成员的单仓检视，并在聚合报告中明确“未发现可证实的跨仓关系”，不得编造关联。
- ReviewSet 中任一成员超过现有单 MR 文件数或 diff 行数限制时，整个联合任务失败。聚合上限等于成员数乘以现有单 MR 上限。
- 联合任务在预检前建立总截止时间；固定两次 Agent 调用共享扣除预检/checkout 耗时后的剩余预算。Git 命令继续沿用现有进程边界和资源限制。

### 4.4 结果与回写

- 生成一份 basename 为 `review-set-<ReviewSet ID 前 12 位>.md` 的聚合 Markdown 报告，包含 `ReqID`、成员及 SHA、上下文状态、审查计划、跨仓证据、findings、责任位置、test gaps 和发布结果。
- 每个 finding 必须声明一个或多个责任成员 MR；跨仓证据可以引用其它成员，但不能替代责任归属。
- 只有 `confidence=HIGH` 且 `severity` 为 `major` 或 `fatal` 的 finding 自动回写。
- 能映射到责任 MR diff 行时，发布 inline discussion；`position=null` 或语法合法但无法映射到当前 diff 时，发布普通 MR note。未知成员、越界路径或非法行号不得回退发布。
- 一个问题需要多个 MR 分别修改时，在各责任 MR 发布定点意见，并使用同一个稳定 finding key 派生各 target marker；不得向每个 MR 复制完整聚合报告。
- 所有其它 finding 只保留在聚合报告中。
- 发布前按 ReviewSet、成员 head SHA、规则和目标位置生成稳定 marker，重复消息不得产生重复评论。
- `MR_REVIEWER_REVIEW_SET_POST_COMMENT` 独立于 webhook 发布开关且默认 `true`；关闭时仍生成聚合报告并将候选标为 `disabled`。开关开启但 `MR_REVIEWER_AGENT_MODEL_NAME` 为空时不发布，任务标为 `success_with_warnings`。
- 任务状态限定为 `rejected`、`failed`、`success`、`success_with_warnings`。拒绝与运行失败都会安全回复并终结原消息，重新执行必须发送新消息。

## 5. 场景二：基于项目依赖映射的 Deep Review

### 5.1 适用入口与路由

- WeLink IM poll、GitLab webhook、`run-once` 和现有 review core 共用相同的项目依赖目录与路由。
- 普通单 MR 默认 one-step，且不得读取项目依赖目录或 clone 依赖仓。
- 单 MR title 去除前导空白后以 `【Deep-Review】` 开头时（忽略大小写）才评估项目依赖目录；仅修改 title 不触发新的 webhook review，后续正常事件使用最新 title。
- `【Deep-Review】` MR 未配置依赖时继续执行现有单仓 two-step；配置 1–3 个依赖且全部准备成功时执行依赖联合 two-step。
- 依赖超过 3 个、目录无效或任一依赖准备失败时，丢弃全部依赖上下文并降级为单仓 one-step；不得执行部分联合检视。
- 依赖上下文只补充证据，不改变主 MR range，也不允许报告与当前 MR diff 无关的依赖仓历史问题。

### 5.2 项目依赖目录

- reviewer 部署侧维护只读 JSON 目录，显式记录 `主 project path -> 直接依赖 project paths`。
- 目录只表达源码仓关系，不表达 Maven 制品、GAV、版本或发布关系，也不递归展开依赖项目自己的映射。
- 主项目必须唯一；project path 和依赖 path 必须为去除首尾空白后的非空字符串；依赖不得重复或指向主项目自身。
- 未配置目录或当前项目无映射不属于失败。已配置但文件不可读、JSON/schema 非法时，Deep Review 必须降级为单仓 one-step。
- 目录允许记录超过 3 个依赖，但对应 Deep Review 必须直接降级，不得按顺序或其它规则选择前 3 个。

### 5.3 依赖源码获取与联合检视

- 配置 1–3 个依赖时，通过 GitLab project API 将受信 project path 转为 HTTPS clone URL。
- 每个依赖仓只 fetch 与主 MR `target_branch` 同名的 `refs/heads/<branch>`，再 detached checkout fetch 得到的 commit SHA；不得 fallback 到 source branch、默认分支、tag 或近似 ref。
- 只有所有依赖都成功准备后才生成 `dependency-review/v1` manifest 并执行联合 two-step；任一失败时删除已准备的部分上下文。
- Python 不按 changed file、package、import、FQCN 或 path 筛选依赖。Agent 读取主 MR changed files 后，自行判断 manifest 中哪些依赖关系与本次变更相关。
- 联合第一阶段只生成主 MR 的变更计划和待验证依赖契约；第二阶段重新读取主 MR diff 与必要依赖源码，允许推翻计划并覆盖遗漏。
- 没有发现可证实依赖关系时仍完成主 MR 的单仓检视，并明确记录“未发现可证实的依赖关系”，不得编造关联。
- finding 可以引用依赖仓作为 evidence，但唯一责任目标和发布目标是主 MR。

### 5.4 降级策略

以下情况必须把上下文状态标为 `degraded`，并将请求的 Deep Review 改为单仓 one-step：

- 目录已配置但不可读、JSON/schema 非法或当前项目目录项无效。
- 当前项目配置超过 3 个直接依赖。
- 任一依赖项目查询失败、无权限、同名 target branch 不存在或 checkout 失败。

报告必须同时记录 requested/effective review mode、稳定 reason code、失败项目和因此未验证的风险。不得把降级任务描述为已完成依赖联合检视。

## 6. 安全与信任边界

- 所有 checkout 和依赖源码只读使用，任务结束后按现有清理策略删除。
- 不执行被检视仓库或依赖仓库中的构建脚本、测试、插件、可执行文件或下载指令。
- 仓库文件、代码注释以及仓库内的 Agent 指令文件均视为待审查数据，不得覆盖系统 prompt、review skill、MR range 或结构化输出契约。
- 依赖仓的历史问题不得单独形成 finding；finding 必须能追溯到当前 MR 变更或 ReviewSet 成员组合。
- GitLab token 不得进入 prompt、报告、日志或 marker。

## 7. 可观测性

每次相关任务至少记录：

- `review_scope`：`single`、`review-set` 或 `dependency-review`。
- ReviewSet 的稳定 ID、`ReqID`、成员、base/head SHA 和 Agent 调用次数。
- `dependency_context_status`：`not_applicable`、`complete` 或 `degraded`。
- requested/effective review mode 与依赖降级 reason code。
- 每个依赖的 project、target branch、实际 commit SHA、准备耗时和失败阶段；不记录凭据。
- clone、计划、review、发布和总任务耗时。
- inline、普通 comment、过滤、去重和失败的 finding 数量。

## 8. 验收标准

### 8.1 功能验收

场景一（Implemented）：

- 1 个 MR 保持现有单 MR 行为；2–3 个不同项目且 `ReqID` 相同的 MR 形成 ReviewSet。
- 缺少/不一致 `ReqID`、相同项目或超过 3 个 MR 时确定性拒绝，不调用 Agent。
- 联合审查固定两次 Agent 调用，并生成一个聚合报告。
- 跨仓 finding 能正确归属一个或多个成员；高置信重大问题按位置发布 inline，或在目标合法但不可定位时降级为普通 note。
- 预检、Agent 或结构化解析失败时不产生评论；发布阶段单目标失败不回滚其它已发布评论，任务转为 `success_with_warnings`；重复请求不产生重复评论。
- 公开发布开关默认开启、可独立关闭；缺少 model name 时聚合报告仍生成但不发布评论。
- 现有 webhook 单 MR、IM 单 MR、title 路由和结构化单 MR finding 契约保持兼容。

场景二（Draft，尚未实现）：

- 普通 one-step 不读取目录；无依赖映射的 Deep Review 维持现有单仓 two-step。
- 配置 1–3 个依赖且全部准备成功时，固定两次 Agent 调用并生成独立依赖联合 manifest/结果。
- 依赖超过 3 个、目录无效或任一依赖准备失败时，固定降级为单仓 one-step、一次 Agent 调用，并明确显示 `degraded` 和未验证范围。
- 依赖仓只使用与主 MR target branch 同名的分支，报告记录实际 commit SHA，且 finding 只能发布到主 MR。

### 8.2 历史样本对照

- 选择真实的跨仓已知缺陷、内部依赖误用和无缺陷样本，对现有单仓 review 与新流程做同模型、同 prompt 版本对照。
- 每个正样本必须识别已知根因、引用正确成员或依赖 commit，并给出正确责任 MR；无法证明时不得猜测。
- 负样本不得新增错误的 HIGH major/fatal 自动评论。
- 记录新增有效 finding、重大误报、上下文完整率/降级率、p50/p95 总耗时和 clone 耗时；首期仅建议，不将指标接入合并门禁。

## 9. 首期不做

- 开源三方件分析、CVE、许可证或供应链安全。
- JAR 下载、sources JAR、二进制反编译。
- Maven、Gradle、POM、GAV 或 dependency version 解析。
- 编译、测试、集成环境或临时制品发布。
- 递归展开传递项目依赖，或 clone 超过 3 个依赖仓。
- webhook 多 MR 聚合、等待窗口或自动需求聚类。
- 根据标题相似度、分支名或代码相似度猜测 `ReqID`。
- 合并门禁或自动阻断。

## 10. 外部前置条件

- GitLab 项目信息 API 必须按 project path 提供 `project_id`，MR 详情 API `GET /projects/{project_id}/isource/merge_requests/{iid}` 必须继续提供精确 `diff_refs` 和 `e2e_issues[0].issue_num` 非空字符串；示例响应见仓库根目录 `gitlab_mr_api.txt`。
- 生产启用的 Agent adapter 必须通过 healthcheck。自动化契约测试覆盖 OpenCode/Claude Code 的 ReviewSet cwd 与提示隔离；本机 Claude Code sibling repo live smoke 已通过，本机未安装 OpenCode，因此未执行其 live smoke。
- 场景一首次生产验证必须先设置 `MR_REVIEWER_REVIEW_SET_POST_COMMENT=false` 对历史正反样本 dry-run，人工复核后再受控开启评论。
- 场景二开始前，部署方需要建立并维护只读项目依赖目录，并为生产实际选用的 Agent 安装 `dependency-code-review` skill。
