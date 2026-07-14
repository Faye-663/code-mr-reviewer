from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mr_reviewer.review_routing import ReviewRoutingDecision


CATALOG_SCHEMA_VERSION = 1
MAX_DEPENDENCY_REPOSITORIES = 3


class RepositoryDependencyCatalogError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RepositoryDependencyCatalog:
    repositories: Mapping[str, tuple[str, ...]]

    def dependencies_for(self, project_path: str) -> tuple[str, ...]:
        return self.repositories.get(project_path, ())


@dataclass(frozen=True, slots=True)
class DependencyReviewSelection:
    requested_review_mode: str
    review_mode: str
    review_scope: str
    dependencies: tuple[str, ...]
    degradation_reason: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _require_exact_fields(value: dict[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected:
        raise ValueError(f"{location} has unexpected fields: {', '.join(unexpected)}")
    if missing:
        raise ValueError(f"{location} is missing fields: {', '.join(missing)}")


def _require_project_path(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value.strip()


def load_repository_dependency_catalog(path: Path) -> RepositoryDependencyCatalog:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RepositoryDependencyCatalogError(
            "catalog_unreadable",
            f"repository dependency catalog is unreadable: {path}",
        ) from exc

    try:
        raw = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(raw, dict):
            raise ValueError("catalog root must be an object")
        _require_exact_fields(raw, {"schema_version", "repositories"}, "catalog")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != CATALOG_SCHEMA_VERSION:
            raise ValueError(f"catalog schema_version must be {CATALOG_SCHEMA_VERSION}")
        if not isinstance(raw["repositories"], list):
            raise ValueError("catalog repositories must be an array")

        repositories: dict[str, tuple[str, ...]] = {}
        for index, repository in enumerate(raw["repositories"]):
            location = f"repositories[{index}]"
            if not isinstance(repository, dict):
                raise ValueError(f"{location} must be an object")
            _require_exact_fields(repository, {"project_path", "dependencies"}, location)
            project_path = _require_project_path(repository["project_path"], f"{location}.project_path")
            if project_path in repositories:
                raise ValueError(f"duplicate project_path: {project_path}")
            if not isinstance(repository["dependencies"], list):
                raise ValueError(f"{location}.dependencies must be an array")

            dependencies: list[str] = []
            seen_dependencies: set[str] = set()
            for dependency_index, dependency in enumerate(repository["dependencies"]):
                dependency_path = _require_project_path(
                    dependency,
                    f"{location}.dependencies[{dependency_index}]",
                )
                if dependency_path == project_path:
                    raise ValueError(f"{project_path} must not depend on itself")
                if dependency_path in seen_dependencies:
                    raise ValueError(f"duplicate dependency for {project_path}: {dependency_path}")
                seen_dependencies.add(dependency_path)
                dependencies.append(dependency_path)

            repositories[project_path] = tuple(sorted(dependencies))
    except (json.JSONDecodeError, _DuplicateJsonKey, ValueError) as exc:
        raise RepositoryDependencyCatalogError(
            "catalog_invalid",
            f"invalid repository dependency catalog: {exc}",
        ) from exc

    # 目录在整个 review 中只读，避免路由之后被调用方意外改写。
    return RepositoryDependencyCatalog(MappingProxyType(repositories))


def select_dependency_review(
    routing: ReviewRoutingDecision,
    project_path: str,
    catalog: RepositoryDependencyCatalog | None = None,
    *,
    catalog_error: str = "",
) -> DependencyReviewSelection:
    requested_review_mode = routing.review_mode
    if requested_review_mode == "one-step":
        return DependencyReviewSelection("one-step", "one-step", "single", (), "")

    if catalog_error:
        return DependencyReviewSelection("two-step", "one-step", "single", (), catalog_error)

    dependencies = catalog.dependencies_for(project_path) if catalog is not None else ()
    if not dependencies:
        return DependencyReviewSelection("two-step", "two-step", "single", (), "")
    if len(dependencies) > MAX_DEPENDENCY_REPOSITORIES:
        # 超限必须整体降级，不能把目录顺序误当成依赖优先级并截取前三个。
        return DependencyReviewSelection(
            "two-step",
            "one-step",
            "single",
            (),
            "dependency_limit_exceeded",
        )
    return DependencyReviewSelection("two-step", "two-step", "dependency-review", dependencies, "")
