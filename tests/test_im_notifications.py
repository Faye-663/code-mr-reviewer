from types import SimpleNamespace

import pytest

import mr_reviewer.im_notifications as notifications
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
        finding_results=[
            {"severity": "major"},
            {"severity": "minor"},
            {"severity": "suggestion"},
        ],
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
    assert "检视总结：共 3 条（fatal 0、major 1、minor 1、suggestion 1）" in terminal
    assert "GitLab：成功（新发布 2 条，已存在 1 条）" in terminal
    assert "Review Report：已上传 OneBox（review-app-mr-7.md）" in terminal
    assert "跟踪ID：review-123" in terminal


def test_accepted_and_queue_full_notifications_show_queue_context():
    single_accepted = render_single_accepted(_mr(), ahead=1)
    review_set_accepted = render_review_set_accepted(
        (_mr("team/app", 7), _mr("team/sdk", 8)),
        ahead=2,
    )
    single_full = notifications.render_single_queue_full(_mr(), 20)
    review_set_full = notifications.render_review_set_queue_full(
        (_mr("team/app", 7), _mr("team/sdk", 8)),
        20,
    )

    assert "IM队列：入队时前方 1 个请求" in single_accepted
    assert "IM队列：入队时前方 2 个请求" in review_set_accepted
    assert single_full.startswith("[代码检视暂未受理]")
    assert "MR：team/app!7" in single_full
    assert "最多等待 20 个请求" in single_full
    assert review_set_full.startswith("[联合代码检视暂未受理]")
    assert "team/app!7" in review_set_full and "team/sdk!8" in review_set_full


def test_single_terminal_without_findings_keeps_mr_identity_and_report_delivery():
    mr = _mr()
    report = ReviewReport(
        markdown="{}",
        head_sha="a" * 40,
        finding_counts={"total": 0, "posted": 0, "skipped_duplicate": 0},
        finding_results=[],
    )
    outcome = SingleMrOutcome(
        review_run_id="review-empty",
        attempt=1,
        disposition="created",
        status="succeeded",
        report=report,
        report_json_path="report.json",
        report_markdown_path="report.md",
        deliveries={
            "gitlab": {"status": "succeeded"},
            "onebox": {"status": "succeeded", "external_ref": "review-empty.md"},
        },
    )

    terminal = render_single_terminal(mr, outcome)

    assert terminal.startswith("[代码检视完成]")
    assert "MR：team/app!7" in terminal
    assert "地址：https://gitlab.example.com/team/app/merge_requests/7" in terminal
    assert "版本：aaaaaaaaaaaa" in terminal
    assert "执行：新建检视任务" in terminal
    assert "检视总结：未发现问题（共 0 条）" in terminal
    assert "GitLab：无需发布（未发现问题）" in terminal
    assert "Review Report：已上传 OneBox（review-empty.md）" in terminal
    assert "跟踪ID：review-empty" in terminal


def test_single_terminal_does_not_claim_no_findings_when_agent_findings_were_rejected():
    report = ReviewReport(
        markdown="{}",
        head_sha="a" * 40,
        finding_counts={"total": 0},
        finding_results=[],
        rejected_findings=[
            {"index": 0, "reason_code": "invalid_finding_contract", "summary": "RULE"}
        ],
    )
    outcome = SingleMrOutcome(
        review_run_id="review-partial",
        attempt=1,
        disposition="created",
        status="succeeded",
        report=report,
        report_json_path="report.json",
        report_markdown_path="report.md",
        deliveries={"gitlab": {"status": "succeeded"}, "onebox": {"status": "disabled"}},
    )

    terminal = render_single_terminal(_mr(), outcome)

    assert "0 条合法 finding，1 条 Agent finding 被拒绝" in terminal
    assert "GitLab：未发布（1 条 Agent finding 被拒绝）" in terminal
    assert "未发现问题" not in terminal


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        ("created", "新建检视任务"),
        ("joined", "加入已有检视任务"),
        ("reused", "复用已有完成结果"),
        ("duplicate", "复用原请求结果"),
    ],
)
def test_single_terminal_names_every_execution_disposition(disposition, expected):
    outcome = SingleMrOutcome(
        review_run_id="review-shared",
        attempt=1,
        disposition=disposition,
        status="succeeded",
        report=ReviewReport(markdown="{}", head_sha="a" * 40, finding_counts={"total": 0}),
        report_json_path="report.json",
        report_markdown_path="report.md",
        deliveries={"gitlab": {"status": "disabled"}, "onebox": {"status": "disabled"}},
    )

    terminal = render_single_terminal(_mr(), outcome)

    assert f"执行：{expected}" in terminal
    assert "跟踪ID：review-shared" in terminal


@pytest.mark.parametrize(
    ("gitlab_status", "onebox_status", "gitlab_expected", "report_expected"),
    [
        ("disabled", "disabled", "GitLab：未启用", "Review Report：未上传（OneBox 未启用）"),
        ("failed", "failed", "GitLab：失败", "Review Report：上传 OneBox 失败"),
        ("unknown", "unknown", "GitLab：结果未知", "Review Report：OneBox 上传结果未知"),
        (
            "skipped_stale",
            "skipped_stale",
            "GitLab：已跳过（MR 版本已过期）",
            "Review Report：未上传（MR 版本已过期）",
        ),
        ("not_run", "not_run", "GitLab：未执行", "Review Report：未上传"),
    ],
)
def test_single_terminal_renders_safe_delivery_states(
    gitlab_status,
    onebox_status,
    gitlab_expected,
    report_expected,
):
    outcome = SingleMrOutcome(
        review_run_id="review-delivery",
        attempt=1,
        disposition="created",
        status="succeeded",
        report=ReviewReport(markdown="{}", head_sha="a" * 40, finding_counts={"total": 0}),
        report_json_path="report.json",
        report_markdown_path="report.md",
        deliveries={
            "gitlab": {"status": gitlab_status, "error": "secret gitlab failure"},
            "onebox": {"status": onebox_status, "error": "secret onebox failure"},
        },
    )

    terminal = render_single_terminal(_mr(), outcome)

    assert gitlab_expected in terminal
    assert report_expected in terminal
    assert "secret" not in terminal


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
    assert "GitLab：已跳过（MR 版本已过期）" in terminal
    assert "Review Report：未上传（MR 版本已过期）" in terminal
    assert "跟踪ID：review-old" in terminal
    assert failed.startswith("[代码检视失败]")
    assert "阶段：MR 信息读取" in failed
    assert "Review Report：未上传" in failed
    assert "跟踪ID：mr-task" in failed
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
        result=SimpleNamespace(
            findings=(SimpleNamespace(severity="major"), SimpleNamespace(severity="minor"))
        ),
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
    assert "检视总结：共 2 条（fatal 0、major 1、minor 1、suggestion 0）" in terminal
    assert "新发布 2 条，已存在 1 条" in terminal
    assert "Review Report：已上传 OneBox（review-set-ffffffffffff.md）" in terminal
    assert "跟踪ID：task-1" in terminal
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
    assert "team/app!7 @ aaaaaaaaaaaa" in terminal
    assert "检视总结：未发现问题（共 0 条）" in terminal
    assert "Review Report：上传 OneBox 失败" in terminal
    assert "跟踪ID：task-1" in terminal
    assert "secret parent path" not in terminal
