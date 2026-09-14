from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from mr_reviewer.dependency_review import DependencyReviewManifest
from mr_reviewer.result_validation import (
    require_exact_fields as _exact_fields,
    require_integer as _integer,
    require_list as _list,
    require_object as _object,
    require_text as _text,
    require_text_list as _text_list,
    normalize_report_text_list,
    parse_findings_isolated,
    parse_review_position,
)
from mr_reviewer.review_result import ALLOWED_CONFIDENCES, ALLOWED_SEVERITIES
from mr_reviewer.structured_output import parse_json_object_output


PLAN_SCHEMA_VERSION = "dependency-review-plan/v1"
RESULT_SCHEMA_VERSION = "dependency-review-result/v1"
PRIMARY_REPO_ID = "primary"


class DependencyReviewPlanParseError(ValueError):
    pass


class StructuredDependencyReviewParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DependencyEvidenceRef:
    repo_id: str
    path: str
    start_line: int
    end_line: int
    detail: str


@dataclass(frozen=True, slots=True)
class DependencyFindingPosition:
    old_path: str
    new_path: str
    old_line: int
    new_line: int
    side: str = "legacy"


@dataclass(frozen=True, slots=True)
class DependencyReviewFinding:
    issue_id: str
    rule_id: str
    severity: str
    confidence: str
    title: str
    impact: str
    evidence_refs: tuple[DependencyEvidenceRef, ...]
    position: DependencyFindingPosition | None
    suggestion: str


@dataclass(frozen=True, slots=True)
class StructuredDependencyReviewResult:
    schema_version: str
    findings: tuple[DependencyReviewFinding, ...]
    relationship_summary: list[str]
    notes: list[str]
    test_gaps: list[str]
    good: list[str]
    structured_parse_status: str = "success"
    rejected_findings: list[dict[str, object]] = field(default_factory=list)
    normalization_warnings: list[dict[str, str]] = field(default_factory=list)


def parse_dependency_review_plan(raw_output: str, manifest: DependencyReviewManifest) -> dict[str, object]:
    return parse_json_object_output(
        raw_output,
        output_type="dependency_review_plan",
        error_label="dependency review plan",
        error_type=DependencyReviewPlanParseError,
        parse_object=lambda payload: _parse_dependency_review_plan_object(payload, manifest),
    )


def _parse_dependency_review_plan_object(
    payload: object,
    manifest: DependencyReviewManifest,
) -> dict[str, object]:
    payload = _object(payload, DependencyReviewPlanParseError, "dependency review plan")
    _exact_fields(
        payload,
        {"schema_version", "primary_focus", "relationships", "open_questions"},
        DependencyReviewPlanParseError,
        "dependency review plan",
    )
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise DependencyReviewPlanParseError(f"schema_version must be {PLAN_SCHEMA_VERSION}")

    dependency_ids, allowed_repo_ids = _manifest_repo_ids(manifest, DependencyReviewPlanParseError)
    primary_focus = _parse_primary_focus(payload.get("primary_focus"))
    relationships = _list(payload, "relationships", DependencyReviewPlanParseError)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "primary_focus": primary_focus,
        "relationships": [
            _parse_relationship(item, index, dependency_ids, allowed_repo_ids)
            for index, item in enumerate(relationships)
        ],
        "open_questions": _text_list(payload, "open_questions", DependencyReviewPlanParseError),
    }


def parse_structured_dependency_review_result(
    raw_output: str,
    manifest: DependencyReviewManifest,
) -> StructuredDependencyReviewResult:
    return parse_json_object_output(
        raw_output,
        output_type="dependency_review_result",
        error_label="dependency review result",
        error_type=StructuredDependencyReviewParseError,
        parse_object=lambda payload: _parse_dependency_review_result_object(payload, manifest),
        prefer_authoritative_agent_output=True,
    )


def _parse_dependency_review_result_object(
    payload: object,
    manifest: DependencyReviewManifest,
) -> StructuredDependencyReviewResult:
    payload = _object(payload, StructuredDependencyReviewParseError, "dependency review result")
    unexpected = set(payload) - {
        "schema_version", "findings", "relationship_summary", "notes", "test_gaps", "good"
    }
    if unexpected:
        raise StructuredDependencyReviewParseError(
            f"dependency review result contains unexpected fields: {sorted(unexpected)}"
        )
    missing_required = {"schema_version", "findings"} - set(payload)
    if missing_required:
        raise StructuredDependencyReviewParseError(
            f"dependency review result is missing fields: {sorted(missing_required)}"
        )
    if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise StructuredDependencyReviewParseError(f"schema_version must be {RESULT_SCHEMA_VERSION}")

    _, allowed_repo_ids = _manifest_repo_ids(manifest, StructuredDependencyReviewParseError)
    raw_findings = _list(payload, "findings", StructuredDependencyReviewParseError)
    parsed_findings, rejected = parse_findings_isolated(
        raw_findings,
        lambda item, index: _parse_finding(item, index, allowed_repo_ids),
        StructuredDependencyReviewParseError,
    )
    relationship_summary, relationship_warnings = normalize_report_text_list(payload, "relationship_summary")
    notes, notes_warnings = normalize_report_text_list(payload, "notes")
    test_gaps, test_gap_warnings = normalize_report_text_list(payload, "test_gaps")
    good, good_warnings = normalize_report_text_list(payload, "good")
    warnings = [*relationship_warnings, *notes_warnings, *test_gap_warnings, *good_warnings]
    return StructuredDependencyReviewResult(
        schema_version=RESULT_SCHEMA_VERSION,
        findings=tuple(parsed_findings),
        relationship_summary=relationship_summary,
        notes=notes,
        test_gaps=test_gaps,
        good=good,
        structured_parse_status="partial" if rejected or warnings else "success",
        rejected_findings=rejected,
        normalization_warnings=warnings,
    )


def _parse_primary_focus(value: object) -> dict[str, object]:
    context = "primary_focus"
    item = _object(value, DependencyReviewPlanParseError, context)
    _exact_fields(
        item,
        {"change_intent", "critical_paths", "test_risks"},
        DependencyReviewPlanParseError,
        context,
    )
    critical_paths = _list(item, "critical_paths", DependencyReviewPlanParseError)
    return {
        "change_intent": _text_list(item, "change_intent", DependencyReviewPlanParseError, context),
        "critical_paths": [
            _parse_critical_path(path, index) for index, path in enumerate(critical_paths)
        ],
        "test_risks": _text_list(item, "test_risks", DependencyReviewPlanParseError, context),
    }


def _parse_critical_path(value: object, index: int) -> dict[str, object]:
    context = f"primary_focus.critical_paths[{index}]"
    item = _object(value, DependencyReviewPlanParseError, context)
    _exact_fields(item, {"path", "reason", "verify"}, DependencyReviewPlanParseError, context)
    verify = _text_list(item, "verify", DependencyReviewPlanParseError, context)
    if not verify:
        raise DependencyReviewPlanParseError(f"{context}.verify must not be empty")
    return {
        "path": _safe_path(_text(item, "path", DependencyReviewPlanParseError, context), DependencyReviewPlanParseError, context),
        "reason": _text(item, "reason", DependencyReviewPlanParseError, context),
        "verify": verify,
    }


def _parse_relationship(
    value: object,
    index: int,
    dependency_ids: set[str],
    allowed_repo_ids: set[str],
) -> dict[str, object]:
    context = f"relationships[{index}]"
    item = _object(value, DependencyReviewPlanParseError, context)
    _exact_fields(
        item,
        {"dependency_repo_id", "contract", "evidence_refs", "verification"},
        DependencyReviewPlanParseError,
        context,
    )
    dependency_repo_id = _text(item, "dependency_repo_id", DependencyReviewPlanParseError, context)
    if dependency_repo_id not in dependency_ids:
        raise DependencyReviewPlanParseError(f"{context}.dependency_repo_id must reference a manifest dependency")
    evidence = _list(item, "evidence_refs", DependencyReviewPlanParseError)
    verification = _text_list(item, "verification", DependencyReviewPlanParseError, context)
    if not evidence or not verification:
        raise DependencyReviewPlanParseError(f"{context} evidence_refs and verification must not be empty")
    relationship_repo_ids = {PRIMARY_REPO_ID, dependency_repo_id}
    parsed_evidence = [
        _parse_evidence(
            ref,
            ref_index,
            relationship_repo_ids & allowed_repo_ids,
            DependencyReviewPlanParseError,
            context,
        )
        for ref_index, ref in enumerate(evidence)
    ]
    if not any(ref.repo_id == dependency_repo_id for ref in parsed_evidence):
        raise DependencyReviewPlanParseError(f"{context} must include target dependency evidence")
    return {
        "dependency_repo_id": dependency_repo_id,
        "contract": _text(item, "contract", DependencyReviewPlanParseError, context),
        "evidence_refs": [_evidence_dict(ref) for ref in parsed_evidence],
        "verification": verification,
    }


def _parse_finding(value: object, index: int, allowed_repo_ids: set[str]) -> DependencyReviewFinding:
    context = f"findings[{index}]"
    item = _object(value, StructuredDependencyReviewParseError, context)
    _exact_fields(
        item,
        {
            "issue_id", "rule_id", "severity", "confidence", "title", "impact",
            "evidence_refs", "position", "suggestion",
        },
        StructuredDependencyReviewParseError,
        context,
    )
    severity = _text(item, "severity", StructuredDependencyReviewParseError, context)
    confidence = _text(item, "confidence", StructuredDependencyReviewParseError, context)
    if severity not in ALLOWED_SEVERITIES:
        raise StructuredDependencyReviewParseError(f"{context}.severity must be one of {sorted(ALLOWED_SEVERITIES)}")
    if confidence not in ALLOWED_CONFIDENCES:
        raise StructuredDependencyReviewParseError(f"{context}.confidence must be one of {sorted(ALLOWED_CONFIDENCES)}")
    evidence = _list(item, "evidence_refs", StructuredDependencyReviewParseError)
    if not evidence:
        raise StructuredDependencyReviewParseError(f"{context}.evidence_refs must not be empty")
    raw_position = item.get("position")
    parsed_evidence = tuple(
        _parse_evidence(ref, ref_index, allowed_repo_ids, StructuredDependencyReviewParseError, context)
        for ref_index, ref in enumerate(evidence)
    )
    position = None if raw_position is None else _parse_position(raw_position, context)
    if position is None and not any(ref.repo_id == PRIMARY_REPO_ID for ref in parsed_evidence):
        raise StructuredDependencyReviewParseError(
            f"{context} without a position must include primary MR evidence"
        )
    return DependencyReviewFinding(
        issue_id=_text(item, "issue_id", StructuredDependencyReviewParseError, context),
        rule_id=_text(item, "rule_id", StructuredDependencyReviewParseError, context),
        severity=severity,
        confidence=confidence,
        title=_text(item, "title", StructuredDependencyReviewParseError, context),
        impact=_text(item, "impact", StructuredDependencyReviewParseError, context),
        evidence_refs=parsed_evidence,
        position=position,
        suggestion=_text(item, "suggestion", StructuredDependencyReviewParseError, context),
    )


def _parse_evidence(value: object, index: int, allowed_repo_ids: set[str], error_type, parent: str) -> DependencyEvidenceRef:
    context = f"{parent}.evidence_refs[{index}]"
    item = _object(value, error_type, context)
    _exact_fields(item, {"repo_id", "path", "start_line", "end_line", "detail"}, error_type, context)
    repo_id = _text(item, "repo_id", error_type, context)
    if repo_id not in allowed_repo_ids:
        raise error_type(f"{context}.repo_id references an unknown manifest repo_id")
    start_line = _integer(item, "start_line", error_type, context)
    end_line = _integer(item, "end_line", error_type, context)
    if start_line < 1 or end_line < start_line:
        raise error_type(f"{context} line range is invalid")
    return DependencyEvidenceRef(
        repo_id=repo_id,
        path=_safe_path(_text(item, "path", error_type, context), error_type, context),
        start_line=start_line,
        end_line=end_line,
        detail=_text(item, "detail", error_type, context),
    )


def _parse_position(value: object, parent: str) -> DependencyFindingPosition:
    context = f"{parent}.position"
    old_path, new_path, old_line, new_line, side = parse_review_position(
        value,
        error_type=StructuredDependencyReviewParseError,
        context=context,
        allow_none=False,
    )
    return DependencyFindingPosition(
        old_path=old_path,
        new_path=new_path,
        old_line=old_line,
        new_line=new_line,
        side=side,
    )


def _manifest_repo_ids(manifest: DependencyReviewManifest, error_type) -> tuple[set[str], set[str]]:
    if manifest.schema_version != "dependency-review/v1":
        raise error_type("manifest schema_version must be dependency-review/v1")
    dependency_ids = {item.repo_id for item in manifest.dependencies}
    if len(dependency_ids) != len(manifest.dependencies) or PRIMARY_REPO_ID in dependency_ids:
        raise error_type("manifest repo_id values must be unique")
    return dependency_ids, {PRIMARY_REPO_ID, *dependency_ids}


def _safe_path(value: str, error_type, context: str) -> str:
    path = PurePosixPath(value)
    if (
        value in {"", "."}
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:/", value)
        or ".." in path.parts
    ):
        raise error_type(f"{context}.path must be a safe relative path")
    return value


def _evidence_dict(evidence: DependencyEvidenceRef) -> dict[str, object]:
    return {
        "repo_id": evidence.repo_id,
        "path": evidence.path,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "detail": evidence.detail,
    }


def dependency_review_result_as_single_review_json(result: StructuredDependencyReviewResult) -> str:
    findings = []
    for finding in result.findings:
        position = finding.position
        if position is None:
            serialized_position = None
        else:
            serialized_position = {
                "path": position.new_path if position.new_line != -1 else position.old_path,
                "line": position.new_line if position.new_line != -1 else position.old_line,
                "side": "new" if position.new_line != -1 else "old",
            }
        evidence = "；".join(
            f"[{ref.repo_id}] {ref.path}:{ref.start_line}-{ref.end_line} {ref.detail}"
            for ref in finding.evidence_refs
        )
        findings.append(
            {
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "confidence": finding.confidence,
                "position": serialized_position,
                "title": finding.title,
                "evidence": evidence,
                "impact": finding.impact,
                "suggestion": finding.suggestion,
            }
        )
    return json.dumps(
        {
            "findings": findings,
            "notes": result.notes,
            "test_gaps": result.test_gaps,
            "good": result.good,
        },
        ensure_ascii=False,
    )
