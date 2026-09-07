from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
import uuid

from mr_reviewer.config import Config
from mr_reviewer.git import GitClient
from mr_reviewer.gitlab import GitLabClient, GitLabMrUrl, parse_gitlab_mr_url
from mr_reviewer.im import ReviewRequest, ReviewSetRejection, ReviewSetRequest, resolve_review_trigger
from mr_reviewer.im_notifications import (
    render_review_set_accepted,
    render_review_set_failed,
    render_review_set_rejected,
    render_review_set_terminal,
    render_single_accepted,
    render_single_failed,
    render_single_terminal,
)
from mr_reviewer.markdown_report import render_structured_output_as_markdown
from mr_reviewer.observability import configure_logging, task_context
from mr_reviewer.opencode import build_agent_runner
from mr_reviewer.process import split_command
from mr_reviewer.review_set import ReviewSetValidationError
from mr_reviewer.review_set_publish import ReviewSetPublisher
from mr_reviewer.review_set_report import render_review_set_report
from mr_reviewer.reviewer import ReviewService
from mr_reviewer.single_mr import SingleMrReviewCoordinator, SingleMrTrigger
from mr_reviewer.repository_dependencies import (
    RepositoryDependencyCatalogError,
    load_repository_dependency_catalog,
)
from mr_reviewer.state import StateStore
from mr_reviewer.webhook import run_webhook_server
from mr_reviewer.welink import poll_messages, reply, reply_review_set, send_text, upload_onebox_report

LOG = logging.getLogger("mr_reviewer")


def build_service(config: Config) -> ReviewService:
    return ReviewService(
        GitLabClient(
            config.gitlab_api_base_url, config.gitlab_token, config.test_gitlab_responses
        ),
        GitClient(),
        build_agent_runner(
            config.agent_type,
            config.agent_command or config.opencode_command,
            debug=config.agent_debug,
            diagnostic_dir=config.agent_diagnostic_dir,
            redaction_token=config.gitlab_token,
        ),
    )


def healthcheck(config: Config) -> int:
    checks = {
        "git": shutil.which("git") is not None,
        "agent": shutil.which(split_command(config.agent_command or config.opencode_command)[0]) is not None,
        "gitlab_base_url": bool(config.gitlab_base_url),
        "gitlab_api_base_url": bool(config.gitlab_api_base_url),
        "gitlab_token": bool(config.gitlab_token),
        "im_poll_command": bool(config.im_poll_command),
        "im_reply_command": bool(config.im_reply_command),
        "welink_group_id": bool(config.welink_group_id),
        "welink_onebox_space_id": bool(config.welink_onebox_space_id),
        "welink_onebox_parent_id": bool(config.welink_onebox_parent_id),
    }
    for name, ok in checks.items():
        print(f"{name}: {'ok' if ok else 'missing'}")
    catalog_ok = True
    if config.repository_dependency_catalog is None:
        print("repository_dependency_catalog: optional")
    else:
        try:
            catalog = load_repository_dependency_catalog(config.repository_dependency_catalog)
        except RepositoryDependencyCatalogError as exc:
            catalog_ok = False
            print(
                "repository_dependency_catalog: "
                f"invalid ({exc.reason_code}) path={config.repository_dependency_catalog}"
            )
        else:
            print(
                "repository_dependency_catalog: "
                f"ok (repositories={len(catalog.repositories)}) path={config.repository_dependency_catalog}"
            )
    print(f"webhook_endpoint: {config.webhook_host}:{config.webhook_port}{config.webhook_path}")
    print(f"webhook_secret: {'ok' if config.webhook_secret else 'optional'}")
    print(f"im_post_comment: {'enabled' if config.im_post_comment else 'disabled'}")
    print(f"im_upload_onebox: {'enabled' if config.im_upload_onebox else 'disabled'}")
    print(f"webhook_post_comment: {'enabled' if config.webhook_post_comment else 'disabled'}")
    print(f"webhook_upload_onebox: {'enabled' if config.webhook_upload_onebox else 'disabled'}")
    print(f"coordination_db_path: {config.coordination_db_path}")
    allowlist_warning = _im_publish_allowlist_warning(config)
    if allowlist_warning:
        print(f"im_publish_allowlist: WARNING ({allowlist_warning})")
    else:
        print("im_publish_allowlist: restricted")
    print(f"review_set_post_comment: {'enabled' if config.review_set_post_comment else 'disabled'}")
    print(f"publish_min_severity: {config.publish_min_severity}")
    print(f"publish_min_confidence: {config.publish_min_confidence}")
    return 0 if all(checks.values()) and catalog_ok else 1


def run_once(config: Config, mr_url: str) -> int:
    service = build_service(config)
    mr = parse_gitlab_mr_url(mr_url, config.gitlab_base_url)
    task_id = f"manual-{uuid.uuid4().hex[:8]}"
    with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
        report = render_structured_output_as_markdown(service.review(mr, config, task_id=task_id))
    print(report.markdown)
    return 0


def poll(config: Config, once: bool) -> int:
    state = StateStore(config.state_path)
    service = build_service(config)
    single_coordinator = None
    allowlist_warning = _im_publish_allowlist_warning(config)
    if allowlist_warning:
        print(
            "WARNING stage=poller_startup outcome=warning "
            f"reason=im_gitlab_publish_unrestricted {allowlist_warning}",
            file=sys.stderr,
        )
    LOG.info(
        "poller status=started once=%s interval_seconds=%s state_path=%s",
        once,
        config.poll_interval_seconds,
        config.state_path,
    )

    while True:
        messages = _poll_messages(config)
        LOG.info("poller status=messages_received count=%s", len(messages))
        for message in messages:
            if state.is_processed(message.message_id):
                LOG.info(
                    "message=%s status=skipped reason=already_processed",
                    message.message_id,
                )
                continue

            request = resolve_review_trigger(message, config)
            if request is None:
                LOG.info(
                    "message=%s status=skipped reason=not_review_request",
                    message.message_id,
                )
                continue
            if isinstance(request, ReviewSetRejection):
                _reject_review_set(config, state, request)
            elif isinstance(request, ReviewSetRequest):
                _process_review_set(config, state, service, request)
            else:
                if single_coordinator is None:
                    single_coordinator = SingleMrReviewCoordinator(
                        service,
                        service.gitlab,
                        config,
                    )
                _process_single_review(config, state, service, single_coordinator, request)

        if once:
            return 0
        time.sleep(config.poll_interval_seconds)


def _poll_messages(config: Config):
    return poll_messages(config)


def _reply(config: Config, markdown: str, mr: GitLabMrUrl) -> None:
    reply(config, markdown, mr)


def _reply_review_set(
        config: Config,
        markdown: str,
        review_set_id: str,
        publish_counts: dict[str, int],
) -> None:
    reply_review_set(config, markdown, review_set_id, publish_counts)


def _send_text(config: Config, text: str) -> None:
    send_text(config, text)


def _upload_onebox_report(config: Config, file_name: str, markdown: str) -> str | None:
    return upload_onebox_report(config, file_name, markdown)


def _im_publish_allowlist_warning(config: Config) -> str:
    if not config.im_post_comment:
        return ""
    users = "restricted" if config.allowed_users else "unrestricted"
    repos = "restricted" if config.allowed_repos else "unrestricted"
    if users == "restricted" and repos == "restricted":
        return ""
    return f"allowed_users={users} allowed_repos={repos}"


def _process_single_review(
        config: Config,
        state: StateStore,
        service: ReviewService,
        coordinator: SingleMrReviewCoordinator,
        request: ReviewRequest,
) -> None:
    task_id = f"mr-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    accepted_status = _safe_im_notify(
        config,
        render_single_accepted(request.mr),
        task_id,
        "single",
        "accepted",
    )
    try:
        LOG.info("task=%s mr=%s/%s status=started", task_id, request.mr.project_path, request.mr.mr_iid)
        with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
            target = service.resolve_target(request.mr)
            outcome = coordinator.handle(
                target,
                SingleMrTrigger(
                    source="im",
                    trigger_id=request.message.message_id,
                    post_comment=config.im_post_comment,
                    upload_onebox=config.im_upload_onebox,
                ),
            )
        terminal_status = _safe_im_notify(
            config,
            render_single_terminal(request.mr, outcome),
            outcome.review_run_id,
            "single",
            "terminal",
        )
        elapsed = time.monotonic() - start
        state.mark_processed(
            request.message.message_id,
            outcome.review_run_id,
            outcome.status,
            notifications={"accepted": accepted_status, "terminal": terminal_status},
        )
        LOG.info(
            "task=%s mr=%s/%s elapsed=%.2fs status=%s",
            outcome.review_run_id,
            request.mr.project_path,
            request.mr.mr_iid,
            elapsed,
            outcome.status,
        )
    except Exception as exc:  # noqa: BLE001 - 保留现有单 MR 顶层失败语义。
        elapsed = time.monotonic() - start
        terminal_status = _safe_im_notify(
            config,
            render_single_failed(request.mr, task_id),
            task_id,
            "single",
            "terminal",
        )
        state.mark_processed(
            request.message.message_id,
            task_id,
            "failed",
            "single_review_failed",
            {"accepted": accepted_status, "terminal": terminal_status},
        )
        LOG.error(
            "task=%s mr=%s/%s elapsed=%.2fs status=failed error_type=%s",
            task_id,
            request.mr.project_path,
            request.mr.mr_iid,
            elapsed,
            type(exc).__name__,
        )


def _reject_review_set(config: Config, state: StateStore, rejection: ReviewSetRejection) -> None:
    task_id = f"review-set-{uuid.uuid4().hex[:12]}"
    terminal_status = _safe_im_notify(
        config,
        render_review_set_rejected(
            rejection.members,
            _rejection_text(rejection.reason_code),
            task_id,
        ),
        task_id,
        "review-set",
        "terminal",
    )
    state.mark_processed(
        rejection.message.message_id,
        task_id,
        "rejected",
        rejection.reason_code,
        {"accepted": "not_applicable", "terminal": terminal_status},
    )
    LOG.info("task=%s review_scope=review-set status=rejected reason=%s", task_id, rejection.reason_code)


def _process_review_set(
        config: Config,
        state: StateStore,
        service: ReviewService,
        request: ReviewSetRequest,
) -> None:
    task_id = f"review-set-{uuid.uuid4().hex[:12]}"
    start = time.monotonic()
    accepted_status = _safe_im_notify(
        config,
        render_review_set_accepted(request.members),
        task_id,
        "review-set",
        "accepted",
    )
    try:
        with task_context(task_id, config.debug_dir, config.log_level == "DEBUG"):
            report = service.review_set(request, config, task_id)
            publication = ReviewSetPublisher(
                service.gitlab,
                config.publication_policy,
            ).publish(
                report,
                enabled=config.review_set_post_comment,
                model_name=config.agent_model_name,
            )
            markdown = render_review_set_report(report, publication)
            file_name = f"review-set-{report.manifest.review_set_id[:12]}.md"
            try:
                upload_error = _upload_onebox_report(config, file_name, markdown)
            except Exception as exc:  # noqa: BLE001 - 报告上传失败不能改写已完成的联合检视。
                upload_error = "onebox_upload_failed"
                LOG.warning(
                    "task=%s review_scope=review-set stage=file_upload outcome=failed error_type=%s",
                    task_id,
                    type(exc).__name__,
                )
            final_status = (
                "success_with_warnings"
                if publication.status == "success_with_warnings" or upload_error
                else publication.status
            )
        terminal_status = _safe_im_notify(
            config,
            render_review_set_terminal(report, publication, file_name, upload_error, task_id),
            task_id,
            "review-set",
            "terminal",
        )
        state.mark_processed(
            request.message.message_id,
            task_id,
            final_status,
            notifications={"accepted": accepted_status, "terminal": terminal_status},
        )
        LOG.info(
            "task=%s review_scope=review-set review_set_id=%s req_id=%s elapsed=%.2fs status=%s",
            task_id,
            report.manifest.review_set_id,
            report.manifest.req_id,
            time.monotonic() - start,
            final_status,
        )
    except ReviewSetValidationError as exc:
        if exc.reason_code in {"req_id_missing", "req_id_mismatch"}:
            terminal_status = _safe_im_notify(
                config,
                render_review_set_rejected(
                    request.members,
                    _rejection_text(exc.reason_code),
                    task_id,
                ),
                task_id,
                "review-set",
                "terminal",
            )
            state.mark_processed(
                request.message.message_id,
                task_id,
                "rejected",
                exc.reason_code,
                {"accepted": accepted_status, "terminal": terminal_status},
            )
            LOG.info(
                "task=%s review_scope=review-set elapsed=%.2fs status=rejected reason=%s",
                task_id,
                time.monotonic() - start,
                exc.reason_code,
            )
        else:
            _fail_review_set(config, state, request, task_id, start, exc, accepted_status)
    except Exception as exc:  # noqa: BLE001 - 联合失败必须终结消息且只向 IM 暴露安全文案。
        _fail_review_set(config, state, request, task_id, start, exc, accepted_status)


def _fail_review_set(
        config: Config,
        state: StateStore,
        request: ReviewSetRequest,
        task_id: str,
        start: float,
        error: Exception,
        accepted_status: str,
) -> None:
    terminal_status = _safe_im_notify(
        config,
        render_review_set_failed(
            request.members,
            task_id,
            str(getattr(error, "stage", "review_set_review")),
        ),
        task_id,
        "review-set",
        "terminal",
    )
    state.mark_processed(
        request.message.message_id,
        task_id,
        "failed",
        "review_set_failed",
        {"accepted": accepted_status, "terminal": terminal_status},
    )
    LOG.error(
        "task=%s review_scope=review-set elapsed=%.2fs status=failed error_type=%s",
        task_id,
        time.monotonic() - start,
        type(error).__name__,
    )


def _safe_im_notify(
        config: Config,
        text: str,
        task_id: str,
        review_scope: str,
        event: str,
) -> str:
    try:
        _send_text(config, text)
    except Exception as exc:  # noqa: BLE001 - 通知失败不能改写检视事实或触发 Agent 重试。
        LOG.warning(
            "task=%s review_scope=%s stage=im_notify event=%s outcome=failed error_type=%s",
            task_id,
            review_scope,
            event,
            type(exc).__name__,
        )
        return "failed"
    LOG.info(
        "task=%s review_scope=%s stage=im_notify event=%s outcome=succeeded",
        task_id,
        review_scope,
        event,
    )
    return "succeeded"


def _rejection_text(reason_code: str) -> str:
    messages = {
        "too_many_mrs": "一条消息最多只能包含 3 个唯一 MR。",
        "same_project": "成员必须来自不同项目。",
        "repo_not_allowed": "消息中包含未授权仓库。",
        "req_id_missing": "至少一个 MR 缺少有效 ReqID。",
        "req_id_mismatch": "成员 MR 的 ReqID 不一致。",
    }
    return messages.get(reason_code, "请求不满足联合检视条件。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mr-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("healthcheck")

    run_once_parser = subparsers.add_parser("run-once")
    run_once_parser.add_argument("mr_url")

    poll_parser = subparsers.add_parser("poll")
    poll_parser.add_argument("--once", action="store_true")

    subparsers.add_parser("webhook")

    args = parser.parse_args(argv)
    config = Config.from_env()
    configure_logging(config.log_level)

    if args.command == "healthcheck":
        return healthcheck(config)
    if args.command == "run-once":
        return run_once(config, args.mr_url)
    if args.command == "poll":
        return poll(config, args.once)
    if args.command == "webhook":
        return run_webhook_server(config, build_service(config))

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
