from __future__ import annotations

import json
from pathlib import Path

import pytest

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl
from mr_reviewer.im import (
    ImMessage,
    ReviewRequest,
    ReviewSetRejection,
    ReviewSetRequest,
    resolve_review_trigger,
)
from mr_reviewer.review_set import ReviewSetPreparer, ReviewSetValidationError, extract_req_id
from mr_reviewer.review_set_result import (
    ReviewSetPlanParseError,
    StructuredReviewSetParseError,
    parse_review_set_plan,
    parse_structured_review_set_result,
)
from mr_reviewer.reviewer import ReviewService, ReviewStageError


def _message(text: str) -> ImMessage:
    return ImMessage(
        message_id="message-1",
        chat_id="group-1",
        sender_id="alice",
        text=text,
        created_at="2026-07-14T00:00:00Z",
    )


def _config(tmp_path: Path | None = None) -> Config:
    return Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="token",
        bot_mention="@ReviewBot",
        allowed_groups={"group-1"},
        allowed_users={"alice"},
        allowed_repos={"team/app", "team/sdk", "team/api", "team/extra"},
        work_dir=tmp_path or Path("work"),
    )


def test_resolve_review_trigger_keeps_single_mr_behavior():
    result = resolve_review_trigger(
        _message("@ReviewBot https://gitlab.example.com/team/app/merge_requests/7"),
        _config(),
    )

    assert isinstance(result, ReviewRequest)
    assert result.mr.project_path == "team/app"
    assert result.mr.mr_iid == 7


def test_resolve_review_trigger_deduplicates_identical_urls_to_single_request():
    url = "https://gitlab.example.com/team/app/merge_requests/7"

    result = resolve_review_trigger(_message(f"@ReviewBot {url} {url}"), _config())

    assert isinstance(result, ReviewRequest)


def test_resolve_review_trigger_builds_review_set_for_different_projects():
    result = resolve_review_trigger(
        _message(
            "@ReviewBot "
            "https://gitlab.example.com/team/app/merge_requests/7 "
            "https://gitlab.example.com/team/sdk/merge_requests/8"
        ),
        _config(),
    )

    assert isinstance(result, ReviewSetRequest)
    assert [(member.project_path, member.mr_iid) for member in result.members] == [
        ("team/app", 7),
        ("team/sdk", 8),
    ]


def test_resolve_review_trigger_builds_three_member_review_set():
    result = resolve_review_trigger(
        _message(
            "@ReviewBot "
            "https://gitlab.example.com/team/app/merge_requests/7 "
            "https://gitlab.example.com/team/sdk/merge_requests/8 "
            "https://gitlab.example.com/team/api/merge_requests/9"
        ),
        _config(),
    )

    assert isinstance(result, ReviewSetRequest)
    assert len(result.members) == 3


def test_resolve_review_trigger_rejects_more_than_three_unique_mrs():
    result = resolve_review_trigger(
        _message(
            "@ReviewBot "
            "https://gitlab.example.com/team/app/merge_requests/1 "
            "https://gitlab.example.com/team/sdk/merge_requests/2 "
            "https://gitlab.example.com/team/api/merge_requests/3 "
            "https://gitlab.example.com/team/extra/merge_requests/4"
        ),
        _config(),
    )

    assert isinstance(result, ReviewSetRejection)
    assert result.reason_code == "too_many_mrs"


def test_resolve_review_trigger_rejects_multiple_mrs_from_same_project():
    result = resolve_review_trigger(
        _message(
            "@ReviewBot "
            "https://gitlab.example.com/team/app/merge_requests/1 "
            "https://gitlab.example.com/team/app/merge_requests/2"
        ),
        _config(),
    )

    assert isinstance(result, ReviewSetRejection)
    assert result.reason_code == "same_project"


def test_resolve_review_trigger_rejects_disallowed_repo_in_review_set():
    result = resolve_review_trigger(
        _message(
            "@ReviewBot "
            "https://gitlab.example.com/team/app/merge_requests/1 "
            "https://gitlab.example.com/other/private/merge_requests/2"
        ),
        _config(),
    )

    assert isinstance(result, ReviewSetRejection)
    assert result.reason_code == "repo_not_allowed"


def test_gitlab_client_uses_project_and_isource_mr_detail_endpoints(monkeypatch):
    paths: list[str] = []
    client = GitLabClient("https://api.example.com/api/v4", "token")
    monkeypatch.setattr(client, "_get_json", lambda path: paths.append(path) or {"id": 5713530})

    client.get_project("team/project")
    client.get_review_set_merge_request(5713530, 10)

    assert paths == [
        "/projects/team%2Fproject",
        "/projects/5713530/isource/merge_requests/10",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"e2e_issues": None},
        {"e2e_issues": []},
        {"e2e_issues": [None]},
        {"e2e_issues": [{}]},
        {"e2e_issues": [{"issue_num": None}]},
        {"e2e_issues": [{"issue_num": "   "}]},
    ],
)
def test_extract_req_id_rejects_missing_or_invalid_first_issue(payload):
    with pytest.raises(ReviewSetValidationError) as exc_info:
        extract_req_id(payload)

    assert exc_info.value.reason_code == "req_id_missing"


def test_extract_req_id_reads_only_first_issue_and_trims_value():
    assert extract_req_id(
        {"e2e_issues": [{"issue_num": " US20260714100001 "}, {"issue_num": "ignored"}]}
    ) == "US20260714100001"


class _RecordingGitLab:
    def __init__(self, req_ids: dict[str, str] | None = None):
        self.req_ids = req_ids or {"team/app": "REQ-1", "team/sdk": "REQ-1"}
        self.calls: list[tuple] = []

    def get_project(self, project_path: str) -> dict:
        self.calls.append(("project", project_path))
        project_id = {"team/app": 101, "team/sdk": 202}[project_path]
        return {"id": project_id, "path_with_namespace": project_path}

    def get_review_set_merge_request(self, project_id: int, mr_iid: int) -> dict:
        project_path = {101: "team/app", 202: "team/sdk"}[project_id]
        self.calls.append(("isource", project_id, mr_iid))
        return {
            "project_id": project_id,
            "iid": mr_iid,
            "diff_refs": {
                "base_sha": f"base-{project_id}",
                "start_sha": f"start-{project_id}",
                "head_sha": f"head-{project_id}",
            },
            "e2e_issues": [{"issue_num": self.req_ids[project_path]}],
        }

    def get_merge_request(self, mr: GitLabMrUrl) -> dict:
        project_id = {"team/app": 101, "team/sdk": 202}[mr.project_path]
        self.calls.append(("mr", mr.project_path, mr.mr_iid))
        return {
            "source_project_id": project_id,
            "target_project_id": project_id,
            "source_branch": f"feature-{mr.mr_iid}",
            "target_branch": "main",
        }

    def get_project_http_url(self, project_id: int) -> str:
        self.calls.append(("repo", project_id))
        return f"https://gitlab.example.com/project-{project_id}.git"


class _RecordingGit:
    def __init__(self, fail_project_id: int | None = None):
        self.calls: list[tuple] = []
        self.fail_project_id = fail_project_id

    def clone_checkout_and_diff(self, checkout, token, work_dir, limits):
        project_id = int(checkout.head_sha.split("-")[-1])
        self.calls.append((checkout, token, Path(work_dir), limits))
        if project_id == self.fail_project_id:
            raise RuntimeError("clone failed")
        repo_path = Path(work_dir) / "repo"
        repo_path.mkdir(parents=True)
        return {
            "repo_path": repo_path,
            "diff": f"diff-{project_id}",
            "changed_files": [f"file-{project_id}.py"],
            "base_sha": checkout.base_sha,
            "head_sha": checkout.head_sha,
        }


def _review_set_request(order: tuple[str, str] = ("team/app", "team/sdk")) -> ReviewSetRequest:
    mr_iids = {"team/app": 7, "team/sdk": 8}
    members = tuple(
        GitLabMrUrl("https://gitlab.example.com", project, mr_iids[project])
        for project in order
    )
    return ReviewSetRequest(_message("@ReviewBot joint review"), members)


def test_review_set_preparer_writes_deterministic_manifest_and_member_workspaces(tmp_path: Path):
    gitlab = _RecordingGitLab()
    git = _RecordingGit()
    task_dir = tmp_path / "review-set-task"

    prepared = ReviewSetPreparer(gitlab, git).prepare(_review_set_request(), _config(tmp_path), task_dir)

    assert prepared.manifest.req_id == "REQ-1"
    assert len(prepared.manifest.review_set_id) == 64
    assert [member.member_id for member in prepared.manifest.members] == ["p101-mr7", "p202-mr8"]
    assert [call[2] for call in git.calls] == [
        task_dir / "members" / "p101-mr7",
        task_dir / "members" / "p202-mr8",
    ]
    manifest = json.loads((task_dir / "review-set.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "review-set/v1"
    assert manifest["members"][0]["repo_path"] == "members/p101-mr7/repo"
    assert manifest["members"][0]["start_sha"] == "start-101"


def test_review_set_id_is_independent_of_member_order(tmp_path: Path):
    first = ReviewSetPreparer(_RecordingGitLab(), _RecordingGit()).prepare(
        _review_set_request(("team/app", "team/sdk")),
        _config(tmp_path),
        tmp_path / "first",
    )
    second = ReviewSetPreparer(_RecordingGitLab(), _RecordingGit()).prepare(
        _review_set_request(("team/sdk", "team/app")),
        _config(tmp_path),
        tmp_path / "second",
    )

    assert first.manifest.review_set_id == second.manifest.review_set_id


def test_review_set_preparer_rejects_mismatched_req_ids_before_clone(tmp_path: Path):
    git = _RecordingGit()
    preparer = ReviewSetPreparer(
        _RecordingGitLab({"team/app": "REQ-1", "team/sdk": "REQ-2"}),
        git,
    )
    task_dir = tmp_path / "review-set-task"

    with pytest.raises(ReviewSetValidationError) as exc_info:
        preparer.prepare(_review_set_request(), _config(tmp_path), task_dir)

    assert exc_info.value.reason_code == "req_id_mismatch"
    assert git.calls == []
    assert not task_dir.exists()


def test_review_set_preparer_cleans_all_members_when_clone_fails(tmp_path: Path):
    task_dir = tmp_path / "review-set-task"

    with pytest.raises(RuntimeError, match="clone failed"):
        ReviewSetPreparer(_RecordingGitLab(), _RecordingGit(fail_project_id=202)).prepare(
            _review_set_request(),
            _config(tmp_path),
            task_dir,
        )

    assert not task_dir.exists()


def _plan_payload() -> dict:
    return {
        "schema_version": "review-set-plan/v1",
        "member_focus": [
            {
                "member_id": "p101-mr7",
                "change_intent": ["调整调用方空值处理"],
                "critical_paths": [
                    {"path": "src/caller.py", "reason": "调用入口", "verify": ["空值契约"]}
                ],
                "test_risks": ["缺少空值测试"],
            },
            {
                "member_id": "p202-mr8",
                "change_intent": ["调整 SDK 返回契约"],
                "critical_paths": [
                    {"path": "src/sdk.py", "reason": "SDK 出口", "verify": ["返回值"]}
                ],
                "test_risks": [],
            },
        ],
        "relationships": [
            {
                "from_member_id": "p101-mr7",
                "to_member_id": "p202-mr8",
                "contract": "调用方依赖 SDK 返回值",
                "evidence_refs": [
                    {
                        "member_id": "p202-mr8",
                        "path": "src/sdk.py",
                        "start_line": 40,
                        "end_line": 42,
                        "detail": "新增 null 返回分支",
                    }
                ],
                "verification": ["确认调用方处理 null"],
            }
        ],
        "open_questions": [],
    }


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


def test_parse_review_set_plan_requires_exact_members():
    plan = parse_review_set_plan(
        json.dumps(_plan_payload(), ensure_ascii=False),
        {"p101-mr7", "p202-mr8"},
    )

    assert plan["schema_version"] == "review-set-plan/v1"
    assert [item["member_id"] for item in plan["member_focus"]] == ["p101-mr7", "p202-mr8"]


def test_parse_review_set_plan_rejects_unknown_member():
    payload = _plan_payload()
    payload["member_focus"][0]["member_id"] = "unknown"

    with pytest.raises(ReviewSetPlanParseError, match="member_focus must cover"):
        parse_review_set_plan(json.dumps(payload), {"p101-mr7", "p202-mr8"})


def test_parse_review_set_result_accepts_multi_target_and_null_position():
    result = parse_structured_review_set_result(json.dumps(_result_payload(), ensure_ascii=False))

    assert result.schema_version == "review-set-review/v1"
    assert result.findings[0].targets[0].position.new_line == 57
    assert result.findings[0].targets[1].position is None


def test_parse_review_set_result_accepts_minor_severity():
    payload = _result_payload()
    payload["findings"][0]["severity"] = "minor"

    result = parse_structured_review_set_result(json.dumps(payload, ensure_ascii=False))

    assert result.findings[0].severity == "minor"


def test_parse_review_set_result_rejects_legacy_severity_typo():
    payload = _result_payload()
    payload["findings"][0]["severity"] = "min" + "jor"

    with pytest.raises(StructuredReviewSetParseError, match="severity"):
        parse_structured_review_set_result(json.dumps(payload, ensure_ascii=False))


def test_parse_review_set_result_rejects_unexpected_fields():
    payload = _result_payload()
    payload["overview"] = "not allowed"

    with pytest.raises(StructuredReviewSetParseError, match="unexpected fields"):
        parse_structured_review_set_result(json.dumps(payload))


class _ReviewSetRunner:
    def __init__(self, invalid_plan: bool = False):
        self.calls: list[tuple] = []
        self.invalid_plan = invalid_plan

    def run_review(self, prompt, cwd, timeout_seconds, prompt_metadata=None):
        self.calls.append((str(prompt), Path(cwd), timeout_seconds, prompt_metadata))
        if prompt_metadata.template_id == "review-set-plan":
            return "not json" if self.invalid_plan else json.dumps(_plan_payload(), ensure_ascii=False)
        return json.dumps(_result_payload(), ensure_ascii=False)


def test_review_service_runs_review_set_as_fixed_two_step_from_task_root(tmp_path: Path):
    runner = _ReviewSetRunner()
    service = ReviewService(_RecordingGitLab(), _RecordingGit(), runner)
    task_dir = tmp_path / "joint-task"

    report = service.review_set(_review_set_request(), _config(tmp_path), task_id="joint-task")

    assert report.manifest.req_id == "REQ-1"
    assert report.agent_call_count == 2
    assert report.result.findings[0].issue_id == "CONTRACT_NULLABILITY_001"
    assert [call[1] for call in runner.calls] == [task_dir, task_dir]
    assert [call[3].template_id for call in runner.calls] == ["review-set-plan", "review-set-review"]
    assert "cross-repo-code-review skill" in runner.calls[0][0]
    assert "review-set.json" in runner.calls[0][0]
    assert "diff-101" not in runner.calls[0][0]
    assert "review-set-plan/v1" in runner.calls[1][0]
    assert not task_dir.exists()


def test_review_service_stops_review_set_when_plan_is_invalid(tmp_path: Path):
    runner = _ReviewSetRunner(invalid_plan=True)
    service = ReviewService(_RecordingGitLab(), _RecordingGit(), runner)

    with pytest.raises(ReviewStageError) as exc_info:
        service.review_set(_review_set_request(), _config(tmp_path), task_id="invalid-plan")

    assert exc_info.value.stage == "review_set_plan"
    assert len(runner.calls) == 1
    assert not (tmp_path / "invalid-plan").exists()
