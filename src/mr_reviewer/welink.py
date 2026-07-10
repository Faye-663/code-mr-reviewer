from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid

from mr_reviewer.config import Config
from mr_reviewer.gitlab import GitLabMrUrl
from mr_reviewer.im import parse_poll_output
from mr_reviewer.observability import write_debug_text
from mr_reviewer.process import format_command, prepare_command, split_command

LOG = logging.getLogger("mr_reviewer")


def command_for_log(args: list[str]) -> str:
    safe_args = []
    skip_text = False
    for arg in args:
        if skip_text:
            safe_args.append(f"<text_chars={len(arg)}>")
            skip_text = False
            continue
        safe_args.append(arg)
        if arg == "--text":
            skip_text = True
    return format_command(prepare_command(safe_args))


def poll_messages(config: Config):
    if not config.im_poll_command:
        raise ValueError("IM poll command is required")
    group_id = _require_welink_group_id(config)
    args = split_command(config.im_poll_command) + ["--group-id", group_id]
    LOG.info("stage=im_poll command=%s", command_for_log(args))
    result = subprocess.run(
        prepare_command(args),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    write_debug_text("im", "poll-stdout", ".log", result.stdout or "", config.gitlab_token)
    write_debug_text("im", "poll-stderr", ".log", result.stderr or "", config.gitlab_token)
    LOG.info(
        "stage=im_poll_result returncode=%s stdout_chars=%s stderr_chars=%s",
        result.returncode,
        len(result.stdout or ""),
        len(result.stderr or ""),
    )
    if result.returncode != 0:
        raise RuntimeError(f"IM poll command failed: {result.stderr.strip()}")
    return parse_poll_output(result.stdout)


def reply(config: Config, markdown: str, mr: GitLabMrUrl) -> None:
    if not config.im_reply_command:
        raise ValueError("IM reply command is required")
    group_id = _require_welink_group_id(config)

    project_name = mr.project_path.split("/")[-1]
    random_suffix = uuid.uuid4().hex[:6]
    prefix = f"review-{project_name}-mr-{mr.mr_iid}-{random_suffix}"
    file_path = None
    try:
        # WeLink 群消息不适合承载完整 Markdown，先落临时文件再上传 OneBox。
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            delete=False,
            encoding="utf-8",
            prefix=prefix,
        ) as f:
            f.write(markdown)
            file_path = f.name

        file_name = os.path.basename(file_path)
        upload_error = upload_report(config, file_path, markdown)

        # 群里只发送文件名通知，避免日志和 IM 文本中出现完整 review 正文。
        if upload_error:
            notify_text = (
                "代码审查报告已生成，但 OneBox 上传失败，请检查 space-id/parent 是否存在或账号是否有权限。"
                f"错误: {upload_error}"
            )
        else:
            notify_text = f"代码审查报告已上传到 WeLink OneBox，群空间Review目录下: {file_name}"
        LOG.info("stage=im_send group_id=%s text_chars=%s", group_id, len(notify_text))
        reply_args = split_command(config.im_reply_command) + [
            "--group-id",
            group_id,
            "--text",
            notify_text,
        ]
        reply_result = subprocess.run(
            prepare_command(reply_args),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        write_debug_text("im", "send-stdout", ".log", reply_result.stdout or "", config.gitlab_token)
        write_debug_text("im", "send-stderr", ".log", reply_result.stderr or "", config.gitlab_token)
        LOG.info("stage=im_send_result returncode=%s stdout_chars=%s stderr_chars=%s", reply_result.returncode, len(reply_result.stdout or ""), len(reply_result.stderr or ""))
        if reply_result.returncode != 0:
            raise RuntimeError(f"IM reply command failed: {reply_result.stderr.strip()}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            LOG.info("stage=file_cleanup path=%s", file_path)


def upload_report(config: Config, file_path: str, markdown: str) -> str | None:
    LOG.info("stage=file_upload path=%s chars=%s", file_path, len(markdown))
    if not config.welink_onebox_space_id or not config.welink_onebox_parent_id:
        message = "missing OneBox space-id/parent config"
        LOG.warning("stage=file_upload_result returncode=skipped error=%s", message)
        return message

    upload_args = [
        "welink-cli",
        "onebox",
        "file-upload",
        "--space-id",
        config.welink_onebox_space_id,
        "--parent",
        config.welink_onebox_parent_id,
        file_path,
    ]
    LOG.info("stage=file_upload command=%s", command_for_log(upload_args))
    upload_result = subprocess.run(
        prepare_command(upload_args),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    write_debug_text("im", "upload-stdout", ".log", upload_result.stdout or "", config.gitlab_token)
    write_debug_text("im", "upload-stderr", ".log", upload_result.stderr or "", config.gitlab_token)
    LOG.info("stage=file_upload_result returncode=%s stdout_chars=%s stderr_chars=%s", upload_result.returncode, len(upload_result.stdout or ""), len(upload_result.stderr or ""))
    if upload_result.returncode != 0:
        error = upload_result.stderr.strip() or upload_result.stdout.strip() or f"returncode={upload_result.returncode}"
        LOG.warning("stage=file_upload_failed error=%s", error)
        return error
    return None


def _require_welink_group_id(config: Config) -> str:
    if not config.welink_group_id:
        raise ValueError("WeLink group ID is required")
    return config.welink_group_id
