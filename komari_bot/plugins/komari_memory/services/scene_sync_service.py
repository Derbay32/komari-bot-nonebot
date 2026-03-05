"""Scene 构建服务：YAML -> Scene Set/Items。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import require

from .config_interface import get_config
from .scene_template_loader import SceneTemplateLoader

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository


@dataclass(frozen=True)
class SceneSyncResult:
    """Scene 构建结果。"""

    set_id: int
    created: bool
    reused_existing_set: bool
    inserted_count: int
    ready_count: int
    pending_count: int


class SceneSyncService:
    """Scene 构建服务。"""

    def __init__(
        self,
        repository: SceneRepository,
        loader: SceneTemplateLoader | None = None,
    ) -> None:
        self.repository = repository
        self.loader = loader or SceneTemplateLoader()

    @staticmethod
    def _instruction_hash(instruction: str) -> str:
        return hashlib.sha256(instruction.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def _get_embedding_provider() -> Any:
        """惰性获取 embedding_provider，避免模块导入阶段强依赖。"""
        return require("embedding_provider")

    @classmethod
    def _resolve_embedding_model(cls) -> str:
        """从 embedding_provider 获取当前 embedding 模型名。"""
        embedding_provider = cls._get_embedding_provider()
        get_model = getattr(embedding_provider, "get_embedding_model", None)
        if not callable(get_model):
            msg = "embedding_provider 未提供 get_embedding_model() 接口"
            raise TypeError(msg)
        model = str(get_model()).strip()
        if not model:
            msg = "embedding_provider 返回空的 embedding 模型名"
            raise RuntimeError(msg)
        return model

    async def build_scene_set(self) -> SceneSyncResult:
        """构建新的 scene set（含 embedding 复用）。"""
        config = get_config()
        template = self.loader.load_scene_template()

        embedding_model = self._resolve_embedding_model()
        instruction_hash = self._instruction_hash(config.embedding_instruction_scene)

        latest_ready = await self.repository.get_latest_ready_set()
        if (
            latest_ready is not None
            and latest_ready.get("source_hash") == template.source_hash
            and latest_ready.get("embedding_model") == embedding_model
            and latest_ready.get("embedding_instruction_hash") == instruction_hash
        ):
            existing_set_id = int(latest_ready["id"])
            logger.info(
                "[KomariMemory] Scene 模板未变化，复用现有 set: id=%s", existing_set_id
            )
            return SceneSyncResult(
                set_id=existing_set_id,
                created=False,
                reused_existing_set=True,
                inserted_count=0,
                ready_count=int(latest_ready.get("item_ready") or 0),
                pending_count=0,
            )

        latest_building = await self.repository.get_latest_set_by_fingerprint(
            source_hash=template.source_hash,
            embedding_model=embedding_model,
            embedding_instruction_hash=instruction_hash,
            status="BUILDING",
        )
        if latest_building is not None:
            existing_set_id = int(latest_building["id"])
            total = int(latest_building.get("item_total") or 0)
            ready = int(latest_building.get("item_ready") or 0)
            failed = int(latest_building.get("item_failed") or 0)
            pending = max(total - ready - failed, 0)
            logger.info(
                "[KomariMemory] 复用构建中的 scene set: id=%s pending=%s",
                existing_set_id,
                pending,
            )
            return SceneSyncResult(
                set_id=existing_set_id,
                created=False,
                reused_existing_set=True,
                inserted_count=0,
                ready_count=ready,
                pending_count=pending,
            )

        set_id = await self.repository.create_scene_set(
            source_path=template.source_path,
            source_hash=template.source_hash,
            embedding_model=embedding_model,
            embedding_instruction_hash=instruction_hash,
            status="BUILDING",
        )

        items_payload: list[dict] = []
        ready_count = 0
        pending_count = 0

        for item in template.items:
            reusable = await self.repository.find_reusable_ready_item(
                scene_key=item.scene_key,
                content_hash=item.content_hash,
                embedding_model=embedding_model,
                embedding_instruction_hash=instruction_hash,
            )

            payload = {
                "scene_key": item.scene_key,
                "scene_type": item.scene_type,
                "content_text": item.content_text,
                "content_hash": item.content_hash,
                "enabled": item.enabled,
                "order_index": item.order_index,
                "embedding": None,
                "embedding_dim": None,
                "status": "PENDING",
                "error_message": None,
                "embedded_at": None,
            }

            if reusable is not None:
                payload["embedding"] = reusable.get("embedding")
                payload["embedding_dim"] = reusable.get("embedding_dim")
                payload["status"] = "READY"
                payload["embedded_at"] = reusable.get("embedded_at")
                ready_count += 1
            else:
                pending_count += 1

            items_payload.append(payload)

        inserted_count = await self.repository.insert_scene_items(set_id, items_payload)
        await self.repository.update_set_counters(set_id)

        if pending_count == 0:
            await self.repository.mark_set_ready(set_id)

        logger.info(
            "[KomariMemory] 构建 scene set 完成: id=%s inserted=%s ready=%s pending=%s",
            set_id,
            inserted_count,
            ready_count,
            pending_count,
        )
        return SceneSyncResult(
            set_id=set_id,
            created=True,
            reused_existing_set=False,
            inserted_count=inserted_count,
            ready_count=ready_count,
            pending_count=pending_count,
        )
