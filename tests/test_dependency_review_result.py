import importlib
import json

import pytest

from mr_reviewer.dependency_review import (
    DependencyReviewManifest,
    DependencyReviewPrimary,
    DependencyReviewRepository,
)


def _result_module():
    return importlib.import_module("mr_reviewer.dependency_review_result")


def _manifest() -> DependencyReviewManifest:
    return DependencyReviewManifest(
        schema_version="dependency-review/v1",
        context_id="context-123",
        primary=DependencyReviewPrimary(
            project_path="team/app",
            mr_iid=7,
            base_sha="a" * 40,
            head_sha="b" * 40,
            target_branch="release",
            repo_path="repo",
        ),
        dependencies=(
            DependencyReviewRepository(
                repo_id="p202",
                project_path="team/sdk",
                branch="release",
                commit_sha="c" * 40,
                repo_path="dependencies/p202/repo",
            ),
            DependencyReviewRepository(
                repo_id="p303",
                project_path="team/api",
                branch="release",
                commit_sha="d" * 40,
                repo_path="dependencies/p303/repo",
            ),
        ),
    )


def _plan_payload() -> dict:
    return {
        "schema_version": "dependency-review-plan/v1",
        "primary_focus": {
            "change_intent": ["调整 SDK 响应处理"],
            "critical_paths": [
                {
                    "path": "src/caller.py",
                    "reason": "主 MR 调用入口",
                    "verify": ["空值处理"],
                }
            ],
            "test_risks": ["缺少契约回归测试"],
        },
        "relationships": [
            {
                "dependency_repo_id": "p202",
                "contract": "主仓依赖 SDK 的空值语义",
                "evidence_refs": [
                    {
                        "repo_id": "p202",
                        "path": "src/sdk.py",
                        "start_line": 40,
                        "end_line": 42,
                        "detail": "SDK 允许返回 null",
                    }
                ],
                "verification": ["确认主仓处理 null"],
            }
        ],
        "open_questions": [],
    }


def _result_payload() -> dict:
    return {
        "schema_version": "dependency-review-result/v1",
        "findings": [
            {
                "issue_id": "CONTRACT_NULLABILITY_001",
                "rule_id": "CONTRACT_NULLABILITY",
                "severity": "major",
                "confidence": "HIGH",
                "title": "主 MR 未处理依赖仓允许的空返回",
                "impact": "生产请求可能触发空指针异常。",
                "evidence_refs": [
                    {
                        "repo_id": "primary",
                        "path": "src/caller.py",
                        "start_line": 55,
                        "end_line": 57,
                        "detail": "主 MR 直接解引用返回值。",
                    },
                    {
                        "repo_id": "p202",
                        "path": "src/sdk.py",
                        "start_line": 40,
                        "end_line": 42,
                        "detail": "依赖仓允许返回 null。",
                    },
                ],
                "position": {
                    "old_path": "src/caller.py",
                    "new_path": "src/caller.py",
                    "old_line": -1,
                    "new_line": 57,
                },
                "suggestion": "在主 MR 解引用前处理 null。",
            }
        ],
        "relationship_summary": ["主仓调用 SDK，空值契约不一致。"],
        "notes": [],
        "test_gaps": ["缺少主仓与 SDK 的契约测试。"],
        "good": [],
    }


def test_parse_dependency_plan_accepts_manifest_relationships():
    module = _result_module()

    plan = module.parse_dependency_review_plan(
        json.dumps(_plan_payload(), ensure_ascii=False),
        _manifest(),
    )

    assert plan["schema_version"] == "dependency-review-plan/v1"
    assert plan["relationships"][0]["dependency_repo_id"] == "p202"
    assert plan["relationships"][0]["evidence_refs"][0]["repo_id"] == "p202"


def test_parse_dependency_plan_allows_no_proven_relationship():
    module = _result_module()
    payload = _plan_payload()
    payload["relationships"] = []

    plan = module.parse_dependency_review_plan(json.dumps(payload, ensure_ascii=False), _manifest())

    assert plan["relationships"] == []


@pytest.mark.parametrize("repo_id", ["primary", "unknown"])
def test_parse_dependency_plan_rejects_non_dependency_relationship_target(repo_id: str):
    module = _result_module()
    payload = _plan_payload()
    payload["relationships"][0]["dependency_repo_id"] = repo_id

    with pytest.raises(module.DependencyReviewPlanParseError, match="dependency_repo_id"):
        module.parse_dependency_review_plan(json.dumps(payload), _manifest())


def test_parse_dependency_plan_requires_evidence_from_target_dependency():
    module = _result_module()
    payload = _plan_payload()
    payload["relationships"][0]["evidence_refs"][0]["repo_id"] = "primary"

    with pytest.raises(module.DependencyReviewPlanParseError, match="target dependency evidence"):
        module.parse_dependency_review_plan(json.dumps(payload), _manifest())


def test_parse_dependency_result_accepts_dependency_evidence_and_primary_position():
    module = _result_module()

    result = module.parse_structured_dependency_review_result(
        json.dumps(_result_payload(), ensure_ascii=False),
        _manifest(),
    )

    finding = result.findings[0]
    assert finding.evidence_refs[1].repo_id == "p202"
    assert finding.position is not None
    assert finding.position.new_line == 57
    assert result.relationship_summary == ["主仓调用 SDK，空值契约不一致。"]


def test_parse_dependency_result_accepts_null_primary_position():
    module = _result_module()
    payload = _result_payload()
    payload["findings"][0]["position"] = None

    result = module.parse_structured_dependency_review_result(json.dumps(payload), _manifest())

    assert result.findings[0].position is None


def test_parse_dependency_result_rejects_unpositioned_dependency_only_finding():
    module = _result_module()
    payload = _result_payload()
    payload["findings"][0]["position"] = None
    payload["findings"][0]["evidence_refs"] = [payload["findings"][0]["evidence_refs"][1]]

    with pytest.raises(module.StructuredDependencyReviewParseError, match="primary MR evidence"):
        module.parse_structured_dependency_review_result(json.dumps(payload), _manifest())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repo_id", "unknown", "unknown manifest repo_id"),
        ("path", "../secret.txt", "safe relative path"),
        ("start_line", 0, "line range is invalid"),
        ("end_line", 39, "line range is invalid"),
    ],
)
def test_parse_dependency_result_rejects_invalid_evidence(field: str, value: object, message: str):
    module = _result_module()
    payload = _result_payload()
    payload["findings"][0]["evidence_refs"][1][field] = value

    with pytest.raises(module.StructuredDependencyReviewParseError, match=message):
        module.parse_structured_dependency_review_result(json.dumps(payload), _manifest())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("new_path", "../caller.py"),
        ("new_line", 0),
        ("old_line", -2),
    ],
)
def test_parse_dependency_result_rejects_invalid_primary_position(field: str, value: object):
    module = _result_module()
    payload = _result_payload()
    payload["findings"][0]["position"][field] = value

    with pytest.raises(module.StructuredDependencyReviewParseError, match="position"):
        module.parse_structured_dependency_review_result(json.dumps(payload), _manifest())


def test_parse_dependency_result_rejects_dependency_repository_as_target():
    module = _result_module()
    payload = _result_payload()
    payload["findings"][0]["target_repo_id"] = "p202"

    with pytest.raises(module.StructuredDependencyReviewParseError, match="unexpected fields"):
        module.parse_structured_dependency_review_result(json.dumps(payload), _manifest())


def test_parse_dependency_result_requires_relationship_summary():
    module = _result_module()
    payload = _result_payload()
    payload["relationship_summary"] = []

    with pytest.raises(module.StructuredDependencyReviewParseError, match="must not be empty"):
        module.parse_structured_dependency_review_result(json.dumps(payload), _manifest())
