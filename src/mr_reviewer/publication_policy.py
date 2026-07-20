from __future__ import annotations

from dataclasses import dataclass

from mr_reviewer.review_result import ALLOWED_CONFIDENCES, ALLOWED_SEVERITIES

SEVERITY_ORDER = ("suggestion", "minor", "major", "fatal")
CONFIDENCE_ORDER = ("LOW", "MEDIUM", "HIGH")


@dataclass(frozen=True, slots=True)
class FindingPublicationPolicy:
    min_severity: str = "minor"
    min_confidence: str = "HIGH"

    def __post_init__(self) -> None:
        if self.min_severity not in ALLOWED_SEVERITIES:
            supported = ", ".join(SEVERITY_ORDER)
            raise ValueError(
                f"unsupported publish minimum severity: {self.min_severity}; "
                f"expected one of {supported}"
            )
        if self.min_confidence not in ALLOWED_CONFIDENCES:
            supported = ", ".join(CONFIDENCE_ORDER)
            raise ValueError(
                f"unsupported publish minimum confidence: {self.min_confidence}; "
                f"expected one of {supported}"
            )

    def filter_reason(self, severity: str, confidence: str) -> str:
        """返回稳定的过滤原因；空字符串表示 finding 满足发布门槛。"""
        if SEVERITY_ORDER.index(severity) < SEVERITY_ORDER.index(self.min_severity):
            return "below_min_severity"
        if CONFIDENCE_ORDER.index(confidence) < CONFIDENCE_ORDER.index(self.min_confidence):
            return "below_min_confidence"
        return ""


DEFAULT_PUBLICATION_POLICY = FindingPublicationPolicy()
