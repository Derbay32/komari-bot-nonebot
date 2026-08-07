"""统一候选集契约：候选条目、单次 rerank 聚合结果与运行时不可用异常。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CandidateSchema:
    """统一候选条目。"""

    key: str
    text: str
    kind: Literal["fixed", "call", "scene"]
    scene_id: str | None = None
    embedding_similarity: float | None = None


@dataclass(frozen=True)
class UnifiedRerankResult:
    """单次 rerank 聚合结果。"""

    alias_hit: bool
    candidates: list[CandidateSchema]
    score_map: dict[str, float]
    meaningful_score: float
    noise_score: float
    call_direct_score: float | None
    call_mention_score: float | None
    best_scene_id: str | None
    best_scene_score: float
    meaningful_prior: float
    noise_prior: float


class SceneRuntimeUnavailableError(RuntimeError):
    """scene 运行时快照暂不可用于判定。"""


__all__ = [
    "CandidateSchema",
    "SceneRuntimeUnavailableError",
    "UnifiedRerankResult",
]
