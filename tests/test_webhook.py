import base64
import json
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mr_reviewer.config import Config
from mr_reviewer.reviewer import ReviewReport
from mr_reviewer.webhook import (
    WebhookReviewEvent,
    handle_webhook_request,
    make_webhook_handler,
    parse_gitlab_merge_request_event,
    write_webhook_monitor_report,
)


def _merge_request_payload(action: str = "update", update_reason: str = "source update") -> dict:
    return {
        "object_kind": "merge_request",
        "project": {
            "path_with_namespace": "team/project",
            "web_url": "https://gitlab.example.com/team/project",
            "http_url": "https://gitlab.example.com/team/project.git",
        },
        "object_attributes": {
            "iid": 7,
            "url": "https://gitlab.example.com/team/project/merge_requests/7",
            "source_branch": "feature/auth",
            "target_branch": "main",
            "action": action,
            "update_reason": update_reason,
            "oldrev": "old-sha",
            "last_commit": {"id": "head-sha"},
            "source": {"http_url": "https://gitlab.example.com/team/project.git"},
            "target": {"http_url": "https://gitlab.example.com/team/project.git"},
        },
    }


def test_parse_gitlab_webhook_builds_review_target_from_payload():
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        allowed_repos={"team/project"},
    )

    event = parse_gitlab_merge_request_event(_merge_request_payload(), config)

    assert event is not None
    assert event.target.project_path == "team/project"
    assert event.target.mr_iid == 7
    assert event.target.mr_url == "https://gitlab.example.com/team/project/merge_requests/7"
    assert event.target.source_branch == "feature/auth"
    assert event.target.target_branch == "main"
    assert event.target.head_sha == "head-sha"
    assert event.target.base_sha is None
    assert event.oldrev == "old-sha"


def test_parse_gitlab_webhook_filters_non_code_update_events():
    config = Config(gitlab_base_url="https://gitlab.example.com")

    assert parse_gitlab_merge_request_event(
        _merge_request_payload(action="update", update_reason="mr update"),
        config,
    ) is None
    assert parse_gitlab_merge_request_event(
        _merge_request_payload(action="close", update_reason="source update"),
        config,
    ) is None
    assert parse_gitlab_merge_request_event(
        {"object_kind": "push"},
        config,
    ) is None
    assert parse_gitlab_merge_request_event(
        _merge_request_payload(action="open", update_reason=""),
        config,
    ) is not None


def test_webhook_secret_is_optional_but_checked_when_configured(caplog):
    payload = json.dumps(_merge_request_payload()).encode("utf-8")
    accepted = []

    open_config = Config(
        gitlab_base_url="https://gitlab.example.com",
        comment_skill="gitlab-mr-comment",
    )
    with caplog.at_level(logging.WARNING, logger="mr_reviewer"):
        response = handle_webhook_request(
            "POST",
            "/webhook/gitlab",
            {},
            payload,
            open_config,
            accepted.append,
        )
    assert response.status == 202
    assert len(accepted) == 1
    assert "webhook_secret_not_configured" in caplog.text

    secure_config = Config(
        gitlab_base_url="https://gitlab.example.com",
        comment_skill="gitlab-mr-comment",
        webhook_secret="expected",
    )
    assert handle_webhook_request(
        "POST",
        "/webhook/gitlab",
        {},
        payload,
        secure_config,
        accepted.append,
    ).status == 401
    assert handle_webhook_request(
        "POST",
        "/webhook/gitlab",
        {"X-Gitlab-Token": "wrong"},
        payload,
        secure_config,
        accepted.append,
    ).status == 403


def test_webhook_requires_comment_skill_for_accepted_events():
    payload = json.dumps(_merge_request_payload()).encode("utf-8")
    config = Config(gitlab_base_url="https://gitlab.example.com")

    response = handle_webhook_request(
        "POST",
        "/webhook/gitlab",
        {},
        payload,
        config,
        lambda event: None,
    )

    assert response.status == 500
    assert response.body["error"]["code"] == "COMMENT_SKILL_REQUIRED"


def test_webhook_http_handler_accepts_valid_post():
    accepted = []
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        comment_skill="gitlab-mr-comment",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_webhook_handler(config, accepted.append))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/webhook/gitlab",
            data=json.dumps(_merge_request_payload()).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))

        assert response.status == 202
        assert body["status"] == "accepted"
        assert accepted[0].target.project_path == "team/project"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_write_webhook_monitor_report_redacts_sensitive_values(tmp_path: Path):
    event = WebhookReviewEvent(
        event_id="team/project!7:head-sha",
        action="update",
        update_reason="source update",
        oldrev="old-sha",
        manual_build=False,
        target=parse_gitlab_merge_request_event(
            _merge_request_payload(),
            Config(gitlab_base_url="https://gitlab.example.com"),
        ).target,
    )
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
    )
    report = ReviewReport(
        markdown="# Review\n\nLooks good.",
        base_sha="base-sha",
        head_sha="head-sha",
        changed_files=["app.py"],
        opencode_returncode=0,
        submission_owner="skill",
        submission_status="unknown",
    )

    basic_token = base64.b64encode(b"oauth2:secret-token").decode("ascii")
    report_path = write_webhook_monitor_report(
        event,
        report,
        config,
        "task-1",
        "failed",
        f"plain secret-token basic {basic_token}",
    )

    text = report_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "secret-token" not in text
    assert basic_token not in text
    assert data["task_id"] == "task-1"
    assert data["error"] == "plain <redacted> basic <redacted>"
    assert data["repo"] == "team/project"
    assert data["submission_owner"] == "skill"
    assert data["submission_status"] == "unknown"
    assert data["markdown_preview"] == "# Review\n\nLooks good."
