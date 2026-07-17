from __future__ import annotations

import re
from dataclasses import dataclass

from mr_reviewer.publication_policy import DEFAULT_PUBLICATION_POLICY, FindingPublicationPolicy
from mr_reviewer.review_result import ReviewFinding, StructuredReviewResult


@dataclass(frozen=True, slots=True)
class DiffRefs:
    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class DiffPosition:
    refs: DiffRefs
    old_path: str
    new_path: str
    old_line: int
    new_line: int

    def to_gitlab_position(self) -> dict:
        return {
            "base_sha": self.refs.base_sha,
            "start_sha": self.refs.start_sha,
            "head_sha": self.refs.head_sha,
            "position_type": "text",
            "old_path": self.old_path,
            "new_path": self.new_path,
            "old_line": self.old_line,
            "new_line": self.new_line,
            "ignore_whitespace_change": False,
        }


@dataclass(frozen=True, slots=True)
class FindingValidationDecision:
    finding: ReviewFinding
    status: str
    reason: str
    position: DiffPosition | None


@dataclass(frozen=True, slots=True)
class DiffPositionResolution:
    position: DiffPosition | None
    reason: str


class DiffPositionMap:
    def __init__(self, positions: list[DiffPosition]):
        self._added_positions = {
            (position.new_path, position.new_line): position
            for position in positions
            if position.old_line == -1
        }
        self._deleted_positions = {
            (position.old_path, position.old_line): position
            for position in positions
            if position.new_line == -1
        }
        self._context_positions = {
            (position.old_path, position.new_path, position.old_line, position.new_line): position
            for position in positions
            if position.old_line != -1 and position.new_line != -1
        }
        self._new_side_positions = {
            (position.new_path, position.new_line): position
            for position in positions
            if position.new_line != -1
        }
        self._old_side_positions = {
            (position.old_path, position.old_line): position
            for position in positions
            if position.old_line != -1
        }

    @classmethod
    def from_unified_diff(cls, diff: str, refs: DiffRefs) -> DiffPositionMap:
        positions: list[DiffPosition] = []
        old_path = ""
        new_path = ""
        old_line: int | None = None
        new_line: int | None = None

        for raw_line in diff.splitlines():
            if raw_line.startswith("diff --git "):
                old_path, new_path = _parse_diff_git_paths(raw_line)
                old_line = None
                new_line = None
                continue
            if raw_line.startswith("--- "):
                old_path = _normalize_diff_path(raw_line[4:].strip())
                continue
            if raw_line.startswith("+++ "):
                new_path = _normalize_diff_path(raw_line[4:].strip())
                continue

            hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
            if hunk:
                old_line = int(hunk.group(1))
                new_line = int(hunk.group(2))
                continue

            if old_line is None or new_line is None or not old_path or not new_path:
                continue
            if raw_line.startswith("\\"):
                continue

            if raw_line.startswith("+"):
                positions.append(DiffPosition(refs, old_path, new_path, -1, new_line))
                new_line += 1
            elif raw_line.startswith("-"):
                positions.append(DiffPosition(refs, old_path, new_path, old_line, -1))
                old_line += 1
            else:
                positions.append(DiffPosition(refs, old_path, new_path, old_line, new_line))
                old_line += 1
                new_line += 1

        return cls(positions)

    def find(self, old_path: str, new_path: str, old_line: int, new_line: int) -> DiffPosition | None:
        return self.resolve(old_path, new_path, old_line, new_line).position

    def resolve(self, old_path: str, new_path: str, old_line: int, new_line: int) -> DiffPositionResolution:
        lines = (old_line, new_line)
        if any(line < -1 or line == 0 for line in lines) or lines == (-1, -1):
            return DiffPositionResolution(None, "invalid_line_value")

        if old_line == -1:
            position = self._added_positions.get((new_path, new_line))
            if position is not None:
                return DiffPositionResolution(position, "")
            if (new_path, new_line) in self._new_side_positions:
                return DiffPositionResolution(None, "inconsistent_line_sides")
            return DiffPositionResolution(None, "line_not_in_diff")

        if new_line == -1:
            position = self._deleted_positions.get((old_path, old_line))
            if position is not None:
                return DiffPositionResolution(position, "")
            if (old_path, old_line) in self._old_side_positions:
                return DiffPositionResolution(None, "inconsistent_line_sides")
            return DiffPositionResolution(None, "line_not_in_diff")

        position = self._context_positions.get((old_path, new_path, old_line, new_line))
        if position is not None:
            return DiffPositionResolution(position, "")
        if (
                (old_path, old_line) in self._old_side_positions
                or (new_path, new_line) in self._new_side_positions
        ):
            return DiffPositionResolution(None, "inconsistent_line_sides")
        return DiffPositionResolution(None, "line_not_in_diff")


def validate_review_findings(
        review: StructuredReviewResult,
        position_map: DiffPositionMap,
        publication_policy: FindingPublicationPolicy = DEFAULT_PUBLICATION_POLICY,
) -> list[FindingValidationDecision]:
    decisions = []
    for finding in review.findings:
        resolution = position_map.resolve(
            finding.old_path,
            finding.new_path,
            finding.old_line,
            finding.new_line,
        )
        if resolution.position is None:
            decisions.append(FindingValidationDecision(finding, "invalid", resolution.reason, None))
            continue
        filter_reason = publication_policy.filter_reason(finding.severity, finding.confidence)
        if filter_reason:
            decisions.append(FindingValidationDecision(finding, "filtered", filter_reason, resolution.position))
            continue
        decisions.append(FindingValidationDecision(finding, "publishable", "", resolution.position))
    return decisions


def _parse_diff_git_paths(line: str) -> tuple[str, str]:
    parts = line.split()
    if len(parts) >= 4:
        return _normalize_diff_path(parts[2]), _normalize_diff_path(parts[3])
    return "", ""


def _normalize_diff_path(path: str) -> str:
    if path == "/dev/null":
        return path
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path
