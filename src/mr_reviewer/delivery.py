from __future__ import annotations

import re
from dataclasses import replace

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabClient
from mr_reviewer.inline_review import (
    DiffPositionMap,
    DiffRefs,
    FindingValidationDecision,
    validate_review_findings,
)
from mr_reviewer.review_result import (
    StructuredReviewParseError,
    StructuredReviewResult,
    parse_structured_review_result,
)
from mr_reviewer.reviewer import MergeRequestReviewTarget, ReviewReport


def deliver_gitlab_review(
    gitlab: GitLabClient,
    config: Config,
    target: MergeRequestReviewTarget,
    report: ReviewReport,
    *,
    enabled: bool,
) -> ReviewReport:
    try:
        structured = parse_structured_review_result(report.markdown)
    except StructuredReviewParseError:
        return replace(
            report,
            submission_owner="python",
            submission_status="parse_failed",
            structured_parse_status="failed",
            finding_counts=_finding_counts([]),
            finding_results=[],
        )

    if report.rejected_findings or report.normalization_warnings:
        structured = replace(
            structured,
            structured_parse_status="partial",
            rejected_findings=[*structured.rejected_findings, *(report.rejected_findings or [])],
            normalization_warnings=[
                *structured.normalization_warnings,
                *(report.normalization_warnings or []),
            ],
        )

    if not enabled:
        results = [
            _unpublished_finding_result(finding, "disabled", "post_comment_disabled")
            for finding in structured.findings
        ]
        return _with_structured_submission(report, structured, "disabled", results)

    if not config.agent_model_name:
        results = [
            _unpublished_finding_result(finding, "model_not_configured", "agent_model_name_missing")
            for finding in structured.findings
        ]
        return _with_structured_submission(report, structured, "model_not_configured", results)

    detail = gitlab.get_mr_detail_for_discussion_position(target)
    refs = diff_refs_from_detail(detail)
    head_validation = {
        "review_head_sha": report.head_sha,
        "current_head_sha": refs.head_sha,
        "status": "current" if report.head_sha == refs.head_sha else "stale",
    }
    report = replace(report, head_validation=head_validation)
    if report.head_sha != refs.head_sha:
        results = [
            _unpublished_finding_result(finding, "skipped_stale", "mr_head_changed")
            for finding in structured.findings
        ]
        return _with_structured_submission(report, structured, "skipped_stale", results)

    position_map = DiffPositionMap.from_unified_diff(report.diff, refs)
    decisions = validate_review_findings(structured, position_map, config.publication_policy)
    publish_results = DiscussionPublisher(gitlab, config.agent_model_name).publish(target, decisions)
    status = "failed" if any(item["status"] == "failed" for item in publish_results) else "posted"
    return _with_structured_submission(report, structured, status, publish_results)


def current_head_validation(
    gitlab: GitLabClient,
    target: MergeRequestReviewTarget,
    review_head_sha: str,
) -> tuple[dict[str, str], dict]:
    detail = gitlab.get_mr_detail_for_discussion_position(target)
    diff_refs = detail.get("diff_refs")
    current_head = diff_refs.get("head_sha") if isinstance(diff_refs, dict) else None
    if not isinstance(current_head, str) or not current_head:
        current_head = detail.get("sha")
    if not isinstance(current_head, str) or not current_head:
        raise ValueError("GitLab MR detail response missing current head SHA")
    return (
        {
            "review_head_sha": review_head_sha,
            "current_head_sha": current_head,
            "status": "current" if review_head_sha == current_head else "stale",
        },
        detail,
    )


class DiscussionPublisher:
    def __init__(self, gitlab: GitLabClient, model_name: str):
        self.gitlab = gitlab
        self.model_name = model_name

    def publish(
        self,
        target: MergeRequestReviewTarget,
        decisions: list[FindingValidationDecision],
    ) -> list[dict]:
        existing_markers = extract_existing_markers(self.gitlab.list_mr_discussions(target))
        results = []
        for decision in decisions:
            if decision.status not in {"publishable_inline", "publishable_note"}:
                results.append(_finding_result(decision, decision.status, decision.reason))
                continue

            marker = finding_marker(target, decision)
            if marker in existing_markers:
                results.append(_finding_result(decision, "skipped_duplicate", "duplicate_marker", marker))
                continue

            try:
                body = discussion_body(decision, marker, self.model_name)
                if decision.status == "publishable_inline":
                    response = self.gitlab.post_mr_discussion(
                        target,
                        body,
                        decision.finding.severity,
                        decision.position.to_gitlab_position(),
                    )
                else:
                    response = self.gitlab.post_mr_note(target, body)
            except Exception as exc:  # noqa: BLE001 - 单条失败不能阻止其它 finding。
                results.append(_finding_result(decision, "failed", str(exc), marker))
                continue

            result_status = "posted" if decision.status == "publishable_inline" else "posted_note"
            result = _finding_result(decision, result_status, decision.reason, marker)
            if decision.status == "publishable_inline":
                result["discussion_id"] = response.get("id")
                notes = response.get("notes") if isinstance(response.get("notes"), list) else []
                if notes and isinstance(notes[0], dict):
                    result["note_id"] = notes[0].get("id")
            else:
                result["note_id"] = response.get("id")
            results.append(result)
            existing_markers.add(marker)
        return results


def diff_refs_from_detail(detail: dict) -> DiffRefs:
    diff_refs = detail.get("diff_refs")
    if not isinstance(diff_refs, dict):
        raise ValueError("GitLab MR detail response missing diff_refs")
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    if not all(isinstance(value, str) and value for value in (base_sha, start_sha, head_sha)):
        raise ValueError("GitLab MR detail diff_refs missing base_sha/start_sha/head_sha")
    return DiffRefs(base_sha=base_sha, start_sha=start_sha, head_sha=head_sha)


def extract_existing_markers(discussions: list[dict]) -> set[str]:
    markers = set()
    for discussion in discussions:
        notes = discussion.get("notes") if isinstance(discussion, dict) else None
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            body = note.get("body")
            if isinstance(body, str):
                markers.update(re.findall(r"<!-- ai-cr:finding:[^>]+ -->", body))
    return markers


def finding_marker(target: MergeRequestReviewTarget, decision: FindingValidationDecision) -> str:
    finding = decision.finding
    head_sha = decision.position.refs.head_sha if decision.position else target.head_sha
    side, path, line = _marker_position(decision)
    return (
        "<!-- ai-cr:finding:"
        f"{target.project_path}:{target.mr_iid}:{head_sha}:{finding.rule_id}:"
        f"{side}:{path}:{line}"
        " -->"
    )


def discussion_body(decision: FindingValidationDecision, marker: str, model_name: str) -> str:
    finding = decision.finding
    downgrade = ""
    if decision.status == "publishable_note":
        side, path, line = _requested_position(finding)
        reason = "未提供可定位位置" if decision.reason == "position_not_provided" else "位置不在当前 diff"
        location = "未提供" if side == "none" else f"{side}:{path}:{line}"
        downgrade = (
            f"**发布说明**\n\n证据请求位置 `{location}`；{reason}，"
            "已降级为普通 MR 评论；未吸附到邻近行。\n\n"
        )
    return (
        f"**🤖 AI Review｜{finding.title}**\n\n"
        f"**判断依据**\n\n{finding.evidence}\n\n"
        f"**影响**\n\n{finding.impact}\n\n"
        f"**建议**\n\n{finding.suggestion}\n\n"
        f"{downgrade}"
        "<details>\n"
        "<summary>审查信息</summary>\n\n"
        f"- 置信度：`{finding.confidence}`\n"
        f"- 规则：`{finding.rule_id}`\n"
        f"- 来源：`AI Review · {model_name}`\n\n"
        "</details>\n\n"
        f"{marker}"
    )


def _requested_position(finding) -> tuple[str, str, int]:
    if finding.new_line != -1:
        return "new", finding.new_path, finding.new_line
    if finding.old_line != -1:
        return "old", finding.old_path, finding.old_line
    return "none", "", -1


def _marker_position(decision: FindingValidationDecision) -> tuple[str, str, int]:
    position = decision.position
    if position is None:
        return _requested_position(decision.finding)
    if position.new_line != -1:
        return "new", position.new_path, position.new_line
    return "old", position.old_path, position.old_line


def _finding_result(
    decision: FindingValidationDecision,
    status: str,
    reason: str,
    marker: str = "",
) -> dict:
    finding = decision.finding
    return _finding_payload(finding, status, reason, marker)


def _unpublished_finding_result(finding, status: str, reason: str) -> dict:
    return _finding_payload(finding, status, reason, "")


def _finding_payload(finding, status: str, reason: str, marker: str) -> dict:
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
        "marker": marker,
    }


def _finding_counts(results: list[dict]) -> dict[str, int]:
    counts = {
        "total": len(results),
        "posted": 0,
        "posted_note": 0,
        "skipped_duplicate": 0,
        "skipped_stale": 0,
        "filtered": 0,
        "invalid": 0,
        "failed": 0,
    }
    for result in results:
        status = result.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _with_structured_submission(
    report: ReviewReport,
    structured: StructuredReviewResult,
    status: str,
    results: list[dict],
) -> ReviewReport:
    return replace(
        report,
        submission_owner="python",
        submission_status=status,
        structured_parse_status=structured.structured_parse_status,
        finding_counts=_finding_counts(results),
        finding_results=results,
        good=structured.good,
        notes=structured.notes,
        test_gaps=structured.test_gaps,
        rejected_findings=structured.rejected_findings,
        normalization_warnings=structured.normalization_warnings,
    )
