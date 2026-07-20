from __future__ import annotations

import json
from pathlib import Path

from mr_reviewer.cli import healthcheck, poll
from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl
from mr_reviewer.im import ImMessage
from mr_reviewer.publication_policy import FindingPublicationPolicy
from mr_reviewer.review_set import (
    PreparedReviewSetMember,
    ReviewSetManifest,
    ReviewSetMember,
)
from mr_reviewer.review_set_publish import ReviewSetPublisher
from mr_reviewer.review_set_report import render_review_set_report
from mr_reviewer.review_set_result import parse_structured_review_set_result
from mr_reviewer.reviewer import ReviewSetReviewReport
from mr_reviewer.welink import reply_review_set


def _members() -> tuple[ReviewSetMember, ReviewSetMember]:
    return (
        ReviewSetMember(
            member_id="p101-mr7",
            project_id=101,
            project_path="team/app",
            mr_iid=7,
            mr_url="https://gitlab.example.com/team/app/merge_requests/7",
            target_repo_url="https://gitlab.example.com/team/app.git",
            source_repo_url="https://gitlab.example.com/team/app.git",
            target_branch="main",
            source_branch="feature-app",
            base_sha="base-101",
            start_sha="start-101",
            head_sha="head-101",
            repo_path="members/p101-mr7/repo",
        ),
        ReviewSetMember(
            member_id="p202-mr8",
            project_id=202,
            project_path="team/sdk",
            mr_iid=8,
            mr_url="https://gitlab.example.com/team/sdk/merge_requests/8",
            target_repo_url="https://gitlab.example.com/team/sdk.git",
            source_repo_url="https://gitlab.example.com/team/sdk.git",
            target_branch="main",
            source_branch="feature-sdk",
            base_sha="base-202",
            start_sha="start-202",
            head_sha="head-202",
            repo_path="members/p202-mr8/repo",
        ),
    )


def _result_payload() -> dict:
    return {
        "schema_version": "review-set-review/v1",
        "findings": [
            {
                "issue_id": "CONTRACT_NULLABILITY_001",
                "rule_id": "CONTRACT_NULLABILITY",
                "severity": "major",
                "confidence": "HIGH",
                "title": "调用方未处理 SDK 空返回",
                "impact": "生产请求可能触发空指针异常。",
                "evidence_refs": [
                    {
                        "member_id": "p202-mr8",
                        "path": "src/sdk.py",
                        "start_line": 40,
                        "end_line": 42,
                        "detail": "SDK 可以返回 null。",
                    }
                ],
                "targets": [
                    {
                        "member_id": "p101-mr7",
                        "position": {
                            "old_path": "src/caller.py",
                            "new_path": "src/caller.py",
                            "old_line": -1,
                            "new_line": 57,
                        },
                        "suggestion": "解引用前处理 null。",
                    },
                    {
                        "member_id": "p202-mr8",
                        "position": None,
                        "suggestion": "在 SDK 契约中明确空值语义。",
                    },
                ],
            }
        ],
        "relationship_summary": ["app 调用 sdk，空值契约不一致。"],
        "notes": [],
        "test_gaps": ["缺少联合契约测试。"],
        "good": [],
    }


def _report(payload: dict | None = None) -> ReviewSetReviewReport:
    app, sdk = _members()
    diff = (
        "diff --git a/src/caller.py b/src/caller.py\n"
        "--- a/src/caller.py\n"
        "+++ b/src/caller.py\n"
        "@@ -56,0 +57,1 @@\n"
        "+value = sdk.call()\n"
    )
    prepared = (
        PreparedReviewSetMember(app, Path("members/p101-mr7/repo"), diff, ("src/caller.py",)),
        PreparedReviewSetMember(sdk, Path("members/p202-mr8/repo"), "", ("src/sdk.py",)),
    )
    manifest = ReviewSetManifest(
        schema_version="review-set/v1",
        review_set_id="a" * 64,
        req_id="REQ-1",
        members=(app, sdk),
        resource_limits={"max_files": 50, "max_diff_lines": 2000},
    )
    return ReviewSetReviewReport(
        manifest=manifest,
        review_plan={
            "schema_version": "review-set-plan/v1",
            "member_focus": [],
            "relationships": [],
            "open_questions": [],
        },
        result=parse_structured_review_set_result(json.dumps(payload or _result_payload(), ensure_ascii=False)),
        members=prepared,
        prompt_templates={},
        agent_call_count=2,
    )


class _PublishingGitLab:
    def __init__(self):
        self.discussions: dict[str, list[dict]] = {"team/app": [], "team/sdk": []}
        self.inline_posts: list[dict] = []
        self.note_posts: list[dict] = []

    def list_mr_discussions(self, target) -> list[dict]:
        return self.discussions[target.project_path]

    def post_mr_discussion(self, target, body, severity, position) -> dict:
        self.inline_posts.append(
            {"project": target.project_path, "body": body, "severity": severity, "position": position}
        )
        self.discussions[target.project_path].append({"individual_note": False, "notes": [{"body": body}]})
        return {"id": "discussion-1", "notes": [{"id": 11}]}

    def post_mr_note(self, mr, body) -> dict:
        self.note_posts.append({"project": mr.project_path, "body": body})
        self.discussions[mr.project_path].append({"individual_note": True, "notes": [{"body": body}]})
        return {"id": 22}


def test_review_set_publisher_posts_inline_and_note_targets():
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(), enabled=True, model_name="GLM5")

    assert publication.status == "success"
    assert gitlab.inline_posts[0]["project"] == "team/app"
    assert gitlab.inline_posts[0]["position"]["new_line"] == 57
    assert gitlab.note_posts[0]["project"] == "team/sdk"
    assert publication.counts["posted_inline"] == 1
    assert publication.counts["posted_note"] == 1
    assert all("<!-- ai-cr:review-set:" in item["marker"] for item in publication.results)


def test_review_set_publisher_uses_default_minor_high_policy():
    payload = _result_payload()
    payload["findings"][0]["severity"] = "minor"
    payload["findings"][0]["targets"] = payload["findings"][0]["targets"][:1]
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(
        _report(payload), enabled=True, model_name="GLM5"
    )

    assert publication.results[0]["status"] == "posted_inline"
    assert gitlab.inline_posts[0]["severity"] == "minor"


def test_review_set_publisher_uses_custom_publication_policy():
    payload = _result_payload()
    payload["findings"][0]["severity"] = "suggestion"
    payload["findings"][0]["confidence"] = "MEDIUM"
    payload["findings"][0]["targets"] = payload["findings"][0]["targets"][:1]
    gitlab = _PublishingGitLab()
    policy = FindingPublicationPolicy("suggestion", "MEDIUM")

    publication = ReviewSetPublisher(gitlab, policy).publish(
        _report(payload), enabled=True, model_name="GLM5"
    )

    assert publication.results[0]["status"] == "posted_inline"
    assert gitlab.inline_posts[0]["severity"] == "suggestion"


def test_review_set_publisher_skips_duplicate_markers_on_repeat():
    gitlab = _PublishingGitLab()
    publisher = ReviewSetPublisher(gitlab)

    publisher.publish(_report(), enabled=True, model_name="GLM5")
    second = publisher.publish(_report(), enabled=True, model_name="GLM5")

    assert second.counts["skipped_duplicate"] == 2
    assert len(gitlab.inline_posts) == 1
    assert len(gitlab.note_posts) == 1


def test_review_set_marker_is_stable_when_agent_text_and_target_order_change():
    first_payload = _result_payload()
    second_payload = _result_payload()
    second_payload["findings"][0]["title"] = "同一问题的不同标题"
    second_payload["findings"][0]["impact"] = "同一影响的不同表述"
    second_payload["findings"][0]["evidence_refs"][0]["detail"] = "不同证据表述"
    second_payload["findings"][0]["targets"][0]["suggestion"] = "不同修复表述"
    second_payload["findings"][0]["targets"].reverse()
    publisher = ReviewSetPublisher(_PublishingGitLab())

    first = publisher.publish(_report(first_payload), enabled=False, model_name="")
    second = publisher.publish(_report(second_payload), enabled=False, model_name="")

    first_markers = {item["member_id"]: item["marker"] for item in first.results}
    second_markers = {item["member_id"]: item["marker"] for item in second.results}
    assert first_markers == second_markers


def test_review_set_publisher_falls_back_to_note_when_valid_position_is_not_in_diff():
    payload = _result_payload()
    payload["findings"][0]["targets"] = [
        {
            "member_id": "p101-mr7",
            "position": {
                "old_path": "src/caller.py",
                "new_path": "src/caller.py",
                "old_line": -1,
                "new_line": 999,
            },
            "suggestion": "修复调用方。",
        }
    ]
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(payload), enabled=True, model_name="GLM5")

    assert gitlab.inline_posts == []
    assert gitlab.note_posts[0]["project"] == "team/app"
    assert publication.results[0]["reason"] == "position_not_in_diff"


def test_review_set_publisher_does_not_fallback_for_inconsistent_line_sides():
    payload = _result_payload()
    payload["findings"][0]["targets"] = [
        {
            "member_id": "p101-mr7",
            "position": {
                "old_path": "src/caller.py",
                "new_path": "src/caller.py",
                "old_line": 56,
                "new_line": 57,
            },
            "suggestion": "修复调用方。",
        }
    ]
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(payload), enabled=True, model_name="GLM5")

    assert publication.results[0]["status"] == "invalid"
    assert publication.results[0]["reason"] == "inconsistent_line_sides"
    assert gitlab.inline_posts == []
    assert gitlab.note_posts == []


def test_review_set_publisher_rejects_unknown_target_without_blocking_valid_target():
    payload = _result_payload()
    payload["findings"][0]["targets"][0]["member_id"] = "unknown"
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(payload), enabled=True, model_name="GLM5")

    assert publication.status == "success_with_warnings"
    assert publication.results[0]["status"] == "invalid"
    assert publication.results[0]["reason"] == "unknown_target_member"
    assert gitlab.note_posts[0]["project"] == "team/sdk"


def test_review_set_publisher_does_not_fallback_for_invalid_line():
    payload = _result_payload()
    payload["findings"][0]["targets"] = [
        {
            "member_id": "p101-mr7",
            "position": {
                "old_path": "src/caller.py",
                "new_path": "src/caller.py",
                "old_line": -1,
                "new_line": 0,
            },
            "suggestion": "修复调用方。",
        }
    ]
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(payload), enabled=True, model_name="GLM5")

    assert publication.results[0]["status"] == "invalid"
    assert publication.results[0]["reason"] == "invalid_target_line"
    assert gitlab.inline_posts == []
    assert gitlab.note_posts == []


def test_review_set_publisher_does_not_fallback_for_windows_absolute_path():
    payload = _result_payload()
    payload["findings"][0]["targets"] = [
        {
            "member_id": "p101-mr7",
            "position": {
                "old_path": "C:/repo/src/caller.py",
                "new_path": "C:/repo/src/caller.py",
                "old_line": -1,
                "new_line": 57,
            },
            "suggestion": "修复调用方。",
        }
    ]
    gitlab = _PublishingGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(payload), enabled=True, model_name="GLM5")

    assert publication.results[0]["status"] == "invalid"
    assert publication.results[0]["reason"] == "invalid_target_path"
    assert gitlab.note_posts == []


def test_review_set_publisher_continues_after_one_target_post_fails():
    class FailingNoteGitLab(_PublishingGitLab):
        def post_mr_note(self, mr, body):
            raise RuntimeError("note unavailable")

    gitlab = FailingNoteGitLab()

    publication = ReviewSetPublisher(gitlab).publish(_report(), enabled=True, model_name="GLM5")

    assert publication.status == "success_with_warnings"
    assert publication.counts["posted_inline"] == 1
    assert publication.counts["failed"] == 1


def test_review_set_publisher_honors_disabled_and_missing_model_modes():
    disabled_gitlab = _PublishingGitLab()
    disabled = ReviewSetPublisher(disabled_gitlab).publish(_report(), enabled=False, model_name="")
    missing_model_gitlab = _PublishingGitLab()
    missing_model = ReviewSetPublisher(missing_model_gitlab).publish(
        _report(), enabled=True, model_name=""
    )

    assert disabled.status == "success"
    assert disabled.counts["disabled"] == 2
    assert disabled_gitlab.inline_posts == []
    assert missing_model.status == "success_with_warnings"
    assert missing_model.counts["model_not_configured"] == 2
    assert missing_model_gitlab.note_posts == []


def test_render_review_set_report_includes_members_findings_and_publish_status():
    publication = ReviewSetPublisher(_PublishingGitLab()).publish(
        _report(), enabled=True, model_name="GLM5"
    )

    markdown = render_review_set_report(_report(), publication)

    assert "# 多 MR 联合代码检视报告" in markdown
    assert "REQ-1" in markdown
    assert "team/app!7" in markdown
    assert "team/sdk!8" in markdown
    assert "## 联合审查计划" in markdown
    assert "调用方未处理 SDK 空返回" in markdown
    assert "src/caller.py:-1 -> src/caller.py:57" in markdown
    assert "posted_inline" in markdown
    assert "posted_note" in markdown


def test_gitlab_client_paginates_merge_request_discussions(monkeypatch):
    paths: list[str] = []
    client = GitLabClient("https://gitlab.example.com/api/v4", "token")

    def fake_get(path: str):
        paths.append(path)
        return [{"id": index} for index in range(100)] if "&page=1" in path else [{"id": 100}]

    monkeypatch.setattr(client, "_get_json", fake_get)
    discussions = client.list_mr_discussions(
        GitLabMrUrl("https://gitlab.example.com", "team/app", 7)
    )

    assert len(discussions) == 101
    assert paths == [
        "/projects/team%2Fapp/merge_requests/7/discussions?per_page=100&page=1",
        "/projects/team%2Fapp/merge_requests/7/discussions?per_page=100&page=2",
    ]


def test_config_reads_review_set_comment_switch(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_REVIEW_SET_POST_COMMENT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_REVIEW_SET_POST_COMMENT=false\n",
        encoding="utf-8",
    )

    assert Config.from_env(env_file).review_set_post_comment is False


def _im_message(text: str) -> ImMessage:
    return ImMessage("message-1", "group-1", "alice", text, "2026-07-14T00:00:00Z")


def _poll_config(tmp_path: Path) -> Config:
    return Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="token",
        bot_mention="@ReviewBot",
        allowed_groups={"group-1"},
        allowed_users={"alice"},
        allowed_repos={"team/app", "team/sdk", "team/api", "team/extra"},
        state_path=tmp_path / "state.json",
        work_dir=tmp_path / "work",
    )


def test_poll_replies_and_terminates_rejected_review_set(tmp_path: Path, monkeypatch):
    message = _im_message(
        "@ReviewBot "
        "https://gitlab.example.com/team/app/merge_requests/1 "
        "https://gitlab.example.com/team/sdk/merge_requests/2 "
        "https://gitlab.example.com/team/api/merge_requests/3 "
        "https://gitlab.example.com/team/extra/merge_requests/4"
    )
    sent: list[str] = []
    monkeypatch.setattr("mr_reviewer.cli._poll_messages", lambda config: [message])
    monkeypatch.setattr("mr_reviewer.cli.build_service", lambda config: object())
    monkeypatch.setattr("mr_reviewer.cli._send_text", lambda config, text: sent.append(text))

    assert poll(_poll_config(tmp_path), once=True) == 0

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["processed"]["message-1"]["status"] == "rejected"
    assert "最多只能包含 3 个" in sent[0]


def test_poll_replies_with_safe_message_and_terminates_failed_review_set(tmp_path: Path, monkeypatch):
    message = _im_message(
        "@ReviewBot "
        "https://gitlab.example.com/team/app/merge_requests/1 "
        "https://gitlab.example.com/team/sdk/merge_requests/2"
    )
    sent: list[str] = []

    class FailingService:
        def review_set(self, request, config, task_id):
            raise RuntimeError("secret internal detail")

    monkeypatch.setattr("mr_reviewer.cli._poll_messages", lambda config: [message])
    monkeypatch.setattr("mr_reviewer.cli.build_service", lambda config: FailingService())
    monkeypatch.setattr("mr_reviewer.cli._send_text", lambda config, text: sent.append(text))

    assert poll(_poll_config(tmp_path), once=True) == 0

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["processed"]["message-1"]["status"] == "failed"
    assert "联合检视执行失败" in sent[0]
    assert "secret internal detail" not in sent[0]
    assert "secret internal detail" not in state["processed"]["message-1"].get("error", "")


def test_healthcheck_prints_review_set_publish_switch(capsys, monkeypatch, tmp_path: Path):
    monkeypatch.setattr("shutil.which", lambda command: f"C:/bin/{command}")
    config = _poll_config(tmp_path)
    config.im_poll_command = "poll"
    config.im_reply_command = "reply"
    config.welink_group_id = "group-1"
    config.welink_onebox_space_id = "space"
    config.welink_onebox_parent_id = "parent"

    assert healthcheck(config) == 0
    output = capsys.readouterr().out
    assert "review_set_post_comment: enabled" in output
    assert "publish_min_severity: minor" in output
    assert "publish_min_confidence: HIGH" in output


def test_poll_delivers_successful_review_set_report(tmp_path: Path, monkeypatch):
    message = _im_message(
        "@ReviewBot "
        "https://gitlab.example.com/team/app/merge_requests/7 "
        "https://gitlab.example.com/team/sdk/merge_requests/8"
    )
    gitlab = _PublishingGitLab()
    delivered: list[tuple] = []

    class SuccessfulService:
        def __init__(self):
            self.gitlab = gitlab

        def review_set(self, request, config, task_id):
            return _report()

    config = _poll_config(tmp_path)
    config.agent_model_name = "GLM5"
    monkeypatch.setattr("mr_reviewer.cli._poll_messages", lambda current: [message])
    monkeypatch.setattr("mr_reviewer.cli.build_service", lambda current: SuccessfulService())
    monkeypatch.setattr(
        "mr_reviewer.cli._reply_review_set",
        lambda current, markdown, review_set_id, counts: delivered.append(
            (markdown, review_set_id, counts)
        ),
    )

    assert poll(config, once=True) == 0

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["processed"]["message-1"]["status"] == "success"
    assert "# 多 MR 联合代码检视报告" in delivered[0][0]
    assert delivered[0][2]["posted_inline"] == 1
    assert delivered[0][2]["posted_note"] == 1


def test_welink_review_set_reply_uses_stable_report_prefix(monkeypatch):
    calls: list[tuple] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        im_reply_command="welink-cli im send-to-group",
        welink_group_id="group-1",
        welink_onebox_space_id="space",
        welink_onebox_parent_id="parent",
    )

    reply_review_set(
        config,
        "# report",
        "abcdef1234567890",
        {"posted_inline": 1, "posted_note": 1, "failed": 0, "invalid": 0},
    )

    uploaded_path = Path(calls[0][0][-1])
    assert uploaded_path.name == "review-set-abcdef123456.md"
    assert "已发布 2 条" in calls[1][0][-1]
