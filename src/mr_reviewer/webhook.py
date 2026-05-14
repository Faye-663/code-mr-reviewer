from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from mr_reviewer.config import Config
from mr_reviewer.reviewer import MergeRequestReviewTarget, ReviewReport, ReviewService

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

    token_status = _check_webhook_token(headers, config.webhook_secret)
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
    if not config.comment_skill:
        return _json_response(500, "COMMENT_SKILL_REQUIRED", "MR_REVIEWER_COMMENT_SKILL is required for webhook mode")

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
    def __init__(self, service: ReviewService, config: Config):
        self.service = service
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
                LOG.info(
                    "task=%s stage=webhook_review repo=%s mr_iid=%s status=started",
                    task_id,
                    event.target.project_path,
                    event.target.mr_iid,
                )
                report = self.service.review_target(event.target, self.config, task_id)
                path = write_webhook_monitor_report(event, report, self.config, task_id, "success")
                LOG.info("task=%s stage=webhook_report path=%s status=success", task_id, path)
            except Exception as exc:  # noqa: BLE001 - webhook 后台任务必须记录失败并继续处理队列。
                LOG.error("task=%s stage=webhook_review status=failed error=%s", task_id, _redact(str(exc), self.config))
                failure_report = ReviewReport(
                    markdown="",
                    head_sha=event.target.head_sha,
                    changed_files=[],
                    submission_owner="skill",
                    submission_status="failed",
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


def run_webhook_server(config: Config, service: ReviewService) -> int:
    if not config.comment_skill:
        raise ValueError("MR_REVIEWER_COMMENT_SKILL is required for webhook mode")

    worker = WebhookReviewQueue(service, config)
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
    }
    if error:
        data["error"] = _redact(error, config)
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


def _check_webhook_token(headers: dict[str, str], expected: str) -> WebhookResponse | None:
    if not expected:
        return None
    normalized = {key.lower(): value for key, value in headers.items()}
    actual = normalized.get("x-gitlab-token")
    if actual is None:
        return _json_response(401, "WEBHOOK_TOKEN_MISSING", "X-Gitlab-Token header is required")
    if actual != expected:
        return _json_response(403, "WEBHOOK_TOKEN_INVALID", "X-Gitlab-Token header is invalid")
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


def _redact(text: str, config: Config) -> str:
    if config.gitlab_token:
        basic_token = base64.b64encode(f"oauth2:{config.gitlab_token}".encode("utf-8")).decode("ascii")
        return text.replace(config.gitlab_token, "<redacted>").replace(basic_token, "<redacted>")
    return text
