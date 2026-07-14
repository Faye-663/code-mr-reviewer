from __future__ import annotations

from mr_reviewer.review_set_publish import ReviewSetPublication
from mr_reviewer.reviewer import ReviewSetReviewReport


def render_review_set_report(report: ReviewSetReviewReport, publication: ReviewSetPublication) -> str:
    lines = [
        "# 多 MR 联合代码检视报告",
        "",
        "## ReviewSet",
        "",
        f"- ReviewSet ID：`{report.manifest.review_set_id}`",
        f"- ReqID：`{report.manifest.req_id}`",
        "- 上下文状态：`complete`",
        f"- Agent 调用次数：{report.agent_call_count}",
        "",
        "| 成员 | Base | Start | Head |",
        "|------|------|-------|------|",
    ]
    for member in report.manifest.members:
        lines.append(
            f"| [{member.project_path}!{member.mr_iid}]({member.mr_url}) | "
            f"`{member.base_sha}` | `{member.start_sha}` | `{member.head_sha}` |"
        )

    lines.extend(["", "## 联合审查计划", ""])
    member_focus = report.review_plan.get("member_focus", [])
    if not member_focus:
        lines.append("- 无成员计划数据。")
    for focus in member_focus:
        intent = "；".join(focus.get("change_intent", [])) or "无"
        risks = "；".join(focus.get("test_risks", [])) or "无"
        lines.append(f"- `{focus['member_id']}`：变更意图 {intent}；测试风险 {risks}")
        for path in focus.get("critical_paths", []):
            lines.append(
                f"  - 关键路径 `{path['path']}`：{path['reason']}；验证 "
                f"{'；'.join(path['verify'])}"
            )
    for relationship in report.review_plan.get("relationships", []):
        lines.append(
            f"- 计划关系 `{relationship['from_member_id']} -> {relationship['to_member_id']}`："
            f"{relationship['contract']}"
        )
    open_questions = report.review_plan.get("open_questions", [])
    if open_questions:
        lines.append(f"- 待确认问题：{'；'.join(open_questions)}")

    lines.extend(["", "## 跨仓关系", ""])
    lines.extend(f"- {item}" for item in report.result.relationship_summary)

    result_by_target = {
        (item["issue_id"], item["target_index"]): item for item in publication.results
    }
    lines.extend(["", "## Findings", ""])
    if not report.result.findings:
        lines.append("- 未发现可报告的问题。")
    for finding_index, finding in enumerate(report.result.findings, start=1):
        lines.extend(
            [
                f"### {finding_index}. [{finding.severity}/{finding.confidence}] {finding.title}",
                "",
                f"- Issue ID：`{finding.issue_id}`",
                f"- Rule：`{finding.rule_id}`",
                f"- 影响：{finding.impact}",
                "- 证据：",
            ]
        )
        for evidence in finding.evidence_refs:
            lines.append(
                f"  - `{evidence.member_id}:{evidence.path}:{evidence.start_line}-{evidence.end_line}`："
                f"{evidence.detail}"
            )
        lines.append("- 责任目标：")
        for target_index, target in enumerate(finding.targets):
            publish = result_by_target[(finding.issue_id, target_index)]
            if target.position is None:
                position = "普通评论"
            else:
                position = (
                    f"`{target.position.old_path}:{target.position.old_line} -> "
                    f"{target.position.new_path}:{target.position.new_line}`"
                )
            lines.append(
                f"  - `{target.member_id}`：位置 {position}；{target.suggestion}；"
                f"状态 `{publish['status']}`；原因 `{publish['reason'] or '-'}`"
            )

    if report.result.notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {item}" for item in report.result.notes)
    if report.result.test_gaps:
        lines.extend(["", "## Test Gaps", ""])
        lines.extend(f"- {item}" for item in report.result.test_gaps)
    if report.result.good:
        lines.extend(["", "## GOOD", ""])
        lines.extend(f"- {item}" for item in report.result.good)

    counts = publication.counts
    lines.extend(
        [
            "",
            "## 发布摘要",
            "",
            f"- 状态：`{publication.status}`",
            f"- Inline：{counts['posted_inline']}",
            f"- 普通评论：{counts['posted_note']}",
            f"- 重复跳过：{counts['skipped_duplicate']}",
            f"- 过滤：{counts['filtered']}",
            f"- 无效：{counts['invalid']}",
            f"- 失败：{counts['failed']}",
            f"- 发布关闭：{counts['disabled']}",
            f"- Model 未配置：{counts['model_not_configured']}",
        ]
    )
    return "\n".join(lines) + "\n"
