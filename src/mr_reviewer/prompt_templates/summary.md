分析本次 GitLab MR 并生成 MR 概要。只总结变更目标、范围、行为、风险和测试变化，不执行代码审查，不要输出 finding。
MR URL: $mr_url
Base SHA: $base_sha
Head SHA: $head_sha
Changed files:
$changed_files
代码仓在 $repo_path 目录。
MR 概要必须只输出 JSON，不要输出 Markdown 或代码围栏。JSON 结构为：
{"overview":"...","change_areas":["..."],"behavior_changes":["..."],"risk_areas":["..."],"test_changes":["..."]}
