import json
from dataclasses import replace
from pathlib import Path

from mr_reviewer.cli import healthcheck
from mr_reviewer.config import Config
from mr_reviewer.markdown_report import render_structured_output_as_markdown
from mr_reviewer.reviewer import MergeRequestReviewTarget, ReviewReport
from mr_reviewer.webhook import WebhookReviewEvent, write_webhook_monitor_report


def _raw_result() -> str:
    return json.dumps({"findings": [], "notes": [], "test_gaps": [], "good": []})


def _complete_report() -> ReviewReport:
    return ReviewReport(
        markdown=_raw_result(),
        review_plan={
            "schema_version": "dependency-review-plan/v1",
            "primary_focus": {
                "change_intent": ["更新依赖调用"],
                "critical_paths": [
                    {"path": "app.py", "reason": "调用入口", "verify": ["返回值契约"]}
                ],
                "test_risks": ["缺少契约测试"],
            },
            "relationships": [],
            "open_questions": [],
        },
        repo="team/app",
        mr_iid=7,
        mr_url="https://gitlab.example.com/team/app/merge_requests/7",
        source_branch="feature/contract",
        target_branch="release",
        base_sha="a" * 40,
        head_sha="b" * 40,
        changed_files=["app.py"],
        title="【Deep-Review】 Contract",
        requested_review_mode="two-step",
        review_mode="two-step",
        review_scope="dependency-review",
        routing_reason="title_prefix",
        routing_marker="【Deep-Review】",
        dependency_context_status="complete",
        dependency_context_id="context-123",
        dependency_repositories=[
            {
                "repo_id": "p202",
                "project_path": "team/sdk",
                "branch": "release",
                "commit_sha": "c" * 40,
                "preparation_seconds": 1.25,
            }
        ],
        dependency_preparation_seconds=1.5,
        dependency_relationship_summary=["主仓依赖 SDK 的空值契约。"],
        agent_call_count=2,
    )


def _event() -> WebhookReviewEvent:
    target = MergeRequestReviewTarget(
        base_url="https://gitlab.example.com",
        project_path="team/app",
        mr_iid=7,
        mr_url="https://gitlab.example.com/team/app/merge_requests/7",
        target_repo_url="https://gitlab.example.com/team/app.git",
        source_repo_url="https://gitlab.example.com/team/app.git",
        target_branch="release",
        source_branch="feature/contract",
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="【Deep-Review】 Contract",
    )
    return WebhookReviewEvent("team/app!7:head", "update", "source update", "old", False, target)


def test_markdown_reports_complete_dependency_context():
    rendered = render_structured_output_as_markdown(_complete_report())

    assert "请求模式：two-step；实际模式：two-step" in rendered.markdown
    assert "审查范围：dependency-review" in rendered.markdown
    assert "目标分支：release" in rendered.markdown
    assert "依赖联合检视：已完成" in rendered.markdown
    assert "team/sdk" in rendered.markdown
    assert "branch=release" in rendered.markdown
    assert "commit=" + "c" * 40 in rendered.markdown
    assert "准备耗时=1.250s" in rendered.markdown
    assert "主仓依赖 SDK 的空值契约" in rendered.markdown
    assert "更新依赖调用" in rendered.markdown


def test_markdown_explicitly_reports_dependency_degradation():
    report = replace(
        _complete_report(),
        review_mode="one-step",
        review_scope="single",
        dependency_context_status="degraded",
        dependency_degradation_reason="catalog_invalid",
        dependency_context_id="",
        dependency_repositories=[],
        dependency_relationship_summary=[],
        agent_call_count=1,
        review_plan=None,
    )

    rendered = render_structured_output_as_markdown(report)

    assert "请求模式：two-step；实际模式：one-step" in rendered.markdown
    assert "未执行依赖联合检视" in rendered.markdown
    assert "catalog_invalid" in rendered.markdown
    assert "依赖联合检视：已完成" not in rendered.markdown


def test_webhook_json_records_dependency_review_audit_fields(tmp_path: Path):
    report_path = write_webhook_monitor_report(
        _event(),
        _complete_report(),
        Config(gitlab_base_url="https://gitlab.example.com", report_dir=tmp_path),
        "task-1",
        "success",
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["requested_review_mode"] == "two-step"
    assert payload["review_mode"] == "two-step"
    assert payload["review_scope"] == "dependency-review"
    assert payload["dependency_context_status"] == "complete"
    assert payload["dependency_context_id"] == "context-123"
    assert payload["dependency_preparation_seconds"] == 1.5
    assert payload["dependency_repositories"][0]["commit_sha"] == "c" * 40
    assert payload["dependency_relationship_summary"] == ["主仓依赖 SDK 的空值契约。"]


def _health_config(tmp_path: Path, catalog: Path | None) -> Config:
    return Config(
        gitlab_base_url="https://gitlab.example.com",
        gitlab_token="token",
        agent_command="agent",
        im_poll_command="poll",
        im_reply_command="reply",
        welink_group_id="group",
        welink_onebox_space_id="space",
        welink_onebox_parent_id="parent",
        repository_dependency_catalog=catalog,
    )


def test_healthcheck_reports_optional_and_valid_dependency_catalog(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda command: f"C:/bin/{command}")

    assert healthcheck(_health_config(tmp_path, None)) == 0
    assert "repository_dependency_catalog: optional" in capsys.readouterr().out

    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema_version":1,"repositories":[]}', encoding="utf-8")
    assert healthcheck(_health_config(tmp_path, catalog)) == 0
    assert "repository_dependency_catalog: ok (repositories=0)" in capsys.readouterr().out


def test_healthcheck_rejects_invalid_dependency_catalog(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda command: f"C:/bin/{command}")
    catalog = tmp_path / "catalog.json"
    catalog.write_text("not json", encoding="utf-8")

    assert healthcheck(_health_config(tmp_path, catalog)) == 1
    assert "repository_dependency_catalog: invalid (catalog_invalid)" in capsys.readouterr().out
