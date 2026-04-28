from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabMrUrl, parse_gitlab_mr_url

URL_RE = re.compile(r"https?://[^\s<>]+")


@dataclass(frozen=True, slots=True)
class ImMessage:
    message_id: str
    chat_id: str
    sender_id: str
    text: str
    created_at: str
    at: bool = False
    at_account_list: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    message: ImMessage
    mr: GitLabMrUrl


def parse_poll_output(stdout: str) -> list[ImMessage]:
    payload = json.loads(stdout or "[]")
    raw_messages = _extract_messages(payload)

    messages: list[ImMessage] = []
    required = ("message_id", "chat_id", "sender_id", "text", "created_at")
    for raw in raw_messages:
        normalized = _normalize_message(raw)
        missing = [field for field in required if normalized.get(field) is None]
        if missing:
            raise ValueError(f"IM message missing required field: {missing[0]}")
        messages.append(ImMessage(**normalized))
    return messages


def _extract_messages(payload: object) -> list[dict]:
    if isinstance(payload, list):
        # 保留 JSON 数组入口，便于测试和本地模拟，不要求真实 WeLink CLI。
        return payload
    if isinstance(payload, dict):
        if payload.get("resultCode") not in (None, "0", 0):
            raise ValueError(f"WeLink query failed: {payload.get('resultContext', payload.get('resultCode'))}")
        chat_info = payload.get("respData", {}).get("chatInfo")
        if isinstance(chat_info, list):
            return chat_info
    raise ValueError("IM poll output must be a WeLink history response or JSON array")


def _normalize_message(raw: dict) -> dict:
    if "msgId" in raw:
        # WeLink CLI 字段名与内部字段名不同，先归一化后再进入触发判断。
        return {
            "message_id": str(raw.get("msgId")),
            "chat_id": str(raw.get("groupId")),
            "sender_id": str(raw.get("sender")),
            "text": str(raw.get("content")),
            "created_at": str(raw.get("serverSendTime")),
            "at": bool(raw.get("at")),
            "at_account_list": tuple(str(account) for account in raw.get("atAccountList", [])),
        }
    return {
        "message_id": str(raw["message_id"]) if "message_id" in raw else None,
        "chat_id": str(raw["chat_id"]) if "chat_id" in raw else None,
        "sender_id": str(raw["sender_id"]) if "sender_id" in raw else None,
        "text": str(raw["text"]) if "text" in raw else None,
        "created_at": str(raw["created_at"]) if "created_at" in raw else None,
        "at": bool(raw.get("at", False)),
        "at_account_list": tuple(str(account) for account in raw.get("at_account_list", [])),
    }


def should_trigger_review(message: ImMessage, config: Config) -> ReviewRequest | None:
    # WeLink 的展示名可能变化，优先用 atAccountList 精确识别 bot 账号。
    mentioned_by_text = bool(config.bot_mention and config.bot_mention in message.text)
    mentioned_by_account = bool(config.bot_account and message.at and config.bot_account in message.at_account_list)
    if not mentioned_by_text and not mentioned_by_account:
        return None
    if config.allowed_groups and message.chat_id not in config.allowed_groups:
        return None
    if config.allowed_users and message.sender_id not in config.allowed_users:
        return None

    for match in URL_RE.finditer(message.text):
        try:
            mr = parse_gitlab_mr_url(match.group(0).rstrip(".,;"), config.gitlab_base_url)
        except ValueError:
            continue
        if config.allowed_repos and mr.project_path not in config.allowed_repos:
            return None
        return ReviewRequest(message=message, mr=mr)
    return None


def split_command(command: str) -> list[str]:
    return shlex.split(command, posix=(os.name != "nt"))


def build_welink_reply_args(command: str, group_id: str, markdown: str) -> list[str]:
    return split_command(command) + ["--group-id", group_id, "--text", markdown]
