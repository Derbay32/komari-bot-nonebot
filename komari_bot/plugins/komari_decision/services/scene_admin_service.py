"""Scene 运维管理服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config_interface import get_config

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository
    from .scene_embedding_worker import SceneEmbeddingWorker
    from .scene_runtime_service import SceneRuntimeService, SceneRuntimeSnapshot


@dataclass(frozen=True)
class SceneRetryResult:
    """FAILED scene set 重试结果。"""

    set_id: int
    reset_failed_items: int
    pending_count: int
    status: str
    transitioned_ready: bool
    transitioned_failed: bool


@dataclass(frozen=True)
class ScenePruneResult:
    """旧 READY scene set 清理结果。"""

    deleted_set_ids: list[int]
    kept_set_ids: list[int]
    active_set_id: int | None


class SceneAdminService:
    """提供 scene 运维操作。"""

    _MAX_RETRY_BATCHES = 128

    def __init__(
        self,
        repository: SceneRepository,
        runtime_service: SceneRuntimeService,
        embedding_worker: SceneEmbeddingWorker,
    ) -> None:
        self._repository = repository
        self._runtime_service = runtime_service
        self._embedding_worker = embedding_worker

    async def activate_ready_set(self, set_id: int) -> SceneRuntimeSnapshot:
        """手动激活指定 READY set。"""
        return await self._runtime_service.switch_active_set(set_id)

    async def rollback_to_previous_ready(self) -> SceneRuntimeSnapshot:
        """回滚到当前 active set 之前的 READY 版本。"""
        ready_sets = await self._repository.list_ready_sets()
        if not ready_sets:
            msg = "不存在可回滚的 READY scene set"
            raise ValueError(msg)

        active_set = await self._repository.get_active_set()
        active_set_id = None if active_set is None else int(active_set["id"])

        target_set_id: int | None = None
        if active_set_id is None:
            target_set_id = int(ready_sets[0]["id"])
        else:
            for index, scene_set in enumerate(ready_sets):
                if int(scene_set["id"]) != active_set_id:
                    continue
                if index + 1 >= len(ready_sets):
                    msg = f"当前 active set 已是最旧 READY 版本: id={active_set_id}"
                    raise ValueError(msg)
                target_set_id = int(ready_sets[index + 1]["id"])
                break

        if target_set_id is None:
            msg = f"当前 active set 不在 READY 列表中: id={active_set_id}"
            raise ValueError(msg)

        return await self._runtime_service.switch_active_set(target_set_id)

    async def retry_failed_set(self, set_id: int) -> SceneRetryResult:
        """重试 FAILED set，直到收敛或达到批次数上限。"""
        reset_failed_items = await self._repository.reopen_failed_set(set_id)

        remaining = self._MAX_RETRY_BATCHES
        last_batch = None
        while remaining > 0:
            batch = await self._embedding_worker.embed_pending_batch(set_id)
            last_batch = batch
            if batch.pending_count <= 0 or batch.fetched_count <= 0:
                break
            remaining -= 1

        if last_batch is None:
            progress = await self._embedding_worker.refresh_set_counters(set_id)
            return SceneRetryResult(
                set_id=set_id,
                reset_failed_items=reset_failed_items,
                pending_count=progress.pending,
                status=progress.status,
                transitioned_ready=progress.transitioned_ready,
                transitioned_failed=progress.transitioned_failed,
            )

        return SceneRetryResult(
            set_id=set_id,
            reset_failed_items=reset_failed_items,
            pending_count=last_batch.pending_count,
            status=last_batch.set_status,
            transitioned_ready=last_batch.transitioned_ready,
            transitioned_failed=last_batch.transitioned_failed,
        )

    async def prune_old_sets(self, keep_versions: int | None = None) -> ScenePruneResult:
        """清理旧 READY set，保留最近 N 个和当前 active。"""
        configured_keep = get_config().scene_keep_versions
        keep_count = configured_keep if keep_versions is None else max(1, keep_versions)

        ready_sets = await self._repository.list_ready_sets()
        active_set = await self._repository.get_active_set()
        active_set_id = None if active_set is None else int(active_set["id"])

        kept_set_ids = [int(scene_set["id"]) for scene_set in ready_sets[:keep_count]]
        if active_set_id is not None and active_set_id not in kept_set_ids:
            kept_set_ids.append(active_set_id)

        deleted_set_ids: list[int] = []
        for scene_set in ready_sets:
            set_id = int(scene_set["id"])
            if set_id in kept_set_ids:
                continue
            deleted = await self._repository.delete_set(set_id)
            if deleted:
                deleted_set_ids.append(set_id)

        return ScenePruneResult(
            deleted_set_ids=deleted_set_ids,
            kept_set_ids=kept_set_ids,
            active_set_id=active_set_id,
        )
