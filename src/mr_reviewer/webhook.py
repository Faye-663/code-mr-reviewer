from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabClient
from mr_reviewer.inline_review import DiffPositionMap, DiffRefs, FindingValidationDecision, validate_review_findings
from mr_reviewer.markdown_report import render_markdown_review_report
from mr_reviewer.observability import task_context
from mr_reviewer.review_result import StructuredReviewParseError, parse_structured_review_result
from mr_reviewer.review_routing import resolve_review_routing
from mr_reviewer.reviewer import MergeRequestReviewTarget, ReviewReport, ReviewService, ReviewStageError

LOG = logging.getLogger("mr_reviewer")


@dataclass(frozen=True, slots=True)
class WebhookReviewEvent:
    event_id: str
    action: str
    update_reason: str
    oldrev: str
    manual_build: bool
    target: MergeRequestReviewTarget


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status: int
    body: dict


def parse_gitlab_merge_request_event(payload: dict, config: Config) -> WebhookReviewEvent | None:
    if payload.get("object_kind") != "merge_request":
        return None

    attrs = _require_dict(payload, "object_attributes")
    action = str(attrs.get("action") or "")
    update_reason = str(attrs.get("update_reason") or "")
    if not (
        action in {"open", "reopen"}
        or (
            action == "update"
            and update_reason == "source update"
        )
    ):
        return None
    if attrs.get("conflict") is True:
        return None

    project = _optional_dict(payload.get("project"))
    source = _optional_dict(attrs.get("source"))
    target_project = _optional_dict(attrs.get("target"))
    project_path = _first_text(
        project.get("path_with_namespace"),
        target_project.get("path_with_namespace"),
        source.get("path_with_namespace"),
    )
    if not project_path:
        raise ValueError("webhook payload missing project.path_with_namespace")
    if config.allowed_repos and project_path not in config.allowed_repos:
        return None

    mr_iid = _require_int(attrs, "iid")
    source_branch = _require_text(attrs, "source_branch")
    target_branch = _require_text(attrs, "target_branch")
    title = _require_text(attrs, "title")
    last_commit = _require_dict(attrs, "last_commit")
    head_sha = _require_text(last_commit, "id")
    target_repo_url = _first_text(
        target_project.get("http_url"),
        target_project.get("git_http_url"),
        project.get("http_url"),
        project.get("git_http_url"),
    )
    if not target_repo_url:
        raise ValueError("webhook payload missing target repository http_url")
    source_repo_url = _first_text(
        source.get("http_url"),
        source.get("git_http_url"),
        target_repo_url,
    )
    mr_url = _first_text(attrs.get("url"), _build_mr_url(project.get("web_url"), mr_iid))
    if not mr_url:
        raise ValueError("webhook payload missing MR url")

    target = MergeRequestReviewTarget(
        base_url=config.gitlab_base_url.rstrip("/"),
        project_path=project_path,
        mr_iid=mr_iid,
        mr_url=mr_url,
        target_repo_url=target_repo_url,
        source_repo_url=source_repo_url,
        target_branch=target_branch,
        source_branch=source_branch,
        base_sha=None,
        head_sha=head_sha,
        title=title,
    )
    return WebhookReviewEvent(
        event_id=f"{project_path}!{mr_iid}:{head_sha}",
        action=action,
        update_reason=update_reason,
        oldrev=str(attrs.get("oldrev") or ""),
        manual_build=bool(payload.get("manual_build", False)),
        target=target,
    )


def handle_webhook_request(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        config: Config,
        enqueue: Callable[[WebhookReviewEvent], None],
) -> WebhookResponse:
    if path != config.webhook_path:
        return _json_response(404, "NOT_FOUND", "webhook path not found")
    if method.upper() != "POST":
        return _json_response(405, "METHOD_NOT_ALLOWED", "webhook only accepts POST")

    token_status = _check_webhook_token(headers, config.webhook_secret, config.webhook_secret_header)
    if token_status is not None:
        return token_status
    if not config.webhook_secret:
        LOG.warning("stage=webhook status=warning reason=webhook_secret_not_configured")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _json_response(400, "INVALID_JSON", f"invalid JSON payload: {exc}")
    if not isinstance(payload, dict):
        return _json_response(400, "INVALID_JSON", "webhook payload must be a JSON object")

    try:
        event = parse_gitlab_merge_request_event(payload, config)
    except ValueError as exc:
        return _json_response(400, "INVALID_WEBHOOK", str(exc))
    if event is None:
        return WebhookResponse(200, {"status": "skipped"})

    enqueue(event)
    return WebhookResponse(
        202,
        {
            "status": "accepted",
            "event_id": event.event_id,
            "repo": event.target.project_path,
            "mr_iid": event.target.mr_iid,
        },
    )


class WebhookReviewQueue:
    def __init__(self, service: ReviewService, gitlab: GitLabClient, config: Config):
        self.service = service
        self.gitlab = gitlab
        self.config = config
        self._queue: queue.Queue[WebhookReviewEvent] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="mr-reviewer-webhook-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, event: WebhookReviewEvent) -> None:
        self._queue.put(event)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            task_id = f"webhook-{uuid.uuid4().hex[:12]}"
            try:
                with task_context(task_id, self.config.debug_dir, self.config.log_level == "DEBUG"):
                    LOG.info(
                        "task=%s stage=webhook_review repo=%s mr_iid=%s status=started",
                        task_id,
                        event.target.project_path,
                        event.target.mr_iid,
                    )
                    report = self.service.review_target(event.target, self.config, task_id, structured_output=True)
                    report = self._submit_comment(event, report)
                    path = write_webhook_monitor_report(event, report, self.config, task_id, "success")
                    LOG.info("task=%s stage=webhook_report path=%s status=success", task_id, path)
            except Exception as exc:  # noqa: BLE001 - webhook 后台任务必须记录失败并继续处理队列。
                LOG.error("task=%s stage=webhook_review status=failed error=%s", task_id, _redact(str(exc), self.config))
                review_plan = exc.review_plan if isinstance(exc, ReviewStageError) else None
                failure_stage = exc.stage if isinstance(exc, ReviewStageError) else ""
                agent_call_count = exc.agent_call_count if isinstance(exc, ReviewStageError) else 0
                routing = resolve_review_routing(event.target.title)
                failure_report = ReviewReport(
                    markdown="",
                    summary=None,
                    review_plan=review_plan,
                    head_sha=event.target.head_sha,
                    changed_files=[],
                    submission_owner="python",
                    submission_status="failed",
                    failure_stage=failure_stage,
                    title=event.target.title,
                    review_mode=routing.review_mode,
                    routing_reason=routing.routing_reason,
                    routing_marker=routing.routing_marker,
                    agent_call_count=agent_call_count,
                )
                try:
                    write_webhook_monitor_report(event, failure_report, self.config, task_id, "failed", str(exc))
                except Exception as report_exc:  # noqa: BLE001 - 记录失败不能让 worker 线程退出。
                    LOG.error(
                        "task=%s stage=webhook_report status=failed error=%s",
                        task_id,
                        _redact(str(report_exc), self.config),
                    )
            finally:
                self._queue.task_done()

    def _submit_comment(self, event: WebhookReviewEvent, report: ReviewReport) -> ReviewReport:
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

        if not self.config.webhook_post_comment:
            results = [_unpublished_finding_result(finding, "disabled", "webhook_post_comment_disabled") for finding in structured.findings]
            return replace(
                report,
                submission_owner="python",
                submission_status="disabled",
                structured_parse_status="success",
                finding_counts=_finding_counts(results),
                finding_results=results,
                good=structured.good,
                notes=structured.notes,
                test_gaps=structured.test_gaps,
            )

        if not self.config.agent_model_name:
            results = [_unpublished_finding_result(finding, "model_not_configured", "agent_model_name_missing") for finding in structured.findings]
            return replace(
                report,
                submission_owner="python",
                submission_status="model_not_configured",
                structured_parse_status="success",
                finding_counts=_finding_counts(results),
                finding_results=results,
                good=structured.good,
                notes=structured.notes,
                test_gaps=structured.test_gaps,
            )

        detail = self.gitlab.get_mr_detail_for_discussion_position(event.target)
        refs = _diff_refs_from_detail(detail)
        position_map = DiffPositionMap.from_unified_diff(report.diff, refs)
        decisions = validate_review_findings(
            structured,
            position_map,
            self.config.publication_policy,
        )
        publish_results = DiscussionPublisher(self.gitlab, self.config.agent_model_name).publish(event.target, decisions)
        status = "failed" if any(item["status"] == "failed" for item in publish_results) else "posted"
        return replace(
            report,
            submission_owner="python",
            submission_status=status,
            structured_parse_status="success",
            finding_counts=_finding_counts(publish_results),
            finding_results=publish_results,
            good=structured.good,
            notes=structured.notes,
            test_gaps=structured.test_gaps,
        )


class DiscussionPublisher:
    def __init__(self, gitlab: GitLabClient, model_name: str):
        self.gitlab = gitlab
        self.model_name = model_name

    def publish(self, target: MergeRequestReviewTarget, decisions: list[FindingValidationDecision]) -> list[dict]:
        existing_markers = _extract_existing_markers(self.gitlab.list_mr_discussions(target))
        results = []
        for decision in decisions:
            if decision.status != "publishable":
                results.append(_finding_result(decision, decision.status, decision.reason))
                continue

            marker = _finding_marker(target, decision)
            if marker in existing_markers:
                results.append(_finding_result(decision, "skipped_duplicate", "duplicate_marker", marker))
                continue

            try:
                response = self.gitlab.post_mr_discussion(
                    target,
                    _discussion_body(decision, marker, self.model_name),
                    decision.finding.severity,
                    decision.position.to_gitlab_position(),
                )
            except Exception as exc:  # noqa: BLE001 - 单条发布失败需要记录后继续处理其它 finding。
                results.append(_finding_result(decision, "failed", str(exc), marker))
                continue

            result = _finding_result(decision, "posted", "", marker)
            result["discussion_id"] = response.get("id")
            notes = response.get("notes") if isinstance(response.get("notes"), list) else []
            if notes and isinstance(notes[0], dict):
                result["note_id"] = notes[0].get("id")
            results.append(result)
        return results


def run_webhook_server(config: Config, service: ReviewService) -> int:
    gitlab = GitLabClient(config.gitlab_api_base_url, config.gitlab_token, config.test_gitlab_responses)
    worker = WebhookReviewQueue(service, gitlab, config)
    worker.start()
    handler = make_webhook_handler(config, worker.enqueue)
    server = ThreadingHTTPServer((config.webhook_host, config.webhook_port), handler)
    LOG.info(
        "stage=webhook_server status=started host=%s port=%s path=%s",
        config.webhook_host,
        config.webhook_port,
        config.webhook_path,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def write_webhook_monitor_report(
        event: WebhookReviewEvent,
        report: ReviewReport,
        config: Config,
        task_id: str,
        status: str,
        error: str | None = None,
) -> Path:
    config.report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    repo_name = _safe_filename(event.target.project_path)
    path = config.report_dir / f"{timestamp}-{repo_name}-mr-{event.target.mr_iid}-{task_id}.json"
    changed_files = report.changed_files or []
    data = {
        "task_id": task_id,
        "status": status,
        "event_id": event.event_id,
        "action": event.action,
        "update_reason": event.update_reason,
        "manual_build": event.manual_build,
        "repo": event.target.project_path,
        "mr_iid": event.target.mr_iid,
        "mr_url": event.target.mr_url,
        "source_branch": event.target.source_branch,
        "target_branch": event.target.target_branch,
        "oldrev": event.oldrev,
        "base_sha": report.base_sha,
        "head_sha": report.head_sha or event.target.head_sha,
        "changed_files": changed_files,
        "changed_files_count": len(changed_files),
        "opencode_returncode": report.opencode_returncode,
        "submission_owner": report.submission_owner,
        "submission_status": report.submission_status,
        "comment_url": None,
        "markdown_preview": report.markdown[:4000],
        "summary": report.summary,
        "review_plan": report.review_plan,
        "good": report.good or [],
        "notes": report.notes or [],
        "test_gaps": report.test_gaps or [],
        "prompt_templates": report.prompt_templates or {},
        "review_mode": report.review_mode,
        "routing_reason": report.routing_reason,
        "routing_marker": report.routing_marker,
        "agent_call_count": report.agent_call_count,
    }
    if report.structured_parse_status:
        data["structured_parse_status"] = report.structured_parse_status
    if report.finding_counts is not None:
        data["finding_counts"] = report.finding_counts
    if report.finding_results is not None:
        data["finding_results"] = report.finding_results
    if report.failure_stage:
        data["failure_stage"] = report.failure_stage
    redacted_error = _redact(error, config) if error else None
    markdown_path = path.with_suffix(".md")
    markdown_path.write_text(
        render_markdown_review_report(event, report, status, redacted_error),
        encoding="utf-8",
    )
    data["markdown_report_path"] = str(markdown_path)
    if error:
        data["error"] = redacted_error
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def make_webhook_handler(config: Config, enqueue: Callable[[WebhookReviewEvent], None]):
    class GitLabWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API 固定使用该命名。
            parsed_path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0") or "0")
            response = handle_webhook_request(
                "POST",
                parsed_path,
                dict(self.headers.items()),
                self.rfile.read(length),
                config,
                enqueue,
            )
            self._write_response(response)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API 固定使用该命名。
            self._write_response(_json_response(405, "METHOD_NOT_ALLOWED", "webhook only accepts POST"))

        def log_message(self, format: str, *args: object) -> None:
            LOG.info("stage=webhook_http message=%s", format % args)

        def _write_response(self, response: WebhookResponse) -> None:
            raw = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return GitLabWebhookHandler


def _check_webhook_token(headers: dict[str, str], expected: str, header_name: str) -> WebhookResponse | None:
    if not expected:
        return None
    normalized = {key.lower(): value for key, value in headers.items()}
    actual = normalized.get(header_name.lower())
    if actual is None:
        return _json_response(401, "WEBHOOK_TOKEN_MISSING", f"{header_name} header is required")
    if actual != expected:
        return _json_response(403, "WEBHOOK_TOKEN_INVALID", f"{header_name} header is invalid")
    return None


def _json_response(status: int, code: str, message: str) -> WebhookResponse:
    return WebhookResponse(status, {"error": {"code": code, "message": message}})


def _require_dict(payload: dict, key: str) -> dict:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"webhook payload missing {key}")
    return value


def _optional_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _require_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"webhook payload missing {key}")
    return value


def _require_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError(f"webhook payload missing {key}")


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _build_mr_url(web_url: object, mr_iid: int) -> str:
    if not isinstance(web_url, str) or not web_url:
        return ""
    return f"{web_url.rstrip('/')}/merge_requests/{mr_iid}"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "repo"


def _diff_refs_from_detail(detail: dict) -> DiffRefs:
    diff_refs = detail.get("diff_refs")
    if not isinstance(diff_refs, dict):
        raise ValueError("GitLab MR detail response missing diff_refs")
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    if not all(isinstance(value, str) and value for value in (base_sha, start_sha, head_sha)):
        raise ValueError("GitLab MR detail diff_refs missing base_sha/start_sha/head_sha")
    return DiffRefs(base_sha=base_sha, start_sha=start_sha, head_sha=head_sha)


def _extract_existing_markers(discussions: list[dict]) -> set[str]:
    markers = set()
    for discussion in discussions:
        notes = discussion.get("notes") if isinstance(discussion, dict) else None
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            body = note.get("body")
            if not isinstance(body, str):
                continue
            markers.update(re.findall(r"<!-- ai-cr:finding:[^>]+ -->", body))
    return markers


def _finding_marker(target: MergeRequestReviewTarget, decision: FindingValidationDecision) -> str:
    finding = decision.finding
    head_sha = decision.position.refs.head_sha if decision.position else target.head_sha
    return (
        "<!-- ai-cr:finding:"
        f"{target.project_path}:{target.mr_iid}:{head_sha}:{finding.rule_id}:"
        f"{finding.old_path}:{finding.new_path}:{finding.old_line}:{finding.new_line}"
        " -->"
    )


def _discussion_body(decision: FindingValidationDecision, marker: str, model_name: str) -> str:
    finding = decision.finding
    return (
        f"【🤖AI Review-{model_name}】[{finding.severity}]{finding.title}\n"
        f"- **影响**: {finding.impact}\n"
        f"- **建议**: {finding.suggestion}\n\n"
        f"{marker}"
    )


def _finding_result(
        decision: FindingValidationDecision,
        status: str,
        reason: str,
        marker: str = "",
) -> dict:
    finding = decision.finding
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
        "skipped_duplicate": 0,
        "filtered": 0,
        "invalid": 0,
        "failed": 0,
    }
    for result in results:
        status = result.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _unpublished_finding_result(finding, status: str, reason: str) -> dict:
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


def _redact(text: str, config: Config) -> str:
    if config.gitlab_token:
        basic_token = base64.b64encode(f"oauth2:{config.gitlab_token}".encode("utf-8")).decode("ascii")
        return text.replace(config.gitlab_token, "<redacted>").replace(basic_token, "<redacted>")
    return text
