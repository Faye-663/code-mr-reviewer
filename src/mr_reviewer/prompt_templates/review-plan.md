分析本次 GitLab MR 并生成严格的审查计划。只识别审查目标、关键路径、契约、不变量、边界和测试风险，不输出 finding，不下最终结论。
MR URL: $mr_url
Base SHA: $base_sha
Head SHA: $head_sha
Changed files:
$changed_files
代码仓在 $repo_path 目录。
审查计划必须只输出 JSON，不要输出 Markdown 或代码围栏。所有字段都必须存在，不要增加字段。JSON 结构为：
{"change_intent":["..."],"critical_paths":[{"path":"src/example.py","reason":"...","verify":["..."]}],"external_contracts":["..."],"state_invariants":["..."],"transaction_async_boundaries":["..."],"test_risks":["..."],"open_questions":["..."]}
critical_paths 中每一项的 path、reason 和 verify 必须非空；其余数组在没有内容时输出空数组。
