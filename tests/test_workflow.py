import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from mr_reviewer.config import Config
from mr_reviewer.cli import _poll_messages, _reply, healthcheck
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabMrUrl
from mr_reviewer.im import ImMessage
from mr_reviewer.opencode import OpenCodeRunner
from mr_reviewer.reviewer import ReviewService


class FakeGitLabClient:
    def get_merge_request(self, mr: GitLabMrUrl):
        return {
            "web_url": "https://gitlab.example.com/team/project/merge_requests/7",
            "title": "Fix auth",
            "source_branch": "feature/auth",
            "target_branch": "main",
            "source_project_id": 1,
            "target_project_id": 1,
            "diff_refs": {"base_sha": "base123", "head_sha": "head456"},
        }

    def get_project_http_url(self, project_id: int):
        if project_id == 2:
            return "https://gitlab.example.com/fork/project.git"
        return "https://gitlab.example.com/team/project.git"


class RecordingGitClient(GitClient):
    def __init__(self):
        self.calls = []

    def clone_checkout_and_diff(self, checkout, token, work_dir, limits):
        self.calls.append((checkout, token, Path(work_dir), limits))
        repo = Path(work_dir) / "repo"
        repo.mkdir(parents=True)
        return {
            "repo_path": repo,
            "diff": "diff --git a/app.py b/app.py\n@@\n-print('bad')\n+print('good')\n",
            "changed_files": ["app.py"],
            "truncated": False,
            "base_sha": checkout.base_sha,
            "head_sha": checkout.head_sha,
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
    assert "code-review" in opencode.prompts[0][0]
    assert "检视范围：feature/auth 到 main 的差异" in opencode.prompts[0][0]
    assert "代码仓在" in opencode.prompts[0][0]
    assert "git diff base123...head456" in opencode.prompts[0][0]
    assert "Changed files 是审查入口" in opencode.prompts[0][0]
    assert "不要使用 git diff --staged、裸 git diff 或 git log -5" in opencode.prompts[0][0]
    assert "diff --git" not in opencode.prompts[0][0]
    assert "Diff:" not in opencode.prompts[0][0]
    checkout, token, work_dir, limits = git.calls[0]
    assert checkout.target_repo_url == "https://gitlab.example.com/team/project.git"
    assert checkout.source_repo_url == "https://gitlab.example.com/team/project.git"
    assert checkout.target_branch == "main"
    assert checkout.source_branch == "feature/auth"
    assert (token, work_dir, limits) == ("secret-token", tmp_path / "task-1", {"max_files": 50, "max_diff_lines": 2000})
    assert not (tmp_path / "task-1").exists()


def test_review_service_uses_source_project_repo_for_fork_mr(tmp_path: Path):
    class ForkGitLabClient(FakeGitLabClient):
        def get_merge_request(self, mr: GitLabMrUrl):
            data = super().get_merge_request(mr)
            data["source_project_id"] = 2
            return data

    git = RecordingGitClient()
    service = ReviewService(ForkGitLabClient(), git, RecordingOpenCodeRunner())

    service.review(
        GitLabMrUrl("https://gitlab.example.com", "team/project", 7),
        Config(gitlab_base_url="https://gitlab.example.com", gitlab_token="secret-token", work_dir=tmp_path),
        task_id="task-fork",
    )

    checkout = git.calls[0][0]
    assert checkout.target_repo_url == "https://gitlab.example.com/team/project.git"
    assert checkout.source_repo_url == "https://gitlab.example.com/fork/project.git"


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
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    welink_cli = bin_dir / "welink-cli.cmd"
    upload_log = tmp_path / "upload.log"
    welink_cli.write_text(
        "@echo off\r\n"
        f"echo %* > \"{upload_log}\"\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "branch", "main"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"], check=True, stdout=subprocess.DEVNULL)
    (repo / "app.py").write_text("print('head')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "head"], check=True, stdout=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    poll_script.write_text(
        "import json\n"
        "print(json.dumps({'resultCode':'0','respData':{'chatInfo':[{'msgId':1,'groupId':'c1','sender':'u1','content':'@ReviewBot https://gitlab.example.com/team/project/merge_requests/7','serverSendTime':'now','at':True,'atAccountList':['bot001']} ]}}))\n",
        encoding="utf-8",
    )
    opencode_script.write_text("print('# Review\\n\\nNo high-confidence issues.')\n", encoding="utf-8")
    gitlab_file.write_text(
        json.dumps(
            {
                "/api/v4/projects/team%2Fproject/merge_requests/7": {
                    "web_url": "https://gitlab.example.com/team/project/merge_requests/7",
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
        "MR_REVIEWER_WELINK_GROUP_ID": "configured-group",
        "MR_REVIEWER_BOT_MENTION": "@ReviewBot",
        "MR_REVIEWER_BOT_ACCOUNT": "bot001",
        "MR_REVIEWER_WORK_DIR": str(tmp_path / "work"),
        "MR_REVIEWER_STATE_PATH": str(tmp_path / "state.json"),
        "MR_REVIEWER_OPENCODE_COMMAND": f"{sys.executable} {opencode_script}",
        "MR_REVIEWER_TEST_GITLAB_RESPONSES": str(gitlab_file),
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
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
    assert reply_args[:2] == ["--group-id", "configured-group"]
    assert reply_args[2] == "--text"
    assert "代码审查报告已上传到 WeLink OneBox" in reply_args[3]
    assert upload_log.read_text(encoding="utf-8").startswith("onebox file-upload")


def test_welink_reply_uses_utf8_and_redacts_text_in_logs(monkeypatch, caplog):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        im_reply_command="welink-cli im send-to-group",
        welink_group_id="619850427",
    )

    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        _reply(config, "# 报告\n内容", GitLabMrUrl("https://gitlab.example.com", "team/project", 7))

    upload_args, upload_kwargs = calls[0]
    assert upload_kwargs["encoding"] == "utf-8"
    assert upload_kwargs["errors"] == "replace"
    assert upload_kwargs["shell"] is True

    args, kwargs = calls[1]
    assert args[-4:-2] == ["--group-id", "619850427"]
    assert args[-2] == "--text"
    assert "代码审查报告已上传到 WeLink OneBox" in args[-1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage=im_send group_id=619850427" in log_text
    assert "# 报告" not in log_text


def test_poll_messages_appends_configured_group_id(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stdout = "[]"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        im_poll_command="welink-cli im query-history-message --query-count 20",
        welink_group_id="619850427",
    )

    assert _poll_messages(config) == []

    args, kwargs = calls[0]
    assert args[-2:] == ["--group-id", "619850427"]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_healthcheck_requires_welink_group_id(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda command: f"C:/bin/{command}")
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="token",
        im_poll_command="welink-cli im query-history-message --query-count 20",
        im_reply_command="welink-cli im send-to-group",
        welink_group_id="619850427",
    )

    assert healthcheck(config) == 0
    assert "welink_group_id: ok" in capsys.readouterr().out

    config.welink_group_id = ""
    assert healthcheck(config) == 1
    assert "welink_group_id: missing" in capsys.readouterr().out


def test_opencode_runner_uses_utf8_and_redacts_prompt_in_logs(monkeypatch, tmp_path: Path, caplog):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stderr = ""
            stdout = "# Review\n"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        output = OpenCodeRunner("opencode", debug=True).run_review("请 review 这段 diff", tmp_path, 60)

    args, kwargs = calls[0]
    assert args == ["opencode", "--print-logs", "--log-level", "DEBUG", "run", "请 review 这段 diff"]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert output == "# Review"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "opencode --print-logs --log-level DEBUG run <prompt_chars=16>" in log_text
    assert "请 review" not in log_text
