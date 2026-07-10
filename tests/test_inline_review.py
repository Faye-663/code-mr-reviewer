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


def test_diff_position_map_builds_added_and_deleted_positions():
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
            _finding(severity="major", confidence="HIGH", new_line=99),
        ],
        notes=[],
        test_gaps=[],
    )

    decisions = validate_review_findings(review, position_map)

    assert [decision.status for decision in decisions] == ["publishable", "filtered", "invalid"]
    assert decisions[0].position is not None
    assert decisions[1].reason == "below_publish_threshold"
    assert decisions[2].reason == "line_not_in_diff"


def _finding(severity: str, confidence: str, new_line: int) -> ReviewFinding:
    return ReviewFinding(
        rule_id="SQL_PERFORMANCE",
        severity=severity,
        confidence=confidence,
        old_path="src/example.py",
        new_path="src/example.py",
        old_line=-1,
        new_line=new_line,
        title="批量查询缺少数量限制",
        evidence="本次变更新增 IN 查询，但未限制集合大小。",
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
