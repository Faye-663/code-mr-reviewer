使用 $skill_name skill 检视 GitLab MR。
MR URL: $mr_url
Base SHA: $base_sha
Head SHA: $head_sha
Changed files:
$changed_files
代码仓在 $repo_path 目录。
只审查 Base SHA 到 Head SHA 的 MR range，不要按本地未提交变更审查。

以下是第一阶段生成的审查计划：
$review_plan_json

该计划只是待验证线索，不是事实或审查边界。必须重新读取代码验证每一项；允许推翻计划，并覆盖计划未列出的变更和风险。

自动检视模式必须只输出 JSON，不要输出 Markdown 或代码围栏。JSON 结构为：
{"findings":[{"rule_id":"...","severity":"major","confidence":"HIGH","position":{"path":"src/example.py","line":42,"side":"new"},"title":"...","evidence":"...","impact":"...","suggestion":"..."}],"notes":[],"test_gaps":[],"good":[]}
如果 suggestion 包含能够直接说明修复方式的具体代码，应在 suggestion 字符串内使用带语言标识的 Markdown fenced code block，并用 JSON 转义保存换行；没有可靠代码方案时只写文字，不要强行生成示例代码。不得使用 GitLab suggestion block。上述代码围栏只允许出现在 suggestion 字符串内，整体输出仍必须是单个 JSON 对象。
severity 只能使用 suggestion、minor、major、fatal；confidence 只能使用 HIGH、MEDIUM、LOW。
position 只表达一个评论锚点，不是范围的起止行：新增或上下文行使用 side="new"，纯删除行使用 side="old"；path 是仓库相对路径，line 是对应侧的正整数行号。
finding 与本次 MR 相关但找不到真实 diff 锚点时，position 必须为 null；禁止伪造或借用邻近 diff 行，Python 会把该 finding 降级为普通 MR 评论并说明证据位置。
