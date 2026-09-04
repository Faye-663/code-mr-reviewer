from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator


ACTIVE_REVIEW_STATUSES = ("queued", "running")
TERMINAL_REVIEW_STATUSES = ("succeeded", "failed", "interrupted", "superseded")


@dataclass(frozen=True, slots=True)
class TriggerIntent:
    source: str
    trigger_id: str
    project_path: str
    mr_iid: int
    head_sha: str
    post_comment: bool
    upload_onebox: bool


@dataclass(frozen=True, slots=True)
class TriggerRegistration:
    review_run_id: str
    attempt: int
    disposition: str
    source: str
    trigger_id: str


@dataclass(frozen=True, slots=True)
class ReviewRunRecord:
    review_run_id: str
    project_path: str
    mr_iid: int
    head_sha: str
    attempt: int
    status: str
    owner_id: str
    lease_until: float | None
    report_json_path: str
    report_markdown_path: str
    superseded_by: str
    error: str
    created_at: str
    updated_at: str

    @property
    def review_key(self) -> str:
        return f"{self.project_path}!{self.mr_iid}:{self.head_sha}"


class ReviewCoordinationStore:
    """SQLite-backed, single-host coordination for IM and webhook processes."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self.path = Path(path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def register_trigger(self, intent: TriggerIntent) -> TriggerRegistration:
        self._validate_intent(intent)
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT review_run_id FROM triggers WHERE source = ? AND trigger_id = ?",
                (intent.source, intent.trigger_id),
            ).fetchone()
            if duplicate is not None:
                run = self._get_run(connection, str(duplicate["review_run_id"]))
                return TriggerRegistration(
                    run.review_run_id,
                    run.attempt,
                    "duplicate",
                    intent.source,
                    intent.trigger_id,
                )

            candidate = connection.execute(
                """
                SELECT * FROM review_runs
                WHERE project_path = ? AND mr_iid = ? AND head_sha = ?
                ORDER BY attempt DESC LIMIT 1
                """,
                (intent.project_path, intent.mr_iid, intent.head_sha),
            ).fetchone()
            if candidate is not None and candidate["status"] in ACTIVE_REVIEW_STATUSES:
                run = self._run_from_row(candidate)
                disposition = "joined"
            elif candidate is not None and candidate["status"] == "succeeded":
                run = self._run_from_row(candidate)
                disposition = "reused"
            else:
                attempt = 1 if candidate is None else int(candidate["attempt"]) + 1
                run_id = f"review-{uuid.uuid4().hex[:16]}"
                timestamp = self._iso_now()
                connection.execute(
                    """
                    INSERT INTO review_runs (
                        review_run_id, project_path, mr_iid, head_sha, attempt,
                        status, owner_id, lease_until, report_json_path,
                        report_markdown_path, superseded_by, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', '', NULL, '', '', '', '', ?, ?)
                    """,
                    (
                        run_id,
                        intent.project_path,
                        intent.mr_iid,
                        intent.head_sha,
                        attempt,
                        timestamp,
                        timestamp,
                    ),
                )
                run = self._get_run(connection, run_id)
                disposition = "created"

            self._supersede_older_heads(connection, intent, run.review_run_id)
            connection.execute(
                """
                INSERT INTO triggers (
                    source, trigger_id, review_run_id, post_comment,
                    upload_onebox, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.source,
                    intent.trigger_id,
                    run.review_run_id,
                    int(intent.post_comment),
                    int(intent.upload_onebox),
                    self._iso_now(),
                ),
            )
            return TriggerRegistration(
                run.review_run_id,
                run.attempt,
                disposition,
                intent.source,
                intent.trigger_id,
            )

    def claim_review(self, review_run_id: str, owner_id: str) -> bool:
        with self._transaction() as connection:
            self._interrupt_expired_runs(connection)
            run = self._get_run(connection, review_run_id)
            if run.status == "running" and run.owner_id == owner_id:
                return True
            if run.status != "queued" or run.superseded_by:
                return False
            active = connection.execute(
                """
                SELECT 1 FROM review_runs
                WHERE status = 'running' AND lease_until > ? AND review_run_id <> ?
                LIMIT 1
                """,
                (self._epoch_now(), review_run_id),
            ).fetchone()
            if active is not None:
                return False
            updated = connection.execute(
                """
                UPDATE review_runs
                SET status = 'running', owner_id = ?, lease_until = ?, updated_at = ?
                WHERE review_run_id = ? AND status = 'queued' AND superseded_by = ''
                """,
                (
                    owner_id,
                    self._lease_until(),
                    self._iso_now(),
                    review_run_id,
                ),
            )
            return updated.rowcount == 1

    def renew_review_lease(self, review_run_id: str, owner_id: str) -> bool:
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE review_runs SET lease_until = ?, updated_at = ?
                WHERE review_run_id = ? AND status = 'running' AND owner_id = ?
                """,
                (self._lease_until(), self._iso_now(), review_run_id, owner_id),
            )
            # superseded owner 仍需续租到协作检查点退出，避免新旧 Agent 物理并行。
            return updated.rowcount == 1

    def complete_review(
        self,
        review_run_id: str,
        owner_id: str,
        report_json_path: str,
        report_markdown_path: str,
    ) -> None:
        with self._transaction() as connection:
            run = self._get_run(connection, review_run_id)
            status = "superseded" if run.superseded_by else "succeeded"
            self._finish_review(
                connection,
                review_run_id,
                owner_id,
                status,
                report_json_path,
                report_markdown_path,
                "",
            )

    def finish_superseded(
        self,
        review_run_id: str,
        owner_id: str,
        report_json_path: str,
        report_markdown_path: str,
    ) -> None:
        with self._transaction() as connection:
            self._finish_review(
                connection,
                review_run_id,
                owner_id,
                "superseded",
                report_json_path,
                report_markdown_path,
                "",
            )

    def fail_review(
        self,
        review_run_id: str,
        owner_id: str,
        error: str,
        report_json_path: str = "",
        report_markdown_path: str = "",
    ) -> None:
        with self._transaction() as connection:
            run = self._get_run(connection, review_run_id)
            status = "superseded" if run.superseded_by else "failed"
            self._finish_review(
                connection,
                review_run_id,
                owner_id,
                status,
                report_json_path,
                report_markdown_path,
                error,
            )

    def interrupt_expired_runs(self) -> int:
        with self._transaction() as connection:
            return self._interrupt_expired_runs(connection)

    def get_run(self, review_run_id: str) -> ReviewRunRecord:
        with self._connect() as connection:
            return self._get_run(connection, review_run_id)

    def list_triggers(self, review_run_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source, trigger_id, post_comment, upload_onebox, created_at
                FROM triggers WHERE review_run_id = ? ORDER BY created_at, source, trigger_id
                """,
                (review_run_id,),
            ).fetchall()
        return [
            {
                "source": str(row["source"]),
                "trigger_id": str(row["trigger_id"]),
                "post_comment": bool(row["post_comment"]),
                "upload_onebox": bool(row["upload_onebox"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def desired_sinks(self, review_run_id: str) -> dict[str, bool]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(post_comment) AS post_comment,
                       MAX(upload_onebox) AS upload_onebox
                FROM triggers WHERE review_run_id = ?
                """,
                (review_run_id,),
            ).fetchone()
        return {
            "gitlab": bool(row["post_comment"]) if row is not None else False,
            "onebox": bool(row["upload_onebox"]) if row is not None else False,
        }

    def is_superseded(self, review_run_id: str) -> bool:
        return bool(self.get_run(review_run_id).superseded_by)

    def claim_delivery(self, review_run_id: str, sink: str, owner_id: str) -> bool:
        self._validate_sink(sink)
        with self._transaction() as connection:
            self._interrupt_expired_deliveries(connection)
            run = self._get_run(connection, review_run_id)
            if run.status != "succeeded" or run.superseded_by:
                return False
            current = connection.execute(
                "SELECT * FROM deliveries WHERE review_run_id = ? AND sink = ?",
                (review_run_id, sink),
            ).fetchone()
            if current is None:
                connection.execute(
                    """
                    INSERT INTO deliveries (
                        review_run_id, sink, status, attempts, owner_id,
                        lease_until, error, external_ref, updated_at
                    ) VALUES (?, ?, 'running', 1, ?, ?, '', '', ?)
                    """,
                    (review_run_id, sink, owner_id, self._lease_until(), self._iso_now()),
                )
                return True
            if current["status"] not in {"failed", "disabled", "pending"}:
                return False
            connection.execute(
                """
                UPDATE deliveries
                SET status = 'running', attempts = attempts + 1, owner_id = ?,
                    lease_until = ?, error = '', updated_at = ?
                WHERE review_run_id = ? AND sink = ?
                """,
                (owner_id, self._lease_until(), self._iso_now(), review_run_id, sink),
            )
            return True

    def finish_delivery(
        self,
        review_run_id: str,
        sink: str,
        owner_id: str,
        status: str,
        *,
        error: str = "",
        external_ref: str = "",
    ) -> None:
        self._validate_sink(sink)
        if status not in {"succeeded", "failed", "unknown", "skipped_stale", "disabled"}:
            raise ValueError(f"unsupported delivery status: {status}")
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE deliveries
                SET status = ?, owner_id = '', lease_until = NULL, error = ?,
                    external_ref = ?, updated_at = ?
                WHERE review_run_id = ? AND sink = ? AND status = 'running' AND owner_id = ?
                """,
                (
                    status,
                    error,
                    external_ref,
                    self._iso_now(),
                    review_run_id,
                    sink,
                    owner_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"delivery lease is not owned: {review_run_id}/{sink}")

    def renew_delivery_lease(self, review_run_id: str, sink: str, owner_id: str) -> bool:
        self._validate_sink(sink)
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE deliveries SET lease_until = ?, updated_at = ?
                WHERE review_run_id = ? AND sink = ?
                  AND status = 'running' AND owner_id = ?
                """,
                (self._lease_until(), self._iso_now(), review_run_id, sink, owner_id),
            )
            return updated.rowcount == 1

    def mark_delivery_disabled(self, review_run_id: str, sink: str) -> None:
        self._validate_sink(sink)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO deliveries (
                    review_run_id, sink, status, attempts, owner_id,
                    lease_until, error, external_ref, updated_at
                ) VALUES (?, ?, 'disabled', 0, '', NULL, '', '', ?)
                ON CONFLICT(review_run_id, sink) DO NOTHING
                """,
                (review_run_id, sink, self._iso_now()),
            )

    def record_delivery(
        self,
        review_run_id: str,
        sink: str,
        status: str,
        *,
        error: str = "",
        external_ref: str = "",
    ) -> None:
        self._validate_sink(sink)
        if status not in {"succeeded", "failed", "unknown", "skipped_stale", "disabled"}:
            raise ValueError(f"unsupported delivery status: {status}")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO deliveries (
                    review_run_id, sink, status, attempts, owner_id,
                    lease_until, error, external_ref, updated_at
                ) VALUES (?, ?, ?, 0, '', NULL, ?, ?, ?)
                ON CONFLICT(review_run_id, sink) DO UPDATE SET
                    status = excluded.status,
                    owner_id = '',
                    lease_until = NULL,
                    error = excluded.error,
                    external_ref = excluded.external_ref,
                    updated_at = excluded.updated_at
                """,
                (review_run_id, sink, status, error, external_ref, self._iso_now()),
            )

    def list_deliveries(self, review_run_id: str) -> dict[str, dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deliveries WHERE review_run_id = ? ORDER BY sink",
                (review_run_id,),
            ).fetchall()
        return {
            str(row["sink"]): {
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "error": str(row["error"]),
                "external_ref": str(row["external_ref"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        }

    def get_delivery(self, review_run_id: str, sink: str) -> dict[str, object] | None:
        self._validate_sink(sink)
        return self.list_deliveries(review_run_id).get(sink)

    def mark_run_superseded(self, review_run_id: str, superseded_by: str = "") -> None:
        with self._transaction() as connection:
            run = self._get_run(connection, review_run_id)
            if run.status in TERMINAL_REVIEW_STATUSES and run.status != "succeeded":
                return
            next_status = "running" if run.status == "running" else "superseded"
            connection.execute(
                """
                UPDATE review_runs
                SET status = ?, superseded_by = ?, updated_at = ?
                WHERE review_run_id = ?
                """,
                (next_status, superseded_by, self._iso_now(), review_run_id),
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_runs (
                    review_run_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    mr_iid INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    lease_until REAL,
                    report_json_path TEXT NOT NULL DEFAULT '',
                    report_markdown_path TEXT NOT NULL DEFAULT '',
                    superseded_by TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_path, mr_iid, head_sha, attempt)
                );
                CREATE INDEX IF NOT EXISTS review_runs_lookup
                    ON review_runs(project_path, mr_iid, head_sha, attempt DESC);
                CREATE INDEX IF NOT EXISTS review_runs_active
                    ON review_runs(status, lease_until);

                CREATE TABLE IF NOT EXISTS triggers (
                    source TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    review_run_id TEXT NOT NULL REFERENCES review_runs(review_run_id),
                    post_comment INTEGER NOT NULL,
                    upload_onebox INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source, trigger_id)
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    review_run_id TEXT NOT NULL REFERENCES review_runs(review_run_id),
                    sink TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    owner_id TEXT NOT NULL DEFAULT '',
                    lease_until REAL,
                    error TEXT NOT NULL DEFAULT '',
                    external_ref TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(review_run_id, sink)
                );
                """
            )

    def _supersede_older_heads(
        self,
        connection: sqlite3.Connection,
        intent: TriggerIntent,
        replacement_run_id: str,
    ) -> None:
        timestamp = self._iso_now()
        connection.execute(
            """
            UPDATE review_runs
            SET status = CASE WHEN status = 'queued' THEN 'superseded' ELSE status END,
                superseded_by = ?, updated_at = ?
            WHERE project_path = ? AND mr_iid = ? AND head_sha <> ?
              AND status IN ('queued', 'running') AND superseded_by = ''
            """,
            (
                replacement_run_id,
                timestamp,
                intent.project_path,
                intent.mr_iid,
                intent.head_sha,
            ),
        )

    def _finish_review(
        self,
        connection: sqlite3.Connection,
        review_run_id: str,
        owner_id: str,
        status: str,
        report_json_path: str,
        report_markdown_path: str,
        error: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE review_runs
            SET status = ?, owner_id = '', lease_until = NULL,
                report_json_path = ?, report_markdown_path = ?, error = ?, updated_at = ?
            WHERE review_run_id = ? AND status = 'running' AND owner_id = ?
            """,
            (
                status,
                report_json_path,
                report_markdown_path,
                error,
                self._iso_now(),
                review_run_id,
                owner_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"review lease is not owned: {review_run_id}")

    def _interrupt_expired_runs(self, connection: sqlite3.Connection) -> int:
        updated = connection.execute(
            """
            UPDATE review_runs
            SET status = 'interrupted', owner_id = '', lease_until = NULL,
                error = 'review lease expired', updated_at = ?
            WHERE status = 'running' AND lease_until <= ?
            """,
            (self._iso_now(), self._epoch_now()),
        )
        return updated.rowcount

    def _interrupt_expired_deliveries(self, connection: sqlite3.Connection) -> None:
        now = self._epoch_now()
        timestamp = self._iso_now()
        connection.execute(
            """
            UPDATE deliveries
            SET status = 'unknown', owner_id = '', lease_until = NULL,
                error = 'OneBox upload outcome is unknown after lease expiry', updated_at = ?
            WHERE status = 'running' AND sink = 'onebox' AND lease_until <= ?
            """,
            (timestamp, now),
        )
        connection.execute(
            """
            UPDATE deliveries
            SET status = 'failed', owner_id = '', lease_until = NULL,
                error = 'GitLab delivery interrupted; markers will be checked before retry', updated_at = ?
            WHERE status = 'running' AND sink = 'gitlab' AND lease_until <= ?
            """,
            (timestamp, now),
        )

    def _get_run(self, connection: sqlite3.Connection, review_run_id: str) -> ReviewRunRecord:
        row = connection.execute(
            "SELECT * FROM review_runs WHERE review_run_id = ?",
            (review_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown review run: {review_run_id}")
        return self._run_from_row(row)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ReviewRunRecord:
        return ReviewRunRecord(
            review_run_id=str(row["review_run_id"]),
            project_path=str(row["project_path"]),
            mr_iid=int(row["mr_iid"]),
            head_sha=str(row["head_sha"]),
            attempt=int(row["attempt"]),
            status=str(row["status"]),
            owner_id=str(row["owner_id"]),
            lease_until=float(row["lease_until"]) if row["lease_until"] is not None else None,
            report_json_path=str(row["report_json_path"]),
            report_markdown_path=str(row["report_markdown_path"]),
            superseded_by=str(row["superseded_by"]),
            error=str(row["error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _lease_until(self) -> float:
        return self._epoch_now() + self.lease_seconds

    def _epoch_now(self) -> float:
        return self._now().timestamp()

    def _iso_now(self) -> str:
        return self._now().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _validate_intent(intent: TriggerIntent) -> None:
        if intent.source not in {"im", "webhook"}:
            raise ValueError(f"unsupported trigger source: {intent.source}")
        if not intent.trigger_id or not intent.project_path or not intent.head_sha or intent.mr_iid <= 0:
            raise ValueError("trigger identity is incomplete")

    @staticmethod
    def _validate_sink(sink: str) -> None:
        if sink not in {"gitlab", "onebox"}:
            raise ValueError(f"unsupported delivery sink: {sink}")
