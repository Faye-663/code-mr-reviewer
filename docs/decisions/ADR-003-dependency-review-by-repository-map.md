# ADR-003: Deep Review 使用项目依赖映射准备联合检视上下文

## Status

Accepted

## Date

2026-07-14

## Context

ADR-002 原计划通过静态 Maven 解析和精确版本 tag 为单 MR 补充内部依赖源码。实际部署中，项目间源码关系由研发团队维护，制品版本与源码 tag 并不总能稳定对应；继续实现 Maven/POM/GAV/version 解析会增加复杂度，却不能保证得到更接近当前协作分支的源码。

项目已经使用 MR title 前缀 `【Deep-Review】` 显式选择高成本 two-step。场景二应只在该显式入口中扩大上下文，并在上下文不完整时给出确定、保守的降级结果。

## Decision

- 部署侧只读 JSON 目录显式维护 `主项目 -> 直接依赖项目`，不解析 Maven、POM、GAV 或 dependency version，也不递归展开依赖关系。
- 普通单 MR 始终维持 one-step，不读取依赖目录。`【Deep-Review】` MR 未配置依赖时维持现有单仓 two-step。
- `【Deep-Review】` MR 配置 1–3 个依赖时，只有所有依赖仓都成功 checkout 后才执行联合 two-step；Agent 读取主 MR changed files，自行判断哪些依赖关系与本次变更相关。
- 依赖超过 3 个、目录无效或任一依赖准备失败时，不使用部分上下文，降级为单仓 one-step，并记录请求模式、实际模式和稳定降级原因。
- 每个依赖仓只 fetch 与主 MR `target_branch` 同名的分支，并 detached checkout 任务开始时解析出的 commit SHA；不得 fallback source branch、默认分支、tag 或近似 ref。
- 依赖仓只提供证据，finding 的唯一责任目标是主 MR；不得评论依赖仓或把其历史问题作为独立 finding。

## Alternatives Considered

### 静态解析 Maven 依赖并按版本 tag checkout

- 优点：能把依赖声明版本与源码 ref 直接关联。
- 缺点：需要实现 Maven effective model 的受限子集，仍无法保证组织内制品版本和源码 tag 一致。
- 未选择原因：项目级依赖关系和同名目标分支更符合当前协作流程。

### Python 根据 import、FQCN 或文件路径筛选依赖仓

- 优点：减少 clone 数量和 Agent 上下文。
- 缺点：静态字符串匹配会漏掉封装调用、反射、配置驱动或非 Java 契约。
- 未选择原因：依赖目录已经把候选限制到最多 3 个，相关性判断交给联合检视更可靠。

### 依赖准备失败时使用成功取得的部分仓库

- 优点：保留部分跨仓证据。
- 缺点：容易把不完整上下文描述为已完成联合检视，并使结果随失败组合变化。
- 未选择原因：全有或全无的降级边界更可复现、更容易审计。

## Consequences

- 场景二无需执行构建工具，也不新增 Maven 解析与版本映射代码。
- 同名 branch 是可变 ref；任务报告必须记录实际 commit SHA，且不能宣称它等同于制品版本源码。
- 显式维护的依赖目录成为联合上下文范围的事实来源，需要部署方负责审查和更新。
- `【Deep-Review】` 在依赖上下文异常时会从请求的 two-step 降级为实际 one-step，报告必须同时保留 requested/effective mode，避免路由信息失真。
- ADR-002 的 ReviewSet 决策继续有效；其场景二 Maven/tag 决策由本 ADR 修订。
