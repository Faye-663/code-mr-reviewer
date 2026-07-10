from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "MR_REVIEWER_"
LOG_LEVELS = {"OFF", "INFO", "DEBUG"}


def _split_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_log_level(value: str) -> str:
    level = value.strip().upper()
    if level not in LOG_LEVELS:
        supported = ", ".join(sorted(LOG_LEVELS))
        raise ValueError(f"unsupported log level: {value}; expected one of {supported}")
    return level


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(slots=True)
class Config:
    gitlab_base_url: str
    gitlab_api_base_url: str = ""
    gitlab_token: str = ""
    im_poll_command: str = ""
    im_reply_command: str = ""
    welink_group_id: str = ""
    welink_onebox_space_id: str = ""
    welink_onebox_parent_id: str = ""
    bot_mention: str = "@Bot"
    bot_account: str = ""
    allowed_groups: set[str] = field(default_factory=set)
    allowed_users: set[str] = field(default_factory=set)
    allowed_repos: set[str] = field(default_factory=set)
    work_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "code-review")
    state_path: Path = Path(".mr-reviewer-state.json")
    agent_type: str = "opencode"
    agent_command: str = ""
    agent_model_name: str = ""
    agent_debug: bool = False
    agent_diagnostic_dir: Path | None = None
    log_level: str = "OFF"
    debug_dir: Path = Path("log/debug")
    opencode_command: str = "opencode"
    opencode_debug: bool = False
    opencode_diagnostic_dir: Path | None = None
    opencode_prompt_transport: str = "argument"
    comment_skill: str = ""
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_path: str = "/webhook/gitlab"
    webhook_secret: str = ""
    webhook_secret_header: str = "X-Gitlab-Token"
    webhook_post_comment: bool = True
    report_dir: Path = Path("log/webhook-reports")
    max_files: int = 50
    max_diff_lines: int = 2000
    task_timeout_seconds: int = 900
    poll_interval_seconds: int = 15
    test_gitlab_responses: Path | None = None

    def __post_init__(self) -> None:
        self.gitlab_base_url = self.gitlab_base_url.rstrip("/")
        if self.gitlab_api_base_url:
            self.gitlab_api_base_url = self.gitlab_api_base_url.rstrip("/")
        elif self.gitlab_base_url:
            self.gitlab_api_base_url = f"{self.gitlab_base_url}/api/v4"

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> "Config":
        dotenv_values = load_dotenv(dotenv_path or Path(".env"))

        def get(name: str, default: str = "") -> str:
            env_name = f"{ENV_PREFIX}{name}"
            value = os.environ.get(env_name, dotenv_values.get(env_name, default))
            return value if value != "" else default

        def has_value(name: str) -> bool:
            env_name = f"{ENV_PREFIX}{name}"
            return bool(os.environ.get(env_name, dotenv_values.get(env_name, "")))

        test_gitlab_responses = get("TEST_GITLAB_RESPONSES")
        opencode_diagnostic_dir = get("OPENCODE_DIAGNOSTIC_DIR")
        opencode_prompt_transport = get("OPENCODE_PROMPT_TRANSPORT", "argument").lower()
        agent_type = get("AGENT_TYPE", "opencode").lower()
        if agent_type not in {"opencode", "claude-code"}:
            raise ValueError(f"unsupported agent type: {agent_type}")
        legacy_opencode_command = get("OPENCODE_COMMAND", "opencode")
        agent_command = get("AGENT_COMMAND")
        if not agent_command:
            agent_command = legacy_opencode_command if agent_type == "opencode" else "claude"
        agent_debug_value = get("AGENT_DEBUG")
        if not agent_debug_value and agent_type == "opencode":
            agent_debug_value = get("OPENCODE_DEBUG", "false")
        agent_diagnostic_dir = get("AGENT_DIAGNOSTIC_DIR")
        if not agent_diagnostic_dir and agent_type == "opencode":
            agent_diagnostic_dir = opencode_diagnostic_dir
        if has_value("LOG_LEVEL"):
            log_level = _parse_log_level(get("LOG_LEVEL"))
        else:
            log_level = "DEBUG" if _parse_bool(agent_debug_value or "false") else "OFF"
        debug_dir_value = get("DEBUG_DIR")
        if not debug_dir_value:
            debug_dir_value = agent_diagnostic_dir or "log/debug"
        return cls(
            gitlab_base_url=get("GITLAB_BASE_URL"),
            gitlab_api_base_url=get("GITLAB_API_BASE_URL"),
            gitlab_token=get("GITLAB_TOKEN"),
            im_poll_command=get("IM_POLL_COMMAND"),
            im_reply_command=get("IM_REPLY_COMMAND"),
            welink_group_id=get("WELINK_GROUP_ID"),
            welink_onebox_space_id=get("WELINK_ONEBOX_SPACE_ID"),
            welink_onebox_parent_id=get("WELINK_ONEBOX_PARENT_ID"),
            bot_mention=get("BOT_MENTION", "@Bot"),
            bot_account=get("BOT_ACCOUNT"),
            allowed_groups=_split_set(get("ALLOWED_GROUPS")),
            allowed_users=_split_set(get("ALLOWED_USERS")),
            allowed_repos=_split_set(get("ALLOWED_REPOS")),
            work_dir=Path(get("WORK_DIR", str(Path(tempfile.gettempdir()) / "code-review"))),
            state_path=Path(get("STATE_PATH", ".mr-reviewer-state.json")),
            agent_type=agent_type,
            agent_command=agent_command,
            agent_model_name=get("AGENT_MODEL_NAME"),
            agent_debug=log_level == "DEBUG",
            agent_diagnostic_dir=Path(debug_dir_value),
            log_level=log_level,
            debug_dir=Path(debug_dir_value),
            opencode_command=legacy_opencode_command,
            opencode_debug=_parse_bool(get("OPENCODE_DEBUG", "false")),
            opencode_diagnostic_dir=Path(opencode_diagnostic_dir) if opencode_diagnostic_dir else None,
            opencode_prompt_transport=opencode_prompt_transport,
            comment_skill=get("COMMENT_SKILL"),
            webhook_host=get("WEBHOOK_HOST", "127.0.0.1"),
            webhook_port=int(get("WEBHOOK_PORT", "8080")),
            webhook_path=get("WEBHOOK_PATH", "/webhook/gitlab"),
            webhook_secret=get("WEBHOOK_SECRET"),
            webhook_secret_header=get("WEBHOOK_SECRET_HEADER", "X-Gitlab-Token"),
            webhook_post_comment=_parse_bool(get("WEBHOOK_POST_COMMENT", "true")),
            report_dir=Path(get("REPORT_DIR", "log/webhook-reports")),
            max_files=int(get("MAX_FILES", "50")),
            max_diff_lines=int(get("MAX_DIFF_LINES", "2000")),
            task_timeout_seconds=int(get("TASK_TIMEOUT_SECONDS", "900")),
            poll_interval_seconds=int(get("POLL_INTERVAL_SECONDS", "15")),
            test_gitlab_responses=Path(test_gitlab_responses) if test_gitlab_responses else None,
        )
