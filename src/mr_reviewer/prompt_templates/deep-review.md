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
{"findings":[{"rule_id":"...","severity":"major","confidence":"HIGH","old_path":"src/example.py","new_path":"src/example.py","old_line":-1,"new_line":42,"title":"...","evidence":"...","impact":"...","suggestion":"..."}],"notes":[],"test_gaps":[],"good":[]}
severity 只能使用 suggestion、minjor、major、fatal；confidence 只能使用 HIGH、MEDIUM、LOW。
