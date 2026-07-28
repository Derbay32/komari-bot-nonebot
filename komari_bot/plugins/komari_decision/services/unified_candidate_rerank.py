"""统一候选集单次 rerank 服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from nonebot import logger

from .config_interface import get_config

if TYPE_CHECKING:
    from komari_bot.plugins.komari_decision.services.scene_runtime_service import (
        SceneRuntimeService,
        SceneRuntimeSnapshot,
    )


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


class UnifiedCandidateRerankService:
    """统一候选集组装与单次 rerank。"""

    def __init__(self, runtime_service: SceneRuntimeService | None = None) -> None:
        self._runtime_service = runtime_service

    async def _get_runtime_snapshot(self) -> SceneRuntimeSnapshot | None:
        """按需获取 runtime scene 缓存快照。"""
        if self._runtime_service is None:
            return None
        try:
            await self._runtime_service.refresh_if_runtime_updated()
        except Exception as exc:
            logger.exception("[UnifiedRerank] 刷新 scene runtime cache 失败")
            msg = "scene runtime cache 刷新失败"
            raise SceneRuntimeUnavailableError(msg) from exc
        return self._runtime_service.get_scene_candidates()

    @staticmethod
    def _get_embedding_provider() -> Any:
        """惰性获取 embedding_provider，避免模块导入阶段强依赖。"""
        from nonebot.plugin import require

        return require("embedding_provider")

    @staticmethod
    def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
        """计算余弦相似度。"""
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot = 0.0
        norm1 = 0.0
        norm2 = 0.0
        for a, b in zip(v1, v2, strict=True):
            dot += a * b
            norm1 += a * a
            norm2 += b * b
        if norm1 <= 0.0 or norm2 <= 0.0:
            return 0.0
        return dot / ((norm1**0.5) * (norm2**0.5))

    @staticmethod
    def detect_alias(message: str, aliases: list[str]) -> bool:
        """检查消息是否命中机器人别名。"""
        content = message.casefold()
        for alias in aliases:
            alias_clean = alias.strip().casefold()
            if alias_clean and alias_clean in content:
                return True
        return False

    async def rank_message(
        self,
        message: str,
        *,
        alias_hit: bool | None = None,
    ) -> UnifiedRerankResult:
        """对单条消息执行统一候选集单次 rerank。"""
        embedding_provider = self._get_embedding_provider()
        config = get_config()

        alias_detected = (
            alias_hit
            if alias_hit is not None
            else self.detect_alias(message, config.bot_aliases)
        )

        runtime_snapshot = await self._get_runtime_snapshot()
        if runtime_snapshot is None:
            msg = "scene runtime snapshot 不可用，请先初始化/迁移 komari_decision scenes"
            raise SceneRuntimeUnavailableError(msg)

        query_vector = await embedding_provider.embed(
            message,
            instruction=config.embedding_instruction_query,
        )
        top_k = max(1, config.scene_top_k)

        noise_prior = self._cosine_similarity(
            query_vector,
            runtime_snapshot.fixed_embeddings["NOISE"],
        )
        meaningful_prior = self._cosine_similarity(
            query_vector,
            runtime_snapshot.fixed_embeddings["MEANINGFUL"],
        )
        runtime_scene_scored = [
            (
                scene.scene_id,
                scene.text,
                self._cosine_similarity(query_vector, scene.embedding),
            )
            for scene in runtime_snapshot.general_candidates
        ]
        runtime_scene_scored.sort(key=lambda item: item[2], reverse=True)
        top_scenes = runtime_scene_scored[:top_k]

        candidates: list[CandidateSchema] = [
            CandidateSchema(
                key="NOISE",
                text=runtime_snapshot.fixed_candidates["NOISE"],
                kind="fixed",
                embedding_similarity=noise_prior,
            ),
            CandidateSchema(
                key="MEANINGFUL",
                text=runtime_snapshot.fixed_candidates["MEANINGFUL"],
                kind="fixed",
                embedding_similarity=meaningful_prior,
            ),
        ]

        if alias_detected:
            candidates.extend(
                [
                    CandidateSchema(
                        key="CALL_DIRECT",
                        text=runtime_snapshot.fixed_candidates["CALL_DIRECT"],
                        kind="call",
                    ),
                    CandidateSchema(
                        key="CALL_MENTION",
                        text=runtime_snapshot.fixed_candidates["CALL_MENTION"],
                        kind="call",
                    ),
                ]
            )
        for scene_id, scene_text, score in top_scenes:
            candidates.append(
                CandidateSchema(
                    key=f"SCENE::{scene_id}",
                    text=scene_text,
                    kind="scene",
                    scene_id=scene_id,
                    embedding_similarity=score,
                )
            )

        rerank_documents = [item.text for item in candidates]
        rerank_results = await embedding_provider.rerank(
            query=message,
            documents=rerank_documents,
            top_n=len(rerank_documents),
            instruction=config.rerank_instruction,
        )

        score_map = {item.key: 0.0 for item in candidates}
        for result in rerank_results:
            if 0 <= result.index < len(candidates):
                score_map[candidates[result.index].key] = result.relevance_score

        best_scene_id: str | None = None
        best_scene_score = 0.0
        for item in candidates:
            if item.kind != "scene":
                continue
            current = score_map.get(item.key, 0.0)
            if best_scene_id is None or current > best_scene_score:
                best_scene_id = item.scene_id
                best_scene_score = current

        return UnifiedRerankResult(
            alias_hit=alias_detected,
            candidates=candidates,
            score_map=score_map,
            meaningful_score=score_map.get("MEANINGFUL", 0.0),
            noise_score=score_map.get("NOISE", 0.0),
            call_direct_score=(
                score_map.get("CALL_DIRECT") if alias_detected else None
            ),
            call_mention_score=(
                score_map.get("CALL_MENTION") if alias_detected else None
            ),
            best_scene_id=best_scene_id,
            best_scene_score=best_scene_score,
            meaningful_prior=meaningful_prior,
            noise_prior=noise_prior,
        )
