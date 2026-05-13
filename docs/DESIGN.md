# 设计方案图

本项目有两条核心路径：

1. **IM 轮询路径**：从 WeLink 群消息中识别 GitLab MR URL，触发 review。
2. **Webhook 路径**：接收 CodeHub Merge Request Hook 事件，触发 review。

两条路径共享同一套 `ReviewService` review 管线（GitLab API → clone → diff → opencode → Markdown 报告）。

## IM 轮询路径

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
    J --> L["fetch target/source branch 并 checkout"]
    K --> L
    L --> M["OpenCodeRunner: 在 repo 目录运行 opencode"]
    M --> N["生成 Markdown review 报告"]
    N --> O["WeLink OneBox 文件上传"]
    O --> P["WeLink 群通知报告文件名"]
    P --> Q["StateStore: 标记消息已处理"]
    Q --> R["清理临时 repo 和报告文件"]
```

## Webhook 路径

```mermaid
flowchart TD
    A["CodeHub Merge Request Hook"] --> B["WebhookHandler.do_POST"]
    B --> C{"校验 Token（若配置）"}
    C -- "不匹配" --> D["403"]
    C -- "通过/未配置" --> E["解析 JSON body"]
    E --> F{"parse_webhook_payload"}
    F -- "非 merge_request / 无效 action / 有冲突 / 不在白名单" --> G["200 ignored"]
    F -- "命中 GitLab MR URL" --> H["返回 200 accepted + 启动 daemon 线程"]
    H --> I["ReviewService.review"]
    I --> J["GitLabClient.post_mr_note: 回写 MR Comment"]
    J --> K["可选: WeLink 通知"]
    K --> L["清理临时 repo 和报告文件"]
```

## 模块边界

- `cli.py`：命令入口、轮询循环、webhook 子命令、WeLink 上传与通知编排。
- `im.py`：WeLink 历史消息解析、字段归一化、触发条件判断。
- `gitlab.py`：GitLab MR URL 解析、MR 元数据与项目 clone URL 查询、MR Comment 回写。
- `git.py`：临时 clone、fork remote 处理、分支 fetch、checkout、diff 与资源限制。
- `reviewer.py`：串联 GitLab、Git 和 opencode 的 review 主流程。
- `opencode.py`：opencode CLI 调用、debug 参数、prompt 日志脱敏。
- `state.py`：本地去重状态文件，避免重复处理同一条 IM 消息。
- `webhook.py`：HTTP 服务器，接收 CodeHub Merge Request Hook → 解析 payload → 触发 review → 回写 MR Comment。
