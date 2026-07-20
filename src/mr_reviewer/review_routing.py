from __future__ import annotations

from dataclasses import dataclass


DEEP_REVIEW_MARKER = "【Deep-Review】"
DEEP_REVIEW_MARKERS = (DEEP_REVIEW_MARKER, "[Deep-Review]")


@dataclass(frozen=True, slots=True)
class ReviewRoutingDecision:
    review_mode: str
    routing_reason: str
    routing_marker: str


def resolve_review_routing(title: str) -> ReviewRoutingDecision:
    normalized = title.lstrip().casefold()
    for marker in DEEP_REVIEW_MARKERS:
        if normalized.startswith(marker.casefold()):
            return ReviewRoutingDecision("two-step", "title_prefix", marker)
    return ReviewRoutingDecision("one-step", "default", "")
