from __future__ import annotations

import re
from dataclasses import dataclass

from mr_reviewer.review_result import ReviewFinding, StructuredReviewResult

PUBLISHABLE_SEVERITIES = {"fatal", "major"}
PUBLISHABLE_CONFIDENCE = "HIGH"


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


class DiffPositionMap:
    def __init__(self, positions: list[DiffPosition]):
        self._new_positions = {
            (position.new_path, position.new_line): position
            for position in positions
            if position.new_line != -1
        }
        self._old_positions = {
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
        # GitLab 同时收到两侧行号时按 new_line 定位；只有新侧缺失时才使用旧侧。
        if new_line != -1:
            return self._new_positions.get((new_path, new_line))
        return self._old_positions.get((old_path, old_line))


def validate_review_findings(
        review: StructuredReviewResult,
        position_map: DiffPositionMap,
) -> list[FindingValidationDecision]:
    decisions = []
    for finding in review.findings:
        position = position_map.find(
            finding.old_path,
            finding.new_path,
            finding.old_line,
            finding.new_line,
        )
        if position is None:
            decisions.append(FindingValidationDecision(finding, "invalid", "line_not_in_diff", None))
            continue
        if finding.severity not in PUBLISHABLE_SEVERITIES or finding.confidence != PUBLISHABLE_CONFIDENCE:
            decisions.append(FindingValidationDecision(finding, "filtered", "below_publish_threshold", position))
            continue
        decisions.append(FindingValidationDecision(finding, "publishable", "", position))
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
