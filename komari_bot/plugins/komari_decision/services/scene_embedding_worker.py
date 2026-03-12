"""Scene PENDING 条目嵌入 worker。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import require

from .config_interface import get_config

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository


@dataclass(frozen=True)
class SceneEmbeddingBatchResult:
    """单批次嵌入结果。"""

    set_id: int
    fetched_count: int
    marked_ready: int
    marked_failed: int
    pending_count: int
    set_status: str
    transitioned_ready: bool
    transitioned_failed: bool


@dataclass(frozen=True)
class SceneSetProgress:
    """Scene set 当前进度。"""

    total: int
    ready: int
    failed: int
    pending: int
    status: str
    transitioned_ready: bool
    transitioned_failed: bool


class SceneEmbeddingWorker:
    """处理 scene PENDING 条目的嵌入 worker。"""

    def __init__(self, repository: SceneRepository, *, batch_size: int = 16) -> None:
        self.repository = repository
        self.batch_size = max(1, batch_size)

    @staticmethod
    def _get_embedding_provider() -> Any:
        """惰性获取 embedding_provider，避免模块导入阶段强依赖。"""
        return require("embedding_provider")

    async def fetch_pending_items(
        self,
        set_id: int,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """拉取待处理条目。"""
        batch_limit = self.batch_size if limit is None else max(1, limit)
        return await self.repository.fetch_pending_items(set_id, limit=batch_limit)

    async def mark_item_ready(self, item_id: int, embedding: list[float]) -> None:
        """回写 READY 条目。"""
        await self.repository.mark_item_ready(item_id, embedding, len(embedding))

    async def mark_item_failed(self, item_id: int, error_message: str) -> None:
        """回写 FAILED 条目。"""
        await self.repository.mark_item_failed(item_id, error_message)

    async def refresh_set_counters(self, set_id: int) -> SceneSetProgress:
        """刷新计数并根据状态收敛 set。"""
        await self.repository.update_set_counters(set_id)
        scene_set = await self.repository.get_scene_set(set_id)
        if scene_set is None:
            msg = f"scene set 不存在: {set_id}"
            raise RuntimeError(msg)

        total = int(scene_set.get("item_total") or 0)
        ready = int(scene_set.get("item_ready") or 0)
        failed = int(scene_set.get("item_failed") or 0)
        pending = max(total - ready - failed, 0)
        status = str(scene_set.get("status") or "BUILDING")

        transitioned_ready = False
        transitioned_failed = False
        if pending == 0 and total > 0:
            if failed > 0 and status != "FAILED":
                await self.repository.mark_set_failed(
                    set_id,
                    f"scene embedding 存在失败条目: failed={failed}",
                )
                status = "FAILED"
                transitioned_failed = True
            elif failed == 0 and status != "READY":
                await self.repository.mark_set_ready(set_id)
                status = "READY"
                transitioned_ready = True

        return SceneSetProgress(
            total=total,
            ready=ready,
            failed=failed,
            pending=pending,
            status=status,
            transitioned_ready=transitioned_ready,
            transitioned_failed=transitioned_failed,
        )

    async def embed_pending_batch(
        self,
        set_id: int,
        *,
        limit: int | None = None,
    ) -> SceneEmbeddingBatchResult:
        """处理一个批次的 PENDING 条目。"""
        pending_items = await self.fetch_pending_items(set_id, limit=limit)
        if not pending_items:
            progress = await self.refresh_set_counters(set_id)
            return SceneEmbeddingBatchResult(
                set_id=set_id,
                fetched_count=0,
                marked_ready=0,
                marked_failed=0,
                pending_count=progress.pending,
                set_status=progress.status,
                transitioned_ready=progress.transitioned_ready,
                transitioned_failed=progress.transitioned_failed,
            )

        config = get_config()
        instruction = config.embedding_instruction_scene.strip()
        embedding_provider = self._get_embedding_provider()

        texts = [str(item["content_text"]) for item in pending_items]

        marked_ready = 0
        marked_failed = 0
        try:
            vectors = await embedding_provider.embed_batch(texts, instruction=instruction)
        except Exception as e:
            error_message = f"embedding 批处理异常: {type(e).__name__}: {e}"
            for item in pending_items:
                await self.mark_item_failed(int(item["id"]), error_message[:500])
                marked_failed += 1
        else:
            if len(vectors) != len(pending_items):
                error_message = (
                    "embedding 返回条目数不匹配: "
                    f"expect={len(pending_items)} got={len(vectors)}"
                )
                for item in pending_items:
                    await self.mark_item_failed(int(item["id"]), error_message)
                    marked_failed += 1
            else:
                for item, vector in zip(pending_items, vectors, strict=True):
                    item_id = int(item["id"])
                    try:
                        embedding = [float(v) for v in vector]
                    except Exception:
                        await self.mark_item_failed(item_id, "embedding 向量格式无效")
                        marked_failed += 1
                        continue

                    if not embedding:
                        await self.mark_item_failed(item_id, "embedding 向量为空")
                        marked_failed += 1
                        continue

                    await self.mark_item_ready(item_id, embedding)
                    marked_ready += 1

        progress = await self.refresh_set_counters(set_id)
        logger.info(
            "[KomariDecision] scene embedding 批处理完成: set={} fetched={} ready={} failed={} pending={} status={}",
            set_id,
            len(pending_items),
            marked_ready,
            marked_failed,
            progress.pending,
            progress.status,
        )
        return SceneEmbeddingBatchResult(
            set_id=set_id,
            fetched_count=len(pending_items),
            marked_ready=marked_ready,
            marked_failed=marked_failed,
            pending_count=progress.pending,
            set_status=progress.status,
            transitioned_ready=progress.transitioned_ready,
            transitioned_failed=progress.transitioned_failed,
        )
