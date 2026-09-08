from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from mr_reviewer.gitlab import GitLabMrUrl

if TYPE_CHECKING:
    from mr_reviewer.review_set import ReviewSetManifest
    from mr_reviewer.review_set_publish import ReviewSetPublication
    from mr_reviewer.reviewer import ReviewSetReviewReport
    from mr_reviewer.single_mr import SingleMrOutcome


_DISPOSITION_LABELS = {
    "created": "新建检视任务",
    "joined": "加入已有检视任务",
    "reused": "复用已有完成结果",
    "duplicate": "复用原请求结果",
}
_SEVERITIES = ("fatal", "major", "minor", "suggestion")
_FAILURE_STAGE_LABELS = {
    "mr_metadata": "MR 信息读取",
    "review_plan": "检视计划",
    "dependency_review_plan": "依赖检视计划",
    "dependency_review": "依赖联合检视",
    "review_set_plan": "联合检视计划",
    "review_set_review": "联合检视执行",
    "review": "检视执行",
}


def render_single_accepted(mr: GitLabMrUrl, ahead: int | None = None) -> str:
    lines = ["[代码检视已受理]", f"MR：{_mr_label(mr)}", f"地址：{_mr_url(mr)}"]
    if ahead is not None:
        lines.append(_queue_line(ahead))
    return "\n".join(lines)


def render_single_queue_full(mr: GitLabMrUrl, limit: int) -> str:
    return "\n".join(
        [
            "[代码检视暂未受理]",
            f"MR：{_mr_label(mr)}",
            f"地址：{_mr_url(mr)}",
            f"原因：当前 IM 检视队列已满（最多等待 {limit} 个请求），请稍后重新发送。",
        ]
    )


def render_single_terminal(mr: GitLabMrUrl, outcome: SingleMrOutcome) -> str:
    report = outcome.report
    lines = [_single_terminal_title(outcome.status), f"MR：{_mr_label(mr)}", f"地址：{_mr_url(mr)}"]
    if report and report.head_sha:
        lines.append(_single_version_line(outcome))
    disposition = _DISPOSITION_LABELS.get(outcome.disposition)
    if disposition:
        lines.append(f"执行：{disposition}")
    if outcome.status in {"failed", "interrupted"}:
        lines.append(f"阶段：{_failure_stage_label(report.failure_stage if report else '')}")
    elif report:
        lines.append(_single_finding_summary(report))
    lines.extend(_single_delivery_lines(outcome))
    lines.append(f"跟踪ID：{outcome.review_run_id}")
    return "\n".join(lines)


def render_single_failed(mr: GitLabMrUrl, task_id: str, stage: str = "mr_metadata") -> str:
    return "\n".join(
        [
            "[代码检视失败]",
            f"MR：{_mr_label(mr)}",
            f"地址：{_mr_url(mr)}",
            f"阶段：{_failure_stage_label(stage)}",
            "GitLab：未执行",
            "Review Report：未上传",
            f"跟踪ID：{task_id}",
        ]
    )


def render_review_set_accepted(
    members: Iterable[GitLabMrUrl],
    ahead: int | None = None,
) -> str:
    lines = ["[联合代码检视已受理]", *_member_lines(members)]
    if ahead is not None:
        lines.append(_queue_line(ahead))
    return "\n".join(lines)


def render_review_set_queue_full(members: Iterable[GitLabMrUrl], limit: int) -> str:
    return "\n".join(
        [
            "[联合代码检视暂未受理]",
            *_member_lines(members),
            f"原因：当前 IM 检视队列已满（最多等待 {limit} 个请求），请稍后重新发送。",
        ]
    )


def render_review_set_rejected(members: Iterable[GitLabMrUrl], reason: str, task_id: str) -> str:
    return "\n".join(
        ["[联合代码检视已拒绝]", *_member_lines(members), f"原因：{reason}", f"跟踪ID：{task_id}"]
    )


def render_review_set_failed(
    members: Iterable[GitLabMrUrl],
    task_id: str,
    stage: str = "review_set_review",
) -> str:
    return "\n".join(
        [
            "[联合代码检视失败]",
            *_member_lines(members),
            f"阶段：{_failure_stage_label(stage)}",
            "GitLab：未执行或未全部完成",
            "Review Report：未上传",
            f"跟踪ID：{task_id}",
        ]
    )


def render_review_set_terminal(
    report: ReviewSetReviewReport,
    publication: ReviewSetPublication,
    file_name: str,
    upload_error: str | None,
    task_id: str,
) -> str:
    has_warnings = publication.status == "success_with_warnings" or bool(upload_error)
    title = "[联合代码检视完成但有告警]" if has_warnings else "[联合代码检视完成]"
    manifest = report.manifest
    counts = publication.counts
    posted = _count(counts, "posted_inline") + _count(counts, "posted_note")
    existing = _count(counts, "skipped_duplicate")
    report_only = _count(counts, "filtered") + _count(counts, "disabled")
    abnormal = _count(counts, "failed") + _count(counts, "invalid") + _count(counts, "model_not_configured")
    return "\n".join(
        [
            title,
            f"ReqID：{manifest.req_id}",
            f"ReviewSet：{manifest.review_set_id[:12]}",
            *_manifest_member_lines(manifest),
            _finding_summary(report.result.findings),
            f"GitLab：新发布 {posted} 条，已存在 {existing} 条，仅报告 {report_only} 条，异常/无效 {abnormal} 条",
            f"Review Report：{'上传 OneBox 失败' if upload_error else f'已上传 OneBox（{file_name}）'}",
            f"跟踪ID：{task_id}",
        ]
    )


def _single_terminal_title(status: str) -> str:
    if status == "success_with_warnings":
        return "[代码检视完成但有告警]"
    if status == "superseded":
        return "[代码检视已过期]"
    if status in {"failed", "interrupted"}:
        return "[代码检视失败]"
    return "[代码检视完成]"


def _single_version_line(outcome: SingleMrOutcome) -> str:
    report = outcome.report
    if report is None:
        return "版本：未知"
    review_head = report.head_sha[:12]
    current_head = (report.head_validation or {}).get("current_head_sha", "")[:12]
    if outcome.status == "superseded" and current_head and current_head != review_head:
        return f"版本：{review_head} → {current_head}"
    return f"版本：{review_head}"


def _single_delivery_lines(outcome: SingleMrOutcome) -> list[str]:
    report = outcome.report
    gitlab = outcome.deliveries.get("gitlab", {})
    onebox = outcome.deliveries.get("onebox", {})
    gitlab_status = str(gitlab.get("status") or "not_run")
    onebox_status = str(onebox.get("status") or "not_run")
    if gitlab_status == "succeeded":
        counts = report.finding_counts if report and report.finding_counts else {}
        if _count(counts, "total") == 0:
            gitlab_text = "无需发布（未发现问题）"
        else:
            gitlab_text = (
                f"成功（新发布 {_count(counts, 'posted')} 条，"
                f"已存在 {_count(counts, 'skipped_duplicate')} 条）"
            )
    else:
        gitlab_text = {
            "disabled": "未启用",
            "failed": "失败",
            "unknown": "结果未知",
            "running": "结果未知",
            "skipped_stale": "已跳过（MR 版本已过期）",
        }.get(gitlab_status, "未执行")
    if onebox_status == "succeeded":
        external_ref = str(onebox.get("external_ref") or "").strip()
        onebox_text = f"已上传 OneBox（{external_ref}）" if external_ref else "已上传 OneBox"
    else:
        onebox_text = {
            "disabled": "未上传（OneBox 未启用）",
            "failed": "上传 OneBox 失败",
            "unknown": "OneBox 上传结果未知",
            "running": "OneBox 上传结果未知",
            "skipped_stale": "未上传（MR 版本已过期）",
        }.get(onebox_status, "未上传")
    return [f"GitLab：{gitlab_text}", f"Review Report：{onebox_text}"]


def _single_finding_summary(report: object) -> str:
    finding_counts = getattr(report, "finding_counts", None)
    total = _count(finding_counts, "total")
    return _finding_summary(getattr(report, "finding_results", ()) or (), total)


def _finding_summary(findings: Iterable[object], total: int | None = None) -> str:
    items = tuple(findings)
    resolved_total = len(items) if total is None else total
    if resolved_total == 0:
        return "检视总结：未发现问题（共 0 条）"
    counts = {severity: 0 for severity in _SEVERITIES}
    for finding in items:
        severity = (
            finding.get("severity")
            if isinstance(finding, dict)
            else getattr(finding, "severity", "")
        )
        if severity in counts:
            counts[severity] += 1
    details = "、".join(f"{severity} {counts[severity]}" for severity in _SEVERITIES)
    return f"检视总结：共 {resolved_total} 条（{details}）"


def _member_lines(members: Iterable[GitLabMrUrl]) -> list[str]:
    return ["成员：", *(f"- {_mr_label(mr)}：{_mr_url(mr)}" for mr in members)]


def _queue_line(ahead: int) -> str:
    return f"IM队列：入队时前方 {ahead} 个请求"


def _manifest_member_lines(manifest: ReviewSetManifest) -> list[str]:
    return [
        "成员：",
        *(
            f"- {member.project_path}!{member.mr_iid} @ {member.head_sha[:12]}：{member.mr_url}"
            for member in manifest.members
        ),
    ]


def _mr_label(mr: GitLabMrUrl) -> str:
    return f"{mr.project_path}!{mr.mr_iid}"


def _mr_url(mr: GitLabMrUrl) -> str:
    return f"{mr.base_url.rstrip('/')}/{mr.project_path}/merge_requests/{mr.mr_iid}"


def _failure_stage_label(stage: str) -> str:
    return _FAILURE_STAGE_LABELS.get(stage, "检视执行")


def _count(counts: dict[str, int] | None, key: str) -> int:
    return int((counts or {}).get(key, 0))
