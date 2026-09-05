from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from mr_reviewer.coordination import ReviewRunRecord
from mr_reviewer.markdown_report import render_review_report, render_structured_output_as_markdown
from mr_reviewer.reviewer import ReviewReport


class ReviewArtifactStore:
    def __init__(self, report_dir: Path):
        self.report_dir = Path(report_dir)

    def write(
        self,
        run: ReviewRunRecord,
        report: ReviewReport,
        *,
        review_status: str,
        triggers: list[dict[str, object]],
        deliveries: dict[str, dict[str, object]],
        error: str = "",
    ) -> tuple[Path, Path]:
        json_path, markdown_path = self._paths(run)
        report = self._preserve_delivery_result(json_path, report)
        normalized = normalize_review_report(report)
        head_validation = normalized.head_validation or {
            "review_head_sha": normalized.head_sha or run.head_sha,
            "current_head_sha": "",
            "status": "not_checked",
        }
        payload = {
            "schema_version": "single-mr-review-run/v1",
            "review_run_id": run.review_run_id,
            "review_key": run.review_key,
            "attempt": run.attempt,
            "review_status": review_status,
            "superseded_by": run.superseded_by,
            "triggers": triggers,
            "deliveries": deliveries,
            "head_validation": head_validation,
            "markdown_report_path": str(markdown_path),
            "error": error,
            "report": asdict(report),
        }
        payload.update(_compatibility_fields(normalized))
        markdown = _render_artifact_markdown(
            normalized,
            run,
            review_status,
            triggers,
            deliveries,
            head_validation,
            error,
        )
        self.report_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(markdown_path, markdown)
        _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return json_path, markdown_path

    def _preserve_delivery_result(self, json_path: Path, report: ReviewReport) -> ReviewReport:
        if report.finding_results is not None or not json_path.is_file():
            return report
        try:
            existing = self.load(json_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return report
        return existing if existing.finding_results is not None else report

    def load(self, path: str | Path) -> ReviewReport:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        report = payload.get("report")
        if not isinstance(report, dict):
            raise ValueError("review artifact does not contain a reusable report")
        return ReviewReport(**report)

    def _paths(self, run: ReviewRunRecord) -> tuple[Path, Path]:
        if run.report_json_path:
            json_path = Path(run.report_json_path)
            return json_path, Path(run.report_markdown_path) if run.report_markdown_path else json_path.with_suffix(".md")
        timestamp = datetime.fromisoformat(run.created_at).strftime("%Y%m%dT%H%M%SZ")
        repo = re.sub(r"[^A-Za-z0-9_.-]+", "_", run.project_path).strip("_") or "repo"
        stem = f"{timestamp}-{repo}-mr-{run.mr_iid}-{run.head_sha[:12]}-{run.review_run_id}"
        return self.report_dir / f"{stem}.json", self.report_dir / f"{stem}.md"


def normalize_review_report(report: ReviewReport) -> ReviewReport:
    if report.structured_parse_status == "success" and report.finding_results is not None:
        return report
    rendered = render_structured_output_as_markdown(report)
    return replace(rendered, markdown=report.markdown)


def _compatibility_fields(report: ReviewReport) -> dict[str, object]:
    return {
        "status": report.submission_status,
        "repo": report.repo,
        "mr_iid": report.mr_iid,
        "mr_url": report.mr_url,
        "source_branch": report.source_branch,
        "target_branch": report.target_branch,
        "base_sha": report.base_sha,
        "head_sha": report.head_sha,
        "changed_files": report.changed_files or [],
        "changed_files_count": len(report.changed_files or []),
        "opencode_returncode": report.opencode_returncode,
        "submission_owner": report.submission_owner,
        "submission_status": report.submission_status,
        "structured_parse_status": report.structured_parse_status,
        "finding_counts": report.finding_counts,
        "finding_results": report.finding_results,
        "summary": report.summary,
        "review_plan": report.review_plan,
        "good": report.good or [],
        "notes": report.notes or [],
        "test_gaps": report.test_gaps or [],
        "prompt_templates": report.prompt_templates or {},
        "requested_review_mode": report.requested_review_mode or report.review_mode,
        "review_mode": report.review_mode,
        "review_scope": report.review_scope,
        "routing_reason": report.routing_reason,
        "routing_marker": report.routing_marker,
        "dependency_context_status": report.dependency_context_status,
        "dependency_degradation_reason": report.dependency_degradation_reason,
        "dependency_failed_project": report.dependency_failed_project,
        "dependency_context_id": report.dependency_context_id,
        "dependency_repositories": report.dependency_repositories or [],
        "dependency_preparation_seconds": report.dependency_preparation_seconds,
        "dependency_relationship_summary": report.dependency_relationship_summary or [],
        "agent_call_count": report.agent_call_count,
        "failure_stage": report.failure_stage,
    }


def _render_artifact_markdown(
    report: ReviewReport,
    run: ReviewRunRecord,
    review_status: str,
    triggers: list[dict[str, object]],
    deliveries: dict[str, dict[str, object]],
    head_validation: dict[str, str],
    error: str,
) -> str:
    lines = [
        "# ReviewRun 状态",
        "",
        f"- ReviewRun：`{run.review_run_id}`",
        f"- ReviewKey：`{run.review_key}`",
        f"- Attempt：{run.attempt}",
        f"- 状态：`{review_status}`",
        f"- Head 校验：`{head_validation.get('status', 'not_checked')}`",
    ]
    if run.superseded_by:
        lines.append(f"- 被替代为：`{run.superseded_by}`")
    lines.extend(["", "## 触发与交付", ""])
    for trigger in triggers:
        lines.append(
            f"- {trigger['source']}：`{trigger['trigger_id']}`；"
            f"GitLab={'on' if trigger['post_comment'] else 'off'}；"
            f"OneBox={'on' if trigger['upload_onebox'] else 'off'}"
        )
    for sink in ("gitlab", "onebox"):
        delivery = deliveries.get(sink, {"status": "pending"})
        lines.append(f"- {sink}：`{delivery.get('status', 'pending')}`")
    lines.extend(["", render_review_report(report, review_status, error or None)])
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp-{uuid.uuid4().hex}")
    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
