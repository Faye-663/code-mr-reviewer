import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert isinstance(opencode.prompts[0][0], str)
    assert "code-review skill" in opencode.prompts[0][0]
    assert "MR URL: https://gitlab.example.com/team/project/merge_requests/7" in opencode.prompts[0][0]
    assert "Base SHA: base123" in opencode.prompts[0][0]
    assert "Head SHA: head456" in opencode.prompts[0][0]
    assert "Changed files:\n- app.py" in opencode.prompts[0][0]
    assert "代码仓在" in opencode.prompts[0][0]
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


def test_review_service_prompt_uses_comment_skill_when_configured(tmp_path: Path):
    opencode = RecordingOpenCodeRunner()
    service = ReviewService(FakeGitLabClient(), RecordingGitClient(), opencode)

    service.review(
        GitLabMrUrl("https://gitlab.example.com", "team/project", 7),
        Config(
            gitlab_base_url="https://gitlab.example.com",
            gitlab_token="secret-token",
            work_dir=tmp_path,
            comment_skill="gitlab-mr-comment",
        ),
        task_id="task-comment-skill",
    )

    prompt = opencode.prompts[0][0]
    assert "gitlab-mr-comment skill" in prompt
    assert "MR URL: https://gitlab.example.com/team/project/merge_requests/7" in prompt
    assert "Base SHA: base123" in prompt
    assert "Head SHA: head456" in prompt
    assert "Changed files:\n- app.py" in prompt
    assert "代码仓在" in prompt


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
        "MR_REVIEWER_WELINK_ONEBOX_SPACE_ID": "space-example",
        "MR_REVIEWER_WELINK_ONEBOX_PARENT_ID": "parent-example",
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
    upload_text = upload_log.read_text(encoding="utf-8")
    assert upload_text.startswith("onebox file-upload")
    assert "--space-id space-example --parent parent-example" in upload_text
    assert "16220079" not in upload_text
    assert " 763 " not in upload_text


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
        welink_group_id="group-example",
        welink_onebox_space_id="space-example",
        welink_onebox_parent_id="parent-example",
    )

    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        _reply(config, "# 报告\n内容", GitLabMrUrl("https://gitlab.example.com", "team/project", 7))

    upload_args, upload_kwargs = calls[0]
    assert upload_args[:5] == ["welink-cli", "onebox", "file-upload", "--space-id", "space-example"]
    assert upload_args[5:7] == ["--parent", "parent-example"]
    assert upload_kwargs["encoding"] == "utf-8"
    assert upload_kwargs["errors"] == "replace"

    args, kwargs = calls[1]
    assert args[-4:-2] == ["--group-id", "group-example"]
    assert args[-2] == "--text"
    assert "代码审查报告已上传到 WeLink OneBox" in args[-1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage=im_send group_id=group-example" in log_text
    assert "# 报告" not in log_text


def test_welink_reply_warns_group_when_onebox_upload_fails(monkeypatch, caplog):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            stdout = ""

        result = Result()
        if len(calls) == 1:
            result.returncode = 1
            result.stderr = "parent not found"
        else:
            result.returncode = 0
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", fake_run)
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        im_reply_command="welink-cli im send-to-group",
        welink_group_id="group-example",
        welink_onebox_space_id="space-example",
        welink_onebox_parent_id="missing-parent",
    )

    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        _reply(config, "# 报告\n内容", GitLabMrUrl("https://gitlab.example.com", "team/project", 7))

    assert len(calls) == 2
    args, kwargs = calls[1]
    assert args[-4:-2] == ["--group-id", "group-example"]
    assert args[-2] == "--text"
    assert "报告已生成" in args[-1]
    assert "OneBox 上传失败" in args[-1]
    assert "space-id/parent" in args[-1]
    assert "# 报告" not in args[-1]
    assert kwargs["encoding"] == "utf-8"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "stage=file_upload_result returncode=1" in log_text
    assert "parent not found" in log_text


def test_welink_reply_warns_group_when_onebox_config_missing(monkeypatch):
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
        welink_group_id="group-example",
    )

    _reply(config, "# 报告\n内容", GitLabMrUrl("https://gitlab.example.com", "team/project", 7))

    assert len(calls) == 1
    args, _ = calls[0]
    assert args[-2] == "--text"
    assert "OneBox 上传失败" in args[-1]
    assert "space-id/parent" in args[-1]


def test_welink_reply_still_fails_when_group_notification_fails(monkeypatch):
    def fake_run(args, **kwargs):
        class Result:
            returncode = 1 if "send-to-group" in args else 0
            stdout = ""
            stderr = "send failed"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        im_reply_command="welink-cli im send-to-group",
        welink_group_id="group-example",
        welink_onebox_space_id="space-example",
        welink_onebox_parent_id="parent-example",
    )

    try:
        _reply(config, "# 报告\n内容", GitLabMrUrl("https://gitlab.example.com", "team/project", 7))
    except RuntimeError as exc:
        assert "IM reply command failed" in str(exc)
    else:
        raise AssertionError("expected IM reply failure")


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
        welink_group_id="group-example",
    )

    assert _poll_messages(config) == []

    args, kwargs = calls[0]
    assert args[-2:] == ["--group-id", "group-example"]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_healthcheck_requires_welink_group_id(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda command: f"C:/bin/{command}")
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="token",
        im_poll_command="welink-cli im query-history-message --query-count 20",
        im_reply_command="welink-cli im send-to-group",
        welink_group_id="group-example",
        welink_onebox_space_id="space-example",
        welink_onebox_parent_id="parent-example",
    )

    assert healthcheck(config) == 0
    output = capsys.readouterr().out
    assert "welink_group_id: ok" in output
    assert "webhook_post_comment: enabled" in output
    assert "missing for webhook" not in output

    config.welink_group_id = ""
    assert healthcheck(config) == 1
    assert "welink_group_id: missing" in capsys.readouterr().out

    config.welink_group_id = "group-example"
    config.welink_onebox_parent_id = ""
    assert healthcheck(config) == 1
    assert "welink_onebox_parent_id: missing" in capsys.readouterr().out


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


def test_opencode_runner_writes_diagnostics(monkeypatch, tmp_path: Path, caplog):
    def fake_run(args, **kwargs):
        class Result:
            returncode = 0
            stderr = "debug logs\n"
            stdout = "# Review\n"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda command: "C:\\bin\\opencode.exe" if command == "opencode" else None)
    monkeypatch.setenv("OPENCODE_TEST_FLAG", "enabled")
    prompt = (
        "MR: https://gitlab.example.com/team/project/merge_requests/7\n"
        "Base SHA: base123\n"
        "Head SHA: head456\n"
        "Changed files: app.py\n"
    )
    diagnostic_root = tmp_path / "diagnostics"

    with caplog.at_level(logging.INFO, logger="mr_reviewer"):
        output = OpenCodeRunner("opencode", debug=True, diagnostic_dir=diagnostic_root).run_review(
            prompt,
            tmp_path,
            60,
        )

    assert output == "# Review"
    diagnostic_path = next(diagnostic_root.iterdir())
    assert diagnostic_path.joinpath("prompt.md").read_text(encoding="utf-8") == prompt
    assert diagnostic_path.joinpath("cwd.txt").read_text(encoding="utf-8") == str(tmp_path)
    command_text = diagnostic_path.joinpath("command.txt").read_text(encoding="utf-8")
    assert "opencode --print-logs --log-level DEBUG run" in command_text
    assert "<prompt_chars=" in command_text
    assert "sha256=" in command_text
    assert "https://gitlab.example.com" not in command_text
    env_summary = json.loads(diagnostic_path.joinpath("env-summary.json").read_text(encoding="utf-8"))
    assert env_summary["debug"] is True
    assert env_summary["resolved_executable"] == "C:\\bin\\opencode.exe"
    assert "OPENCODE_TEST_FLAG" in env_summary["related_env_names"]
    assert diagnostic_path.joinpath("stdout.md").read_text(encoding="utf-8") == "# Review\n"
    assert diagnostic_path.joinpath("stderr.log").read_text(encoding="utf-8") == "debug logs\n"
    assert diagnostic_path.joinpath("returncode.txt").read_text(encoding="utf-8") == "0"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "mr_url_present=True" in log_text
    assert "prompt_sha256=" in log_text
    assert "diagnostic_path=" in log_text
    assert "https://gitlab.example.com" not in log_text


def test_opencode_runner_can_send_prompt_as_file(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stderr = ""
            stdout = "# Review\n"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    prompt = (
        "MR: https://gitlab.example.com/team/project/merge_requests/7\n"
        "Base SHA: base123\n"
        "Head SHA: head456\n"
    )
    diagnostic_root = tmp_path / "diagnostics"

    output = OpenCodeRunner(
        "opencode",
        debug=True,
        diagnostic_dir=diagnostic_root,
        prompt_transport="file",
    ).run_review(prompt, tmp_path, 60)

    args, kwargs = calls[0]
    assert output == "# Review"
    assert args[:5] == ["opencode", "--print-logs", "--log-level", "DEBUG", "run"]
    assert args[5] == "--file"
    prompt_file = Path(args[6])
    assert prompt_file.name == "prompt.md"
    assert prompt_file.read_text(encoding="utf-8") == prompt
    assert "https://gitlab.example.com" not in args
    assert "请读取附件 prompt.md" in args[7]
    assert kwargs["cwd"] == tmp_path
    command_text = prompt_file.parent.joinpath("command.txt").read_text(encoding="utf-8")
    assert "--file" in command_text
    assert "prompt.md" in command_text
    assert "https://gitlab.example.com" not in command_text


def test_opencode_runner_rejects_unknown_prompt_transport():
    with pytest.raises(ValueError, match="unsupported opencode prompt transport: clipboard"):
        OpenCodeRunner("opencode", prompt_transport="clipboard")
