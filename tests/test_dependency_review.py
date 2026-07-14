import hashlib
import importlib
import json
from pathlib import Path

import pytest


def _dependency_module():
    return importlib.import_module("mr_reviewer.dependency_review")


class _FakeGitLab:
    def __init__(self, projects: dict[str, dict]):
        self.projects = projects
        self.calls: list[str] = []

    def get_project(self, project_path: str) -> dict:
        self.calls.append(project_path)
        value = self.projects[project_path]
        if isinstance(value, Exception):
            raise value
        return value


class _FakeGit:
    def __init__(self, commits: dict[str, str | Exception]):
        self.commits = commits
        self.calls: list[tuple[str, str, Path]] = []

    def clone_checkout_branch(self, repo_url: str, branch: str, token: str, repo_path: Path) -> str:
        self.calls.append((repo_url, branch, repo_path))
        value = self.commits[repo_url]
        repo_path.mkdir(parents=True, exist_ok=True)
        (repo_path / "contract.txt").write_text(repo_url, encoding="utf-8")
        if isinstance(value, Exception):
            raise value
        return value


def _primary(module, repo_path: str = "repo"):
    return module.DependencyReviewPrimary(
        project_path="team/repo-a",
        mr_iid=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        target_branch="release",
        repo_path=repo_path,
    )


def test_preparer_writes_deterministic_manifest_without_clone_urls(tmp_path: Path):
    module = _dependency_module()
    projects = {
        "team/repo-b": {
            "id": 202,
            "path_with_namespace": "team/repo-b",
            "http_url_to_repo": "https://gitlab.example.com/team/repo-b.git",
        },
        "team/repo-c": {
            "id": 303,
            "path_with_namespace": "team/repo-c",
            "http_url_to_repo": "https://gitlab.example.com/team/repo-c.git",
        },
    }
    commits = {
        "https://gitlab.example.com/team/repo-b.git": "c" * 40,
        "https://gitlab.example.com/team/repo-c.git": "d" * 40,
    }
    task_dir = tmp_path / "task"
    prepared = module.DependencyReviewPreparer(_FakeGitLab(projects), _FakeGit(commits)).prepare(
        primary=_primary(module),
        dependency_project_paths=("team/repo-c", "team/repo-b"),
        token="secret-token",
        task_dir=task_dir,
    )

    manifest_payload = json.loads((task_dir / "dependency-review.json").read_text(encoding="utf-8"))
    expected_context = hashlib.sha256(
        ("b" * 40 + "\nteam/repo-b@" + "c" * 40 + "\nteam/repo-c@" + "d" * 40).encode("utf-8")
    ).hexdigest()

    assert manifest_payload == prepared.manifest.to_dict()
    assert manifest_payload["schema_version"] == "dependency-review/v1"
    assert manifest_payload["context_id"] == expected_context
    assert manifest_payload["primary"] == {
        "project_path": "team/repo-a",
        "mr_iid": 17,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "target_branch": "release",
        "repo_path": "repo",
    }
    assert [item["project_path"] for item in manifest_payload["dependencies"]] == [
        "team/repo-b",
        "team/repo-c",
    ]
    assert all(item["branch"] == "release" for item in manifest_payload["dependencies"])
    assert all(item["repo_path"].startswith("dependencies/p") for item in manifest_payload["dependencies"])
    serialized = json.dumps(manifest_payload)
    assert "secret-token" not in serialized
    assert "https://" not in serialized


def test_preparer_removes_all_dependencies_when_any_checkout_fails(tmp_path: Path):
    module = _dependency_module()
    projects = {
        "team/repo-b": {
            "id": 202,
            "path_with_namespace": "team/repo-b",
            "http_url_to_repo": "https://gitlab.example.com/team/repo-b.git",
        },
        "team/repo-c": {
            "id": 303,
            "path_with_namespace": "team/repo-c",
            "http_url_to_repo": "https://gitlab.example.com/team/repo-c.git",
        },
    }
    commits = {
        "https://gitlab.example.com/team/repo-b.git": "c" * 40,
        "https://gitlab.example.com/team/repo-c.git": RuntimeError("checkout failed"),
    }
    task_dir = tmp_path / "task"
    primary_repo = task_dir / "repo"
    primary_repo.mkdir(parents=True)
    (primary_repo / "keep.txt").write_text("primary", encoding="utf-8")

    with pytest.raises(module.DependencyReviewPreparationError) as exc_info:
        module.DependencyReviewPreparer(_FakeGitLab(projects), _FakeGit(commits)).prepare(
            primary=_primary(module),
            dependency_project_paths=("team/repo-b", "team/repo-c"),
            token="secret-token",
            task_dir=task_dir,
        )

    assert exc_info.value.reason_code == "dependency_checkout_failed"
    assert exc_info.value.project_path == "team/repo-c"
    assert not (task_dir / "dependencies").exists()
    assert not (task_dir / "dependency-review.json").exists()
    assert (primary_repo / "keep.txt").read_text(encoding="utf-8") == "primary"


@pytest.mark.parametrize(
    "project_payload",
    [
        {"id": 202, "path_with_namespace": "team/other", "http_url_to_repo": "https://gitlab/x.git"},
        {"id": 202, "path_with_namespace": "team/repo-b", "http_url_to_repo": "http://gitlab/x.git"},
        {"id": True, "path_with_namespace": "team/repo-b", "http_url_to_repo": "https://gitlab/x.git"},
    ],
)
def test_preparer_rejects_untrusted_gitlab_project_metadata(tmp_path: Path, project_payload: dict):
    module = _dependency_module()

    with pytest.raises(module.DependencyReviewPreparationError) as exc_info:
        module.DependencyReviewPreparer(
            _FakeGitLab({"team/repo-b": project_payload}),
            _FakeGit({}),
        ).prepare(
            primary=_primary(module),
            dependency_project_paths=("team/repo-b",),
            token="secret-token",
            task_dir=tmp_path / "task",
        )

    assert exc_info.value.reason_code == "dependency_project_lookup_failed"
    assert exc_info.value.project_path == "team/repo-b"
    assert not (tmp_path / "task" / "dependencies").exists()
