"""Neutral chat generation result shared by DSH generation and fulfillment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .image_reading_session import ImageFailureSummary


@dataclass(frozen=True)
class ReplyResult:
    """A generated chat candidate plus runtime facts owned outside the model.

    ``hindsight_target_fingerprint`` is annotated by the generation wrapper from
    the active task memory session; it is never accepted from model arguments.
    ``image_failure_summary`` remains the safe aggregate used by the pre-DSH
    notification boundary until TSK-205 owns image transport.
    """

    content: str
    favorability_delta: int | None = None
    favorability_reason: str | None = None
    hindsight_target_fingerprint: str | None = None
    image_failure_summary: ImageFailureSummary | None = None


__all__ = ["ReplyResult"]
