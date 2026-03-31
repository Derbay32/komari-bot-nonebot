"""Scene active set 运行时缓存服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

_REQUIRED_FIXED_KEYS = ("NOISE", "MEANINGFUL", "CALL_DIRECT", "CALL_MENTION")

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository


@dataclass(frozen=True)
class SceneRuntimeGeneralCandidate:
    """运行时 general scene 候选。"""

    scene_id: str
    text: str
    embedding: list[float]
    order_index: int


@dataclass(frozen=True)
class SceneRuntimeSnapshot:
    """运行时 scene 缓存快照。"""

    set_id: int
    runtime_updated_at: str
    fixed_candidates: dict[str, str]
    fixed_embeddings: dict[str, list[float]]
    general_candidates: list[SceneRuntimeGeneralCandidate]


class SceneRuntimeService:
    """维护 active set 的运行时缓存。"""

    def __init__(self, repository: SceneRepository) -> None:
        self.repository = repository
        self._snapshot: SceneRuntimeSnapshot | None = None

    @staticmethod
    def _to_float_list(raw: object) -> list[float]:
        if not isinstance(raw, list):
            return []
        try:
            return [float(v) for v in raw]
        except (TypeError, ValueError):
            return []

    @staticmethod
    def _build_snapshot(
        active_set: dict,
        items: list[dict],
    ) -> SceneRuntimeSnapshot:
        set_id = int(active_set["id"])
        runtime_updated_at = str(active_set.get("runtime_updated_at") or "")
        fixed_candidates: dict[str, str] = {}
        fixed_embeddings: dict[str, list[float]] = {}
        general_candidates: list[SceneRuntimeGeneralCandidate] = []

        for item in items:
            scene_key = str(item.get("scene_key") or "").strip()
            scene_type = str(item.get("scene_type") or "").strip()
            text = str(item.get("content_text") or "").strip()
            embedding = SceneRuntimeService._to_float_list(item.get("embedding"))
            order_index = int(item.get("order_index") or 0)
            if not scene_key or not scene_type or not text or not embedding:
                continue

            if scene_type == "fixed":
                fixed_candidates[scene_key] = text
                fixed_embeddings[scene_key] = embedding
            elif scene_type == "general":
                general_candidates.append(
                    SceneRuntimeGeneralCandidate(
                        scene_id=scene_key,
                        text=text,
                        embedding=embedding,
                        order_index=order_index,
                    )
                )

        missing = [
            key
            for key in _REQUIRED_FIXED_KEYS
            if key not in fixed_candidates or key not in fixed_embeddings
        ]
        if missing:
            msg = f"active set 缺少固定候选或 embedding: {missing}"
            raise RuntimeError(msg)
        if not general_candidates:
            msg = "active set 缺少可用 general scene 候选"
            raise RuntimeError(msg)

        general_candidates.sort(key=lambda item: item.order_index)
        return SceneRuntimeSnapshot(
            set_id=set_id,
            runtime_updated_at=runtime_updated_at,
            fixed_candidates=fixed_candidates,
            fixed_embeddings=fixed_embeddings,
            general_candidates=general_candidates,
        )

    async def load_active_set_cache(self) -> bool:
        """加载当前 active set 到内存缓存。"""
        active_set = await self.repository.get_active_set()
        if active_set is None:
            if self._snapshot is not None:
                logger.warning("[KomariDecision] active set 已清空，runtime cache 重置")
            self._snapshot = None
            return False

        set_id = int(active_set["id"])
        if str(active_set.get("status")) != "READY":
            msg = f"active set 非 READY 状态: id={set_id}"
            raise RuntimeError(msg)

        items = await self.repository.list_items_by_set(
            set_id,
            status="READY",
            enabled_only=True,
        )
        snapshot = self._build_snapshot(active_set, items)
        self._snapshot = snapshot
        logger.info(
            "[KomariDecision] scene runtime cache 已加载: set={} scenes={}",
            snapshot.set_id,
            len(snapshot.general_candidates),
        )
        return True

    async def refresh_if_runtime_updated(self) -> bool:
        """检测 runtime 指针变化并按需刷新缓存。"""
        active_set = await self.repository.get_active_set()
        if active_set is None:
            changed = self._snapshot is not None
            if changed:
                self._snapshot = None
                logger.warning("[KomariDecision] active set 不存在，runtime cache 已清空")
            return changed

        set_id = int(active_set["id"])
        runtime_updated_at = str(active_set.get("runtime_updated_at") or "")
        if (
            self._snapshot is not None
            and self._snapshot.set_id == set_id
            and self._snapshot.runtime_updated_at == runtime_updated_at
        ):
            return False

        await self.load_active_set_cache()
        return True

    def get_scene_candidates(self) -> SceneRuntimeSnapshot | None:
        """获取当前缓存快照。"""
        return self._snapshot

    async def switch_active_set(self, set_id: int) -> SceneRuntimeSnapshot:
        """原子切换 active set 并刷新缓存。"""
        await self.repository.switch_active_set(set_id)
        loaded = await self.load_active_set_cache()
        if not loaded or self._snapshot is None:
            msg = f"切换 active set 后缓存加载失败: set={set_id}"
            raise RuntimeError(msg)
        return self._snapshot
