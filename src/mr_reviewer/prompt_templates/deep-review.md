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
old_line 和 new_line 表示同一个评论锚点在变更前后的行号，不是范围的起止行。新增行必须使用 old_line=-1、new_line=新增后的行号；删除行必须使用 old_line=删除前的行号、new_line=-1；只有 diff 中未修改的上下文行才同时提供两者。
finding 与本次 MR 相关但找不到真实 diff 锚点时，禁止伪造或借用邻近 diff 行；使用实际证据位置，Python 会将无法定位的 finding 只保留在本地报告。
