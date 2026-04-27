import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabMrUrl
from mr_reviewer.im import ImMessage
from mr_reviewer.opencode import OpenCodeRunner
from mr_reviewer.reviewer import ReviewService


class FakeGitLabClient:
    def get_merge_request(self, mr: GitLabMrUrl):
        return {
            "web_url": "https://gitlab.example.com/team/project/-/merge_requests/7",
            "title": "Fix auth",
            "source_branch": "feature/auth",
            "target_branch": "main",
            "source_project_id": 1,
            "target_project_id": 1,
            "diff_refs": {"base_sha": "base123", "head_sha": "head456"},
        }

    def get_project_http_url(self, project_id: int):
        return "https://gitlab.example.com/team/project.git"


class RecordingGitClient(GitClient):
    def __init__(self):
        self.calls = []

    def clone_checkout_and_diff(self, repo_url, token, base_sha, head_sha, work_dir, limits):
        self.calls.append((repo_url, token, base_sha, head_sha, Path(work_dir), limits))
        repo = Path(work_dir) / "repo"
        repo.mkdir(parents=True)
        return {
            "repo_path": repo,
            "diff": "diff --git a/app.py b/app.py\n@@\n-print('bad')\n+print('good')\n",
            "changed_files": ["app.py"],
            "truncated": False,
            "base_sha": base_sha,
            "head_sha": head_sha,
        }


class RecordingOpenCodeRunner(OpenCodeRunner):
    def __init__(self):
        self.prompts = []

    def run_review(self, prompt, cwd, timeout_seconds):
        self.prompts.append((prompt, Path(cwd), timeout_seconds))
        return "# Review\n\nNo high-confidence issues."


def test_review_service_generates_markdown_and_cleans_workdir(tmp_path: Path):
    git = RecordingGitClient()
    opencode = RecordingOpenCodeRunner()
    service = ReviewService(FakeGitLabClient(), git, opencode)
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        work_dir=tmp_path,
        max_files=50,
        max_diff_lines=2000,
        task_timeout_seconds=900,
    )

    report = service.review(
        GitLabMrUrl("https://gitlab.example.com", "team/project", 7),
        config,
        task_id="task-1",
    )

    assert report.markdown.startswith("# Review")
    assert "secret-token" not in opencode.prompts[0][0]
    assert "mr-review" in opencode.prompts[0][0]
    assert git.calls[0][2:] == ("base123", "head456", tmp_path / "task-1", {"max_files": 50, "max_diff_lines": 2000})
    assert not (tmp_path / "task-1").exists()


def test_review_service_logs_major_stages(tmp_path: Path, caplog):
    service = ReviewService(FakeGitLabClient(), RecordingGitClient(), RecordingOpenCodeRunner())
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="secret-token",
        work_dir=tmp_path,
    )

    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        service.review(GitLabMrUrl("https://gitlab.example.com", "team/project", 7), config, task_id="task-log")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage=gitlab_fetch" in log_text
    assert "stage=diff_ready" in log_text
    assert "stage=opencode_review" in log_text
    assert "stage=cleanup" in log_text
    assert "secret-token" not in log_text


def test_poll_once_runs_review_and_replies(tmp_path: Path):
    poll_script = tmp_path / "poll.py"
    reply_file = tmp_path / "reply.json"
    opencode_script = tmp_path / "opencode.py"
    gitlab_file = tmp_path / "gitlab.json"
    repo = tmp_path / "origin"

    subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "app.py").write_text("print('head')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "head"], check=True, stdout=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    poll_script.write_text(
        "import json\n"
        "print(json.dumps({'resultCode':'0','respData':{'chatInfo':[{'msgId':1,'groupId':'c1','sender':'u1','content':'@ReviewBot https://gitlab.example.com/team/project/-/merge_requests/7','serverSendTime':'now','at':True,'atAccountList':['bot001']} ]}}))\n",
        encoding="utf-8",
    )
    opencode_script.write_text("print('# Review\\n\\nNo high-confidence issues.')\n", encoding="utf-8")
    gitlab_file.write_text(
        json.dumps(
            {
                "/api/v4/projects/team%2Fproject/merge_requests/7": {
                    "web_url": "https://gitlab.example.com/team/project/-/merge_requests/7",
                    "title": "MR",
                    "source_branch": "feature",
                    "target_branch": "main",
                    "source_project_id": 1,
                    "target_project_id": 1,
                    "diff_refs": {"base_sha": base, "head_sha": head},
                },
                "/api/v4/projects/1": {"http_url_to_repo": str(repo)},
            }
        ),
        encoding="utf-8",
    )

    reply_script = tmp_path / "reply.py"
    reply_script.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:], ensure_ascii=False), encoding='utf-8')\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update({
        "MR_REVIEWER_GITLAB_BASE_URL": "https://gitlab.example.com",
        "MR_REVIEWER_GITLAB_TOKEN": "token",
        "MR_REVIEWER_IM_POLL_COMMAND": f"{sys.executable} {poll_script}",
        "MR_REVIEWER_IM_REPLY_COMMAND": f"{sys.executable} {reply_script} {reply_file}",
        "MR_REVIEWER_BOT_MENTION": "@ReviewBot",
        "MR_REVIEWER_BOT_ACCOUNT": "bot001",
        "MR_REVIEWER_WORK_DIR": str(tmp_path / "work"),
        "MR_REVIEWER_STATE_PATH": str(tmp_path / "state.json"),
        "MR_REVIEWER_OPENCODE_COMMAND": f"{sys.executable} {opencode_script}",
        "MR_REVIEWER_TEST_GITLAB_RESPONSES": str(gitlab_file),
    })

    result = subprocess.run(
        [sys.executable, "-m", "mr_reviewer.cli", "poll", "--once"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert "success" in result.stderr
    reply_args = json.loads(reply_file.read_text(encoding="utf-8"))
    assert reply_args[:2] == ["--group-id", "c1"]
    assert reply_args[2] == "--text"
    assert reply_args[3].startswith("# Review")
