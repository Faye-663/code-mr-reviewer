from __future__ import annotations

from typing import TYPE_CHECKING

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
    if error:
        lines.append(f"- Error: {error}")

    lines.extend(["", "## Findings", ""])
    if report.finding_results:
        for index, finding in enumerate(report.finding_results, start=1):
            lines.extend(_finding_lines(index, finding))
    else:
        lines.append("- <none>")

    if report.submission_status == "parse_failed":
        lines.extend(["", "## Raw Output", "", "```text", report.markdown, "```"])

    return "\n".join(lines) + "\n"


def _finding_lines(index: int, finding: dict) -> list[str]:
    lines = [
        f"### {index}. {finding.get('title') or finding.get('rule_id') or '<unknown>'}",
        "",
        f"- Status: {finding.get('status', '')}",
        f"- Reason: {finding.get('reason', '')}",
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


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
