import json
import logging
from pathlib import Path

import pytest

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabMrUrl
from mr_reviewer.review_result import parse_structured_review_result
from mr_reviewer.reviewer import ReviewService, ReviewStageError


class _GitLab:
    def __init__(self, title: str):
        self.title = title
        self.dependency_queries: list[str] = []

    def get_merge_request(self, mr: GitLabMrUrl) -> dict:
        return {
            "title": self.title,
            "source_branch": "feature/contract",
            "target_branch": "release",
            "source_project_id": 101,
            "target_project_id": 101,
            "diff_refs": {"base_sha": "a" * 40, "head_sha": "b" * 40},
        }

    def get_project_http_url(self, project_id: int) -> str:
        assert project_id == 101
        return "https://gitlab.example.com/team/app.git"

    def get_project(self, project_path: str) -> dict:
        self.dependency_queries.append(project_path)
        project_id = {
            "team/repo-b": 202,
            "team/repo-c": 303,
            "team/repo-d": 404,
            "team/repo-e": 505,
        }[project_path]
        return {
            "id": project_id,
            "path_with_namespace": project_path,
            "http_url_to_repo": f"https://gitlab.example.com/{project_path}.git",
        }


class _Git:
    def __init__(self, fail_project: str = ""):
        self.primary_calls: list[tuple] = []
        self.dependency_calls: list[tuple[str, str, Path]] = []
        self.fail_project = fail_project

    def clone_checkout_and_diff(self, checkout, token, work_dir, limits) -> dict:
        self.primary_calls.append((checkout, token, Path(work_dir), limits))
        repo_path = Path(work_dir) / "repo"
        repo_path.mkdir(parents=True)
        return {
            "repo_path": repo_path,
            "diff": "diff --git a/app.py b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            "changed_files": ["app.py"],
            "truncated": False,
            "base_sha": checkout.base_sha,
            "head_sha": checkout.head_sha,
        }

    def clone_checkout_branch(self, repo_url: str, branch: str, token: str, repo_path: Path) -> str:
        project_path = repo_url.removeprefix("https://gitlab.example.com/").removesuffix(".git")
        self.dependency_calls.append((project_path, branch, repo_path))
        repo_path.mkdir(parents=True)
        if project_path == self.fail_project:
            raise RuntimeError("dependency checkout failed")
        return {
            "team/repo-b": "c" * 40,
            "team/repo-c": "d" * 40,
            "team/repo-d": "e" * 40,
            "team/repo-e": "f" * 40,
        }[project_path]


class _OutsideTaskGit(_Git):
    def clone_checkout_and_diff(self, checkout, token, work_dir, limits) -> dict:
        diff_info = super().clone_checkout_and_diff(checkout, token, work_dir, limits)
        outside_repo = Path(work_dir).parent / "outside-repo"
        outside_repo.mkdir()
        diff_info["repo_path"] = outside_repo
        return diff_info


def _single_plan() -> dict:
    return {
        "change_intent": ["更新调用方"],
        "critical_paths": [{"path": "app.py", "reason": "调用入口", "verify": ["兼容性"]}],
        "external_contracts": [],
        "state_invariants": [],
        "transaction_async_boundaries": [],
        "test_risks": [],
        "open_questions": [],
    }


def _dependency_plan() -> dict:
    return {
        "schema_version": "dependency-review-plan/v1",
        "primary_focus": {
            "change_intent": ["更新依赖调用"],
            "critical_paths": [{"path": "app.py", "reason": "调用入口", "verify": ["返回值"]}],
            "test_risks": [],
        },
        "relationships": [
            {
                "dependency_repo_id": "p202",
                "contract": "主仓依赖 repo-b 返回值",
                "evidence_refs": [
                    {
                        "repo_id": "p202",
                        "path": "src/sdk.py",
                        "start_line": 1,
                        "end_line": 2,
                        "detail": "返回值允许为空",
                    }
                ],
                "verification": ["检查主仓空值处理"],
            }
        ],
        "open_questions": [],
    }


def _dependency_result() -> dict:
    return {
        "schema_version": "dependency-review-result/v1",
        "findings": [
            {
                "issue_id": "CONTRACT_001",
                "rule_id": "CONTRACT",
                "severity": "major",
                "confidence": "HIGH",
                "title": "主 MR 未处理依赖空返回",
                "impact": "请求可能失败。",
                "evidence_refs": [
                    {
                        "repo_id": "primary",
                        "path": "app.py",
                        "start_line": 1,
                        "end_line": 1,
                        "detail": "主 MR 直接使用返回值。",
                    },
                    {
                        "repo_id": "p202",
                        "path": "src/sdk.py",
                        "start_line": 1,
                        "end_line": 2,
                        "detail": "依赖返回值允许为空。",
                    },
                ],
                "position": {
                    "old_path": "app.py",
                    "new_path": "app.py",
                    "old_line": -1,
                    "new_line": 1,
                },
                "suggestion": "在主 MR 增加空值处理。",
            }
        ],
        "relationship_summary": ["主仓依赖 repo-b 的返回值契约。"],
        "notes": [],
        "test_gaps": ["缺少契约测试。"],
        "good": [],
    }


class _Runner:
    def __init__(self, invalid_dependency_plan: bool = False, invalid_dependency_result: bool = False):
        self.calls: list[tuple] = []
        self.invalid_dependency_plan = invalid_dependency_plan
        self.invalid_dependency_result = invalid_dependency_result

    def run_review(self, prompt, cwd, timeout_seconds, prompt_metadata=None) -> str:
        self.calls.append((str(prompt), Path(cwd), timeout_seconds, prompt_metadata))
        template_id = prompt_metadata.template_id
        if template_id == "review-plan":
            return json.dumps(_single_plan(), ensure_ascii=False)
        if template_id == "dependency-review-plan":
            return "not json" if self.invalid_dependency_plan else json.dumps(_dependency_plan(), ensure_ascii=False)
        if template_id == "dependency-review":
            return "not json" if self.invalid_dependency_result else json.dumps(_dependency_result(), ensure_ascii=False)
        return json.dumps({"findings": [], "notes": [], "test_gaps": [], "good": []})


def _write_catalog(path: Path, dependencies: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repositories": [{"project_path": "team/app", "dependencies": dependencies}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, catalog: Path | None) -> Config:
    return Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        work_dir=tmp_path,
        repository_dependency_catalog=catalog,
    )


def _review(service: ReviewService, config: Config, task_id: str):
    return service.review(
        GitLabMrUrl("https://gitlab.example.com", "team/app", 7),
        config,
        task_id,
    )


def test_normal_review_does_not_read_invalid_catalog(tmp_path: Path):
    gitlab = _GitLab("Fix contract")
    runner = _Runner()

    report = _review(
        ReviewService(gitlab, _Git(), runner),
        _config(tmp_path, tmp_path / "missing.json"),
        "normal",
    )

    assert report.requested_review_mode == "one-step"
    assert report.review_mode == "one-step"
    assert report.review_scope == "single"
    assert report.dependency_context_status == "not_applicable"
    assert report.dependency_degradation_reason == ""
    assert gitlab.dependency_queries == []
    assert [call[3].template_id for call in runner.calls] == ["review"]


def test_deep_review_without_mapping_keeps_single_repository_two_step(tmp_path: Path):
    runner = _Runner()
    report = _review(
        ReviewService(_GitLab("【Deep-Review】 Contract"), _Git(), runner),
        _config(tmp_path, _write_catalog(tmp_path / "catalog.json", [])),
        "deep-single",
    )

    assert report.requested_review_mode == "two-step"
    assert report.review_mode == "two-step"
    assert report.review_scope == "single"
    assert report.dependency_context_status == "not_applicable"
    assert [call[3].template_id for call in runner.calls] == ["review-plan", "deep-review"]


def test_deep_review_prepares_all_dependencies_and_runs_joint_two_step(tmp_path: Path):
    gitlab = _GitLab("【Deep-Review】 Contract")
    git = _Git()
    runner = _Runner()
    task_dir = tmp_path / "joint"

    report = _review(
        ReviewService(gitlab, git, runner),
        _config(tmp_path, _write_catalog(tmp_path / "catalog.json", ["team/repo-c", "team/repo-b"])),
        "joint",
    )

    assert report.requested_review_mode == "two-step"
    assert report.review_mode == "two-step"
    assert report.review_scope == "dependency-review"
    assert report.dependency_context_status == "complete"
    assert report.dependency_degradation_reason == ""
    assert report.dependency_context_id
    assert report.dependency_relationship_summary == ["主仓依赖 repo-b 的返回值契约。"]
    assert [item["project_path"] for item in report.dependency_repositories] == [
        "team/repo-b",
        "team/repo-c",
    ]
    assert [(call[0], call[1]) for call in git.dependency_calls] == [
        ("team/repo-b", "release"),
        ("team/repo-c", "release"),
    ]
    assert [call[3].template_id for call in runner.calls] == [
        "dependency-review-plan",
        "dependency-review",
    ]
    assert [call[1] for call in runner.calls] == [task_dir, task_dir]
    assert report.agent_call_count == 2
    assert parse_structured_review_result(report.markdown).findings[0].rule_id == "CONTRACT"
    assert not task_dir.exists()


def test_deep_review_over_limit_degrades_to_single_one_step(tmp_path: Path):
    gitlab = _GitLab("【Deep-Review】 Contract")
    git = _Git()
    runner = _Runner()
    dependencies = ["team/repo-b", "team/repo-c", "team/repo-d", "team/repo-e"]

    report = _review(
        ReviewService(gitlab, git, runner),
        _config(tmp_path, _write_catalog(tmp_path / "catalog.json", dependencies)),
        "over-limit",
    )

    assert report.review_mode == "one-step"
    assert report.review_scope == "single"
    assert report.dependency_context_status == "degraded"
    assert report.dependency_degradation_reason == "dependency_limit_exceeded"
    assert git.dependency_calls == []
    assert gitlab.dependency_queries == []
    assert [call[3].template_id for call in runner.calls] == ["review"]


@pytest.mark.parametrize(
    ("catalog_content", "reason"),
    [(None, "catalog_unreadable"), ("not json", "catalog_invalid")],
)
def test_deep_review_with_invalid_catalog_degrades_to_single_one_step(
    tmp_path: Path,
    catalog_content: str | None,
    reason: str,
):
    catalog = tmp_path / "catalog.json"
    if catalog_content is not None:
        catalog.write_text(catalog_content, encoding="utf-8")
    runner = _Runner()

    report = _review(
        ReviewService(_GitLab("【Deep-Review】 Contract"), _Git(), runner),
        _config(tmp_path, catalog),
        f"invalid-{reason}",
    )

    assert report.review_mode == "one-step"
    assert report.dependency_context_status == "degraded"
    assert report.dependency_degradation_reason == reason
    assert [call[3].template_id for call in runner.calls] == ["review"]


def test_dependency_checkout_failure_discards_all_and_runs_single_one_step(tmp_path: Path):
    git = _Git(fail_project="team/repo-c")
    runner = _Runner()
    task_id = "checkout-failure"

    report = _review(
        ReviewService(_GitLab("【Deep-Review】 Contract"), git, runner),
        _config(tmp_path, _write_catalog(tmp_path / "catalog.json", ["team/repo-b", "team/repo-c"])),
        task_id,
    )

    assert report.review_mode == "one-step"
    assert report.review_scope == "single"
    assert report.dependency_context_status == "degraded"
    assert report.dependency_degradation_reason == "dependency_checkout_failed"
    assert report.dependency_failed_project == "team/repo-c"
    assert report.dependency_repositories == []
    assert [call[3].template_id for call in runner.calls] == ["review"]
    assert not (tmp_path / task_id).exists()


def test_primary_repo_outside_task_dir_degrades_to_single_one_step(tmp_path: Path):
    git = _OutsideTaskGit()
    runner = _Runner()
    task_id = "primary-context-invalid"

    report = _review(
        ReviewService(_GitLab("【Deep-Review】 Contract"), git, runner),
        _config(tmp_path, _write_catalog(tmp_path / "catalog.json", ["team/repo-b"])),
        task_id,
    )

    assert report.review_mode == "one-step"
    assert report.review_scope == "single"
    assert report.dependency_context_status == "degraded"
    assert report.dependency_degradation_reason == "primary_context_invalid"
    assert report.dependency_failed_project == "team/app"
    assert git.dependency_calls == []
    assert [call[3].template_id for call in runner.calls] == ["review"]
    assert not (tmp_path / task_id).exists()


def test_invalid_dependency_plan_stops_before_second_agent_call_and_cleans(tmp_path: Path):
    runner = _Runner(invalid_dependency_plan=True)
    task_id = "invalid-joint-plan"

    with pytest.raises(ReviewStageError) as exc_info:
        _review(
            ReviewService(_GitLab("【Deep-Review】 Contract"), _Git(), runner),
            _config(tmp_path, _write_catalog(tmp_path / "catalog.json", ["team/repo-b"])),
            task_id,
        )

    assert exc_info.value.stage == "dependency_review_plan"
    assert exc_info.value.agent_call_count == 1
    assert exc_info.value.report_context["dependency_context_status"] == "complete"
    assert len(runner.calls) == 1
    assert not (tmp_path / task_id).exists()


def test_invalid_dependency_result_records_second_stage_and_cleans(tmp_path: Path):
    runner = _Runner(invalid_dependency_result=True)
    task_id = "invalid-joint-result"

    with pytest.raises(ReviewStageError) as exc_info:
        _review(
            ReviewService(_GitLab("【Deep-Review】 Contract"), _Git(), runner),
            _config(tmp_path, _write_catalog(tmp_path / "catalog.json", ["team/repo-b"])),
            task_id,
        )

    assert exc_info.value.stage == "dependency_review"
    assert exc_info.value.agent_call_count == 2
    assert exc_info.value.review_plan["schema_version"] == "dependency-review-plan/v1"
    assert exc_info.value.report_context["dependency_context_status"] == "complete"
    assert len(runner.calls) == 2
    assert not (tmp_path / task_id).exists()


def test_joint_review_logs_exact_dependency_commits_and_preparation_time(tmp_path: Path, caplog):
    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        _review(
            ReviewService(_GitLab("【Deep-Review】 Contract"), _Git(), _Runner()),
            _config(tmp_path, _write_catalog(tmp_path / "catalog.json", ["team/repo-b"])),
            "dependency-log",
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "requested_review_mode=two-step review_mode=two-step review_scope=dependency-review" in log_text
    assert "project=team/repo-b branch=release commit=" + "c" * 40 in log_text
    assert "elapsed=" in log_text
