使用 $skill_name skill 检视 GitLab MR。
MR URL: $mr_url
Base SHA: $base_sha
Head SHA: $head_sha
Changed files:
$changed_files
代码仓在 $repo_path 目录。
只审查 Base SHA 到 Head SHA 的 MR range，不要按本地未提交变更审查。

以下是第一阶段生成的 MR 概要，作为本次代码审查的上下文：
$summary_json

自动检视模式必须只输出 JSON，不要输出 Markdown 或代码围栏。JSON 结构为：
{"findings":[{"rule_id":"...","severity":"major","confidence":"HIGH","old_path":"src/example.py","new_path":"src/example.py","old_line":-1,"new_line":42,"title":"...","evidence":"...","impact":"...","suggestion":"..."}],"notes":[],"test_gaps":[],"good":[]}
severity 只能使用 suggestion、minjor、major、fatal；confidence 只能使用 HIGH、MEDIUM、LOW。
