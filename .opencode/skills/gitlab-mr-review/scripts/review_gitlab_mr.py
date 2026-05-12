from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple


class MrUrl(NamedTuple):
    base_url: str
    project_path: str
    mr_iid: int


class Config(NamedTuple):
    gitlab_base_url: str
    gitlab_token: str
    opencode_command: str
    work_dir: Path
    submit_comment: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review a GitLab MR with opencode and post the report as an MR comment.")
    parser.add_argument("mr_url", help="GitLab MR URL, for example https://gitlab.example.com/team/project/merge_requests/7")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        result = review_gitlab_mr(args.mr_url, config)
    except Exception as exc:
        token = os.environ.get("GITLAB_TOKEN", "")
        print(f"error: {redact(str(exc), token)}", file=sys.stderr)
        return 1

    print(f"report_path={result['report_path']}")
    print(f"base_sha={result['base_sha']}")
    print(f"head_sha={result['head_sha']}")
    print(f"changed_files={result['changed_files_count']}")
    print(f"comment_submitted={str(result['comment_submitted']).lower()}")
    return 0


def load_config() -> Config:
    base_url = os.environ.get("GITLAB_BASE_URL", "").strip()
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not base_url:
        raise ValueError("GITLAB_BASE_URL is required")
    if not token:
        raise ValueError("GITLAB_TOKEN is required")

    work_dir = Path(os.environ.get("MR_REVIEW_WORK_DIR") or Path(tempfile.gettempdir()) / "gitlab-mr-review")
    return Config(
        gitlab_base_url=normalize_base_url(base_url),
        gitlab_token=token,
        opencode_command=os.environ.get("OPENCODE_COMMAND", "opencode"),
        work_dir=work_dir,
        submit_comment=_parse_bool(os.environ.get("MR_REVIEW_SUBMIT_COMMENT", "true")),
    )


def review_gitlab_mr(mr_url: str, config: Config) -> dict[str, object]:
    mr = parse_mr_url(mr_url, config.gitlab_base_url)
    client = GitLabApi(config.gitlab_base_url, config.gitlab_token)
    metadata = client.get_json(mr_api_path(mr.project_path, mr.mr_iid))
    base_sha, head_sha = choose_diff_refs(metadata)
    target_repo_url = client.get_project_http_url(int(metadata["target_project_id"]))
    source_repo_url = client.get_project_http_url(int(metadata.get("source_project_id") or metadata["target_project_id"]))

    task_dir = config.work_dir / f"mr-{mr.mr_iid}-{int(time.time())}"
    repo_path = task_dir / "repo"
    report_path = task_dir / "review-report.md"
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        clone_checkout(
            target_repo_url=target_repo_url,
            source_repo_url=source_repo_url,
            target_branch=metadata["target_branch"],
            source_branch=metadata["source_branch"],
            base_sha=base_sha,
            head_sha=head_sha,
            repo_path=repo_path,
            token=config.gitlab_token,
        )
        changed_files = git_output(["git", "diff", "--name-only", f"{base_sha}...{head_sha}"], repo_path).splitlines()
        prompt = build_review_prompt(
            mr_url=f"{mr.base_url}/{mr.project_path}/merge_requests/{mr.mr_iid}",
            base_sha=base_sha,
            head_sha=head_sha,
            changed_files=changed_files,
            repo_path=repo_path,
        )
        report = run_opencode_review(config.opencode_command, prompt, repo_path)
        report_path.write_text(report, encoding="utf-8")
        comment_submitted = False
        if config.submit_comment:
            client.post_form(mr_note_api_path(mr.project_path, mr.mr_iid), {"body": report})
            comment_submitted = True
        return {
            "report_path": str(report_path),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files_count": len(changed_files),
            "comment_submitted": comment_submitted,
        }
    except Exception as exc:
        raise RuntimeError(redact(str(exc), config.gitlab_token)) from exc


def parse_mr_url(url: str, base_url: str) -> MrUrl:
    parsed = urllib.parse.urlparse(url)
    base = urllib.parse.urlparse(normalize_base_url(base_url))
    if (parsed.scheme, parsed.netloc.lower()) != (base.scheme, base.netloc.lower()):
        raise ValueError("GitLab host does not match GITLAB_BASE_URL")

    marker = "/merge_requests/"
    if marker not in parsed.path:
        raise ValueError("URL is not a GitLab merge request URL")
    project_part, iid_part = parsed.path.split(marker, 1)
    project_path = urllib.parse.unquote(project_part.strip("/"))
    iid = iid_part.strip("/").split("/", 1)[0]
    if not project_path or not iid.isdigit():
        raise ValueError("GitLab MR URL is missing project path or MR IID")
    return MrUrl(normalize_base_url(base_url), project_path, int(iid))


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def mr_api_path(project_path: str, mr_iid: int) -> str:
    project = urllib.parse.quote(project_path, safe="")
    return f"/api/v4/projects/{project}/merge_requests/{mr_iid}"


def mr_note_api_path(project_path: str, mr_iid: int) -> str:
    return f"{mr_api_path(project_path, mr_iid)}/notes"


def choose_diff_refs(metadata: dict[str, object]) -> tuple[str, str]:
    diff_refs = metadata.get("diff_refs")
    if not isinstance(diff_refs, dict):
        diff_refs = {}
    base_sha = diff_refs.get("base_sha") or diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha") or metadata.get("sha")
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        raise ValueError("GitLab MR response does not include usable diff refs")
    return base_sha, head_sha


def build_review_prompt(
    *,
    mr_url: str,
    base_sha: str,
    head_sha: str,
    changed_files: list[str],
    repo_path: Path,
) -> str:
    files = "\n".join(f"- {path}" for path in changed_files) or "- <none>"
    return (
        "使用 code-review skill 检视 GitLab MR。\n"
        f"MR URL: {mr_url}\n"
        f"Base SHA: {base_sha}\n"
        f"Head SHA: {head_sha}\n"
        "Changed files:\n"
        f"{files}\n"
        f"代码仓在 {repo_path} 目录。\n"
        "只审查 Base SHA 到 Head SHA 的 MR range，不要按本地未提交变更审查。"
    )


def clone_checkout(
    *,
    target_repo_url: str,
    source_repo_url: str,
    target_branch: str,
    source_branch: str,
    base_sha: str,
    head_sha: str,
    repo_path: Path,
    token: str,
) -> None:
    env = git_env(token)
    work_dir = repo_path.parent
    # token 通过 Git extraHeader 进入环境，避免出现在命令行和日志里。
    git_run(["git", "-c", "credential.helper=", "clone", "--no-checkout", target_repo_url, str(repo_path)], work_dir, env, token)
    source_remote = "origin"
    if source_repo_url != target_repo_url:
        git_run(["git", "remote", "add", "source", source_repo_url], repo_path, env, token)
        source_remote = "source"
    git_run(["git", "fetch", "origin", target_branch], repo_path, env, token)
    git_run(["git", "fetch", source_remote, source_branch], repo_path, env, token)
    git_run(["git", "checkout", head_sha], repo_path, env, token)
    git_run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], repo_path, env, token)


def git_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_auth_token(token)}",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    return env


def basic_auth_token(token: str) -> str:
    return base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")


def git_run(args: list[str], cwd: Path, env: dict[str, str], token: str) -> None:
    git_output(args, cwd, env=env, token=token)


def git_output(args: list[str], cwd: Path, env: dict[str, str] | None = None, token: str = "") -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git command failed: {redact(result.stderr.strip(), token)}")
    return result.stdout


def run_opencode_review(command: str, prompt: str, repo_path: Path) -> str:
    args = shlex.split(command, posix=(os.name != "nt")) + ["run", prompt]
    result = subprocess.run(
        prepare_command(args),
        cwd=repo_path,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"opencode run failed: {result.stderr.strip()}")
    return result.stdout.strip()


def prepare_command(args: list[str]) -> list[str]:
    if os.name != "nt" or not args:
        return args
    executable = shutil.which(args[0])
    if not executable:
        return args
    if Path(executable).suffix.lower() not in {".bat", ".cmd"}:
        return args
    # Windows CreateProcess 直接运行批处理文件不稳定，call 能兼容带空格路径。
    return ["cmd.exe", "/d", "/c", "call", executable, *args[1:]]


class GitLabApi:
    def __init__(self, base_url: str, token: str):
        self.base_url = normalize_base_url(base_url)
        self.token = token

    def get_json(self, path: str) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json"},
        )
        return self._open_json(request)

    def get_project_http_url(self, project_id: int) -> str:
        project = self.get_json(f"/api/v4/projects/{project_id}")
        repo_url = project.get("http_url_to_repo")
        if not isinstance(repo_url, str) or not repo_url:
            raise ValueError("GitLab project response does not include http_url_to_repo")
        return repo_url

    def post_form(self, path: str, fields: dict[str, str]) -> dict[str, object]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "PRIVATE-TOKEN": self.token,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            },
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitLab API request failed: HTTP {exc.code}") from exc


def redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "<redacted>")
        text = text.replace(basic_auth_token(token), "<redacted>")
    return text


def _parse_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


if __name__ == "__main__":
    raise SystemExit(main())
