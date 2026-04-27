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
    base_sha: str
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

        env = os.environ.copy()
        git_prefix = ["git"]
        if checkout.target_repo_url.startswith("http") and token:
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

        self._run([*git_prefix, "clone", "--no-checkout", checkout.target_repo_url, str(repo_path)], cwd=work_dir, env=env)
        if checkout.source_repo_url != checkout.target_repo_url:
            self._run(["git", "remote", "add", "source", checkout.source_repo_url], cwd=repo_path, env=env)
            source_remote = "source"
        else:
            source_remote = "origin"

        self._run(["git", "fetch", "origin", checkout.target_branch], cwd=repo_path, env=env)
        self._run(["git", "fetch", source_remote, checkout.source_branch], cwd=repo_path, env=env)
        self._run(["git", "checkout", checkout.head_sha], cwd=repo_path, env=env)

        changed_files = self._run(
            ["git", "diff", "--name-only", f"{checkout.base_sha}...{checkout.head_sha}"],
            cwd=repo_path,
            env=env,
        ).splitlines()
        if len(changed_files) > limits["max_files"]:
            raise ResourceLimitError(f"changed file count exceeds limit: {len(changed_files)} > {limits['max_files']}")

        diff = self._run(["git", "diff", f"{checkout.base_sha}...{checkout.head_sha}"], cwd=repo_path, env=env)
        line_count = len(diff.splitlines())
        if line_count > limits["max_diff_lines"]:
            raise ResourceLimitError(f"diff line count exceeds limit: {line_count} > {limits['max_diff_lines']}")

        return {
            "repo_path": repo_path,
            "diff": diff,
            "changed_files": changed_files,
            "truncated": False,
            "base_sha": checkout.base_sha,
            "head_sha": checkout.head_sha,
        }

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
