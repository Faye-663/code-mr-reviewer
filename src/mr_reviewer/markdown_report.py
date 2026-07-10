from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mr_reviewer.review_result import StructuredReviewParseError, parse_structured_review_result
from mr_reviewer.reviewer import ReviewReport

if TYPE_CHECKING:
    from mr_reviewer.webhook import WebhookReviewEvent


def render_markdown_review_report(
        event: WebhookReviewEvent,
        report: ReviewReport,
        status: str,
        error: str | None = None,
) -> str:
    lines = [
        "# GitLab MR Review Report",
        "",
        "## MR",
        "",
        f"- Repo: {event.target.project_path}",
        f"- MR: {event.target.project_path}!{event.target.mr_iid}",
        f"- URL: {event.target.mr_url}",
        f"- Source branch: {event.target.source_branch}",
        f"- Target branch: {event.target.target_branch}",
        f"- Base SHA: {report.base_sha}",
        f"- Head SHA: {report.head_sha or event.target.head_sha}",
        "",
        "## Result",
        "",
        f"- Worker status: {status}",
        f"- Submission owner: {report.submission_owner}",
        f"- Submission status: {report.submission_status}",
        f"- Structured parse status: {report.structured_parse_status or 'not_run'}",
    ]
    if report.finding_counts:
        lines.append(f"- Finding counts: {_format_counts(report.finding_counts)}")
    if report.failure_stage:
        lines.append(f"- Failure stage: {report.failure_stage}")
    if error:
        lines.append(f"- Error: {error}")

    lines.extend(_summary_lines(report.summary))

    lines.extend(["", "## Findings", ""])
    if report.finding_results:
        for index, finding in enumerate(report.finding_results, start=1):
            lines.extend(_finding_lines(index, finding))
    else:
        lines.append("- <none>")

    if report.submission_status == "parse_failed":
        lines.extend(["", "## Raw Output", "", "```text", report.markdown, "```"])

    return "\n".join(lines) + "\n"


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
        )
        return replace(
            failed_report,
            markdown=_render_local_markdown_report(failed_report, "success", str(exc), report.markdown),
        )

    finding_results = [
        {
            "rule_id": finding.rule_id,
            "severity": finding.severity,
            "confidence": finding.confidence,
            "old_path": finding.old_path,
            "new_path": finding.new_path,
            "old_line": finding.old_line,
            "new_line": finding.new_line,
            "title": finding.title,
            "evidence": finding.evidence,
            "suggestion": finding.suggestion,
            "status": "monitor_only",
            "reason": "not_published_for_entry",
            "marker": "",
        }
        for finding in structured.findings
    ]
    rendered_report = replace(
        report,
        structured_parse_status="success",
        finding_counts=_local_counts(finding_results),
        finding_results=finding_results,
    )
    return replace(rendered_report, markdown=_render_local_markdown_report(rendered_report, "success"))


def _render_local_markdown_report(
        report: ReviewReport,
        status: str,
        error: str | None = None,
        raw_output: str | None = None,
) -> str:
    lines = [
        "# GitLab MR Review Report",
        "",
        "## MR",
        "",
        f"- Repo: {report.repo}",
        f"- MR: {report.repo}!{report.mr_iid}" if report.mr_iid is not None else "- MR: <unknown>",
        f"- URL: {report.mr_url}",
        f"- Source branch: {report.source_branch}",
        f"- Target branch: {report.target_branch}",
        f"- Base SHA: {report.base_sha}",
        f"- Head SHA: {report.head_sha}",
        "",
        "## Result",
        "",
        f"- Worker status: {status}",
        f"- Submission owner: {report.submission_owner}",
        f"- Submission status: {report.submission_status}",
        f"- Structured parse status: {report.structured_parse_status or 'not_run'}",
    ]
    if report.finding_counts:
        lines.append(f"- Finding counts: {_format_counts(report.finding_counts)}")
    if report.failure_stage:
        lines.append(f"- Failure stage: {report.failure_stage}")
    if error:
        lines.append(f"- Error: {error}")

    lines.extend(_summary_lines(report.summary))

    lines.extend(["", "## Findings", ""])
    if report.finding_results:
        for index, finding in enumerate(report.finding_results, start=1):
            lines.extend(_finding_lines(index, finding))
    else:
        lines.append("- <none>")

    if raw_output is not None:
        lines.extend(["", "## Raw Output", "", "```text", raw_output, "```"])

    return "\n".join(lines) + "\n"


def _finding_lines(index: int, finding: dict) -> list[str]:
    lines = [
        f"### {index}. {finding.get('title') or finding.get('rule_id') or '<unknown>'}",
        "",
        f"- Status: {finding.get('status', '')}",
        f"- Reason: {finding.get('reason', '')}",
        f"- Rule ID: {finding.get('rule_id', '')}",
        f"- Severity: {finding.get('severity', '')}",
        f"- Confidence: {finding.get('confidence', '')}",
        f"- File: {finding.get('new_path') or finding.get('old_path')}",
        f"- Lines: old={finding.get('old_line')} new={finding.get('new_line')}",
    ]
    if finding.get("discussion_id"):
        lines.append(f"- Discussion ID: {finding['discussion_id']}")
    if finding.get("note_id"):
        lines.append(f"- Note ID: {finding['note_id']}")
    if finding.get("evidence"):
        lines.append(f"- Evidence: {finding['evidence']}")
    if finding.get("suggestion"):
        lines.append(f"- Suggestion: {finding['suggestion']}")
    return lines + [""]


def _summary_lines(summary: dict[str, object] | None) -> list[str]:
    lines = ["", "## MR Summary", ""]
    if not summary:
        return lines + ["- <none>"]

    lines.append(str(summary["overview"]))
    labels = {
        "change_areas": "Change areas",
        "behavior_changes": "Behavior changes",
        "risk_areas": "Risk areas",
        "test_changes": "Test changes",
    }
    for field, label in labels.items():
        values = summary.get(field, [])
        lines.extend(["", f"### {label}", ""])
        if values:
            lines.extend(f"- {value}" for value in values)
        else:
            lines.append("- <none>")
    return lines


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _local_counts(results: list[dict]) -> dict[str, int]:
    counts = {
        "total": len(results),
        "monitor_only": 0,
        "parse_failed": 0,
    }
    for result in results:
        if result.get("status") == "monitor_only":
            counts["monitor_only"] += 1
    return counts
