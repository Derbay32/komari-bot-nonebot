"""统一候选集单次 rerank 服务。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from nonebot import logger

from .config_interface import get_config

_SCENE_TEMPLATE_PATH = Path("config") / "prompts" / "komari_memory_scenes.yaml"

_REQUIRED_FIXED_KEYS = ("NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION")


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


class UnifiedCandidateRerankService:
    """统一候选集组装与单次 rerank。"""

    def __init__(self) -> None:
        self._cached_path: Path | None = None
        self._cached_mtime: float = 0.0
        self._cached_instruction: str = ""
        self._cached_fixed: dict[str, str] = {}
        self._cached_scenes: list[dict[str, str]] = []
        self._cached_embeddings: dict[str, list[float]] = {}

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

    def _resolve_template_path(self) -> Path:
        if _SCENE_TEMPLATE_PATH.is_absolute():
            return _SCENE_TEMPLATE_PATH
        return _SCENE_TEMPLATE_PATH.resolve()

    def _load_template(self, path: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
        """加载 scene 模板。"""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        raw_fixed = data.get("fixed_candidates", {})
        if not isinstance(raw_fixed, dict):
            msg = "fixed_candidates 必须是对象"
            raise TypeError(msg)

        missing = [key for key in _REQUIRED_FIXED_KEYS if key not in raw_fixed]
        if missing:
            msg = f"fixed_candidates 缺失必需键: {missing}"
            raise ValueError(msg)

        fixed = {
            key: str(raw_fixed[key]).strip()
            for key in _REQUIRED_FIXED_KEYS
        }

        raw_scenes = data.get("general_scenes", [])
        if not isinstance(raw_scenes, list):
            msg = "general_scenes 必须是数组"
            raise TypeError(msg)

        scenes: list[dict[str, str]] = []
        for idx, item in enumerate(raw_scenes):
            if not isinstance(item, dict):
                logger.warning("[UnifiedRerank] 忽略非法 scene 项: index=%s", idx)
                continue
            scene_id = str(item.get("id", "")).strip()
            scene_text = str(item.get("text", "")).strip()
            if not scene_id or not scene_text:
                logger.warning("[UnifiedRerank] 忽略空 scene 项: index=%s", idx)
                continue
            scenes.append({"id": scene_id, "text": scene_text})

        if not scenes:
            msg = "general_scenes 不能为空"
            raise ValueError(msg)

        return fixed, scenes

    async def _ensure_embeddings(self) -> None:
        """确保模板和候选 embedding 已加载。"""
        embedding_provider = self._get_embedding_provider()
        config = get_config()
        path = self._resolve_template_path()

        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            msg = f"无法读取 scene 模板: {path}"
            raise RuntimeError(msg) from e

        instruction = config.embedding_instruction_scene.strip()
        cache_valid = (
            self._cached_path == path
            and self._cached_mtime == mtime
            and self._cached_instruction == instruction
            and self._cached_embeddings
        )
        if cache_valid:
            return

        fixed, scenes = self._load_template(path)

        items: list[tuple[str, str]] = []
        for key, text in fixed.items():
            items.append((f"fixed::{key}", text))
        items.extend(
            [
                (
                    f"scene::{scene['id']}",
                    scene["text"],
                )
                for scene in scenes
            ]
        )

        vectors = await embedding_provider.embed_batch(
            [item[1] for item in items],
            instruction=instruction,
        )
        if len(vectors) != len(items):
            msg = (
                "[UnifiedRerank] embedding 数量异常: "
                f"expect={len(items)}, got={len(vectors)}"
            )
            raise RuntimeError(msg)

        self._cached_path = path
        self._cached_mtime = mtime
        self._cached_instruction = instruction
        self._cached_fixed = fixed
        self._cached_scenes = scenes
        self._cached_embeddings = {
            key: vector
            for (key, _), vector in zip(items, vectors, strict=True)
        }
        logger.info(
            "[UnifiedRerank] 场景模板已加载: scenes=%s, instruction_hash=%s",
            len(scenes),
            hash(instruction),
        )

    async def rank_message(
        self,
        message: str,
        *,
        alias_hit: bool | None = None,
    ) -> UnifiedRerankResult:
        """对单条消息执行统一候选集单次 rerank。"""
        embedding_provider = self._get_embedding_provider()
        await self._ensure_embeddings()
        config = get_config()

        alias_detected = (
            alias_hit
            if alias_hit is not None
            else self.detect_alias(message, config.bot_aliases)
        )

        query_vector = await embedding_provider.embed(
            message,
            instruction=config.embedding_instruction_query,
        )

        # 固定候选 embedding 先验
        meaningful_prior = self._cosine_similarity(
            query_vector, self._cached_embeddings["fixed::MEANINGFUL"]
        )
        noise_prior = self._cosine_similarity(
            query_vector, self._cached_embeddings["fixed::NOISE"]
        )

        # scene top-k 召回
        scene_scored: list[tuple[dict[str, str], float]] = []
        for scene in self._cached_scenes:
            score = self._cosine_similarity(
                query_vector, self._cached_embeddings[f"scene::{scene['id']}"]
            )
            scene_scored.append((scene, score))
        scene_scored.sort(key=lambda x: x[1], reverse=True)

        top_k = max(1, config.scene_top_k)
        top_scenes = scene_scored[:top_k]

        candidates: list[CandidateSchema] = [
            CandidateSchema(
                key="NOISE",
                text=self._cached_fixed["NOISE"],
                kind="fixed",
                embedding_similarity=noise_prior,
            ),
            CandidateSchema(
                key="MEANINGFUL",
                text=self._cached_fixed["MEANINGFUL"],
                kind="fixed",
                embedding_similarity=meaningful_prior,
            ),
        ]

        if alias_detected:
            candidates.extend(
                [
                    CandidateSchema(
                        key="CALL_DIRECT",
                        text=self._cached_fixed["CALL_DIRECT"],
                        kind="call",
                    ),
                    CandidateSchema(
                        key="CALL_MENTION",
                        text=self._cached_fixed["CALL_MENTION"],
                        kind="call",
                    ),
                ]
            )

        for scene, score in top_scenes:
            candidates.append(
                CandidateSchema(
                    key=f"SCENE::{scene['id']}",
                    text=scene["text"],
                    kind="scene",
                    scene_id=scene["id"],
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
