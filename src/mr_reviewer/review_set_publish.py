from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl
from mr_reviewer.inline_review import DiffPosition, DiffPositionMap, DiffRefs
from mr_reviewer.review_set import PreparedReviewSetMember, ReviewSetMember
from mr_reviewer.review_set_result import ReviewSetFinding, ReviewSetFindingTarget
from mr_reviewer.reviewer import ReviewSetReviewReport

LOG = logging.getLogger("mr_reviewer")
PUBLISHABLE_SEVERITIES = {"major", "fatal"}
PUBLISHABLE_CONFIDENCE = "HIGH"
MARKER_RE = re.compile(r"<!-- ai-cr:review-set:[^>]+ -->")


@dataclass(frozen=True, slots=True)
class ReviewSetPublication:
    status: str
    results: tuple[dict[str, object], ...]
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _TargetDecision:
    finding: ReviewSetFinding
    target: ReviewSetFindingTarget
    target_index: int
    member: ReviewSetMember | None
    status: str
    reason: str
    position: DiffPosition | None
    marker: str


class ReviewSetPublisher:
    def __init__(self, gitlab: GitLabClient):
        self.gitlab = gitlab

    def publish(
            self,
            report: ReviewSetReviewReport,
            *,
            enabled: bool,
            model_name: str,
    ) -> ReviewSetPublication:
        # 先完成全量语义校验，避免校验过程中途失败时已经产生部分评论。
        decisions = self._validate(report)
        if not enabled:
            results = tuple(self._unpublished(decision, "disabled", "review_set_post_comment_disabled") for decision in decisions)
            return _publication(results)
        if not model_name:
            results = tuple(self._unpublished(decision, "model_not_configured", "agent_model_name_missing") for decision in decisions)
            return _publication(results)

        existing_by_member: dict[str, set[str] | None] = {}
        for decision in decisions:
            if decision.member is None or decision.status not in {"publishable_inline", "publishable_note"}:
                continue
            member_id = decision.member.member_id
            if member_id in existing_by_member:
                continue
            try:
                existing_by_member[member_id] = _extract_markers(
                    self.gitlab.list_mr_discussions(decision.member)
                )
            except Exception as exc:  # noqa: BLE001 - 无法完成去重时不得冒险重复发布。
                LOG.exception(
                    "review_scope=review-set stage=list_discussions member_id=%s status=failed",
                    member_id,
                )
                existing_by_member[member_id] = None

        results: list[dict[str, object]] = []
        for decision in decisions:
            if decision.status not in {"publishable_inline", "publishable_note"}:
                results.append(self._result(decision, decision.status, decision.reason))
                continue
            existing = existing_by_member.get(decision.member.member_id)
            if existing is None:
                results.append(self._result(decision, "failed", "duplicate_check_failed"))
                continue
            if decision.marker in existing:
                results.append(self._result(decision, "skipped_duplicate", "duplicate_marker"))
                continue
            try:
                result = self._post(decision, model_name)
                existing.add(decision.marker)
                results.append(result)
            except Exception:  # noqa: BLE001 - 单目标失败记录后继续其它已验证目标。
                LOG.exception(
                    "review_scope=review-set stage=publish member_id=%s issue_id=%s status=failed",
                    decision.member.member_id,
                    decision.finding.issue_id,
                )
                results.append(self._result(decision, "failed", "gitlab_publish_failed"))
        return _publication(tuple(results))

    def _validate(self, report: ReviewSetReviewReport) -> tuple[_TargetDecision, ...]:
        members = {member.member_id: member for member in report.manifest.members}
        prepared = {member.member.member_id: member for member in report.members}
        decisions = []
        for finding in report.result.findings:
            evidence_reason = _evidence_error(finding, members)
            finding_key = _finding_key(finding)
            for target_index, target in enumerate(finding.targets):
                member = members.get(target.member_id)
                marker = _marker(report.manifest.review_set_id, finding_key, target)
                if evidence_reason:
                    decisions.append(_TargetDecision(finding, target, target_index, member, "invalid", evidence_reason, None, marker))
                    continue
                if member is None:
                    decisions.append(_TargetDecision(finding, target, target_index, None, "invalid", "unknown_target_member", None, marker))
                    continue
                position_error = _position_error(target)
                if position_error:
                    decisions.append(_TargetDecision(finding, target, target_index, member, "invalid", position_error, None, marker))
                    continue
                if finding.severity not in PUBLISHABLE_SEVERITIES or finding.confidence != PUBLISHABLE_CONFIDENCE:
                    decisions.append(_TargetDecision(finding, target, target_index, member, "filtered", "below_publish_threshold", None, marker))
                    continue
                if target.position is None:
                    decisions.append(_TargetDecision(finding, target, target_index, member, "publishable_note", "position_not_provided", None, marker))
                    continue
                resolution = _position_map(prepared[member.member_id]).resolve(
                    target.position.old_path,
                    target.position.new_path,
                    target.position.old_line,
                    target.position.new_line,
                )
                if resolution.position is not None:
                    decisions.append(_TargetDecision(finding, target, target_index, member, "publishable_inline", "", resolution.position, marker))
                elif resolution.reason == "line_not_in_diff":
                    decisions.append(_TargetDecision(finding, target, target_index, member, "publishable_note", "position_not_in_diff", None, marker))
                else:
                    decisions.append(_TargetDecision(finding, target, target_index, member, "invalid", resolution.reason, None, marker))
        return tuple(decisions)

    def _post(self, decision: _TargetDecision, model_name: str) -> dict[str, object]:
        body = _comment_body(decision, model_name)
        if decision.status == "publishable_inline":
            response = self.gitlab.post_mr_discussion(
                decision.member,
                body,
                decision.finding.severity,
                decision.position.to_gitlab_position(),
            )
            result = self._result(decision, "posted_inline", decision.reason)
            result["discussion_id"] = response.get("id")
            notes = response.get("notes") if isinstance(response, dict) else None
            if isinstance(notes, list) and notes and isinstance(notes[0], dict):
                result["note_id"] = notes[0].get("id")
            return result

        response = self.gitlab.post_mr_note(
            GitLabMrUrl("", decision.member.project_path, decision.member.mr_iid),
            body,
        )
        result = self._result(decision, "posted_note", decision.reason)
        result["note_id"] = response.get("id") if isinstance(response, dict) else None
        return result

    def _unpublished(self, decision: _TargetDecision, status: str, reason: str) -> dict[str, object]:
        if decision.status in {"invalid", "filtered"}:
            return self._result(decision, decision.status, decision.reason)
        return self._result(decision, status, reason)

    @staticmethod
    def _result(decision: _TargetDecision, status: str, reason: str) -> dict[str, object]:
        return {
            "issue_id": decision.finding.issue_id,
            "rule_id": decision.finding.rule_id,
            "severity": decision.finding.severity,
            "confidence": decision.finding.confidence,
            "title": decision.finding.title,
            "impact": decision.finding.impact,
            "member_id": decision.target.member_id,
            "target_index": decision.target_index,
            "suggestion": decision.target.suggestion,
            "status": status,
            "reason": reason,
            "marker": decision.marker,
        }


def _position_map(prepared: PreparedReviewSetMember) -> DiffPositionMap:
    member = prepared.member
    refs = DiffRefs(member.base_sha, member.start_sha, member.head_sha)
    return DiffPositionMap.from_unified_diff(prepared.diff, refs)


def _evidence_error(finding: ReviewSetFinding, members: dict[str, ReviewSetMember]) -> str:
    for evidence in finding.evidence_refs:
        if evidence.member_id not in members:
            return "unknown_evidence_member"
        if not _safe_repo_path(evidence.path):
            return "invalid_evidence_path"
    return ""


def _position_error(target: ReviewSetFindingTarget) -> str:
    position = target.position
    if position is None:
        return ""
    if not _safe_diff_path(position.old_path) or not _safe_diff_path(position.new_path):
        return "invalid_target_path"
    lines = (position.old_line, position.new_line)
    if any(line < -1 or line == 0 for line in lines) or lines == (-1, -1):
        return "invalid_target_line"
    return ""


def _safe_diff_path(path: str) -> bool:
    return path == "/dev/null" or _safe_repo_path(path)


def _safe_repo_path(path: str) -> bool:
    if not path or "\\" in path or re.match(r"^[A-Za-z]:/", path):
        return False
    candidate = PurePosixPath(path)
    return not candidate.is_absolute() and ".." not in candidate.parts


def _finding_key(finding: ReviewSetFinding) -> str:
    canonical = {
        "rule_id": finding.rule_id,
        "evidence": sorted(
            (
                item.member_id,
                item.path,
                item.start_line,
                item.end_line,
            )
            for item in finding.evidence_refs
        ),
        "targets": sorted(
            (
                target.member_id,
                _position_tuple(target),
            )
            for target in finding.targets
        ),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _position_tuple(target: ReviewSetFindingTarget) -> tuple:
    if target.position is None:
        return ("note",)
    return (
        target.position.old_path,
        target.position.new_path,
        target.position.old_line,
        target.position.new_line,
    )


def _marker(review_set_id: str, finding_key: str, target: ReviewSetFindingTarget) -> str:
    target_text = json.dumps(
        [target.member_id, _position_tuple(target)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    target_key = hashlib.sha256(target_text.encode("utf-8")).hexdigest()[:16]
    return f"<!-- ai-cr:review-set:{review_set_id}:{finding_key}:{target_key} -->"


def _extract_markers(discussions: list[dict]) -> set[str]:
    markers = set()
    for discussion in discussions:
        notes = discussion.get("notes") if isinstance(discussion, dict) else None
        if not isinstance(notes, list):
            continue
        for note in notes:
            body = note.get("body") if isinstance(note, dict) else None
            if isinstance(body, str):
                markers.update(MARKER_RE.findall(body))
    return markers


def _comment_body(decision: _TargetDecision, model_name: str) -> str:
    finding = decision.finding
    evidence = "；".join(
        f"{item.member_id}:{item.path}:{item.start_line}-{item.end_line} {item.detail}"
        for item in finding.evidence_refs
    )
    return (
        f"【🤖AI Review-{model_name}】[{finding.severity}][ReviewSet]{finding.title}\n"
        f"- **影响**: {finding.impact}\n"
        f"- **证据**: {evidence}\n"
        f"- **建议**: {decision.target.suggestion}\n\n"
        f"{decision.marker}"
    )


def _publication(results: tuple[dict[str, object], ...]) -> ReviewSetPublication:
    counts = {
        "total": len(results),
        "posted_inline": 0,
        "posted_note": 0,
        "skipped_duplicate": 0,
        "filtered": 0,
        "invalid": 0,
        "failed": 0,
        "disabled": 0,
        "model_not_configured": 0,
    }
    for result in results:
        status = result["status"]
        if status in counts:
            counts[status] += 1
    warning = any(item["status"] in {"invalid", "failed", "model_not_configured"} for item in results)
    return ReviewSetPublication("success_with_warnings" if warning else "success", results, counts)
