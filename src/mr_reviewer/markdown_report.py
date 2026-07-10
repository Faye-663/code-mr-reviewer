from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mr_reviewer.review_result import StructuredReviewParseError, parse_structured_review_result
from mr_reviewer.reviewer import ReviewReport

if TYPE_CHECKING:
    from mr_reviewer.webhook import WebhookReviewEvent


SEVERITIES = ("fatal", "major", "minjor", "suggestion")


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
    lines.extend(_discoveries(report.summary, repo, mr_iid, mr_url, report.base_sha, head_sha))
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
        summary: dict[str, object] | None, repo: str, mr_iid: int | None, mr_url: str, base_sha: str, head_sha: str
) -> list[str]:
    lines = [
        f"- MR：{repo}!{mr_iid}" if mr_iid is not None else "- MR：<unknown>",
        f"- URL：{mr_url or '<unknown>'}",
        f"- 审查范围：Base SHA = {base_sha or '<unknown>'}，Head SHA = {head_sha or '<unknown>'}",
    ]
    if not summary:
        return lines + ["- 变更内容概述：<未生成>"]
    lines.append(f"- 变更内容概述：{summary.get('overview', '<未生成>')}")
    for field, label in (("change_areas", "变更区域"), ("behavior_changes", "行为变化"), ("risk_areas", "风险区域"), ("test_changes", "测试变化")):
        values = summary.get(field, [])
        text = "；".join(str(value) for value in values) if values else "无"
        lines.append(f"- {label}：{text}")
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
    status = finding.get("status", "")
    labels = {
        "posted": "已提交MR评论",
        "skipped_duplicate": "已存在相同MR评论",
        "monitor_only": "仅写入本地报告",
        "disabled": "未提交（已关闭）",
        "model_not_configured": "未提交（未配置模型名）",
        "parse_failed": "未提交（结构化结果无效）",
        "failed": "提交失败",
    }
    return labels.get(status, f"未提交（{status or '未知'}）")


def _severity_status(severity: str, count: int) -> str:
    if count == 0:
        return "通过"
    return {"fatal": "阻止", "major": "警告", "minjor": "警告", "suggestion": "备注"}[severity]


def _verdict(counts: dict[str, int]) -> str:
    if counts["fatal"]:
        return f"阻止 — {counts['fatal']} 个 fatal 级别问题必须在合并前解决。"
    warning = counts["major"] + counts["minjor"]
    if warning:
        return f"警告 — {warning} 个 major/minjor 级别问题应在合并前解决。"
    if counts["suggestion"]:
        return f"备注 — {counts['suggestion']} 个 suggestion 级别建议可按需处理。"
    return "通过 — 未发现 fatal、major、minjor 或 suggestion 级别问题。"


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
