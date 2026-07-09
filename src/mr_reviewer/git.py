from __future__ import annotations

import base64
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mr_reviewer.process import format_command, prepare_command

LOG = logging.getLogger("mr_reviewer")


class ResourceLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitCheckout:
    target_repo_url: str
    source_repo_url: str
    target_branch: str
    source_branch: str
    base_sha: str | None
    head_sha: str


class GitClient:
    def clone_checkout_and_diff(
            self,
            checkout: GitCheckout,
            token: str,
            work_dir: Path,
            limits: dict[str, int],
    ) -> dict:
        repo_path = work_dir / "repo"
        work_dir.mkdir(parents=True, exist_ok=True)

        git_prefix, env = self._prepare_git_environment(checkout, token)
        self._clone_target_repo(git_prefix, checkout, repo_path, work_dir, env)
        source_remote = self._ensure_source_remote(checkout, repo_path, env)
        self._fetch_review_refs(checkout, repo_path, source_remote, env)
        self._run(["git", "checkout", checkout.head_sha], cwd=repo_path, env=env)

        base_sha = self._resolve_base_sha(checkout, repo_path, env)
        changed_files = self._changed_files(repo_path, base_sha, checkout.head_sha, env)
        self._enforce_changed_file_limit(changed_files, limits["max_files"])

        diff = self._diff(repo_path, base_sha, checkout.head_sha, env)
        self._enforce_diff_line_limit(diff, limits["max_diff_lines"])

        return {
            "repo_path": repo_path,
            "diff": diff,
            "changed_files": changed_files,
            "truncated": False,
            "base_sha": base_sha,
            "head_sha": checkout.head_sha,
        }

    def _prepare_git_environment(self, checkout: GitCheckout, token: str) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        git_prefix = ["git"]
        if not checkout.target_repo_url.startswith("http") or not token:
            return git_prefix, env

        # 禁用 credential helper/GCM 弹窗；token 通过 Git 环境配置传入，避免出现在命令行。
        git_prefix = ["git", "-c", "credential.helper="]
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {self._basic_auth_token(token)}",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
            }
        )
        return git_prefix, env

    def _clone_target_repo(
            self,
            git_prefix: list[str],
            checkout: GitCheckout,
            repo_path: Path,
            work_dir: Path,
            env: dict[str, str],
    ) -> None:
        self._run(
            [*git_prefix, "clone", "--no-checkout", checkout.target_repo_url, str(repo_path)],
            cwd=work_dir,
            env=env,
        )

    def _ensure_source_remote(self, checkout: GitCheckout, repo_path: Path, env: dict[str, str]) -> str:
        if checkout.source_repo_url == checkout.target_repo_url:
            return "origin"

        # fork MR 的 source branch 不一定存在于 target repo，必须额外添加 source remote。
        self._run(["git", "remote", "add", "source", checkout.source_repo_url], cwd=repo_path, env=env)
        return "source"

    def _fetch_review_refs(
            self,
            checkout: GitCheckout,
            repo_path: Path,
            source_remote: str,
            env: dict[str, str],
    ) -> None:
        # 显式 fetch 两个分支后再 checkout head_sha，保证本地 review 目录拥有完整对比上下文。
        self._run(
            ["git", "fetch", "origin", f"{checkout.target_branch}:refs/remotes/origin/{checkout.target_branch}"],
            cwd=repo_path,
            env=env,
        )
        self._run(
            [
                "git",
                "fetch",
                source_remote,
                f"{checkout.source_branch}:refs/remotes/{source_remote}/{checkout.source_branch}",
            ],
            cwd=repo_path,
            env=env,
        )

    def _resolve_base_sha(self, checkout: GitCheckout, repo_path: Path, env: dict[str, str]) -> str:
        if checkout.base_sha is not None:
            return checkout.base_sha

        # Webhook 只提供 head，完整 MR range 需要以 target branch 的 merge-base 为准。
        base_sha = self._run(
            ["git", "merge-base", f"refs/remotes/origin/{checkout.target_branch}", checkout.head_sha],
            cwd=repo_path,
            env=env,
        ).strip()
        if not base_sha:
            raise RuntimeError("git merge-base did not return a base commit")
        return base_sha

    def _changed_files(
            self,
            repo_path: Path,
            base_sha: str,
            head_sha: str,
            env: dict[str, str],
    ) -> list[str]:
        return self._run(
            ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
            cwd=repo_path,
            env=env,
        ).splitlines()

    def _diff(self, repo_path: Path, base_sha: str, head_sha: str, env: dict[str, str]) -> str:
        return self._run(["git", "diff", f"{base_sha}...{head_sha}"], cwd=repo_path, env=env)

    def _enforce_changed_file_limit(self, changed_files: list[str], max_files: int) -> None:
        if len(changed_files) > max_files:
            raise ResourceLimitError(f"changed file count exceeds limit: {len(changed_files)} > {max_files}")

    def _enforce_diff_line_limit(self, diff: str, max_diff_lines: int) -> None:
        line_count = len(diff.splitlines())
        if line_count > max_diff_lines:
            raise ResourceLimitError(f"diff line count exceeds limit: {line_count} > {max_diff_lines}")

    def _run(self, args: list[str], cwd: Path, env: dict[str, str]) -> str:
        LOG.info("stage=git command=%s cwd=%s", _format_command(args), cwd)
        result = subprocess.run(
            prepare_command(args),
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git command failed: {result.stderr.strip()}")
        return result.stdout

    def _basic_auth_token(self, token: str) -> str:
        raw = f"oauth2:{token}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")


def _format_command(args: list[str]) -> str:
    return format_command(prepare_command(args))
