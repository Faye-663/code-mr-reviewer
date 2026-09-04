from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

from mr_reviewer.config import Config
from mr_reviewer.coordination import (
    ReviewCoordinationStore,
    ReviewRunRecord,
    TriggerIntent,
    TriggerRegistration,
)
from mr_reviewer.delivery import current_head_validation, deliver_gitlab_review
from mr_reviewer.markdown_report import render_review_report, render_structured_output_as_markdown
from mr_reviewer.review_artifacts import ReviewArtifactStore
from mr_reviewer.review_result import parse_structured_review_result
from mr_reviewer.reviewer import (
    MergeRequestReviewTarget,
    ReviewCancelledError,
    ReviewReport,
    ReviewStageError,
    ReviewService,
)
from mr_reviewer.welink import upload_onebox_report


LOG = logging.getLogger("mr_reviewer")


@dataclass(frozen=True, slots=True)
class SingleMrTrigger:
    source: str
    trigger_id: str
    post_comment: bool
    upload_onebox: bool


@dataclass(frozen=True, slots=True)
class SingleMrOutcome:
    review_run_id: str
    attempt: int
    disposition: str
    status: str
    report: ReviewReport | None
    report_json_path: str
    report_markdown_path: str
    deliveries: dict[str, dict[str, object]]


class ReviewSupersededError(RuntimeError):
    pass


class SingleMrDeliveryCoordinator:
    def __init__(
        self,
        gitlab,
        config: Config,
        store: ReviewCoordinationStore,
        *,
        upload_onebox: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self.gitlab = gitlab
        self.config = config
        self.store = store
        self.upload_onebox = upload_onebox or (
            lambda file_name, markdown: upload_onebox_report(config, file_name, markdown)
        )

    def deliver(
        self,
        run: ReviewRunRecord,
        target: MergeRequestReviewTarget,
        report: ReviewReport,
        owner_id: str,
    ) -> tuple[ReviewReport, str]:
        desired = self.store.desired_sinks(run.review_run_id)
        trigger_ids = ",".join(
            str(item["trigger_id"]) for item in self.store.list_triggers(run.review_run_id)
        )
        LOG.info(
            "task=%s review_run_id=%s trigger_id=%s repo=%s mr_iid=%s head_sha=%s "
            "stage=delivery outcome=started gitlab=%s onebox=%s",
            run.review_run_id,
            run.review_run_id,
            trigger_ids,
            run.project_path,
            run.mr_iid,
            run.head_sha,
            desired["gitlab"],
            desired["onebox"],
        )
        for sink, enabled in desired.items():
            if not enabled:
                self.store.mark_delivery_disabled(run.review_run_id, sink)

        if not any(desired.values()):
            return _normalize_report(report), "succeeded"

        try:
            validation, _ = current_head_validation(self.gitlab, target, report.head_sha)
        except Exception as exc:  # noqa: BLE001 - Head 无法确认时不得执行任何外部写入。
            validation = {
                "review_head_sha": report.head_sha,
                "current_head_sha": "",
                "status": "failed",
            }
            report = replace(report, head_validation=validation)
            for sink, enabled in desired.items():
                if enabled:
                    self.store.record_delivery(run.review_run_id, sink, "failed", error=str(exc))
            return _normalize_report(report), "success_with_warnings"

        report = replace(report, head_validation=validation)
        if validation["status"] == "stale":
            for sink, enabled in desired.items():
                if enabled:
                    self.store.record_delivery(run.review_run_id, sink, "skipped_stale")
            self.store.mark_run_superseded(run.review_run_id)
            return _normalize_report(report), "superseded"

        if desired["gitlab"]:
            report = self._deliver_gitlab(run, target, report, owner_id)
            if report.submission_status == "skipped_stale":
                if desired["onebox"]:
                    self.store.record_delivery(run.review_run_id, "onebox", "skipped_stale")
                self.store.mark_run_superseded(run.review_run_id)
                return report, "superseded"
        else:
            report = _normalize_report(report)

        if desired["onebox"]:
            self._deliver_onebox(run, report, owner_id)

        deliveries = self.store.list_deliveries(run.review_run_id)
        warning_statuses = {"failed", "unknown", "running"}
        status = (
            "success_with_warnings"
            if any(item.get("status") in warning_statuses for item in deliveries.values())
            else "succeeded"
        )
        LOG.info(
            "task=%s review_run_id=%s trigger_id=%s repo=%s mr_iid=%s head_sha=%s "
            "stage=delivery outcome=%s",
            run.review_run_id,
            run.review_run_id,
            trigger_ids,
            run.project_path,
            run.mr_iid,
            run.head_sha,
            status,
        )
        return report, status

    def _deliver_gitlab(
        self,
        run: ReviewRunRecord,
        target: MergeRequestReviewTarget,
        report: ReviewReport,
        owner_id: str,
    ) -> ReviewReport:
        if not self.store.claim_delivery(run.review_run_id, "gitlab", owner_id):
            self._wait_for_delivery(run.review_run_id, "gitlab")
            return report
        try:
            with self._delivery_heartbeat(run.review_run_id, "gitlab", owner_id):
                delivered = deliver_gitlab_review(self.gitlab, self.config, target, report, enabled=True)
            if delivered.submission_status == "skipped_stale":
                status = "skipped_stale"
            elif delivered.submission_status in {"failed", "parse_failed", "model_not_configured"}:
                status = "failed"
            else:
                status = "succeeded"
            self.store.finish_delivery(
                run.review_run_id,
                "gitlab",
                owner_id,
                status,
                error="" if status == "succeeded" else delivered.submission_status,
            )
            return delivered
        except Exception as exc:  # noqa: BLE001 - One sink 失败不能阻止另一个 sink。
            self.store.finish_delivery(
                run.review_run_id,
                "gitlab",
                owner_id,
                "failed",
                error=str(exc),
            )
            LOG.warning(
                "review_run_id=%s stage=gitlab_delivery outcome=failed error_type=%s",
                run.review_run_id,
                type(exc).__name__,
            )
            return report

    def _deliver_onebox(self, run: ReviewRunRecord, report: ReviewReport, owner_id: str) -> None:
        if not self.store.claim_delivery(run.review_run_id, "onebox", owner_id):
            self._wait_for_delivery(run.review_run_id, "onebox")
            return
        file_name = _onebox_file_name(run)
        markdown = render_review_report(_normalize_report(report), "succeeded")
        try:
            with self._delivery_heartbeat(run.review_run_id, "onebox", owner_id):
                error = self.upload_onebox(file_name, markdown)
        except Exception as exc:  # noqa: BLE001 - 明确失败允许后续 Trigger 重试该 sink。
            self.store.finish_delivery(
                run.review_run_id,
                "onebox",
                owner_id,
                "failed",
                error=str(exc),
                external_ref=file_name,
            )
            return
        except BaseException:  # 上传进程被中断时结果不可判定，不允许自动重试。
            self.store.finish_delivery(run.review_run_id, "onebox", owner_id, "unknown")
            raise
        if error:
            self.store.finish_delivery(
                run.review_run_id,
                "onebox",
                owner_id,
                "failed",
                error=error,
                external_ref=file_name,
            )
        else:
            self.store.finish_delivery(
                run.review_run_id,
                "onebox",
                owner_id,
                "succeeded",
                external_ref=file_name,
            )

    def _wait_for_delivery(self, review_run_id: str, sink: str) -> None:
        deadline = time.monotonic() + self.config.task_timeout_seconds
        while time.monotonic() < deadline:
            delivery = self.store.get_delivery(review_run_id, sink)
            if delivery is None or delivery["status"] != "running":
                return
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for {sink} delivery: {review_run_id}")

    @contextmanager
    def _delivery_heartbeat(
        self,
        review_run_id: str,
        sink: str,
        owner_id: str,
    ) -> Iterator[None]:
        stop = threading.Event()

        def renew() -> None:
            interval = max(1.0, self.store.lease_seconds / 3)
            while not stop.wait(interval):
                if not self.store.renew_delivery_lease(review_run_id, sink, owner_id):
                    return

        heartbeat = threading.Thread(
            target=renew,
            name=f"{review_run_id}-{sink}-lease",
            daemon=True,
        )
        heartbeat.start()
        try:
            yield
        finally:
            stop.set()
            heartbeat.join(timeout=1)


class SingleMrReviewCoordinator:
    def __init__(
        self,
        service: ReviewService,
        gitlab,
        config: Config,
        *,
        store: ReviewCoordinationStore | None = None,
        upload_onebox: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self.service = service
        self.gitlab = gitlab
        self.config = config
        self.store = store or ReviewCoordinationStore(config.coordination_db_path)
        self.artifacts = ReviewArtifactStore(config.report_dir)
        self.delivery = SingleMrDeliveryCoordinator(
            gitlab,
            config,
            self.store,
            upload_onebox=upload_onebox,
        )
        self.owner_id = f"process-{uuid.uuid4().hex[:12]}"
        self.store.interrupt_expired_runs()

    def register(
        self,
        target: MergeRequestReviewTarget,
        trigger: SingleMrTrigger,
    ) -> TriggerRegistration:
        registration = self.store.register_trigger(
            TriggerIntent(
                source=trigger.source,
                trigger_id=trigger.trigger_id,
                project_path=target.project_path,
                mr_iid=target.mr_iid,
                head_sha=target.head_sha,
                post_comment=trigger.post_comment,
                upload_onebox=trigger.upload_onebox,
            )
        )
        LOG.info(
            "review_run_id=%s trigger_id=%s source=%s repo=%s mr_iid=%s head_sha=%s "
            "stage=trigger_registered outcome=%s",
            registration.review_run_id,
            trigger.trigger_id,
            trigger.source,
            target.project_path,
            target.mr_iid,
            target.head_sha,
            registration.disposition,
        )
        return registration

    def refresh_target(self, target: MergeRequestReviewTarget) -> MergeRequestReviewTarget:
        """Resolve the current MR head before creating or joining a ReviewRun."""
        detail = self.gitlab.get_mr_detail_for_discussion_position(target)
        diff_refs = detail.get("diff_refs")
        refs = diff_refs if isinstance(diff_refs, dict) else {}
        head_sha = refs.get("head_sha") or detail.get("sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise ValueError("GitLab MR detail response missing current head SHA")
        base_sha = refs.get("base_sha") or refs.get("start_sha") or target.base_sha
        return replace(
            target,
            head_sha=head_sha,
            base_sha=str(base_sha) if base_sha else None,
            title=str(detail.get("title") or target.title),
            source_branch=str(detail.get("source_branch") or target.source_branch),
            target_branch=str(detail.get("target_branch") or target.target_branch),
        )

    def handle(
        self,
        target: MergeRequestReviewTarget,
        trigger: SingleMrTrigger,
    ) -> SingleMrOutcome:
        current_target = self.refresh_target(target)
        registration = self.register(current_target, trigger)
        return self.process(registration, current_target)

    def process(
        self,
        registration: TriggerRegistration,
        target: MergeRequestReviewTarget,
    ) -> SingleMrOutcome:
        deadline = time.monotonic() + self.config.task_timeout_seconds
        while time.monotonic() < deadline:
            run = self.store.get_run(registration.review_run_id)
            if run.status == "succeeded":
                report = self.artifacts.load(run.report_json_path)
                delivered, status = self.delivery.deliver(run, target, report, self.owner_id)
                return self._finalize_outcome(run, registration, delivered, status)
            if run.status in {"failed", "interrupted", "superseded"}:
                report = self.artifacts.load(run.report_json_path) if run.report_json_path else None
                return self._outcome(run, registration, run.status, report)
            if run.status == "queued" and self.store.claim_review(run.review_run_id, self.owner_id):
                LOG.info(
                    "task=%s review_run_id=%s trigger_id=%s repo=%s mr_iid=%s head_sha=%s "
                    "stage=review_claim outcome=owner",
                    run.review_run_id,
                    run.review_run_id,
                    registration.trigger_id,
                    run.project_path,
                    run.mr_iid,
                    run.head_sha,
                )
                return self._execute_owned(run.review_run_id, registration, target)
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for review run: {registration.review_run_id}")

    def _execute_owned(
        self,
        review_run_id: str,
        registration: TriggerRegistration,
        target: MergeRequestReviewTarget,
    ) -> SingleMrOutcome:
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(review_run_id, stop_heartbeat),
            name=f"{review_run_id}-lease",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._raise_if_superseded(review_run_id)
            report = self.service.review_target(
                target,
                self.config,
                task_id=review_run_id,
                structured_output=True,
                should_cancel=lambda: self.store.is_superseded(review_run_id),
            )
            report = _complete_identity(report, target)
            parse_structured_review_result(report.markdown)
            if self.store.is_superseded(review_run_id):
                return self._finish_owned_superseded(review_run_id, registration, report)

            run = self.store.get_run(review_run_id)
            paths = self.artifacts.write(
                run,
                report,
                review_status="running",
                triggers=self.store.list_triggers(review_run_id),
                deliveries=self.store.list_deliveries(review_run_id),
            )
            self.store.complete_review(review_run_id, self.owner_id, str(paths[0]), str(paths[1]))
            run = self.store.get_run(review_run_id)
            if run.status == "superseded":
                return self._finalize_terminal_superseded(run, registration, report)
            delivered, status = self.delivery.deliver(run, target, report, self.owner_id)
            return self._finalize_outcome(run, registration, delivered, status)
        except (ReviewSupersededError, ReviewCancelledError):
            report = _failure_report(target, None, "superseded")
            return self._finish_owned_superseded(review_run_id, registration, report)
        except Exception as exc:  # noqa: BLE001 - 顶层必须持久化失败并释放 lease。
            run = self.store.get_run(review_run_id)
            if run.superseded_by and run.status == "running":
                report = _failure_report(target, exc, "superseded")
                return self._finish_owned_superseded(review_run_id, registration, report)
            if run.status == "succeeded":
                LOG.warning(
                    "task=%s review_run_id=%s repo=%s mr_iid=%s head_sha=%s "
                    "stage=delivery outcome=failed error_type=%s",
                    review_run_id,
                    review_run_id,
                    run.project_path,
                    run.mr_iid,
                    run.head_sha,
                    type(exc).__name__,
                )
                try:
                    return self._finalize_outcome(
                        run,
                        registration,
                        report,
                        "success_with_warnings",
                    )
                except Exception:  # noqa: BLE001 - 审查已成功时不能因报告刷新失败改写状态。
                    return self._outcome(run, registration, "success_with_warnings", report)
            report = _failure_report(target, exc, "failed")
            report_json_path = ""
            report_markdown_path = ""
            try:
                paths = self.artifacts.write(
                    run,
                    report,
                    review_status="failed",
                    triggers=self.store.list_triggers(review_run_id),
                    deliveries=self.store.list_deliveries(review_run_id),
                    error=str(exc),
                )
                report_json_path, report_markdown_path = str(paths[0]), str(paths[1])
            except Exception as report_exc:  # noqa: BLE001 - 仍需释放 ReviewRun lease。
                LOG.error(
                    "task=%s review_run_id=%s repo=%s mr_iid=%s head_sha=%s "
                    "stage=local_report outcome=failed error_type=%s",
                    review_run_id,
                    review_run_id,
                    run.project_path,
                    run.mr_iid,
                    run.head_sha,
                    type(report_exc).__name__,
                )
            self.store.fail_review(
                review_run_id,
                self.owner_id,
                str(exc),
                report_json_path,
                report_markdown_path,
            )
            return self._outcome(self.store.get_run(review_run_id), registration, "failed", report)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)

    def _finish_owned_superseded(
        self,
        review_run_id: str,
        registration: TriggerRegistration,
        report: ReviewReport,
    ) -> SingleMrOutcome:
        run = self.store.get_run(review_run_id)
        replacement = self.store.get_run(run.superseded_by) if run.superseded_by else None
        validation = {
            "review_head_sha": run.head_sha,
            "current_head_sha": replacement.head_sha if replacement else "",
            "status": "stale",
        }
        report = replace(report, head_validation=validation)
        for sink, enabled in self.store.desired_sinks(review_run_id).items():
            self.store.record_delivery(
                review_run_id,
                sink,
                "skipped_stale" if enabled else "disabled",
            )
        report_json_path = ""
        report_markdown_path = ""
        try:
            paths = self.artifacts.write(
                run,
                report,
                review_status="superseded",
                triggers=self.store.list_triggers(review_run_id),
                deliveries=self.store.list_deliveries(review_run_id),
            )
            report_json_path, report_markdown_path = str(paths[0]), str(paths[1])
        except Exception as report_exc:  # noqa: BLE001 - 报告失败不能让旧 Head 永久占用 lease。
            LOG.error(
                "task=%s review_run_id=%s trigger_id=%s repo=%s mr_iid=%s head_sha=%s "
                "stage=local_report outcome=failed error_type=%s",
                review_run_id,
                review_run_id,
                registration.trigger_id,
                run.project_path,
                run.mr_iid,
                run.head_sha,
                type(report_exc).__name__,
            )
        self.store.finish_superseded(
            review_run_id,
            self.owner_id,
            report_json_path,
            report_markdown_path,
        )
        return self._outcome(self.store.get_run(review_run_id), registration, "superseded", report)

    def _finalize_outcome(
        self,
        run: ReviewRunRecord,
        registration: TriggerRegistration,
        report: ReviewReport,
        status: str,
    ) -> SingleMrOutcome:
        refreshed = self.store.get_run(run.review_run_id)
        review_status = "superseded" if status == "superseded" else refreshed.status
        paths = self.artifacts.write(
            refreshed,
            report,
            review_status=review_status,
            triggers=self.store.list_triggers(run.review_run_id),
            deliveries=self.store.list_deliveries(run.review_run_id),
        )
        return SingleMrOutcome(
            review_run_id=run.review_run_id,
            attempt=run.attempt,
            disposition=registration.disposition,
            status=status,
            report=report,
            report_json_path=str(paths[0]),
            report_markdown_path=str(paths[1]),
            deliveries=self.store.list_deliveries(run.review_run_id),
        )

    def _finalize_terminal_superseded(
        self,
        run: ReviewRunRecord,
        registration: TriggerRegistration,
        report: ReviewReport,
    ) -> SingleMrOutcome:
        replacement = self.store.get_run(run.superseded_by) if run.superseded_by else None
        report = replace(
            report,
            head_validation={
                "review_head_sha": run.head_sha,
                "current_head_sha": replacement.head_sha if replacement else "",
                "status": "stale",
            },
        )
        for sink, enabled in self.store.desired_sinks(run.review_run_id).items():
            self.store.record_delivery(
                run.review_run_id,
                sink,
                "skipped_stale" if enabled else "disabled",
            )
        return self._finalize_outcome(run, registration, report, "superseded")

    def _outcome(
        self,
        run: ReviewRunRecord,
        registration: TriggerRegistration,
        status: str,
        report: ReviewReport | None,
    ) -> SingleMrOutcome:
        return SingleMrOutcome(
            review_run_id=run.review_run_id,
            attempt=run.attempt,
            disposition=registration.disposition,
            status=status,
            report=report,
            report_json_path=run.report_json_path,
            report_markdown_path=run.report_markdown_path,
            deliveries=self.store.list_deliveries(run.review_run_id),
        )

    def _heartbeat(self, review_run_id: str, stop: threading.Event) -> None:
        interval = max(1.0, self.store.lease_seconds / 3)
        while not stop.wait(interval):
            if not self.store.renew_review_lease(review_run_id, self.owner_id):
                return

    def _raise_if_superseded(self, review_run_id: str) -> None:
        if self.store.is_superseded(review_run_id):
            raise ReviewSupersededError(review_run_id)


def _normalize_report(report: ReviewReport) -> ReviewReport:
    if report.structured_parse_status == "success" and report.finding_results is not None:
        return report
    rendered = render_structured_output_as_markdown(report)
    return replace(rendered, markdown=report.markdown)


def _complete_identity(report: ReviewReport, target: MergeRequestReviewTarget) -> ReviewReport:
    return replace(
        report,
        repo=report.repo or target.project_path,
        mr_iid=report.mr_iid or target.mr_iid,
        mr_url=report.mr_url or target.mr_url,
        source_branch=report.source_branch or target.source_branch,
        target_branch=report.target_branch or target.target_branch,
        head_sha=report.head_sha or target.head_sha,
        title=report.title or target.title,
    )


def _failure_report(
    target: MergeRequestReviewTarget,
    error: Exception | None,
    status: str,
) -> ReviewReport:
    report_context = error.report_context if isinstance(error, ReviewStageError) else {}
    return ReviewReport(
        markdown="",
        repo=target.project_path,
        mr_iid=target.mr_iid,
        mr_url=target.mr_url,
        source_branch=target.source_branch,
        target_branch=target.target_branch,
        base_sha=target.base_sha or "",
        head_sha=target.head_sha,
        changed_files=[],
        submission_owner="python",
        submission_status=status,
        failure_stage=error.stage if isinstance(error, ReviewStageError) else "",
        review_plan=error.review_plan if isinstance(error, ReviewStageError) else None,
        title=target.title,
        requested_review_mode=str(report_context.get("requested_review_mode") or ""),
        review_mode=str(report_context.get("review_mode") or ""),
        review_scope=str(report_context.get("review_scope") or "single"),
        dependency_context_status=str(report_context.get("dependency_context_status") or "not_applicable"),
        dependency_degradation_reason=str(report_context.get("dependency_degradation_reason") or ""),
        dependency_failed_project=str(report_context.get("dependency_failed_project") or ""),
        dependency_context_id=str(report_context.get("dependency_context_id") or ""),
        dependency_repositories=list(report_context.get("dependency_repositories") or []),
        dependency_preparation_seconds=report_context.get("dependency_preparation_seconds"),
        agent_call_count=error.agent_call_count if isinstance(error, ReviewStageError) else 0,
    )


def _onebox_file_name(run: ReviewRunRecord) -> str:
    project = re.sub(r"[^A-Za-z0-9_.-]+", "-", run.project_path.split("/")[-1]).strip("-") or "project"
    return f"review-{project}-mr-{run.mr_iid}-{run.head_sha[:12]}-{run.review_run_id}.md"
