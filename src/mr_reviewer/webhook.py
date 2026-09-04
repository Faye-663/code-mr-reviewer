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
from mr_reviewer.delivery import deliver_gitlab_review
from mr_reviewer.gitlab import GitLabClient
from mr_reviewer.markdown_report import render_markdown_review_report
from mr_reviewer.observability import task_context
from mr_reviewer.review_routing import resolve_review_routing
from mr_reviewer.reviewer import MergeRequestReviewTarget, ReviewReport, ReviewService, ReviewStageError
from mr_reviewer.single_mr import SingleMrReviewCoordinator, SingleMrTrigger

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


@dataclass(frozen=True, slots=True)
class _RepositoryMetadata:
    project_path: str
    project: dict
    source: dict
    target_project: dict


def parse_gitlab_merge_request_event(payload: dict, config: Config) -> WebhookReviewEvent | None:
    attrs = _eligible_merge_request_attributes(payload)
    if attrs is None:
        return None

    metadata = _parse_repository_metadata(payload, attrs, config)
    if metadata is None:
        return None
    target = _build_review_target(attrs, metadata, config)
    action = str(attrs.get("action") or "")
    update_reason = str(attrs.get("update_reason") or "")
    return WebhookReviewEvent(
        event_id=f"{target.project_path}!{target.mr_iid}:{target.head_sha}",
        action=action,
        update_reason=update_reason,
        oldrev=str(attrs.get("oldrev") or ""),
        manual_build=bool(payload.get("manual_build", False)),
        target=target,
    )


def _eligible_merge_request_attributes(payload: dict) -> dict | None:
    if payload.get("object_kind") != "merge_request":
        return None
    attrs = _require_dict(payload, "object_attributes")
    action = str(attrs.get("action") or "")
    update_reason = str(attrs.get("update_reason") or "")
    eligible = action in {"open", "reopen"} or (
        action == "update" and update_reason == "source update"
    )
    if not eligible or attrs.get("conflict") is True:
        return None
    return attrs


def _parse_repository_metadata(
        payload: dict,
        attrs: dict,
        config: Config,
) -> _RepositoryMetadata | None:
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

    return _RepositoryMetadata(
        project_path=project_path,
        project=project,
        source=source,
        target_project=target_project,
    )


def _build_review_target(
        attrs: dict,
        metadata: _RepositoryMetadata,
        config: Config,
) -> MergeRequestReviewTarget:
    mr_iid = _require_int(attrs, "iid")
    source_branch = _require_text(attrs, "source_branch")
    target_branch = _require_text(attrs, "target_branch")
    title = _require_text(attrs, "title")
    last_commit = _require_dict(attrs, "last_commit")
    head_sha = _require_text(last_commit, "id")
    target_repo_url = _first_text(
        metadata.target_project.get("http_url"),
        metadata.target_project.get("git_http_url"),
        metadata.project.get("http_url"),
        metadata.project.get("git_http_url"),
    )
    if not target_repo_url:
        raise ValueError("webhook payload missing target repository http_url")
    source_repo_url = _first_text(
        metadata.source.get("http_url"),
        metadata.source.get("git_http_url"),
        target_repo_url,
    )
    mr_url = _first_text(
        attrs.get("url"),
        _build_mr_url(metadata.project.get("web_url"), mr_iid),
    )
    if not mr_url:
        raise ValueError("webhook payload missing MR url")
    return MergeRequestReviewTarget(
        base_url=config.gitlab_base_url.rstrip("/"),
        project_path=metadata.project_path,
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


def handle_webhook_request(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        config: Config,
        enqueue: Callable[[WebhookReviewEvent], object | None],
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
    transport_event_id = _header_value(headers, "X-Gitlab-Event-UUID")
    if transport_event_id:
        event = replace(event, event_id=transport_event_id)

    registration = enqueue(event)
    extra = {}
    if registration is not None:
        extra = {
            "review_run_id": registration.review_run_id,
            "disposition": registration.disposition,
        }
    return WebhookResponse(
        202,
        {
            "status": "accepted",
            "event_id": event.event_id,
            "repo": event.target.project_path,
            "mr_iid": event.target.mr_iid,
            **extra,
        },
    )


class WebhookReviewQueue:
    def __init__(
        self,
        service: ReviewService,
        gitlab: GitLabClient,
        config: Config,
        coordinator: SingleMrReviewCoordinator | None = None,
    ):
        self.service = service
        self.gitlab = gitlab
        self.config = config
        self.coordinator = coordinator
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="mr-reviewer-webhook-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, event: WebhookReviewEvent):
        if self.coordinator is None:
            self._queue.put(event)
            return None
        event = replace(event, target=self.coordinator.refresh_target(event.target))
        registration = self.coordinator.register(
            event.target,
            SingleMrTrigger(
                source="webhook",
                trigger_id=event.event_id,
                post_comment=self.config.webhook_post_comment,
                upload_onebox=self.config.webhook_upload_onebox,
            ),
        )
        if registration.disposition != "duplicate":
            self._queue.put((event, registration))
        return registration

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if self.coordinator is not None:
                event, registration = item
                try:
                    outcome = self.coordinator.process(registration, event.target)
                    LOG.info(
                        "task=%s review_run_id=%s trigger_id=%s repo=%s mr_iid=%s head_sha=%s "
                        "stage=webhook_review outcome=%s",
                        registration.review_run_id,
                        registration.review_run_id,
                        registration.trigger_id,
                        event.target.project_path,
                        event.target.mr_iid,
                        event.target.head_sha,
                        outcome.status,
                    )
                except Exception as exc:  # noqa: BLE001 - 后台任务失败后必须继续消费队列。
                    LOG.exception(
                        "task=%s review_run_id=%s trigger_id=%s repo=%s mr_iid=%s head_sha=%s "
                        "stage=webhook_review outcome=failed error_type=%s",
                        registration.review_run_id,
                        registration.review_run_id,
                        registration.trigger_id,
                        event.target.project_path,
                        event.target.mr_iid,
                        event.target.head_sha,
                        type(exc).__name__,
                    )
                finally:
                    self._queue.task_done()
                continue
            event = item
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
                failure_report = _build_failure_review_report(event, exc)
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
        return deliver_gitlab_review(
            self.gitlab,
            self.config,
            event.target,
            report,
            enabled=self.config.webhook_post_comment,
        )


def run_webhook_server(config: Config, service: ReviewService) -> int:
    gitlab = GitLabClient(config.gitlab_api_base_url, config.gitlab_token, config.test_gitlab_responses)
    coordinator = SingleMrReviewCoordinator(service, gitlab, config)
    worker = WebhookReviewQueue(service, gitlab, config, coordinator)
    worker.start()
    handler = make_webhook_handler(config, worker.enqueue)
    server = ThreadingHTTPServer((config.webhook_host, config.webhook_port), handler)
    LOG.info(
        "stage=webhook_server outcome=started host=%s port=%s path=%s "
        "post_comment=%s upload_onebox=%s coordination_db_path=%s",
        config.webhook_host,
        config.webhook_port,
        config.webhook_path,
        config.webhook_post_comment,
        config.webhook_upload_onebox,
        config.coordination_db_path,
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
    data = _build_webhook_monitor_payload(event, report, task_id, status)
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


def _build_webhook_monitor_payload(
        event: WebhookReviewEvent,
        report: ReviewReport,
        task_id: str,
        status: str,
) -> dict[str, object]:
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
    }
    if report.head_validation is not None:
        data["head_validation"] = report.head_validation
    if report.structured_parse_status:
        data["structured_parse_status"] = report.structured_parse_status
    if report.finding_counts is not None:
        data["finding_counts"] = report.finding_counts
    if report.finding_results is not None:
        data["finding_results"] = report.finding_results
    if report.failure_stage:
        data["failure_stage"] = report.failure_stage
    return data


def make_webhook_handler(config: Config, enqueue: Callable[[WebhookReviewEvent], object | None]):
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


def _header_value(headers: dict[str, str], header_name: str) -> str:
    normalized = {key.lower(): value for key, value in headers.items()}
    return str(normalized.get(header_name.lower()) or "").strip()


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


def _build_failure_review_report(
        event: WebhookReviewEvent,
        error: Exception,
) -> ReviewReport:
    review_plan = error.review_plan if isinstance(error, ReviewStageError) else None
    failure_stage = error.stage if isinstance(error, ReviewStageError) else ""
    agent_call_count = error.agent_call_count if isinstance(error, ReviewStageError) else 0
    report_context = error.report_context if isinstance(error, ReviewStageError) else {}
    routing = resolve_review_routing(event.target.title)
    return ReviewReport(
        markdown="",
        summary=None,
        review_plan=review_plan,
        head_sha=event.target.head_sha,
        changed_files=[],
        submission_owner="python",
        submission_status="failed",
        failure_stage=failure_stage,
        title=event.target.title,
        requested_review_mode=str(report_context.get("requested_review_mode") or routing.review_mode),
        review_mode=str(report_context.get("review_mode") or routing.review_mode),
        review_scope=str(report_context.get("review_scope") or "single"),
        routing_reason=routing.routing_reason,
        routing_marker=routing.routing_marker,
        dependency_context_status=str(
            report_context.get("dependency_context_status") or "not_applicable"
        ),
        dependency_degradation_reason=str(
            report_context.get("dependency_degradation_reason") or ""
        ),
        dependency_failed_project=str(report_context.get("dependency_failed_project") or ""),
        dependency_context_id=str(report_context.get("dependency_context_id") or ""),
        dependency_repositories=list(report_context.get("dependency_repositories") or []),
        dependency_preparation_seconds=report_context.get("dependency_preparation_seconds"),
        agent_call_count=agent_call_count,
    )


def _redact(text: str, config: Config) -> str:
    if config.gitlab_token:
        basic_token = base64.b64encode(f"oauth2:{config.gitlab_token}".encode("utf-8")).decode("ascii")
        return text.replace(config.gitlab_token, "<redacted>").replace(basic_token, "<redacted>")
    return text
