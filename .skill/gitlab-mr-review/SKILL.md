---
name: gitlab-mr-review
description: "Use when reviewing a GitLab MR URL with OpenCode or Claude Code through the bundled two-step script. Not for reviewing uncommitted local changes or modifying the target repository. Output: a local summary-plus-review report and, when enabled, an MR comment containing only the review."
---

# GitLab MR Review

你负责把一个 GitLab MR URL 交给确定性脚本处理。这个 skill 只做端到端编排：获取 MR 元数据、clone/fetch/checkout、先生成 MR 概要，再把概要作为上下文调用 `code-review skill`。本地 Markdown 报告使用“代码检视报告 / Discoveries / 检视意见 / 检视摘要”结构；按配置写回现有 MR 的 comment 只包含 review JSON 正文。

## 输入

用户必须提供 GitLab MR URL，例如：

```text
https://gitlab.example.com/team/project/merge_requests/7
```

如果没有明确 MR URL，先要求用户补充，不要猜测仓库或 MR 编号。

## 环境变量

- `GITLAB_BASE_URL`：GitLab 根地址，例如 `https://gitlab.example.com`。
- `GITLAB_API_BASE_URL`：完整 GitLab REST API 根地址；为空时使用 `<GITLAB_BASE_URL>/api/v4`。
- `GITLAB_TOKEN`：GitLab token，用于 MR API、HTTPS clone 和提交 MR comment。
- `MR_REVIEWER_AGENT_TYPE`：可选，`opencode` 或 `claude-code`，默认 `opencode`。
- `MR_REVIEWER_AGENT_COMMAND`：可选；为空时根据 agent type 使用 `opencode` 或 `claude`。
- `MR_REVIEW_WORK_DIR`：可选，默认系统临时目录下的 `gitlab-mr-review`。
- `MR_REVIEW_SUBMIT_COMMENT`：可选，默认 `true`；设置为 `false` 时只生成本地 Markdown 报告，不写回 MR。

缺少 `GITLAB_BASE_URL` 或 `GITLAB_TOKEN` 时停止执行，并说明缺少的配置。API 使用独立域名或前缀时必须显式配置 `GITLAB_API_BASE_URL`。

## 执行方式

复制 skill 时必须保留 `prompt_templates/` 目录；它保存 two-step 的固定 prompt，不能通过环境变量覆盖。模板版本由内容 SHA-256 前 12 位确定，供脚本与自动入口定位问题。

从当前 skill 目录运行脚本：

```powershell
python <复制后的skill目录>/gitlab-mr-review/scripts/review_gitlab_mr.py "<mr-url>"
```

脚本会执行以下确定性动作：

1. 解析 MR URL，并校验 host 与 `GITLAB_BASE_URL` 一致。
2. 调 GitLab API 获取 MR 元数据和 target/source project 的 HTTPS clone URL。
3. clone target repo 到临时目录，显式 fetch target/source 分支。
4. checkout `diff_refs.head_sha`，读取 `Base SHA`、`Head SHA` 和 `Changed files`。
5. 第一次调用已配置的 OpenCode 或 Claude Code，只生成严格结构化的 MR 概要。
6. 第二次调用同一 Agent，把概要作为上下文，要求使用现有 `code-review skill` 审查本地 repo 的 MR range。
7. 写出包含概要与 review 的本地 Markdown 报告；当 `MR_REVIEW_SUBMIT_COMMENT` 不是 `false` 时，只把 review 正文提交到现有 MR comment。

## 边界

- 不要复制或改写 `code-review skill` 的审查规则；本 skill 只负责从 MR URL 到评论写回。
- 不要创建新 MR，不要修改被检视仓库代码。
- 不要把完整 diff 塞进 prompt；只传 `MR URL`、`Base SHA`、`Head SHA`、`Changed files` 和本地 repo path。
- 不要把第一步 MR 概要提交到线上 comment；概要只进入本地报告和第二步 review 上下文。
- 不要在日志或最终输出中泄露 `GITLAB_TOKEN`。

## 输出

向用户报告脚本 stdout 中的关键信息：报告路径、Base/Head SHA、changed files 数量、是否已提交 MR comment。失败时保留脚本错误信息，但确认其中没有 token。
