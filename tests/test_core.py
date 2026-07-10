import json
import importlib.util
import urllib.parse
from pathlib import Path

import pytest

from mr_reviewer.config import Config
from mr_reviewer.git import GitCheckout, GitClient, ResourceLimitError
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, choose_diff_refs, parse_gitlab_mr_url
from mr_reviewer.im import ImMessage, build_welink_reply_args, parse_poll_output, should_trigger_review
from mr_reviewer.observability import task_context
from mr_reviewer.process import prepare_command
from mr_reviewer.state import StateStore


def _load_gitlab_mr_review_script():
    path = Path(".skill/gitlab-mr-review/scripts/review_gitlab_mr.py")
    spec = importlib.util.spec_from_file_location("gitlab_mr_review_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_code_review_skill_targets_gitlab_mr_range():
    skill = Path(".skill/code-review/SKILL.md").read_text(encoding="utf-8")

    assert 'description: "Use when reviewing GitLab merge requests' in skill
    assert "Output: strict JSON" in skill
    assert "Base SHA" in skill
    assert "Head SHA" in skill
    assert "Changed files" in skill
    assert "git diff <base_sha>...<head_sha>" in skill
    assert "git diff --staged" not in skill
    assert "`git diff`" not in skill
    assert "git log --oneline -5" not in skill
    assert "HIGH 问题可以谨慎合并" not in skill
    assert "只有 HIGH 问题" not in skill
    assert '"findings"' in skill
    assert '"severity"' in skill
    assert "suggestion" in skill
    assert "minjor" in skill
    assert "major" in skill
    assert "fatal" in skill
    assert "JSDoc" not in skill
    assert "格式不一致" not in skill
    assert "src/api/client.ts" not in skill
    assert "const apiKey" not in skill
    assert "当可能且合适时" in skill
    assert "错误做法" in skill
    assert "正确做法" in skill


def test_gitlab_mr_review_skill_package_exists():
    skill_path = Path(".skill/gitlab-mr-review/SKILL.md")
    script_path = Path(".skill/gitlab-mr-review/scripts/review_gitlab_mr.py")

    skill = skill_path.read_text(encoding="utf-8")

    assert 'name: gitlab-mr-review' in skill
    assert 'description: "' in skill
    assert "review_gitlab_mr.py" in skill
    assert "GITLAB_BASE_URL" in skill
    assert "GITLAB_API_BASE_URL" in skill
    assert "GITLAB_TOKEN" in skill
    assert "MR_REVIEWER_AGENT_TYPE" in skill
    assert "MR_REVIEWER_AGENT_COMMAND" in skill
    assert "code-review skill" in skill
    assert script_path.exists()
    assert not Path(".opencode").exists()


def test_readme_documents_optional_agent_skill_usage():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "Agent skill 直接使用" in readme
    assert ".skill/gitlab-mr-review" in readme
    assert "gitlab-mr-review skill" in readme
    assert "不替代现有 WeLink 自动轮询模式" in readme
    assert "MR_REVIEW_SUBMIT_COMMENT=false" in readme
    assert "MR_REVIEWER_GITLAB_API_BASE_URL" in readme
    assert "provider/model" in readme


def test_gitlab_mr_review_script_parses_mr_url():
    script = _load_gitlab_mr_review_script()

    parsed = script.parse_mr_url(
        "https://gitlab.example.com/team/project/merge_requests/7",
        "https://gitlab.example.com",
    )

    assert parsed.project_path == "team/project"
    assert parsed.mr_iid == 7


def test_gitlab_mr_review_script_builds_gitlab_api_paths():
    script = _load_gitlab_mr_review_script()

    assert script.mr_api_path("team/project", 7) == "/projects/team%2Fproject/merge_requests/7"
    assert script.mr_note_api_path("team/project", 7) == "/projects/team%2Fproject/merge_requests/7/notes"


def test_gitlab_mr_review_script_reads_independent_api_base_url(monkeypatch):
    script = _load_gitlab_mr_review_script()
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_API_BASE_URL", "https://api.example.com/api/api/v4")
    monkeypatch.setenv("GITLAB_TOKEN", "secret-token")

    config = script.load_config()

    assert config.gitlab_base_url == "https://gitlab.example.com"
    assert config.gitlab_api_base_url == "https://api.example.com/api/api/v4"


def test_gitlab_mr_review_script_builds_scoped_opencode_prompt(tmp_path: Path):
    script = _load_gitlab_mr_review_script()

    prompt = script.build_review_prompt(
        mr_url="https://gitlab.example.com/team/project/merge_requests/7",
        base_sha="base123",
        head_sha="head456",
        changed_files=["src/App.java", "src/AppTest.java"],
        repo_path=tmp_path,
    )

    assert "code-review skill" in prompt
    assert "Base SHA: base123" in prompt
    assert "Head SHA: head456" in prompt
    assert "Changed files:" in prompt
    assert "src/App.java" in prompt
    assert str(tmp_path) in prompt
    assert "diff --git" not in prompt
    assert "Diff:" not in prompt


def test_gitlab_mr_review_script_redacts_token_from_logs():
    script = _load_gitlab_mr_review_script()

    text = script.redact("clone failed for secret-token", "secret-token")

    assert "secret-token" not in text
    assert "<redacted>" in text


def test_gitlab_mr_review_script_wraps_windows_cmd_opencode(monkeypatch):
    script = _load_gitlab_mr_review_script()
    monkeypatch.setattr(script.os, "name", "nt")
    monkeypatch.setattr(script.shutil, "which", lambda command: "D:\\Program Files\\nodejs\\opencode.CMD")

    prepared = script.prepare_command(["opencode", "run", "prompt"])

    assert prepared[:4] == ["cmd.exe", "/d", "/c", "call"]
    assert prepared[4] == "D:\\Program Files\\nodejs\\opencode.CMD"
    assert prepared[5:] == ["run", "prompt"]


def test_gitlab_mr_review_script_sends_opencode_prompt_as_file(monkeypatch, tmp_path: Path):
    script = _load_gitlab_mr_review_script()
    calls = []
    transferred = []

    def fake_run(args, **kwargs):
        calls.append(args)
        prompt_file = Path(args[args.index("--file") + 1])
        transferred.append(prompt_file.read_text(encoding="utf-8"))

        class Result:
            returncode = 0
            stderr = ""
            stdout = "review"

        return Result()

    monkeypatch.setattr(script.subprocess, "run", fake_run)

    result = script.run_agent_review("opencode", "opencode", "line1\nBase SHA: base", tmp_path)

    assert result == "review"
    assert transferred == ["line1\nBase SHA: base"]
    assert calls[0][1:4] == [
        "run",
        "Follow the instructions in the attached file.",
        "--file",
    ]
    assert Path(calls[0][4]).suffix == ".md"


def test_gitlab_mr_review_script_sends_claude_prompt_via_stdin(monkeypatch, tmp_path: Path):
    script = _load_gitlab_mr_review_script()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stderr = ""
            stdout = "review"

        return Result()

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    prompt = "line1\nBase SHA: base\nHead SHA: head"

    result = script.run_agent_review("claude-code", "claude", prompt, tmp_path)

    args, kwargs = calls[0]
    assert result == "review"
    assert args == ["claude", "-p", "--output-format", "text"]
    assert kwargs["input"] == prompt


def test_gitlab_mr_review_script_runs_summary_before_review_and_keeps_summary_local(monkeypatch, tmp_path: Path):
    script = _load_gitlab_mr_review_script()
    prompts = []
    responses = iter(
        [
            json.dumps(
                {
                    "overview": "修复认证流程",
                    "change_areas": ["auth"],
                    "behavior_changes": ["刷新token"],
                    "risk_areas": ["并发刷新"],
                    "test_changes": ["新增测试"],
                },
                ensure_ascii=False,
            ),
            "# Review\n\nOnly review findings.",
        ]
    )

    def fake_run(agent_type, command, prompt, repo_path):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(script, "run_agent_review", fake_run)

    result = script.run_two_step_review(
        "opencode",
        "opencode",
        "https://gitlab.example.com/team/project/merge_requests/7",
        "base",
        "head",
        ["auth.py"],
        tmp_path,
    )

    assert len(prompts) == 2
    assert "生成 MR 概要" in prompts[0]
    assert '"overview": "修复认证流程"' in prompts[1]
    assert "修复认证流程" in result["local_report"]
    assert result["comment_body"] == "# Review\n\nOnly review findings."
    assert "修复认证流程" not in result["comment_body"]


def test_config_treats_empty_dotenv_values_as_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_WORK_DIR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_WORK_DIR=\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert str(config.work_dir).endswith("code-review")


def test_config_defaults_to_opencode_agent_with_debug_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_AGENT_TYPE", raising=False)
    monkeypatch.delenv("MR_REVIEWER_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("MR_REVIEWER_AGENT_DEBUG", raising=False)
    monkeypatch.delenv("MR_REVIEWER_OPENCODE_DEBUG", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n", encoding="utf-8")

    config = Config.from_env(env_file)

    assert config.agent_type == "opencode"
    assert config.agent_command == "opencode"
    assert config.agent_debug is False
    assert config.log_level == "OFF"


def test_config_uses_explicit_log_level_before_legacy_agent_debug(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_LOG_LEVEL", raising=False)
    monkeypatch.delenv("MR_REVIEWER_AGENT_DEBUG", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_LOG_LEVEL=INFO\n"
        "MR_REVIEWER_AGENT_DEBUG=true\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.log_level == "INFO"
    assert config.agent_debug is False


def test_config_maps_legacy_agent_debug_to_debug_log_level(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_LOG_LEVEL", raising=False)
    monkeypatch.delenv("MR_REVIEWER_AGENT_DEBUG", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_AGENT_DEBUG=true\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.log_level == "DEBUG"
    assert config.agent_debug is True


def test_config_rejects_unsupported_log_level(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_LOG_LEVEL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_LOG_LEVEL=TRACE\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported log level"):
        Config.from_env(env_file)


def test_config_can_disable_opencode_debug(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_OPENCODE_DEBUG", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_OPENCODE_DEBUG=false\n",
        encoding="utf-8",
    )

    assert Config.from_env(env_file).opencode_debug is False


def test_config_uses_claude_code_command_and_ignores_opencode_legacy_command(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_AGENT_TYPE", raising=False)
    monkeypatch.delenv("MR_REVIEWER_AGENT_COMMAND", raising=False)
    monkeypatch.delenv("MR_REVIEWER_OPENCODE_COMMAND", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_AGENT_TYPE=claude-code\n"
        "MR_REVIEWER_OPENCODE_COMMAND=legacy-opencode\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.agent_type == "claude-code"
    assert config.agent_command == "claude"
    assert config.agent_debug is False


def test_config_reads_opencode_diagnostic_dir(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_OPENCODE_DIAGNOSTIC_DIR", raising=False)
    diagnostic_dir = tmp_path / "opencode-diag"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        f"MR_REVIEWER_OPENCODE_DIAGNOSTIC_DIR={diagnostic_dir}\n",
        encoding="utf-8",
    )

    assert Config.from_env(env_file).opencode_diagnostic_dir == diagnostic_dir


def test_config_reads_opencode_prompt_transport(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_OPENCODE_PROMPT_TRANSPORT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_OPENCODE_PROMPT_TRANSPORT=file\n",
        encoding="utf-8",
    )

    assert Config.from_env(env_file).opencode_prompt_transport == "file"


def test_config_reads_webhook_comment_and_secret_header(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_WEBHOOK_POST_COMMENT", raising=False)
    monkeypatch.delenv("MR_REVIEWER_WEBHOOK_SECRET_HEADER", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_WEBHOOK_POST_COMMENT=false\n"
        "MR_REVIEWER_WEBHOOK_SECRET_HEADER=X-CodeHub-Token\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.webhook_post_comment is False
    assert config.webhook_secret_header == "X-CodeHub-Token"


def test_config_reads_welink_group_id(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_WELINK_GROUP_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_WELINK_GROUP_ID=group-example\n",
        encoding="utf-8",
    )

    assert Config.from_env(env_file).welink_group_id == "group-example"


def test_config_reads_welink_onebox_target(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_WELINK_ONEBOX_SPACE_ID", raising=False)
    monkeypatch.delenv("MR_REVIEWER_WELINK_ONEBOX_PARENT_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_WELINK_ONEBOX_SPACE_ID=space-example\n"
        "MR_REVIEWER_WELINK_ONEBOX_PARENT_ID=parent-example\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.welink_onebox_space_id == "space-example"
    assert config.welink_onebox_parent_id == "parent-example"


def test_config_reads_independent_gitlab_api_base_url(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_GITLAB_API_BASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_GITLAB_API_BASE_URL=https://api.example.com/api/api/v4\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.gitlab_base_url == "https://gitlab.example.com"
    assert config.gitlab_api_base_url == "https://api.example.com/api/api/v4"


def test_config_defaults_gitlab_api_base_url_to_web_api_v4(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_GITLAB_API_BASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com/\n", encoding="utf-8")

    config = Config.from_env(env_file)

    assert config.gitlab_api_base_url == "https://gitlab.example.com/api/v4"


def test_parse_gitlab_mr_url_with_nested_project_path():
    parsed = parse_gitlab_mr_url(
        "https://gitlab.example.com/a/b/c/merge_requests/42",
        "https://gitlab.example.com",
    )

    assert parsed == GitLabMrUrl(
        base_url="https://gitlab.example.com",
        project_path="a/b/c",
        mr_iid=42,
    )


def test_reject_non_matching_gitlab_host():
    with pytest.raises(ValueError, match="GitLab host"):
        parse_gitlab_mr_url(
            "https://evil.example.com/a/b/-/merge_requests/1",
            "https://gitlab.example.com",
        )


def test_should_trigger_only_when_mentioned_and_allowed():
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="token",
        im_poll_command="poll",
        im_reply_command="reply",
        bot_mention="@ReviewBot",
        allowed_groups={"group-1"},
        allowed_users={"alice"},
        allowed_repos={"team/project"},
    )
    message = ImMessage(
        message_id="m1",
        chat_id="group-1",
        sender_id="alice",
        text="@ReviewBot please review https://gitlab.example.com/team/project/merge_requests/7",
        created_at="2026-04-27T00:00:00Z",
    )

    request = should_trigger_review(message, config)

    assert request is not None
    assert request.mr.project_path == "team/project"
    assert request.mr.mr_iid == 7


def test_should_trigger_when_welink_at_account_matches():
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        bot_account="bot-example",
    )
    message = ImMessage(
        message_id="88863928388808372",
        chat_id="group-example",
        sender_id="user-example",
        text="@李承阳 https://gitlab.example.com/team/project/merge_requests/7",
        created_at="1777278567776",
        at=True,
        at_account_list=("bot-example",),
    )

    request = should_trigger_review(message, config)

    assert request is not None
    assert request.mr.project_path == "team/project"


def test_should_not_trigger_without_bot_mention():
    config = Config(gitlab_base_url="https://gitlab.example.com", bot_mention="@ReviewBot")
    message = ImMessage(
        message_id="m1",
        chat_id="group-1",
        sender_id="alice",
        text="https://gitlab.example.com/team/project/merge_requests/7",
        created_at="2026-04-27T00:00:00Z",
    )

    assert should_trigger_review(message, config) is None


def test_parse_poll_output_requires_message_fields():
    payload = json.dumps(
        [
            {
                "message_id": "m1",
                "chat_id": "c1",
                "sender_id": "u1",
                "text": "@bot",
                "created_at": "2026-04-27T00:00:00Z",
            }
        ]
    )

    assert parse_poll_output(payload)[0].message_id == "m1"

    with pytest.raises(ValueError, match="message_id"):
        parse_poll_output(json.dumps([{"chat_id": "c1"}]))


def test_parse_welink_history_response():
    payload = json.dumps(
        {
            "respData": {
                "chatInfo": [
                    {
                        "at": True,
                        "atAccountList": ["bot-example"],
                        "content": "@李承阳 xxx",
                        "contentType": "TEXT_MSG",
                        "groupId": "group-example",
                        "groupType": 0,
                        "msgId": 88863928388808372,
                        "receiver": "",
                        "sender": "user-example",
                        "serverSendTime": 1777278567776,
                    }
                ],
                "maxMsgId": 88863928388808372,
                "minMsgId": 88863918719013463,
                "msgTotalCount": 5,
            },
            "resultCode": "0",
            "resultContext": "Operate Success",
            "sno": None,
        }
    )

    message = parse_poll_output(payload)[0]

    assert message == ImMessage(
        message_id="88863928388808372",
        chat_id="group-example",
        sender_id="user-example",
        text="@李承阳 xxx",
        created_at="1777278567776",
        at=True,
        at_account_list=("bot-example",),
    )


def test_build_welink_reply_args_uses_group_id_and_text():
    args = build_welink_reply_args("welink-cli im send-to-group", "group-example", "# Report")

    assert args == ["welink-cli", "im", "send-to-group", "--group-id", "group-example", "--text", "# Report"]


def test_choose_diff_refs_prefers_gitlab_diff_refs():
    mr = {
        "diff_refs": {"base_sha": "base", "head_sha": "head"},
        "sha": "sha",
    }

    assert choose_diff_refs(mr) == ("base", "head")


def test_gitlab_client_posts_mr_note(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id": 123}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = GitLabClient("https://api.example.com/api/api/v4", "secret-token")
    result = client.post_mr_note(
        GitLabMrUrl("https://gitlab.example.com", "team/project", 7),
        "# Review\n\nLooks good.",
    )

    request, timeout = requests[0]
    assert result == {"id": 123}
    assert timeout == 30
    assert request.full_url == "https://api.example.com/api/api/v4/projects/team%2Fproject/merge_requests/7/notes"
    assert request.get_method() == "POST"
    assert request.headers["Private-token"] == "secret-token"
    assert request.headers["Content-type"] == "application/x-www-form-urlencoded; charset=utf-8"
    assert urllib.parse.parse_qs(request.data.decode("utf-8")) == {"body": ["# Review\n\nLooks good."]}


def test_gitlab_client_posts_mr_discussion_as_json(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id": "discussion-1", "notes": [{"id": 456}]}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = GitLabClient("https://api.example.com/api/api/v4", "secret-token")
    result = client.post_mr_discussion(
        GitLabMrUrl("https://gitlab.example.com", "team/project", 7),
        "**[major][HIGH] title**",
        "major",
        {
            "base_sha": "base",
            "start_sha": "start",
            "head_sha": "head",
            "position_type": "text",
            "old_path": "src/example.py",
            "new_path": "src/example.py",
            "old_line": -1,
            "new_line": 42,
            "ignore_whitespace_change": False,
        },
    )

    request, timeout = requests[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert result == {"id": "discussion-1", "notes": [{"id": 456}]}
    assert timeout == 30
    assert request.full_url == "https://api.example.com/api/api/v4/projects/team%2Fproject/merge_requests/7/discussions"
    assert request.get_method() == "POST"
    assert request.headers["Private-token"] == "secret-token"
    assert request.headers["Content-type"] == "application/json; charset=utf-8"
    assert payload["body"] == "**[major][HIGH] title**"
    assert payload["severity"] == "major"
    assert payload["position"]["new_line"] == 42


def test_gitlab_api_debug_artifact_is_task_scoped_and_redacted(monkeypatch, tmp_path: Path):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"id": 123}'

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    client = GitLabClient("https://api.example.com/api/v4", "secret-token")

    with task_context("webhook-abc", tmp_path / "debug", enabled=True):
        client.post_mr_note(GitLabMrUrl("https://gitlab.example.com", "team/project", 7), "token=secret-token")

    artifacts = list((tmp_path / "debug").glob("*/webhook-abc/api/*.json"))
    assert len(artifacts) == 1
    content = artifacts[0].read_text(encoding="utf-8")
    assert "secret-token" not in content
    assert "<redacted>" in content


def test_gitlab_client_uses_mr_detail_endpoint_without_isource(monkeypatch):
    paths = []
    client = GitLabClient("https://api.example.com/api/api/v4", "secret-token")
    monkeypatch.setattr(client, "_get_json", lambda path: paths.append(path) or {"diff_refs": {}})

    target = type("Target", (), {"project_path": "team/project", "mr_iid": 7})()
    client.get_mr_detail_for_discussion_position(target)

    assert paths == ["/projects/team%2Fproject/merge_requests/7"]


def test_state_store_tracks_processed_messages(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")

    assert not store.is_processed("m1")
    store.mark_processed("m1", "task-1", "success")

    reloaded = StateStore(tmp_path / "state.json")
    assert reloaded.is_processed("m1")
    assert reloaded.data["lastMessageId"] == "m1"
    assert reloaded.data["processed"]["m1"]["status"] == "success"


def test_git_clone_uses_non_interactive_token_auth(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if args[-2:] == ["diff", "--name-only"]:
            Result.stdout = "app.py\n"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    GitClient().clone_checkout_and_diff(
        GitCheckout(
            target_repo_url="https://gitlab.example.com/team/project.git",
            source_repo_url="https://gitlab.example.com/team/project.git",
            target_branch="main",
            source_branch="feature/auth",
            base_sha="base",
            head_sha="head",
        ),
        "secret-token",
        tmp_path,
        {"max_files": 50, "max_diff_lines": 2000},
    )

    clone_args, clone_kwargs = calls[0]
    clone_env = clone_kwargs["env"]
    assert clone_args[:3] == ["git", "-c", "credential.helper="]
    assert clone_kwargs["encoding"] == "utf-8"
    assert clone_kwargs["errors"] == "replace"
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_env["GCM_INTERACTIVE"] == "never"
    assert clone_env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert clone_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in " ".join(clone_args)

    commands = [" ".join(args) for args, _ in calls]
    assert any("fetch origin main" in command for command in commands)
    assert any("fetch origin feature/auth" in command for command in commands)
    assert any("checkout head" in command for command in commands)


def test_git_clone_computes_merge_base_when_base_sha_missing(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if args[-3:] == ["merge-base", "refs/remotes/origin/main", "head"]:
            Result.stdout = "base\n"
        elif args[-3:] == ["diff", "--name-only", "base...head"]:
            Result.stdout = "app.py\n"
        elif args[-2:] == ["diff", "base...head"]:
            Result.stdout = "diff --git a/app.py b/app.py\n"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = GitClient().clone_checkout_and_diff(
        GitCheckout(
            target_repo_url="https://gitlab.example.com/team/project.git",
            source_repo_url="https://gitlab.example.com/team/project.git",
            target_branch="main",
            source_branch="feature/auth",
            base_sha=None,
            head_sha="head",
        ),
        "secret-token",
        tmp_path,
        {"max_files": 50, "max_diff_lines": 2000},
    )

    commands = [" ".join(args) for args, _ in calls]
    assert any("merge-base refs/remotes/origin/main head" in command for command in commands)
    assert result["base_sha"] == "base"
    assert result["head_sha"] == "head"
    assert result["changed_files"] == ["app.py"]


def test_git_clone_rejects_changed_file_count_over_limit(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if args[-3:] == ["diff", "--name-only", "base...head"]:
            Result.stdout = "a.py\nb.py\nc.py\n"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ResourceLimitError, match="changed file count exceeds limit: 3 > 2"):
        GitClient().clone_checkout_and_diff(
            GitCheckout(
                target_repo_url="https://gitlab.example.com/team/project.git",
                source_repo_url="https://gitlab.example.com/team/project.git",
                target_branch="main",
                source_branch="feature/auth",
                base_sha="base",
                head_sha="head",
            ),
            "secret-token",
            tmp_path,
            {"max_files": 2, "max_diff_lines": 2000},
        )

    commands = [" ".join(args) for args, _ in calls]
    assert any("diff --name-only base...head" in command for command in commands)
    assert not any(command.endswith("diff base...head") for command in commands)


def test_git_clone_rejects_diff_line_count_over_limit(tmp_path: Path, monkeypatch):
    def fake_run(args, **kwargs):
        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if args[-3:] == ["diff", "--name-only", "base...head"]:
            Result.stdout = "app.py\n"
        elif args[-2:] == ["diff", "base...head"]:
            Result.stdout = "line1\nline2\nline3\n"
        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ResourceLimitError, match="diff line count exceeds limit: 3 > 2"):
        GitClient().clone_checkout_and_diff(
            GitCheckout(
                target_repo_url="https://gitlab.example.com/team/project.git",
                source_repo_url="https://gitlab.example.com/team/project.git",
                target_branch="main",
                source_branch="feature/auth",
                base_sha="base",
                head_sha="head",
            ),
            "secret-token",
            tmp_path,
            {"max_files": 50, "max_diff_lines": 2},
        )


def test_prepare_command_wraps_windows_cmd_files(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("shutil.which", lambda command: "D:\\Program Files\\nodejs\\node_global\\opencode.CMD" if command == "opencode" else None)

    prepared = prepare_command(["opencode", "run", "使用 codehub-mr-review skill 检视代码"])

    assert prepared[:4] == ["cmd.exe", "/d", "/c", "call"]
    assert prepared[4] == "D:\\Program Files\\nodejs\\node_global\\opencode.CMD"
    assert prepared[5:] == ["run", "使用 codehub-mr-review skill 检视代码"]
