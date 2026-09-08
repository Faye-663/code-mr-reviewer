from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mr_reviewer.review_result import StructuredReviewParseError, parse_structured_review_result
from mr_reviewer.reviewer import ReviewReport

if TYPE_CHECKING:
    from mr_reviewer.webhook import WebhookReviewEvent


SEVERITIES = ("fatal", "major", "minor", "suggestion")


def render_markdown_review_report(
        event: WebhookReviewEvent,
        report: ReviewReport,
        status: str,
        error: str | None = None,
) -> str:
    return _render_report(report, status, error, event)


def render_structured_output_as_markdown(report: ReviewReport) -> ReviewReport:
    try:
        structured = parse_structured_review_result(report.markdown)
    except StructuredReviewParseError as exc:
        failed_report = replace(
            report,
            structured_parse_status="failed",
            submission_status="parse_failed",
            finding_counts=_local_counts([]),
            finding_results=[],
            good=[],
            notes=[],
            test_gaps=[],
        )
        return replace(failed_report, markdown=_render_report(failed_report, "failed", str(exc)))

    finding_results = [_finding_to_result(finding, "monitor_only", "not_published_for_entry") for finding in structured.findings]
    rendered_report = replace(
        report,
        structured_parse_status="success",
        finding_counts=_local_counts(finding_results),
        finding_results=finding_results,
        good=structured.good,
        notes=structured.notes,
        test_gaps=structured.test_gaps,
    )
    return replace(rendered_report, markdown=_render_report(rendered_report, "success"))


def render_review_report(report: ReviewReport, status: str, error: str | None = None) -> str:
    """Render an already-normalized single-MR report without reparsing Agent output."""
    return _render_report(report, status, error)


def _render_report(
        report: ReviewReport,
        status: str,
        error: str | None = None,
        event: WebhookReviewEvent | None = None,
) -> str:
    repo = event.target.project_path if event else report.repo
    mr_iid = event.target.mr_iid if event else report.mr_iid
    mr_url = event.target.mr_url if event else report.mr_url
    head_sha = report.head_sha or (event.target.head_sha if event else "")
    lines = ["# 代码检视报告", "", "## Discoveries", ""]
    lines.extend(_discoveries(report, repo, mr_iid, mr_url, report.base_sha, head_sha))
    if report.notes:
        lines.append(f"- 检视备注：{'；'.join(report.notes)}")
    if report.test_gaps:
        lines.append(f"- 测试缺口：{'；'.join(report.test_gaps)}")
    if error:
        lines.extend(["", f"- 执行错误：{error}"])
    if report.failure_stage:
        lines.append(f"- 失败阶段：{report.failure_stage}")

    lines.extend(["", "## 检视意见", ""])
    results = report.finding_results or []
    if results:
        for index, finding in enumerate(results, start=1):
            lines.extend(_finding_lines(index, finding))
    else:
        lines.append("- 未发现可报告的问题。")

    lines.extend(["", "## 检视摘要", "", "| 严重程度 | 数量 | 状态 |", "|----------|------|------|"])
    severity_counts = {severity: sum(1 for item in results if item.get("severity") == severity) for severity in SEVERITIES}
    for severity in SEVERITIES:
        lines.append(f"| {severity} | {severity_counts[severity]} | {_severity_status(severity, severity_counts[severity])} |")
    lines.extend(["", f"**裁决**：{_verdict(severity_counts)}"])

    good = report.good or []
    if good:
        lines.extend(["", "## GOOD", ""])
        lines.extend(f"- {item}" for item in good)
    return "\n".join(lines) + "\n"


def _discoveries(
        report: ReviewReport, repo: str, mr_iid: int | None, mr_url: str, base_sha: str, head_sha: str
) -> list[str]:
    changed_files = report.changed_files or []
    lines = [
        f"- MR：{repo}!{mr_iid}" if mr_iid is not None else "- MR：<unknown>",
        f"- 标题：{report.title or '<unknown>'}",
        f"- URL：{mr_url or '<unknown>'}",
        f"- 提交范围：Base SHA = {base_sha or '<unknown>'}，Head SHA = {head_sha or '<unknown>'}",
        f"- 目标分支：{report.target_branch or '<unknown>'}",
        f"- 请求模式：{report.requested_review_mode or report.review_mode or '<unknown>'}；"
        f"实际模式：{report.review_mode or '<unknown>'}；路由原因：{report.routing_reason or 'unknown'}",
        f"- 审查范围：{report.review_scope or 'single'}",
        f"- 变更文件：{len(changed_files)} 个",
    ]
    if changed_files:
        lines.append(f"- 文件列表：{'；'.join(changed_files)}")
    lines.extend(_dependency_context_lines(report))
    plan = report.review_plan
    if not plan:
        return lines
    lines.append("- 审查计划：")
    if plan.get("schema_version") == "dependency-review-plan/v1":
        focus = plan.get("primary_focus", {})
        fields = (("change_intent", "变更意图"), ("test_risks", "测试风险"))
        critical_paths = focus.get("critical_paths", [])
    else:
        focus = plan
        fields = (
            ("change_intent", "变更意图"),
            ("external_contracts", "外部契约"),
            ("state_invariants", "状态不变量"),
            ("transaction_async_boundaries", "事务/异步边界"),
            ("test_risks", "测试风险"),
            ("open_questions", "待确认问题"),
        )
        critical_paths = plan.get("critical_paths", [])
    for field, label in fields:
        values = focus.get(field, [])
        text = "；".join(str(value) for value in values) if values else "无"
        lines.append(f"  - {label}：{text}")
    for path in critical_paths:
        lines.append(
            f"  - 关键路径：{path['path']} — {path['reason']}；验证：{'；'.join(path['verify'])}"
        )
    return lines


def _dependency_context_lines(report: ReviewReport) -> list[str]:
    if report.dependency_context_status == "degraded":
        lines = [
            "- 依赖联合检视：未执行依赖联合检视；"
            f"降级原因={report.dependency_degradation_reason or '<unknown>'}"
        ]
        if report.dependency_failed_project:
            lines.append(f"- 失败依赖项目：{report.dependency_failed_project}")
        if report.dependency_preparation_seconds is not None:
            lines.append(f"- 依赖准备耗时：{report.dependency_preparation_seconds:.3f}s")
        return lines
    if report.dependency_context_status != "complete":
        return ["- 依赖上下文：not_applicable"]

    lines = [
        "- 依赖联合检视：已完成",
        f"- Dependency Context ID：{report.dependency_context_id or '<unknown>'}",
    ]
    if report.dependency_preparation_seconds is not None:
        lines.append(f"- 依赖准备总耗时：{report.dependency_preparation_seconds:.3f}s")
    for repository in report.dependency_repositories or []:
        lines.append(
            "- 依赖项目："
            f"{repository.get('project_path', '<unknown>')}；"
            f"branch={repository.get('branch', '<unknown>')}；"
            f"commit={repository.get('commit_sha', '<unknown>')}；"
            f"准备耗时={float(repository.get('preparation_seconds', 0.0)):.3f}s"
        )
    for summary in report.dependency_relationship_summary or []:
        lines.append(f"- 依赖关系结论：{summary}")
    return lines


def _finding_lines(index: int, finding: dict) -> list[str]:
    path = finding.get("new_path") or finding.get("old_path") or "<unknown>"
    line = finding.get("new_line") if finding.get("new_line", -1) != -1 else finding.get("old_line", "<unknown>")
    return [
        f"### [{finding.get('severity', 'suggestion')}] {finding.get('title') or finding.get('rule_id') or '<unknown>'}",
        "",
        f"**文件**: {path}:{line}",
        "",
        f"**证据**: {finding.get('evidence', '')}",
        "",
        f"**影响**: {finding.get('impact', '')}",
        "",
        f"**MR评论状态**：{_comment_status(finding)}",
        "",
        f"**建议**: {finding.get('suggestion', '')}",
        "",
    ]


def _comment_status(finding: dict) -> str:
    return format_comment_status(
        str(finding.get("status") or ""),
        str(finding.get("reason") or ""),
    )


def format_comment_status(status: str, reason: str) -> str:
    if status in {"posted", "posted_inline"}:
        return "已提交 MR 行内评论"
    if status == "posted_note":
        detail = {
            "position_not_provided": "未提供 diff 位置",
            "position_not_in_diff": "位置不在当前 diff",
        }.get(reason)
        return f"已提交普通 MR 评论（{detail}）" if detail else "已提交普通 MR 评论"
    if status == "skipped_duplicate":
        return "未重复提交（相同 MR 评论已存在）"
    if status == "monitor_only":
        return "未提交（当前入口仅生成本地报告）"
    if status == "disabled":
        return "未提交（GitLab 评论发布未启用）"
    if status == "model_not_configured":
        return "未提交（Agent 模型名未配置）"
    if status == "parse_failed":
        return "未提交（结构化检视结果无效）"
    if status == "skipped_stale":
        return "未提交（MR 版本已变化）"
    if status == "failed":
        return "提交失败（详情见任务日志）"
    if status == "filtered":
        detail = {
            "below_min_severity": "严重程度低于当前发布门槛",
            "below_min_confidence": "置信度低于当前发布门槛",
        }.get(reason, "未满足当前发布门槛")
        return f"未提交（{detail}）"
    if status == "invalid":
        detail = {
            "line_not_in_diff": "位置不在当前 MR diff",
            "inconsistent_line_sides": "diff 行号两侧不一致",
            "invalid_line_value": "评论行号无效",
            "invalid_target_line": "评论行号无效",
            "invalid_target_path": "评论路径无效",
            "invalid_evidence_path": "证据路径无效",
            "unknown_target_member": "责任 MR 不属于当前 ReviewSet",
            "unknown_evidence_member": "证据 MR 不属于当前 ReviewSet",
        }.get(reason, "评论位置或目标无效")
        return f"未提交（{detail}）"
    return "未提交（原因未知）"


def _severity_status(severity: str, count: int) -> str:
    if count == 0:
        return "通过"
    return {"fatal": "阻止", "major": "警告", "minor": "警告", "suggestion": "备注"}[severity]


def _verdict(counts: dict[str, int]) -> str:
    if counts["fatal"]:
        return f"阻止 — {counts['fatal']} 个 fatal 级别问题必须在合并前解决。"
    warning = counts["major"] + counts["minor"]
    if warning:
        return f"警告 — {warning} 个 major/minor 级别问题应在合并前解决。"
    if counts["suggestion"]:
        return f"备注 — {counts['suggestion']} 个 suggestion 级别建议可按需处理。"
    return "通过 — 未发现 fatal、major、minor 或 suggestion 级别问题。"


def _finding_to_result(finding, status: str, reason: str) -> dict:
    return {
        "rule_id": finding.rule_id,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "old_path": finding.old_path,
        "new_path": finding.new_path,
        "old_line": finding.old_line,
        "new_line": finding.new_line,
        "title": finding.title,
        "evidence": finding.evidence,
        "impact": finding.impact,
        "suggestion": finding.suggestion,
        "status": status,
        "reason": reason,
        "marker": "",
    }


def _local_counts(results: list[dict]) -> dict[str, int]:
    counts = {"total": len(results), "monitor_only": 0, "parse_failed": 0}
    for result in results:
        if result.get("status") == "monitor_only":
            counts["monitor_only"] += 1
    return counts
