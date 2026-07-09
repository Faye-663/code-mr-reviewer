# 设计方案图

本项目有两类触发入口：WeLink IM poll 和 GitLab webhook。入口负责接收事件、过滤不可处理请求，然后把 GitLab MR 信息交给共用的 review core。review core 负责 clone/fetch/checkout、生成 diff、调用 opencode，并返回结构化 JSON review 结果；Python 侧再负责校验、inline 发布或 Markdown 渲染。

## 总体结构

```mermaid
flowchart TD
    A["WeLink IM poll"] --> B["解析群消息与 @Bot 触发条件"]
    C["GitLab webhook"] --> D["校验 path / method / secret"]
    D --> E["解析 merge_request payload"]
    B --> F["ReviewService"]
    E --> F
    F --> G["GitClient: clone / fetch / checkout / diff"]
    G --> H["OpenCodeRunner: 在 repo 目录运行 opencode"]
    H --> I["结构化 JSON review 结果"]
    I --> J["Python parser / validator"]
    J --> K["IM 入口: 渲染 Markdown 上传 OneBox 并通知群聊"]
    J --> L["webhook 入口: 发布 GitLab inline discussion"]
    J --> M["写入本地 JSON / Markdown 报告"]
```

## WeLink IM poll 流程

```mermaid
flowchart TD
    A["WeLink 群消息"] --> B["poll: 查询群历史"]
    B --> C["parse_poll_output: 解析 respData.chatInfo"]
    C --> D{"should_trigger_review"}
    D -- "未 @Bot / 不在白名单 / 无 MR URL" --> E["跳过消息"]
    D -- "命中 GitLab MR URL" --> F["GitLabClient: 获取 MR 元数据"]
    F --> G["ReviewService.review"]
    G --> H["生成结构化 JSON review 结果"]
    H --> I["Python 渲染 Markdown 报告"]
    I --> J["WeLink OneBox 文件上传"]
    J --> K["WeLink 群通知报告文件名"]
    K --> L["StateStore: 标记消息已处理"]
```

## GitLab webhook 流程

```mermaid
flowchart TD
    A["GitLab Merge Request Hook"] --> B["webhook: path / method / secret 校验"]
    B --> C["parse_gitlab_merge_request_event"]
    C --> D{"是否 open / reopen / source update 且无冲突"}
    D -- "否" --> E["返回 skipped"]
    D -- "是" --> F["WebhookReviewQueue.enqueue"]
    F --> G["ReviewService.review_target"]
    G --> H["clone / fetch / checkout / diff"]
    H --> I["OpenCodeRunner: 调用 opencode"]
    I --> J["parse JSON / validate finding position"]
    J --> K{"MR_REVIEWER_WEBHOOK_POST_COMMENT"}
    K -- "true" --> L["GitLabClient.post_mr_discussion"]
    K -- "false" --> M["跳过 inline 发布"]
    L --> N["write_webhook_monitor_report + Markdown report"]
    M --> N
```

## 模块边界

- `cli.py`：命令入口、轮询循环和 review service 装配。
- `welink.py`：WeLink poll/reply 命令执行、OneBox 上传与群通知编排。
- `webhook.py`：GitLab webhook HTTP handler、secret 校验、payload 解析、后台队列、inline discussion 发布编排和本地报告写入。
- `im.py`：WeLink 历史消息解析、字段归一化、触发条件判断。
- `gitlab.py`：GitLab MR URL 解析、MR 元数据、MR 详情 diff_refs、项目 clone URL 查询、discussions 读取与 inline discussion 发布。
- `git.py`：临时 clone、fork remote 处理、分支 fetch、checkout、diff 与资源限制。
- `reviewer.py`：共用 review core，串联 GitLab、Git 和 opencode。
- `review_result.py` / `inline_review.py` / `markdown_report.py`：结构化 JSON 解析、finding 行定位校验、GitLab inline 发布结果整理和 Markdown 报告渲染。
- `opencode.py`：opencode CLI 调用、debug 参数、prompt 日志脱敏。
- `state.py`：IM poll 的本地去重状态文件，避免重复处理同一条 IM 消息。
