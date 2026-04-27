import json
from pathlib import Path

import pytest

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabMrUrl, choose_diff_refs, parse_gitlab_mr_url
from mr_reviewer.im import ImMessage, build_welink_reply_args, parse_poll_output, should_trigger_review
from mr_reviewer.state import StateStore


def test_config_treats_empty_dotenv_values_as_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_WORK_DIR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_WORK_DIR=\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert str(config.work_dir).endswith("mr-review")


def test_parse_gitlab_mr_url_with_nested_project_path():
    parsed = parse_gitlab_mr_url(
        "https://gitlab.example.com/a/b/c/-/merge_requests/42",
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
        text="@ReviewBot please review https://gitlab.example.com/team/project/-/merge_requests/7",
        created_at="2026-04-27T00:00:00Z",
    )

    request = should_trigger_review(message, config)

    assert request is not None
    assert request.mr.project_path == "team/project"
    assert request.mr.mr_iid == 7


def test_should_trigger_when_welink_at_account_matches():
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        bot_account="l00808734",
    )
    message = ImMessage(
        message_id="88863928388808372",
        chat_id="619850427",
        sender_id="d00808710",
        text="@李承阳 https://gitlab.example.com/team/project/-/merge_requests/7",
        created_at="1777278567776",
        at=True,
        at_account_list=("l00808734",),
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
        text="https://gitlab.example.com/team/project/-/merge_requests/7",
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
                        "atAccountList": ["l00808734"],
                        "content": "@李承阳 xxx",
                        "contentType": "TEXT_MSG",
                        "groupId": 619850427,
                        "groupType": 0,
                        "msgId": 88863928388808372,
                        "receiver": "",
                        "sender": "d00808710",
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
        chat_id="619850427",
        sender_id="d00808710",
        text="@李承阳 xxx",
        created_at="1777278567776",
        at=True,
        at_account_list=("l00808734",),
    )


def test_build_welink_reply_args_uses_group_id_and_text():
    args = build_welink_reply_args("welink-cli im send-to-group", "619850427", "# Report")

    assert args == ["welink-cli", "im", "send-to-group", "--group-id", "619850427", "--text", "# Report"]


def test_choose_diff_refs_prefers_gitlab_diff_refs():
    mr = {
        "diff_refs": {"base_sha": "base", "head_sha": "head"},
        "sha": "sha",
    }

    assert choose_diff_refs(mr) == ("base", "head")


def test_state_store_tracks_processed_messages(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")

    assert not store.is_processed("m1")
    store.mark_processed("m1", "task-1", "success")

    reloaded = StateStore(tmp_path / "state.json")
    assert reloaded.is_processed("m1")
    assert reloaded.data["lastMessageId"] == "m1"
    assert reloaded.data["processed"]["m1"]["status"] == "success"
