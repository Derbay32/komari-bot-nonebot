"""Scene 运维与回滚服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

from .config_interface import get_config

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository
    from .scene_embedding_worker import SceneEmbeddingWorker
    from .scene_runtime_service import SceneRuntimeService


@dataclass(frozen=True)
class SceneRetryResult:
    """失败版本重试结果。"""

    set_id: int
    reset_failed_items: int
    batches: int
    status: str
    item_total: int
    item_ready: int
    item_failed: int
    pending_count: int
    activated: bool


class SceneAdminService:
    """提供 scene 版本激活、回滚、重试、清理能力。"""

    def __init__(
        self,
        repository: SceneRepository,
        runtime_service: SceneRuntimeService,
        embedding_worker: SceneEmbeddingWorker,
    ) -> None:
        self.repository = repository
        self.runtime_service = runtime_service
        self.embedding_worker = embedding_worker

    async def activate_ready_set(self, set_id: int) -> int:
        """激活指定 READY set。"""
        scene_set = await self.repository.get_scene_set(set_id)
        if scene_set is None:
            msg = f"scene set 不存在: {set_id}"
            raise ValueError(msg)
        if str(scene_set.get("status")) != "READY":
            msg = f"只能激活 READY set: id={set_id} status={scene_set.get('status')}"
            raise ValueError(msg)

        await self.runtime_service.switch_active_set(set_id)
        logger.info("[KomariMemory] SceneAdmin 激活 READY set: id=%s", set_id)
        return set_id

    async def rollback_to_previous_ready(self) -> int:
        """回滚到上一个 READY set。"""
        active_set = await self.repository.get_active_set()
        active_id = int(active_set["id"]) if active_set is not None else None

        ready_sets = await self.repository.list_ready_sets()
        target_id: int | None = None
        for row in ready_sets:
            candidate = int(row["id"])
            if active_id is None or candidate != active_id:
                target_id = candidate
                break

        if target_id is None:
            msg = "没有可回滚的 READY set"
            raise ValueError(msg)

        await self.runtime_service.switch_active_set(target_id)
        logger.info(
            "[KomariMemory] SceneAdmin 回滚完成: from=%s to=%s",
            active_id,
            target_id,
        )
        return target_id

    async def retry_failed_set(
        self,
        set_id: int,
        *,
        max_batches: int = 64,
        activate_when_ready: bool = False,
    ) -> SceneRetryResult:
        """重试 FAILED set（将失败条目回退到 PENDING 再嵌入）。"""
        reset_failed_items = await self.repository.reopen_failed_set(set_id)
        batches = 0
        remaining = max(1, max_batches)
        while remaining > 0:
            batch = await self.embedding_worker.embed_pending_batch(set_id)
            batches += 1
            if batch.pending_count <= 0 or batch.fetched_count <= 0:
                break
            remaining -= 1

        scene_set = await self.repository.get_scene_set(set_id)
        if scene_set is None:
            msg = f"scene set 不存在: {set_id}"
            raise RuntimeError(msg)

        status = str(scene_set.get("status") or "BUILDING")
        item_total = int(scene_set.get("item_total") or 0)
        item_ready = int(scene_set.get("item_ready") or 0)
        item_failed = int(scene_set.get("item_failed") or 0)
        pending_count = max(item_total - item_ready - item_failed, 0)
        activated = False

        if activate_when_ready and status == "READY":
            await self.runtime_service.switch_active_set(set_id)
            activated = True

        logger.info(
            "[KomariMemory] SceneAdmin 重试完成: id=%s status=%s ready=%s failed=%s pending=%s",
            set_id,
            status,
            item_ready,
            item_failed,
            pending_count,
        )
        return SceneRetryResult(
            set_id=set_id,
            reset_failed_items=reset_failed_items,
            batches=batches,
            status=status,
            item_total=item_total,
            item_ready=item_ready,
            item_failed=item_failed,
            pending_count=pending_count,
            activated=activated,
        )

    async def prune_old_sets(self, *, keep_versions: int | None = None) -> list[int]:
        """清理旧 READY set，保留最近 N 个和当前 active。"""
        config = get_config()
        keep = max(1, keep_versions or int(config.scene_keep_versions))

        active_set = await self.repository.get_active_set()
        active_id = int(active_set["id"]) if active_set is not None else None
        ready_sets = await self.repository.list_ready_sets()

        keep_ids: set[int] = set()
        for index, row in enumerate(ready_sets):
            if index >= keep:
                break
            keep_ids.add(int(row["id"]))
        if active_id is not None:
            keep_ids.add(active_id)

        removed_ids: list[int] = []
        for row in ready_sets:
            set_id = int(row["id"])
            if set_id in keep_ids:
                continue
            if await self.repository.delete_set(set_id):
                removed_ids.append(set_id)

        logger.info(
            "[KomariMemory] SceneAdmin 清理旧版本: keep=%s removed=%s",
            keep,
            removed_ids,
        )
        return removed_ids
