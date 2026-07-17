import base64
import json
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mr_reviewer.config import Config
from mr_reviewer.reviewer import ReviewReport, ReviewStageError
from mr_reviewer.webhook import (
    WebhookReviewEvent,
    WebhookReviewQueue,
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
            "title": "Fix auth",
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


def _review_plan() -> dict[str, object]:
    return {
        "change_intent": ["修复认证流程"],
        "critical_paths": [{"path": "auth", "reason": "刷新token", "verify": ["并发刷新"]}],
        "external_contracts": [],
        "state_invariants": [],
        "transaction_async_boundaries": [],
        "test_risks": ["新增测试"],
        "open_questions": [],
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


def test_parse_gitlab_webhook_accepts_reopen_events():
    config = Config(gitlab_base_url="https://gitlab.example.com")

    event = parse_gitlab_merge_request_event(
        _merge_request_payload(action="reopen", update_reason=""),
        config,
    )

    assert event is not None
    assert event.action == "reopen"
    assert event.target.mr_iid == 7


def test_parse_gitlab_webhook_skips_conflicted_merge_requests():
    payload = _merge_request_payload(action="open", update_reason="")
    payload["object_attributes"]["conflict"] = True

    event = parse_gitlab_merge_request_event(
        payload,
        Config(gitlab_base_url="https://gitlab.example.com"),
    )

    assert event is None


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


def test_webhook_accepts_events_without_comment_skill():
    payload = json.dumps(_merge_request_payload()).encode("utf-8")
    accepted = []
    config = Config(gitlab_base_url="https://gitlab.example.com")

    response = handle_webhook_request(
        "POST",
        "/webhook/gitlab",
        {},
        payload,
        config,
        accepted.append,
    )

    assert response.status == 202
    assert accepted[0].target.project_path == "team/project"


def test_webhook_secret_uses_configured_header():
    payload = json.dumps(_merge_request_payload()).encode("utf-8")
    accepted = []
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        webhook_secret="expected",
        webhook_secret_header="X-CodeHub-Token",
    )

    assert handle_webhook_request(
        "POST",
        "/webhook/gitlab",
        {"X-Gitlab-Token": "expected"},
        payload,
        config,
        accepted.append,
    ).status == 401

    response = handle_webhook_request(
        "POST",
        "/webhook/gitlab",
        {"X-CodeHub-Token": "expected"},
        payload,
        config,
        accepted.append,
    )

    assert response.status == 202
    assert len(accepted) == 1


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
        review_plan=_review_plan(),
        base_sha="base-sha",
        head_sha="head-sha",
        changed_files=["app.py"],
        opencode_returncode=0,
        submission_owner="skill",
        submission_status="unknown",
        prompt_templates={
            "review_plan": {"id": "review-plan", "version": "abc123def456"},
            "review": {"id": "review", "version": "789abc456def"},
        },
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
    assert data["summary"] is None
    assert data["review_plan"]["change_intent"] == ["修复认证流程"]
    assert data["prompt_templates"]["review"]["version"] == "789abc456def"
    markdown = Path(data["markdown_report_path"]).read_text(encoding="utf-8")
    assert "## Discoveries" in markdown
    assert "修复认证流程" in markdown


def test_webhook_worker_posts_inline_discussion_from_python(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    service = _RecordingReviewService()
    gitlab = _RecordingGitLabClient()
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
        webhook_post_comment=True,
        agent_model_name="GLM5",
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert service.targets == [event.target]
    assert service.structured_output_flags == [True]
    assert gitlab.comments == []
    assert len(gitlab.discussions) == 1
    posted = gitlab.discussions[0]
    assert posted["target"] == event.target
    assert posted["severity"] == "major"
    assert posted["position"]["old_line"] == -1
    assert posted["position"]["new_line"] == 2
    assert "【🤖AI Review-GLM5】[major]批量查询缺少数量限制" in posted["body"]
    assert "- **影响**: 大请求可能导致数据库资源耗尽。" in posted["body"]
    assert "<!-- ai-cr:finding:team/project:7:head-sha:SQL_PERFORMANCE:src/example.py:src/example.py:-1:2 -->" in posted["body"]
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["submission_owner"] == "python"
    assert report["submission_status"] == "posted"
    assert report["structured_parse_status"] == "success"
    assert report["finding_counts"]["posted"] == 1
    markdown_report_path = Path(report["markdown_report_path"])
    assert markdown_report_path.exists()
    markdown_report = markdown_report_path.read_text(encoding="utf-8")
    assert "# 代码检视报告" in markdown_report
    assert "team/project!7" in markdown_report
    assert "已提交MR评论" in markdown_report
    assert report["summary"] is None
    assert report["review_plan"]["change_intent"] == ["修复认证流程"]
    assert "## Discoveries" in markdown_report
    assert "修复认证流程" not in posted["body"]


def test_webhook_worker_uses_custom_publication_policy(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    payload = json.loads(_RecordingReviewService().markdown)
    payload["findings"][0]["severity"] = "suggestion"
    payload["findings"][0]["confidence"] = "MEDIUM"
    service = _RecordingReviewService(json.dumps(payload, ensure_ascii=False))
    gitlab = _RecordingGitLabClient()
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
        webhook_post_comment=True,
        agent_model_name="GLM5",
        publish_min_severity="suggestion",
        publish_min_confidence="MEDIUM",
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert len(gitlab.discussions) == 1
    assert gitlab.discussions[0]["severity"] == "suggestion"


def test_webhook_worker_uses_default_minjor_high_policy(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    payload = json.loads(_RecordingReviewService().markdown)
    payload["findings"][0]["severity"] = "minjor"
    service = _RecordingReviewService(json.dumps(payload, ensure_ascii=False))
    gitlab = _RecordingGitLabClient()
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
        webhook_post_comment=True,
        agent_model_name="GLM5",
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert len(gitlab.discussions) == 1
    assert gitlab.discussions[0]["severity"] == "minjor"


def test_webhook_worker_keeps_non_diff_finding_local(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    payload = json.loads(_RecordingReviewService().markdown)
    payload["findings"][0]["new_line"] = 99
    service = _RecordingReviewService(json.dumps(payload, ensure_ascii=False))
    gitlab = _RecordingGitLabClient()
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
        webhook_post_comment=True,
        agent_model_name="GLM5",
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert gitlab.comments == []
    assert gitlab.discussions == []
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["finding_results"][0]["status"] == "invalid"
    assert report["finding_results"][0]["reason"] == "line_not_in_diff"


def test_webhook_worker_can_skip_python_comment(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    service = _RecordingReviewService()
    gitlab = _RecordingGitLabClient()
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        report_dir=tmp_path,
        webhook_post_comment=False,
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert gitlab.comments == []
    assert gitlab.discussions == []
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["submission_owner"] == "python"
    assert report["submission_status"] == "disabled"


def test_webhook_worker_keeps_findings_local_when_model_name_is_missing(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(), Config(gitlab_base_url="https://gitlab.example.com")
    )
    assert event is not None
    assert event.target.title == "Fix auth"
    gitlab = _RecordingGitLabClient()
    queue = WebhookReviewQueue(
        _RecordingReviewService(),
        gitlab,
        Config(
            gitlab_base_url="https://gitlab.example.com",
            gitlab_token="secret-token",
            report_dir=tmp_path,
            webhook_post_comment=True,
        ),
    )
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert gitlab.discussions == []
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["submission_status"] == "model_not_configured"
    assert report["finding_results"][0]["status"] == "model_not_configured"


def test_webhook_worker_skips_duplicate_inline_discussion(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    service = _RecordingReviewService()
    gitlab = _RecordingGitLabClient(
        existing_marker="<!-- ai-cr:finding:team/project:7:head-sha:SQL_PERFORMANCE:src/example.py:src/example.py:-1:2 -->"
    )
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
        webhook_post_comment=True,
        agent_model_name="GLM5",
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert gitlab.discussions == []
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["submission_status"] == "posted"
    assert report["finding_counts"]["skipped_duplicate"] == 1


def test_webhook_worker_does_not_publish_when_structured_output_is_invalid(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None
    service = _RecordingReviewService(markdown="not json")
    gitlab = _RecordingGitLabClient()
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        report_dir=tmp_path,
        webhook_post_comment=True,
    )
    queue = WebhookReviewQueue(service, gitlab, config)
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    assert gitlab.comments == []
    assert gitlab.discussions == []
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["submission_status"] == "parse_failed"
    assert report["structured_parse_status"] == "failed"
    markdown_report_path = Path(report["markdown_report_path"])
    assert markdown_report_path.exists()
    markdown_report = markdown_report_path.read_text(encoding="utf-8")
    assert "## 检视摘要" in markdown_report


def test_webhook_worker_records_review_stage_failure_with_completed_plan(tmp_path: Path):
    event = parse_gitlab_merge_request_event(
        _merge_request_payload(),
        Config(gitlab_base_url="https://gitlab.example.com"),
    )
    assert event is not None

    class FailingReviewService:
        def review_target(self, target, config, task_id, structured_output=False):
            raise ReviewStageError(
                "review",
                RuntimeError("agent unavailable"),
                _review_plan(),
            )

    queue = WebhookReviewQueue(
        FailingReviewService(),
        _RecordingGitLabClient(),
        Config(gitlab_base_url="https://gitlab.example.com", report_dir=tmp_path),
    )
    queue.start()

    queue.enqueue(event)
    queue._queue.join()

    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["failure_stage"] == "review"
    assert report["summary"] is None
    assert report["review_plan"]["change_intent"] == ["修复认证流程"]
    markdown = Path(report["markdown_report_path"]).read_text(encoding="utf-8")
    assert "修复认证流程" in markdown
    assert "失败阶段：review" in markdown


class _RecordingReviewService:
    def __init__(self, markdown: str | None = None):
        self.targets = []
        self.structured_output_flags = []
        self.markdown = markdown or json.dumps(
            {
                "findings": [
                    {
                        "rule_id": "SQL_PERFORMANCE",
                        "severity": "major",
                        "confidence": "HIGH",
                        "old_path": "src/example.py",
                        "new_path": "src/example.py",
                        "old_line": -1,
                        "new_line": 2,
                        "title": "批量查询缺少数量限制",
                        "evidence": "本次变更新增 IN 查询，但未限制集合大小。",
                        "impact": "大请求可能导致数据库资源耗尽。",
                        "suggestion": "限制集合大小或拆批查询。",
                    }
                ],
                "notes": [],
                "test_gaps": [],
            },
            ensure_ascii=False,
        )

    def review_target(self, target, config, task_id, structured_output=False):
        self.targets.append(target)
        self.structured_output_flags.append(structured_output)
        return ReviewReport(
            markdown=self.markdown,
            review_plan=_review_plan(),
            base_sha="base-sha",
            head_sha=target.head_sha,
            changed_files=["app.py"],
            diff=(
                "diff --git a/src/example.py b/src/example.py\n"
                "--- a/src/example.py\n"
                "+++ b/src/example.py\n"
                "@@ -1,1 +1,2 @@\n"
                " old\n"
                "+added\n"
            ),
            opencode_returncode=0,
            submission_owner="none",
            submission_status="unknown",
        )


class _RecordingGitLabClient:
    def __init__(self, existing_marker: str = ""):
        self.comments = []
        self.discussions = []
        self.existing_marker = existing_marker

    def post_mr_note(self, target, body):
        self.comments.append((target, body))
        return {"id": 1}

    def get_mr_detail_for_discussion_position(self, target):
        return {
            "diff_refs": {
                "base_sha": "base-sha",
                "start_sha": "start-sha",
                "head_sha": "head-sha",
            }
        }

    def list_mr_discussions(self, target):
        if not self.existing_marker:
            return []
        return [{"notes": [{"body": f"existing\n{self.existing_marker}"}]}]

    def post_mr_discussion(self, target, body, severity, position):
        self.discussions.append(
            {
                "target": target,
                "body": body,
                "severity": severity,
                "position": position,
            }
        )
        return {"id": "discussion-1", "notes": [{"id": 123}]}
