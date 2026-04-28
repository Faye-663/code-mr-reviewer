# 设计方案图

本项目的核心路径是：从 WeLink 群消息中识别 GitLab MR URL，在临时目录 clone 并拉取 MR 的 target/source 分支，然后在本地仓库目录调用 opencode 生成 Markdown review 报告，最后上传报告文件并发送群通知。

```mermaid
flowchart TD
    A["WeLink 群消息"] --> B["poll: 查询群历史"]
    B --> C["parse_poll_output: 解析 respData.chatInfo"]
    C --> D{"should_trigger_review"}
    D -- "未 @Bot / 不在白名单 / 无 MR URL" --> E["跳过消息"]
    D -- "命中 GitLab MR URL" --> F["GitLabClient: 获取 MR 元数据"]
    F --> G["GitLabClient: 获取 target/source repo URL"]
    G --> H["GitClient: clone target repo"]
    H --> I{"source repo 是否不同"}
    I -- "是，fork MR" --> J["添加 source remote"]
    I -- "否" --> K["使用 origin"]
    J --> L["fetch target/source branch"]
    K --> L
    L --> M["checkout diff_refs.head_sha"]
    M --> N["生成本地 diff 并检查资源限制"]
    N --> O["OpenCodeRunner: 在 repo 目录运行 opencode"]
    O --> P["生成 Markdown review 报告"]
    P --> Q["写入临时 .md 文件"]
    Q --> R["WeLink OneBox 文件上传"]
    R --> S["WeLink 群通知报告文件名"]
    S --> T["StateStore: 标记消息已处理"]
    T --> U["清理临时 repo 和报告文件"]
```

## 模块边界

- `cli.py`：命令入口、轮询循环、WeLink 上传与通知编排。
- `im.py`：WeLink 历史消息解析、字段归一化、触发条件判断。
- `gitlab.py`：GitLab MR URL 解析、MR 元数据与项目 clone URL 查询。
- `git.py`：临时 clone、fork remote 处理、分支 fetch、checkout、diff 与资源限制。
- `reviewer.py`：串联 GitLab、Git 和 opencode 的 review 主流程。
- `opencode.py`：opencode CLI 调用、debug 参数、prompt 日志脱敏。
- `state.py`：本地去重状态文件，避免重复处理同一条 IM 消息。
