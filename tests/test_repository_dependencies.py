import importlib
import json
from pathlib import Path

import pytest

from mr_reviewer.config import Config
from mr_reviewer.review_routing import resolve_review_routing


def _dependencies_module():
    return importlib.import_module("mr_reviewer.repository_dependencies")


def _write_catalog(path: Path, repositories: list[dict]) -> Path:
    path.write_text(
        json.dumps({"schema_version": 1, "repositories": repositories}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_config_reads_repository_dependency_catalog(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MR_REVIEWER_REPOSITORY_DEPENDENCY_CATALOG", raising=False)
    catalog_path = tmp_path / "repository-dependencies.json"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MR_REVIEWER_GITLAB_BASE_URL=https://gitlab.example.com\n"
        f"MR_REVIEWER_REPOSITORY_DEPENDENCY_CATALOG={catalog_path}\n",
        encoding="utf-8",
    )

    assert Config.from_env(env_path).repository_dependency_catalog == catalog_path


def test_catalog_loads_direct_repository_dependencies(tmp_path: Path):
    module = _dependencies_module()
    path = _write_catalog(
        tmp_path / "catalog.json",
        [
            {"project_path": "team/repo-a", "dependencies": ["team/repo-c", "team/repo-b"]},
            {"project_path": "team/repo-empty", "dependencies": []},
        ],
    )

    catalog = module.load_repository_dependency_catalog(path)

    assert catalog.dependencies_for("team/repo-a") == ("team/repo-b", "team/repo-c")
    assert catalog.dependencies_for("team/repo-empty") == ()
    assert catalog.dependencies_for("team/unmapped") == ()


@pytest.mark.parametrize(
    ("repositories", "message"),
    [
        (
            [
                {"project_path": "team/repo-a", "dependencies": []},
                {"project_path": "team/repo-a", "dependencies": []},
            ],
            "duplicate project_path",
        ),
        (
            [{"project_path": "team/repo-a", "dependencies": ["team/repo-b", "team/repo-b"]}],
            "duplicate dependency",
        ),
        (
            [{"project_path": "team/repo-a", "dependencies": ["team/repo-a"]}],
            "must not depend on itself",
        ),
        (
            [{"project_path": "team/repo-a", "dependencies": [], "tag_template": "v1"}],
            "unexpected fields",
        ),
    ],
)
def test_catalog_rejects_invalid_repository_relationships(tmp_path: Path, repositories: list[dict], message: str):
    module = _dependencies_module()
    path = _write_catalog(tmp_path / "catalog.json", repositories)

    with pytest.raises(module.RepositoryDependencyCatalogError, match=message) as exc_info:
        module.load_repository_dependency_catalog(path)

    assert exc_info.value.reason_code == "catalog_invalid"


def test_catalog_reports_unreadable_file_with_stable_reason(tmp_path: Path):
    module = _dependencies_module()

    with pytest.raises(module.RepositoryDependencyCatalogError) as exc_info:
        module.load_repository_dependency_catalog(tmp_path / "missing.json")

    assert exc_info.value.reason_code == "catalog_unreadable"


def test_normal_review_does_not_degrade_for_catalog_error():
    module = _dependencies_module()

    selection = module.select_dependency_review(
        resolve_review_routing("Fix authentication"),
        "team/repo-a",
        catalog_error="catalog_invalid",
    )

    assert selection.requested_review_mode == "one-step"
    assert selection.review_mode == "one-step"
    assert selection.review_scope == "single"
    assert selection.dependencies == ()
    assert selection.degradation_reason == ""


def test_deep_review_without_mapping_keeps_single_repository_two_step(tmp_path: Path):
    module = _dependencies_module()
    catalog = module.load_repository_dependency_catalog(_write_catalog(tmp_path / "catalog.json", []))

    selection = module.select_dependency_review(
        resolve_review_routing("【Deep-Review】 Validate auth"),
        "team/repo-a",
        catalog,
    )

    assert selection.requested_review_mode == "two-step"
    assert selection.review_mode == "two-step"
    assert selection.review_scope == "single"
    assert selection.dependencies == ()
    assert selection.degradation_reason == ""


def test_deep_review_with_three_dependencies_selects_joint_review(tmp_path: Path):
    module = _dependencies_module()
    catalog = module.load_repository_dependency_catalog(
        _write_catalog(
            tmp_path / "catalog.json",
            [
                {
                    "project_path": "team/repo-a",
                    "dependencies": ["team/repo-d", "team/repo-b", "team/repo-c"],
                }
            ],
        )
    )

    selection = module.select_dependency_review(
        resolve_review_routing("【Deep-Review】 Validate auth"),
        "team/repo-a",
        catalog,
    )

    assert selection.review_mode == "two-step"
    assert selection.review_scope == "dependency-review"
    assert selection.dependencies == ("team/repo-b", "team/repo-c", "team/repo-d")
    assert selection.degradation_reason == ""


def test_deep_review_with_four_dependencies_degrades_to_single_one_step(tmp_path: Path):
    module = _dependencies_module()
    catalog = module.load_repository_dependency_catalog(
        _write_catalog(
            tmp_path / "catalog.json",
            [
                {
                    "project_path": "team/repo-a",
                    "dependencies": ["team/repo-b", "team/repo-c", "team/repo-d", "team/repo-e"],
                }
            ],
        )
    )

    selection = module.select_dependency_review(
        resolve_review_routing("【Deep-Review】 Validate auth"),
        "team/repo-a",
        catalog,
    )

    assert selection.requested_review_mode == "two-step"
    assert selection.review_mode == "one-step"
    assert selection.review_scope == "single"
    assert selection.dependencies == ()
    assert selection.degradation_reason == "dependency_limit_exceeded"


def test_deep_review_with_catalog_error_degrades_to_single_one_step():
    module = _dependencies_module()

    selection = module.select_dependency_review(
        resolve_review_routing("【Deep-Review】 Validate auth"),
        "team/repo-a",
        catalog_error="catalog_unreadable",
    )

    assert selection.requested_review_mode == "two-step"
    assert selection.review_mode == "one-step"
    assert selection.review_scope == "single"
    assert selection.dependencies == ()
    assert selection.degradation_reason == "catalog_unreadable"
