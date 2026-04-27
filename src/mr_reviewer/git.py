from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class ResourceLimitError(RuntimeError):
    pass


class GitClient:
    def clone_checkout_and_diff(
        self,
        repo_url: str,
        token: str,
        base_sha: str,
        head_sha: str,
        work_dir: Path,
        limits: dict[str, int],
    ) -> dict:
        repo_path = work_dir / "repo"
        work_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        git_prefix = ["git"]
        if repo_url.startswith("http") and token:
            askpass = self._write_askpass(work_dir)
            # Windows Git Credential Manager 会弹窗；这里对本次命令禁用 helper，并用 askpass 非交互注入 token。
            git_prefix = ["git", "-c", "credential.helper=", "-c", f"core.askPass={askpass}"]
            env.update(
                {
                    "GIT_ASKPASS": str(askpass),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "never",
                    "GIT_USERNAME": "oauth2",
                    "GIT_PASSWORD": token,
                }
            )

        self._run([*git_prefix, "clone", "--no-checkout", repo_url, str(repo_path)], cwd=work_dir, env=env)
        self._run(["git", "checkout", head_sha], cwd=repo_path, env=env)

        changed_files = self._run(
            ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
            cwd=repo_path,
            env=env,
        ).splitlines()
        if len(changed_files) > limits["max_files"]:
            raise ResourceLimitError(f"changed file count exceeds limit: {len(changed_files)} > {limits['max_files']}")

        diff = self._run(["git", "diff", f"{base_sha}...{head_sha}"], cwd=repo_path, env=env)
        line_count = len(diff.splitlines())
        if line_count > limits["max_diff_lines"]:
            raise ResourceLimitError(f"diff line count exceeds limit: {line_count} > {limits['max_diff_lines']}")

        return {
            "repo_path": repo_path,
            "diff": diff,
            "changed_files": changed_files,
            "truncated": False,
            "base_sha": base_sha,
            "head_sha": head_sha,
        }

    def _run(self, args: list[str], cwd: Path, env: dict[str, str]) -> str:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"git command failed: {result.stderr.strip()}")
        return result.stdout

    def _write_askpass(self, work_dir: Path) -> Path:
        askpass_py = work_dir / "git_askpass.py"
        askpass_cmd = work_dir / "git_askpass.cmd"
        askpass_py.write_text(
            "import os, sys\n"
            "prompt = ' '.join(sys.argv[1:]).lower()\n"
            "if 'username' in prompt or 'user name' in prompt:\n"
            "    print(os.environ.get('GIT_USERNAME', 'oauth2'))\n"
            "else:\n"
            "    print(os.environ.get('GIT_PASSWORD', ''))\n",
            encoding="utf-8",
        )
        askpass_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "{askpass_py}" "%*"\r\n',
            encoding="utf-8",
        )
        return askpass_cmd
