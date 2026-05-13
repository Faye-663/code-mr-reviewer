"""
CodeHub webhook HTTP server — receives Merge Request Hook events and
triggers the existing review pipeline.

Usage: mr-reviewer webhook [--host HOST] [--port PORT]
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, parse_gitlab_mr_url
from mr_reviewer.reviewer import ReviewReport, ReviewService

LOG = logging.getLogger("mr_reviewer")

# ---------------------------------------------------------------------------
# Webhook payload parsing
# ---------------------------------------------------------------------------


def parse_webhook_payload(body: dict, config: Config) -> GitLabMrUrl | None:
    """Extract a GitLabMrUrl from a CodeHub merge-request webhook body.

    Returns None when the event should not trigger a review (non-MR event,
    ignored action, conflict, or repo not in allowed list).
    """
    if body.get("object_kind") != "merge_request":
        LOG.info("webhook object_kind=%s ignored", body.get("object_kind"))
        return None

    attrs = body.get("object_attributes", {})
    action = attrs.get("action")

    # open/reopen → review; update + source update → re-review (new commits)
    if action in ("open", "reopen"):
        pass
    elif action == "update" and attrs.get("update_reason") == "source update":
        pass
    else:
        LOG.info(
            "webhook mr action=%s update_reason=%s ignored",
            action,
            attrs.get("update_reason"),
        )
        return None

    if attrs.get("conflict"):
        LOG.info("webhook mr has conflicts, skipped")
        return None

    mr_url = attrs.get("url")
    if not mr_url:
        LOG.warning("webhook payload missing object_attributes.url")
        return None

    try:
        mr = parse_gitlab_mr_url(mr_url, config.gitlab_base_url)
    except ValueError as exc:
        LOG.warning("webhook failed to parse MR URL: %s", exc)
        return None

    if config.allowed_repos and mr.project_path not in config.allowed_repos:
        LOG.info("webhook project=%s not in allowed_repos, ignored", mr.project_path)
        return None

    return mr


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each connection in a new daemon thread."""

    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    config: Config,
    service: ReviewService,
    gitlab: GitLabClient,
) -> type[BaseHTTPRequestHandler]:
    """Factory that closes over dependencies into a handler class."""

    class _WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._respond(400, b"empty body")
                return
            body_bytes = self.rfile.read(content_length)

            if config.webhook_secret:
                received = self.headers.get(config.webhook_secret_header, "")
                if received != config.webhook_secret:
                    LOG.warning("webhook invalid token")
                    self._respond(403, b"invalid token")
                    return

            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError as exc:
                LOG.warning("webhook invalid JSON: %s", exc)
                self._respond(400, b"invalid JSON")
                return

            mr = parse_webhook_payload(payload, config)
            if mr is None:
                self._respond(200, b"ignored")
                return

            thread = threading.Thread(
                target=self._process_mr,
                args=(mr,),
                daemon=True,
            )
            thread.start()
            self._respond(200, b"accepted")

        def _process_mr(self, mr: GitLabMrUrl) -> None:
            task_id = (
                f"webhook-{mr.project_path.replace('/', '-')}"
                f"-mr-{mr.mr_iid}-{uuid.uuid4().hex[:8]}"
            )
            try:
                LOG.info(
                    "webhook task=%s mr=%s/%s status=started",
                    task_id,
                    mr.project_path,
                    mr.mr_iid,
                )
                report: ReviewReport = service.review(mr, config, task_id)
                LOG.info(
                    "webhook task=%s stage=report_ready report_chars=%s",
                    task_id,
                    len(report.markdown),
                )

                if config.webhook_post_comment:
                    gitlab.post_mr_note(mr, report.markdown)
                    LOG.info("webhook task=%s stage=comment_posted", task_id)

                if config.im_reply_command and config.welink_group_id:
                    from mr_reviewer.cli import _reply  # noqa: PLC0415

                    _reply(config, report.markdown, mr)
                    LOG.info("webhook task=%s stage=welink_notified", task_id)

                LOG.info(
                    "webhook task=%s mr=%s/%s status=success",
                    task_id,
                    mr.project_path,
                    mr.mr_iid,
                )
            except Exception as exc:  # noqa: BLE001
                LOG.error(
                    "webhook task=%s mr=%s/%s status=failed error=%s",
                    task_id,
                    mr.project_path,
                    mr.mr_iid,
                    exc,
                )

        def _respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            LOG.info("webhook access: %s", fmt % args)

    return _WebhookHandler


def run_webhook_server(config: Config, service: ReviewService) -> int:
    """Start the webhook HTTP server and block until KeyboardInterrupt."""

    gitlab = GitLabClient(
        config.gitlab_base_url,
        config.gitlab_token,
        config.test_gitlab_responses,
    )

    handler = make_handler(config, service, gitlab)
    server = ThreadedHTTPServer((config.webhook_host, config.webhook_port), handler)

    LOG.info("webhook server listening on %s:%s", config.webhook_host, config.webhook_port)
    print(f"Webhook server listening on http://{config.webhook_host}:{config.webhook_port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("webhook server shutting down")
        print("\nShutting down webhook server...", flush=True)
        server.shutdown()

    return 0
