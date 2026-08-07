"""Scene 构建服务：YAML -> Scene Set/Items。"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nonebot import logger

from .config_interface import get_config
from .scene_template_loader import (
    PostgresSceneTemplateLoader,
    SceneTemplateLoaderProtocol,
    SceneTemplatePayload,
)

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
        loader: SceneTemplateLoaderProtocol | None = None,
        ) -> None:
        self.repository = repository
        self.loader = loader or PostgresSceneTemplateLoader(repository)

    @staticmethod
    def _instruction_hash(instruction: str) -> str:
        return hashlib.sha256(instruction.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def _get_embedding_provider() -> Any:
        """惰性获取 embedding_provider，避免模块导入阶段强依赖。"""
        from komari_bot.plugins import embedding_provider

        return embedding_provider

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
        raw_template = self.loader.load_scene_template()
        template = await raw_template if inspect.isawaitable(raw_template) else raw_template
        if not isinstance(template, SceneTemplatePayload):
            msg = "scene loader 返回类型无效"
            raise TypeError(msg)

        embedding_model = self._resolve_embedding_model()
        instruction_hash = self._instruction_hash(config.embedding_instruction_scene)

        scene_set, created = await self.repository.get_or_create_scene_set(
            source_path=template.source_path,
            source_hash=template.source_hash,
            embedding_model=embedding_model,
            embedding_instruction_hash=instruction_hash,
            status="BUILDING",
        )
        set_id = int(scene_set["id"])
        existing_status = str(scene_set.get("status") or "BUILDING")
        if not created and existing_status in {"READY", "FAILED"}:
            total = int(scene_set.get("item_total") or 0)
            ready = int(scene_set.get("item_ready") or 0)
            failed = int(scene_set.get("item_failed") or 0)
            pending = max(total - ready - failed, 0)
            logger.debug(
                "[KomariDecision] Scene fingerprint 已存在，复用 set: id={} status={}",
                set_id,
                existing_status,
            )
            return SceneSyncResult(
                set_id=set_id,
                created=False,
                reused_existing_set=True,
                inserted_count=0,
                ready_count=ready,
                pending_count=pending,
            )

        items_payload: list[dict] = []

        for item in template.items:
            if item.scene_id is None:
                reusable = await self.repository.find_reusable_ready_item(
                    scene_key=item.scene_key,
                    content_hash=item.content_hash,
                    embedding_model=embedding_model,
                    embedding_instruction_hash=instruction_hash,
                )
            else:
                reusable = await self.repository.find_reusable_ready_item(
                    scene_id=item.scene_id,
                    content_hash=item.content_hash,
                    embedding_model=embedding_model,
                    embedding_instruction_hash=instruction_hash,
                )

            payload = {
                "scene_key": item.scene_key,
                "scene_id": item.scene_id,
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

            items_payload.append(payload)

        inserted_count = await self.repository.insert_scene_items(set_id, items_payload)
        progress = await self.repository.refresh_set_progress(set_id)
        total = int(progress.get("item_total") or 0)
        ready_count = int(progress.get("item_ready") or 0)
        failed_count = int(progress.get("item_failed") or 0)
        pending_count = max(total - ready_count - failed_count, 0)

        logger.info(
            "[KomariDecision] 构建 scene set 完成: id={} inserted={} ready={} pending={}",
            set_id,
            inserted_count,
            ready_count,
            pending_count,
        )
        return SceneSyncResult(
            set_id=set_id,
            created=created,
            reused_existing_set=not created,
            inserted_count=inserted_count,
            ready_count=ready_count,
            pending_count=pending_count,
        )
