"""Scene PENDING 条目嵌入 worker。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nonebot import logger

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
    rescheduled_count: int
    stale_count: int
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
        from komari_bot.plugins import embedding_provider

        return embedding_provider

    async def claim_pending_items(
        self,
        set_id: int,
        *,
        owner_token: str,
        lease_seconds: int,
        max_attempts: int,
        retry_base_seconds: int,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """原子认领待处理条目。"""
        batch_limit = self.batch_size if limit is None else max(1, limit)
        return await self.repository.claim_pending_items(
            set_id,
            owner_token=owner_token,
            limit=batch_limit,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
        )

    async def mark_item_ready(
        self,
        item_id: int,
        *,
        owner_token: str,
        embedding: list[float],
    ) -> bool:
        """回写 READY 条目。"""
        return await self.repository.mark_item_ready(
            item_id,
            owner_token,
            embedding,
            len(embedding),
        )

    async def complete_item_failure(
        self,
        item_id: int,
        *,
        owner_token: str,
        error_code: str,
        error_message: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str:
        """回写失败结果，由仓库决定退避重试或最终失败。"""
        return await self.repository.complete_item_failure(
            item_id,
            owner_token=owner_token,
            error_code=error_code,
            error_message=error_message,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
        )

    async def refresh_set_counters(self, set_id: int) -> SceneSetProgress:
        """刷新计数并根据状态收敛 set。"""
        scene_set = await self.repository.refresh_set_progress(set_id)

        total = int(scene_set.get("item_total") or 0)
        ready = int(scene_set.get("item_ready") or 0)
        failed = int(scene_set.get("item_failed") or 0)
        pending = max(total - ready - failed, 0)
        status = str(scene_set.get("status") or "BUILDING")
        previous_status = str(scene_set.get("previous_status") or status)
        transitioned_ready = previous_status != "READY" and status == "READY"
        transitioned_failed = previous_status != "FAILED" and status == "FAILED"

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
        """认领并处理一个批次的 scene 条目。"""
        config = get_config()
        owner_token = uuid4().hex
        pending_items = await self.claim_pending_items(
            set_id,
            owner_token=owner_token,
            lease_seconds=config.scene_embedding_lease_seconds,
            max_attempts=config.scene_embedding_max_attempts,
            retry_base_seconds=config.scene_embedding_retry_base_seconds,
            limit=limit,
        )
        if not pending_items:
            progress = await self.refresh_set_counters(set_id)
            return SceneEmbeddingBatchResult(
                set_id=set_id,
                fetched_count=0,
                marked_ready=0,
                marked_failed=0,
                rescheduled_count=0,
                stale_count=0,
                pending_count=progress.pending,
                set_status=progress.status,
                transitioned_ready=progress.transitioned_ready,
                transitioned_failed=progress.transitioned_failed,
            )

        instruction = config.embedding_instruction_scene.strip()
        embedding_provider = self._get_embedding_provider()

        texts = [str(item["content_text"]) for item in pending_items]

        marked_ready = 0
        marked_failed = 0
        rescheduled_count = 0
        stale_count = 0

        async def _record_failure(
            item_id: int,
            error_code: str,
            error_message: str,
        ) -> None:
            nonlocal marked_failed, rescheduled_count, stale_count
            outcome = await self.complete_item_failure(
                item_id,
                owner_token=owner_token,
                error_code=error_code,
                error_message=error_message[:500],
                max_attempts=config.scene_embedding_max_attempts,
                retry_base_seconds=config.scene_embedding_retry_base_seconds,
            )
            match outcome:
                case "failed":
                    marked_failed += 1
                case "pending":
                    rescheduled_count += 1
                case _:
                    stale_count += 1

        try:
            vectors = await embedding_provider.embed_batch(texts, instruction=instruction)
        except Exception as exc:
            error_message = f"embedding 批处理异常: {type(exc).__name__}"
            for item in pending_items:
                await _record_failure(
                    int(item["id"]),
                    "embedding_batch_exception",
                    error_message,
                )
        else:
            if len(vectors) != len(pending_items):
                error_message = (
                    "embedding 返回条目数不匹配: "
                    f"expect={len(pending_items)} got={len(vectors)}"
                )
                for item in pending_items:
                    await _record_failure(
                        int(item["id"]),
                        "embedding_count_mismatch",
                        error_message,
                    )
            else:
                for item, vector in zip(pending_items, vectors, strict=True):
                    item_id = int(item["id"])
                    try:
                        embedding = [float(v) for v in vector]
                    except Exception:
                        await _record_failure(
                            item_id,
                            "embedding_vector_invalid",
                            "embedding 向量格式无效",
                        )
                        continue

                    if not embedding:
                        await _record_failure(
                            item_id,
                            "embedding_vector_empty",
                            "embedding 向量为空",
                        )
                        continue

                    if await self.mark_item_ready(
                        item_id,
                        owner_token=owner_token,
                        embedding=embedding,
                    ):
                        marked_ready += 1
                    else:
                        stale_count += 1

        progress = await self.refresh_set_counters(set_id)
        logger.info(
            "[KomariDecision] scene embedding 批处理完成: "
            "set={} fetched={} ready={} failed={} retry={} stale={} pending={} status={}",
            set_id,
            len(pending_items),
            marked_ready,
            marked_failed,
            rescheduled_count,
            stale_count,
            progress.pending,
            progress.status,
        )
        return SceneEmbeddingBatchResult(
            set_id=set_id,
            fetched_count=len(pending_items),
            marked_ready=marked_ready,
            marked_failed=marked_failed,
            rescheduled_count=rescheduled_count,
            stale_count=stale_count,
            pending_count=progress.pending,
            set_status=progress.status,
            transitioned_ready=progress.transitioned_ready,
            transitioned_failed=progress.transitioned_failed,
        )
