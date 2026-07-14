import subprocess
from pathlib import Path

import pytest

from mr_reviewer.git import DependencyBranchMissingError, GitClient


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _create_remote_with_release_branch(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Test User")
    _git(source, "config", "user.email", "test@example.com")
    (source / "contract.txt").write_text("main\n", encoding="utf-8")
    _git(source, "add", "contract.txt")
    _git(source, "commit", "-m", "main contract")
    _git(source, "tag", "release-only-tag")

    _git(source, "switch", "-c", "release")
    (source / "contract.txt").write_text("release\n", encoding="utf-8")
    _git(source, "commit", "-am", "release contract")
    release_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "switch", "main")

    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    return remote, release_sha


def test_git_fetches_only_exact_target_branch_and_checks_out_detached(tmp_path: Path):
    remote, release_sha = _create_remote_with_release_branch(tmp_path)
    repo_path = tmp_path / "checkout"

    actual_sha = GitClient().clone_checkout_branch(str(remote), "release", "", repo_path)

    assert actual_sha == release_sha
    assert _git(repo_path, "rev-parse", "HEAD") == release_sha
    assert _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert (repo_path / "contract.txt").read_text(encoding="utf-8") == "release\n"
    assert _git(repo_path, "for-each-ref", "--format=%(refname)", "refs/remotes") == (
        "refs/remotes/origin/dependency-review"
    )
    assert _git(repo_path, "tag", "--list") == ""


def test_git_does_not_fallback_to_default_branch_or_same_named_tag(tmp_path: Path):
    remote, _ = _create_remote_with_release_branch(tmp_path)

    with pytest.raises(DependencyBranchMissingError):
        GitClient().clone_checkout_branch(
            str(remote),
            "release-only-tag",
            "",
            tmp_path / "checkout",
        )
