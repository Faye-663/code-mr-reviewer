from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mr_reviewer.config import Config
from mr_reviewer.coordination import ReviewCoordinationStore, TriggerIntent
from mr_reviewer.reviewer import MergeRequestReviewTarget, ReviewReport
from mr_reviewer.single_mr import SingleMrReviewCoordinator, SingleMrTrigger


def _trigger(
    trigger_id: str,
    *,
    source: str = "webhook",
    head_sha: str = "head-a",
    post_comment: bool = True,
    upload_onebox: bool = False,
) -> TriggerIntent:
    return TriggerIntent(
        source=source,
        trigger_id=trigger_id,
        project_path="team/project",
        mr_iid=7,
        head_sha=head_sha,
        post_comment=post_comment,
        upload_onebox=upload_onebox,
    )


def test_same_review_key_has_one_owner_and_joins_running_run(tmp_path: Path):
    database = tmp_path / "coordination.sqlite3"
    first = ReviewCoordinationStore(database)
    second = ReviewCoordinationStore(database)

    created = first.register_trigger(_trigger("hook-1"))
    joined = second.register_trigger(_trigger("im-1", source="im", upload_onebox=True))

    assert created.disposition == "created"
    assert joined.disposition == "joined"
    assert joined.review_run_id == created.review_run_id
    assert first.claim_review(created.review_run_id, "worker-a")
    assert not second.claim_review(joined.review_run_id, "worker-b")
    assert first.desired_sinks(created.review_run_id) == {
        "gitlab": True,
        "onebox": True,
    }


def test_duplicate_trigger_is_not_registered_twice(tmp_path: Path):
    store = ReviewCoordinationStore(tmp_path / "coordination.sqlite3")

    first = store.register_trigger(_trigger("hook-1"))
    duplicate = store.register_trigger(_trigger("hook-1"))

    assert duplicate.disposition == "duplicate"
    assert duplicate.review_run_id == first.review_run_id
    assert len(store.list_triggers(first.review_run_id)) == 1


def test_successful_run_is_reused_but_failed_run_gets_new_attempt(tmp_path: Path):
    store = ReviewCoordinationStore(tmp_path / "coordination.sqlite3")
    first = store.register_trigger(_trigger("hook-1"))
    assert store.claim_review(first.review_run_id, "worker-a")
    store.complete_review(first.review_run_id, "worker-a", "report.json", "report.md")

    reused = store.register_trigger(_trigger("im-1", source="im"))
    assert reused.disposition == "reused"
    assert reused.review_run_id == first.review_run_id

    failed = store.register_trigger(_trigger("hook-failed", head_sha="head-b"))
    assert store.claim_review(failed.review_run_id, "worker-a")
    store.fail_review(failed.review_run_id, "worker-a", "agent failed")
    retried = store.register_trigger(_trigger("hook-retry", head_sha="head-b"))

    assert retried.disposition == "created"
    assert retried.review_run_id != failed.review_run_id
    assert retried.attempt == 2


def test_new_head_supersedes_unfinished_run_and_preserves_global_slot(tmp_path: Path):
    store = ReviewCoordinationStore(tmp_path / "coordination.sqlite3")
    old = store.register_trigger(_trigger("hook-a", head_sha="head-a"))
    assert store.claim_review(old.review_run_id, "worker-a")

    new = store.register_trigger(_trigger("hook-b", head_sha="head-b"))

    old_record = store.get_run(old.review_run_id)
    assert old_record.superseded_by == new.review_run_id
    assert old_record.status == "running"
    assert store.is_superseded(old.review_run_id)
    assert store.renew_review_lease(old.review_run_id, "worker-a")
    assert not store.claim_review(new.review_run_id, "worker-b")

    store.finish_superseded(old.review_run_id, "worker-a", "old.json", "old.md")
    assert store.claim_review(new.review_run_id, "worker-b")


def test_expired_running_lease_is_interrupted_and_not_recovered_automatically(tmp_path: Path):
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    store = ReviewCoordinationStore(
        tmp_path / "coordination.sqlite3",
        now=lambda: now,
        lease_seconds=30,
    )
    registration = store.register_trigger(_trigger("hook-1"))
    assert store.claim_review(registration.review_run_id, "worker-a")

    later = ReviewCoordinationStore(
        store.path,
        now=lambda: now + timedelta(seconds=31),
        lease_seconds=30,
    )
    later.interrupt_expired_runs()

    assert later.get_run(registration.review_run_id).status == "interrupted"
    assert later.get_run(registration.review_run_id).owner_id == ""


def test_delivery_claim_retries_failed_sink_but_not_succeeded_or_unknown(tmp_path: Path):
    store = ReviewCoordinationStore(tmp_path / "coordination.sqlite3")
    registration = store.register_trigger(_trigger("hook-1"))
    assert store.claim_review(registration.review_run_id, "worker-a")
    store.complete_review(registration.review_run_id, "worker-a", "report.json", "report.md")

    assert store.claim_delivery(registration.review_run_id, "gitlab", "worker-a")
    store.finish_delivery(registration.review_run_id, "gitlab", "worker-a", "failed", error="HTTP 500")
    assert store.claim_delivery(registration.review_run_id, "gitlab", "worker-b")
    store.finish_delivery(registration.review_run_id, "gitlab", "worker-b", "succeeded")
    assert not store.claim_delivery(registration.review_run_id, "gitlab", "worker-c")

    assert store.claim_delivery(registration.review_run_id, "onebox", "worker-a")
    store.finish_delivery(registration.review_run_id, "onebox", "worker-a", "unknown")
    assert not store.claim_delivery(registration.review_run_id, "onebox", "worker-b")


def test_config_preserves_entry_delivery_defaults(tmp_path: Path, monkeypatch):
    for name in (
        "MR_REVIEWER_IM_POST_COMMENT",
        "MR_REVIEWER_IM_UPLOAD_ONEBOX",
        "MR_REVIEWER_WEBHOOK_POST_COMMENT",
        "MR_REVIEWER_WEBHOOK_UPLOAD_ONEBOX",
        "MR_REVIEWER_COORDINATION_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.im_post_comment is False
    assert config.im_upload_onebox is True
    assert config.webhook_post_comment is True
    assert config.webhook_upload_onebox is False
    assert config.coordination_db_path == Path("log/review-coordination.sqlite3")


def test_config_reads_independent_entry_delivery_switches(tmp_path: Path, monkeypatch):
    for name in (
        "MR_REVIEWER_IM_POST_COMMENT",
        "MR_REVIEWER_IM_UPLOAD_ONEBOX",
        "MR_REVIEWER_WEBHOOK_POST_COMMENT",
        "MR_REVIEWER_WEBHOOK_UPLOAD_ONEBOX",
        "MR_REVIEWER_COORDINATION_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "coordination.sqlite3"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        "MR_REVIEWER_IM_POST_COMMENT=true\n"
        "MR_REVIEWER_IM_UPLOAD_ONEBOX=false\n"
        "MR_REVIEWER_WEBHOOK_POST_COMMENT=false\n"
        "MR_REVIEWER_WEBHOOK_UPLOAD_ONEBOX=true\n"
        f"MR_REVIEWER_COORDINATION_DB_PATH={database}\n",
        encoding="utf-8",
    )

    config = Config.from_env(env_file)

    assert config.im_post_comment is True
    assert config.im_upload_onebox is False
    assert config.webhook_post_comment is False
    assert config.webhook_upload_onebox is True
    assert config.coordination_db_path == database


def test_coordinator_reuses_review_and_delivers_each_sink_once(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        agent_model_name="GLM5",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
    )
    service = _ReviewService()
    gitlab = _GitLabClient()
    uploads: list[tuple[str, str]] = []
    coordinator = SingleMrReviewCoordinator(
        service,
        gitlab,
        config,
        upload_onebox=lambda file_name, markdown: uploads.append((file_name, markdown)) or None,
    )

    webhook = coordinator.handle(
        _target(),
        SingleMrTrigger("webhook", "hook-1", post_comment=True, upload_onebox=False),
    )
    im = coordinator.handle(
        _target(),
        SingleMrTrigger("im", "message-1", post_comment=True, upload_onebox=True),
    )

    assert webhook.review_run_id == im.review_run_id
    assert im.disposition == "reused"
    assert service.calls == 1
    assert len(gitlab.discussions) == 1
    assert len(uploads) == 1
    reports = list(config.report_dir.glob("*.json"))
    assert len(reports) == 1
    payload = __import__("json").loads(reports[0].read_text(encoding="utf-8"))
    assert len(payload["triggers"]) == 2
    assert payload["deliveries"]["gitlab"]["status"] == "succeeded"
    assert payload["deliveries"]["onebox"]["status"] == "succeeded"


def test_coordinator_marks_stale_review_and_skips_both_external_sinks(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        agent_model_name="GLM5",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
    )
    service = _ReviewService()
    gitlab = _GitLabClient(current_head_sha="head-a")
    uploads: list[tuple[str, str]] = []
    coordinator = SingleMrReviewCoordinator(
        service,
        gitlab,
        config,
        upload_onebox=lambda file_name, markdown: uploads.append((file_name, markdown)) or None,
    )
    original_review = service.review_target

    def review_then_advance_head(*args, **kwargs):
        report = original_review(*args, **kwargs)
        gitlab.current_head_sha = "head-b"
        return report

    service.review_target = review_then_advance_head

    outcome = coordinator.handle(
        _target(head_sha="head-a"),
        SingleMrTrigger("webhook", "hook-a", post_comment=True, upload_onebox=True),
    )

    assert outcome.status == "superseded"
    assert gitlab.discussions == []
    assert uploads == []
    payload = __import__("json").loads(next(config.report_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["review_status"] == "superseded"
    assert payload["head_validation"]["status"] == "stale"
    assert payload["deliveries"]["gitlab"]["status"] == "skipped_stale"
    assert payload["deliveries"]["onebox"]["status"] == "skipped_stale"


def test_coordinator_retries_review_failure_with_new_attempt(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
    )
    service = _ReviewService(failures=1)
    coordinator = SingleMrReviewCoordinator(service, _GitLabClient(), config)

    failed = coordinator.handle(
        _target(),
        SingleMrTrigger("webhook", "hook-1", post_comment=False, upload_onebox=False),
    )
    retried = coordinator.handle(
        _target(),
        SingleMrTrigger("webhook", "hook-2", post_comment=False, upload_onebox=False),
    )

    assert failed.status == "failed"
    assert retried.status == "succeeded"
    assert retried.attempt == 2
    assert service.calls == 2
    assert len(list(config.report_dir.glob("*.json"))) == 2


def test_coordinator_reuses_review_when_retrying_failed_onebox_delivery(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
    )
    service = _ReviewService()
    upload_calls = 0

    def upload(file_name: str, markdown: str) -> str | None:
        nonlocal upload_calls
        upload_calls += 1
        return "temporary OneBox failure" if upload_calls == 1 else None

    coordinator = SingleMrReviewCoordinator(
        service,
        _GitLabClient(),
        config,
        upload_onebox=upload,
    )

    first = coordinator.handle(
        _target(),
        SingleMrTrigger("im", "message-1", post_comment=False, upload_onebox=True),
    )
    retried = coordinator.handle(
        _target(),
        SingleMrTrigger("im", "message-2", post_comment=False, upload_onebox=True),
    )

    assert first.status == "success_with_warnings"
    assert retried.status == "succeeded"
    assert retried.disposition == "reused"
    assert service.calls == 1
    assert upload_calls == 2
    assert retried.deliveries["onebox"]["attempts"] == 2


def test_coordinator_records_disabled_sinks_in_local_report(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
    )
    coordinator = SingleMrReviewCoordinator(_ReviewService(), _GitLabClient(), config)

    outcome = coordinator.handle(
        _target(),
        SingleMrTrigger("im", "message-1", post_comment=False, upload_onebox=False),
    )

    assert outcome.status == "succeeded"
    assert outcome.deliveries["gitlab"]["status"] == "disabled"
    assert outcome.deliveries["onebox"]["status"] == "disabled"
    assert Path(outcome.report_json_path).is_file()
    assert Path(outcome.report_markdown_path).is_file()


def test_local_report_write_failure_blocks_delivery_and_releases_review_lease(
    tmp_path: Path,
    monkeypatch,
):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        agent_model_name="GLM5",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
    )
    gitlab = _GitLabClient()
    uploads: list[tuple[str, str]] = []
    coordinator = SingleMrReviewCoordinator(
        _ReviewService(),
        gitlab,
        config,
        upload_onebox=lambda file_name, markdown: uploads.append((file_name, markdown)) or None,
    )
    monkeypatch.setattr(
        coordinator.artifacts,
        "write",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    outcome = coordinator.handle(
        _target(),
        SingleMrTrigger("webhook", "hook-1", post_comment=True, upload_onebox=True),
    )

    assert outcome.status == "failed"
    assert coordinator.store.get_run(outcome.review_run_id).status == "failed"
    assert gitlab.discussions == []
    assert uploads == []


def test_new_head_cancels_active_run_before_next_run_starts(tmp_path: Path):
    config = Config(
        gitlab_base_url="https://gitlab.example.com",
        report_dir=tmp_path / "reports",
        coordination_db_path=tmp_path / "coordination.sqlite3",
        task_timeout_seconds=10,
    )
    started = threading.Event()
    calls: list[str] = []

    class SupersedableService(_ReviewService):
        def review_target(self, target, config, task_id, structured_output=True, should_cancel=None):
            calls.append(target.head_sha)
            if target.head_sha == "head-a":
                started.set()
                while not should_cancel():
                    time.sleep(0.01)
                raise RuntimeError("old head cancelled")
            return super().review_target(target, config, task_id, structured_output, should_cancel)

    service = SupersedableService()
    first = SingleMrReviewCoordinator(service, _GitLabClient(), config)
    second = SingleMrReviewCoordinator(service, _GitLabClient(), config)
    old = first.register(
        _target("head-a"),
        SingleMrTrigger("webhook", "hook-a", post_comment=False, upload_onebox=False),
    )
    outcomes = []
    thread = threading.Thread(
        target=lambda: outcomes.append(first.process(old, _target("head-a"))),
        daemon=True,
    )
    thread.start()
    assert started.wait(timeout=2)

    new = second.register(
        _target("head-b"),
        SingleMrTrigger("webhook", "hook-b", post_comment=False, upload_onebox=False),
    )
    latest = second.process(new, _target("head-b"))
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcomes[0].status == "superseded"
    assert latest.status == "succeeded"
    assert calls == ["head-a", "head-b"]
    assert first.store.get_run(old.review_run_id).superseded_by == new.review_run_id
    assert len(list(config.report_dir.glob("*.json"))) == 2


def _target(head_sha: str = "head-sha") -> MergeRequestReviewTarget:
    return MergeRequestReviewTarget(
        base_url="https://gitlab.example.com",
        project_path="team/project",
        mr_iid=7,
        mr_url="https://gitlab.example.com/team/project/merge_requests/7",
        target_repo_url="https://gitlab.example.com/team/project.git",
        source_repo_url="https://gitlab.example.com/team/project.git",
        target_branch="main",
        source_branch="feature/auth",
        base_sha="base-sha",
        head_sha=head_sha,
        title="Fix auth",
    )


class _ReviewService:
    def __init__(self, failures: int = 0):
        self.calls = 0
        self.failures = failures

    def review_target(self, target, config, task_id, structured_output=True, should_cancel=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("agent failed")
        return ReviewReport(
            markdown=(
                '{"findings":[{"rule_id":"R1","severity":"major","confidence":"HIGH",'
                '"old_path":"app.py","new_path":"app.py","old_line":-1,"new_line":2,'
                '"title":"问题","evidence":"证据","impact":"影响","suggestion":"建议"}],'
                '"notes":[],"test_gaps":[]}'
            ),
            repo=target.project_path,
            mr_iid=target.mr_iid,
            mr_url=target.mr_url,
            source_branch=target.source_branch,
            target_branch=target.target_branch,
            base_sha="base-sha",
            head_sha=target.head_sha,
            changed_files=["app.py"],
            diff=(
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,1 +1,2 @@\n"
                " old\n"
                "+added\n"
            ),
        )


class _GitLabClient:
    def __init__(self, current_head_sha: str = "head-sha"):
        self.current_head_sha = current_head_sha
        self.discussions: list[dict] = []

    def get_mr_detail_for_discussion_position(self, target):
        return {
            "diff_refs": {
                "base_sha": "base-sha",
                "start_sha": "start-sha",
                "head_sha": self.current_head_sha,
            }
        }

    def list_mr_discussions(self, target):
        return []

    def post_mr_discussion(self, target, body, severity, position):
        self.discussions.append(
            {"target": target, "body": body, "severity": severity, "position": position}
        )
        return {"id": "discussion-1", "notes": [{"id": 1}]}
