from types import SimpleNamespace

from mr_reviewer.gitlab import GitLabMrUrl
from mr_reviewer.im_notifications import (
    render_review_set_accepted,
    render_review_set_rejected,
    render_review_set_terminal,
    render_single_accepted,
    render_single_failed,
    render_single_terminal,
)
from mr_reviewer.review_set import ReviewSetManifest, ReviewSetMember
from mr_reviewer.review_set_publish import ReviewSetPublication
from mr_reviewer.reviewer import ReviewReport, ReviewSetReviewReport
from mr_reviewer.single_mr import SingleMrOutcome


def _mr(project: str = "team/app", iid: int = 7) -> GitLabMrUrl:
    return GitLabMrUrl("https://gitlab.example.com", project, iid)


def _member(project: str, iid: int, head: str) -> ReviewSetMember:
    return ReviewSetMember(
        member_id=f"{project}-{iid}",
        project_id=iid,
        project_path=project,
        mr_iid=iid,
        mr_url=f"https://gitlab.example.com/{project}/merge_requests/{iid}",
        target_repo_url="https://gitlab.example.com/target.git",
        source_repo_url="https://gitlab.example.com/source.git",
        target_branch="main",
        source_branch="feature",
        base_sha="b" * 40,
        start_sha="b" * 40,
        head_sha=head,
        repo_path=f"members/{iid}/repo",
    )


def test_single_notifications_identify_mr_version_disposition_and_deliveries():
    mr = _mr()
    report = ReviewReport(
        markdown="{}",
        head_sha="a" * 40,
        finding_counts={"total": 3, "posted": 2, "skipped_duplicate": 1},
    )
    outcome = SingleMrOutcome(
        review_run_id="review-123",
        attempt=1,
        disposition="reused",
        status="succeeded",
        report=report,
        report_json_path="report.json",
        report_markdown_path="report.md",
        deliveries={
            "gitlab": {"status": "succeeded"},
            "onebox": {"status": "succeeded", "external_ref": "review-app-mr-7.md"},
        },
    )

    accepted = render_single_accepted(mr)
    terminal = render_single_terminal(mr, outcome)

    assert accepted.startswith("[代码检视已受理]")
    assert "team/app!7" in accepted
    assert "https://gitlab.example.com/team/app/merge_requests/7" in accepted
    assert terminal.startswith("[代码检视完成]")
    assert "版本：aaaaaaaaaaaa" in terminal
    assert "执行：复用已有完成结果" in terminal
    assert "检视发现：3 条" in terminal
    assert "GitLab：成功（新发布 2 条，已存在 1 条）" in terminal
    assert "OneBox：成功（review-app-mr-7.md）" in terminal
    assert "任务号：review-123" in terminal


def test_single_terminal_reports_superseded_version_and_safe_failure():
    mr = _mr()
    report = ReviewReport(
        markdown="{}",
        head_sha="a" * 40,
        head_validation={"review_head_sha": "a" * 40, "current_head_sha": "c" * 40, "status": "stale"},
    )
    outcome = SingleMrOutcome(
        review_run_id="review-old",
        attempt=1,
        disposition="created",
        status="superseded",
        report=report,
        report_json_path="",
        report_markdown_path="",
        deliveries={"gitlab": {"status": "skipped_stale"}, "onebox": {"status": "skipped_stale"}},
    )

    terminal = render_single_terminal(mr, outcome)
    failed = render_single_failed(mr, "mr-task", "mr_metadata")

    assert terminal.startswith("[代码检视已过期]")
    assert "版本：aaaaaaaaaaaa → cccccccccccc" in terminal
    assert terminal.count("已跳过（版本过期）") == 2
    assert failed.startswith("[代码检视失败]")
    assert "阶段：MR 信息读取" in failed
    assert "secret internal detail" not in failed


def test_review_set_notifications_identify_every_member_and_delivery_result():
    app = _member("team/app", 7, "a" * 40)
    sdk = _member("team/sdk", 8, "c" * 40)
    manifest = ReviewSetManifest(
        schema_version="review-set/v1",
        review_set_id="f" * 64,
        req_id="REQ-42",
        members=(app, sdk),
        resource_limits={},
    )
    report = ReviewSetReviewReport(
        manifest=manifest,
        review_plan={},
        result=SimpleNamespace(findings=(object(), object())),
        members=(),
        prompt_templates={},
        agent_call_count=2,
    )
    publication = ReviewSetPublication(
        status="success_with_warnings",
        results=(),
        counts={
            "total": 4,
            "posted_inline": 1,
            "posted_note": 1,
            "skipped_duplicate": 1,
            "filtered": 0,
            "disabled": 0,
            "failed": 1,
            "invalid": 0,
            "model_not_configured": 0,
        },
    )

    accepted = render_review_set_accepted((_mr("team/app", 7), _mr("team/sdk", 8)))
    terminal = render_review_set_terminal(report, publication, "review-set-ffffffffffff.md", None, "task-1")
    rejected = render_review_set_rejected(
        (_mr("team/app", 7), _mr("team/sdk", 8)),
        "ReqID 不一致。",
        "task-2",
    )

    assert accepted.startswith("[联合代码检视已受理]")
    assert "team/app!7" in accepted and "team/sdk!8" in accepted
    assert terminal.startswith("[联合代码检视完成但有告警]")
    assert "ReqID：REQ-42" in terminal
    assert "ReviewSet：ffffffffffff" in terminal
    assert "team/app!7 @ aaaaaaaaaaaa" in terminal
    assert "team/sdk!8 @ cccccccccccc" in terminal
    assert "检视发现：2 条" in terminal
    assert "新发布 2 条，已存在 1 条" in terminal
    assert "OneBox：成功（review-set-ffffffffffff.md）" in terminal
    assert rejected.startswith("[联合代码检视已拒绝]")
    assert "team/app!7" in rejected and "team/sdk!8" in rejected
    assert "原因：ReqID 不一致。" in rejected


def test_review_set_upload_failure_is_rendered_without_raw_error():
    member = _member("team/app", 7, "a" * 40)
    report = ReviewSetReviewReport(
        manifest=ReviewSetManifest("review-set/v1", "f" * 64, "REQ-42", (member,), {}),
        review_plan={},
        result=SimpleNamespace(findings=()),
        members=(),
        prompt_templates={},
        agent_call_count=2,
    )
    publication = ReviewSetPublication("success", (), {"total": 0})

    terminal = render_review_set_terminal(
        report,
        publication,
        "review-set-ffffffffffff.md",
        "secret parent path was rejected",
        "task-1",
    )

    assert terminal.startswith("[联合代码检视完成但有告警]")
    assert "OneBox：失败" in terminal
    assert "secret parent path" not in terminal
