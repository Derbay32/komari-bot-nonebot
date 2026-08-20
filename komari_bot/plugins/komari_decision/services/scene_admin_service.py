"""Scene 运维管理服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._fixed_scene_rules import validate_required_fixed_scene_write
from .config_interface import get_config

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository
    from .scene_sync_service import SceneSyncResult, SceneSyncService


@dataclass(frozen=True)
class ScenePruneResult:
    """旧 READY scene set 清理结果。"""

    deleted_set_ids: list[int]
    kept_set_ids: list[int]
    active_set_id: int | None


class SceneAdminService:
    """提供 scene 运维操作。"""

    def __init__(
        self,
        repository: SceneRepository,
        sync_service: SceneSyncService,
    ) -> None:
        self._repository = repository
        self._sync_service = sync_service

    async def sync_scenes(self) -> SceneSyncResult:
        """触发场景同步，原样返回 SceneSyncService 的现有 frozen 结果。"""
        return await self._sync_service.build_scene_set()

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

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出 scene 内容表记录，直通 SceneRepository.list_scenes。"""
        return await self._repository.list_scenes(enabled_only=enabled_only)

    async def get_scene_by_key(self, scene_key: str) -> dict[str, Any] | None:
        """按 scene_key 获取 scene 内容记录，直通 SceneRepository.get_scene_by_key。"""
        return await self._repository.get_scene_by_key(scene_key)

    async def upsert_scene(
        self,
        *,
        scene_key: str,
        scene_type: str,
        content_text: str,
        enabled: bool = True,
        order_index: int = 0,
    ) -> dict[str, Any]:
        """校验必需 fixed scene 规则后新增或更新内容记录。"""
        validate_required_fixed_scene_write(
            scene_key=scene_key.strip(),
            scene_type=scene_type.strip(),
            enabled=enabled,
        )
        return await self._repository.upsert_scene(
            scene_key=scene_key,
            scene_type=scene_type,
            content_text=content_text,
            enabled=enabled,
            order_index=order_index,
        )
