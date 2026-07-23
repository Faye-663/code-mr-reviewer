import json
from pathlib import Path

from mr_reviewer.gitlab import GitLabClient
from mr_reviewer.inline_review import DiffPositionMap, DiffRefs, validate_review_findings
from mr_reviewer.review_result import ReviewFinding, StructuredReviewResult
from mr_reviewer.reviewer import MergeRequestReviewTarget


def test_gitlab_client_reads_mr_detail_diff_refs(tmp_path: Path):
    fixture = tmp_path / "gitlab.json"
    fixture.write_text(
        json.dumps(
            {
                "/projects/team%2Fproject/merge_requests/7": {
                    "diff_refs": {
                        "base_sha": "base-sha",
                        "start_sha": "start-sha",
                        "head_sha": "head-sha",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    client = GitLabClient("https://gitlab.example.com/api/v4", "secret-token", fixture)

    detail = client.get_mr_detail_for_discussion_position(_target())

    assert detail["diff_refs"] == {
        "base_sha": "base-sha",
        "start_sha": "start-sha",
        "head_sha": "head-sha",
    }


def test_diff_position_map_builds_added_deleted_and_context_positions():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -10,3 +10,4 @@
 unchanged
-removed
+replacement
+added
 context
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )

    added = position_map.find("src/example.py", "src/example.py", -1, 12)
    deleted = position_map.find("src/example.py", "src/example.py", 11, -1)
    context = position_map.find("src/example.py", "src/example.py", 10, 10)

    assert added is not None
    assert added.to_gitlab_position() == {
        "base_sha": "base-sha",
        "start_sha": "start-sha",
        "head_sha": "head-sha",
        "position_type": "text",
        "old_path": "src/example.py",
        "new_path": "src/example.py",
        "old_line": -1,
        "new_line": 12,
        "ignore_whitespace_change": False,
    }
    assert deleted is not None
    assert deleted.old_line == 11
    assert deleted.new_line == -1
    assert context is not None
    assert context.old_line == 10
    assert context.new_line == 10


def test_validate_review_findings_classifies_publishable_filtered_and_invalid():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -1,1 +1,2 @@
 old
+added
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[
            _finding(severity="major", confidence="HIGH", new_line=2),
            _finding(severity="suggestion", confidence="HIGH", new_line=2),
            _finding(severity="minor", confidence="MEDIUM", new_line=2),
            _finding(severity="major", confidence="HIGH", new_line=99),
        ],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert [decision.status for decision in decisions] == [
        "publishable",
        "filtered",
        "filtered",
        "invalid",
    ]
    assert decisions[0].position is not None
    assert decisions[1].reason == "below_min_severity"
    assert decisions[2].reason == "below_min_confidence"
    assert decisions[3].reason == "line_not_in_diff"


def test_validate_review_findings_normalizes_same_line_replacement_to_new_side():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -119,1 +119,1 @@
-old
+replacement
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[_finding(severity="major", confidence="HIGH", old_line=119, new_line=119)],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert decisions[0].status == "publishable"
    assert decisions[0].reason == ""
    assert decisions[0].position is not None
    assert decisions[0].position.old_line == -1
    assert decisions[0].position.new_line == 119


def test_diff_position_map_keeps_same_line_replacement_strict_by_default():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -119,1 +119,1 @@
-old
+replacement
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )

    resolution = position_map.resolve("src/example.py", "src/example.py", 119, 119)

    assert resolution.position is None
    assert resolution.reason == "inconsistent_line_sides"


def test_validate_review_findings_does_not_fallback_when_new_side_is_invalid():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -119,1 +119,1 @@
-old
+replacement
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[_finding(severity="major", confidence="HIGH", old_line=119, new_line=999)],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert decisions[0].status == "invalid"
    assert decisions[0].reason == "inconsistent_line_sides"


def test_validate_review_findings_rejects_new_file_start_and_end_lines():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/new.py b/src/new.py
new file mode 100644
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,3 @@
+first
+second
+third
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[
            _finding(
                severity="major",
                confidence="HIGH",
                old_path="src/new.py",
                new_path="src/new.py",
                old_line=1,
                new_line=3,
            )
        ],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert decisions[0].status == "invalid"
    assert decisions[0].reason == "inconsistent_line_sides"
    assert decisions[0].position is None


def test_validate_review_findings_accepts_exact_context_line_pair():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -10,2 +20,2 @@
 unchanged
-old
+replacement
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[_finding(severity="major", confidence="HIGH", old_line=10, new_line=20)],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert decisions[0].status == "publishable"
    assert decisions[0].position is not None
    assert decisions[0].position.old_line == 10
    assert decisions[0].position.new_line == 20


def test_diff_position_map_preserves_renamed_paths_for_all_position_kinds():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/old.py b/src/new.py
similarity index 80%
rename from src/old.py
rename to src/new.py
--- a/src/old.py
+++ b/src/new.py
@@ -10,2 +20,2 @@
 unchanged
-old
+replacement
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[
            _finding(
                severity="major",
                confidence="HIGH",
                old_path="src/old.py",
                new_path="src/new.py",
                old_line=10,
                new_line=20,
            ),
            _finding(
                severity="major",
                confidence="HIGH",
                old_path="src/old.py",
                new_path="src/new.py",
                old_line=11,
                new_line=-1,
            ),
            _finding(
                severity="major",
                confidence="HIGH",
                old_path="src/old.py",
                new_path="src/new.py",
                old_line=-1,
                new_line=21,
            ),
        ],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert [decision.status for decision in decisions] == [
        "publishable",
        "publishable",
        "publishable",
    ]
    assert [decision.position.to_gitlab_position() for decision in decisions] == [
        {
            "base_sha": "base-sha",
            "start_sha": "start-sha",
            "head_sha": "head-sha",
            "position_type": "text",
            "old_path": "src/old.py",
            "new_path": "src/new.py",
            "old_line": 10,
            "new_line": 20,
            "ignore_whitespace_change": False,
        },
        {
            "base_sha": "base-sha",
            "start_sha": "start-sha",
            "head_sha": "head-sha",
            "position_type": "text",
            "old_path": "src/old.py",
            "new_path": "src/new.py",
            "old_line": 11,
            "new_line": -1,
            "ignore_whitespace_change": False,
        },
        {
            "base_sha": "base-sha",
            "start_sha": "start-sha",
            "head_sha": "head-sha",
            "position_type": "text",
            "old_path": "src/old.py",
            "new_path": "src/new.py",
            "old_line": -1,
            "new_line": 21,
            "ignore_whitespace_change": False,
        },
    ]


def test_validate_review_findings_rejects_invalid_line_values():
    position_map = DiffPositionMap.from_unified_diff(
        """
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -1,1 +1,1 @@
-old
+replacement
""".strip(),
        DiffRefs(base_sha="base-sha", start_sha="start-sha", head_sha="head-sha"),
    )
    review = StructuredReviewResult(
        findings=[
            _finding(severity="major", confidence="HIGH", old_line=-1, new_line=0),
            _finding(severity="major", confidence="HIGH", old_line=-2, new_line=-1),
            _finding(severity="major", confidence="HIGH", old_line=-1, new_line=-1),
        ],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert [decision.reason for decision in decisions] == [
        "invalid_line_value",
        "invalid_line_value",
        "invalid_line_value",
    ]


def _finding(
        severity: str,
        confidence: str,
        new_line: int,
        old_line: int = -1,
        old_path: str = "src/example.py",
        new_path: str = "src/example.py",
) -> ReviewFinding:
    return ReviewFinding(
        rule_id="SQL_PERFORMANCE",
        severity=severity,
        confidence=confidence,
        old_path=old_path,
        new_path=new_path,
        old_line=old_line,
        new_line=new_line,
        title="批量查询缺少数量限制",
        evidence="本次变更新增 IN 查询，但未限制集合大小。",
        impact="大请求可能导致数据库资源耗尽。",
        suggestion="限制集合大小或拆批查询。",
    )


def _target() -> MergeRequestReviewTarget:
    return MergeRequestReviewTarget(
        base_url="https://gitlab.example.com",
        project_path="team/project",
        mr_iid=7,
        mr_url="https://gitlab.example.com/team/project/merge_requests/7",
        target_repo_url="https://gitlab.example.com/team/project.git",
        source_repo_url="https://gitlab.example.com/team/project.git",
        target_branch="main",
        source_branch="feature/auth",
        base_sha=None,
        head_sha="head-sha",
    )
