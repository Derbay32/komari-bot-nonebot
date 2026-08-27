"""对话 processing 生命周期深 module（TSK-150）。

定位：把「认领 → 心跳续租 → 读取缓冲 → 业务回调 → 门控 ack / 恢复 / dead-letter」
的编排从 summary_worker.perform_summary 提炼为深 module，调用方只提供业务步骤
（ConversationProcessor）与观测钩子（CollectorProvider），不接触任何 lease 协议。

双入口：

- ``process_conversation_snapshot``：发起一次快照处理（新缓冲认领或孤儿接管）；
- ``run_worker_cycle``：孤儿扫描 → 逐键接管 → 活跃群触发，永不抛出。

行为冻结红线：F1-F25（docs/research/2026-08-14-summary-processing-lifecycle-facts.md），
编排语义逐行搬运自 summary_worker.py 的 perform_summary / _renew_conversation_lease /
_stop_summary_attempt / summary_worker_task 孤儿接管循环；测试面见
docs/research/2026-08-15-processing-lifecycle-test-plan.md。
冻结怪癖说明：TSK-149 严格冻结期的三项怪癖（attempt_count 写死、
lease-lost 被重试、set 回读裸 RuntimeError）已分别由 TSK-155 / TSK-157 解除。
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Never, Protocol
from uuid import uuid4

from nonebot import logger

from ..core.retry import get_retry_attempts, retry_async
from .admission import memory_conversation_business_admitted
from .conversation_processing import (
    ConversationLeaseLostError,
    ConversationSnapshotClaim,
)
from .conversation_processing import (
    InvalidChunkLedgerError as _InvalidChunkLedgerError,
)
from .redis_manager import MessageSchema


class _ProcessingStorage(Protocol):
    """module 私有 storage 协议：14 个对话 processing 动词 + 配置视图。

    与 RedisManager 的真实方法签名结构对齐，目标是未来可零适配直接注入。
    config 注解放宽为 Any：pyright 对可变协议成员做双向互检，SimpleNamespace
    无法满足最小配置协议（pyright 1.1.409 限制），运行时只读 lease 字段。
    """

    config: Any

    async def claim_conversation_buffer(
        self,
        group_id: str,
        owner_token: str,
        token: str,
    ) -> ConversationSnapshotClaim: ...

    async def claim_existing_conversation_processing(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> ConversationSnapshotClaim: ...

    async def get_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> list[MessageSchema]: ...

    async def renew_processing_conversation_lease(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool: ...

    async def ack_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool: ...

    async def restore_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool: ...

    async def dead_letter_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
        *,
        failure_code: str,
        attempt_count: int,
    ) -> bool: ...

    async def initialize_conversation_chunk_manifest(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        manifest_json: str,
    ) -> str: ...

    async def get_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
    ) -> str | None: ...

    async def set_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
        value: str,
    ) -> None: ...

    async def get_orphaned_conversation_processing_keys(self) -> list[tuple[str, str]]: ...

    async def get_active_groups(self) -> list[str]: ...

    async def should_trigger_summary(self, group_id: str) -> bool: ...

    async def update_last_summary(self, group_id: str) -> None: ...


class ChunkLedger(Protocol):
    """业务侧分块账本视图：manifest 初始化 + 字段读写。"""

    async def initialize_manifest(self, manifest_json: str) -> str: ...

    async def get(self, field: str) -> str | None: ...

    async def set(self, field: str, value: str) -> None: ...


@dataclass(slots=True)
class ProcessingSession:
    """一次处理尝试的会话载体：业务方从中读取消息、账本与观测 collector。"""

    group_id: str
    processing_key: str
    messages: list[MessageSchema]
    # collector / ledger 注解放宽为 Any：pyright 对可变协议成员做双向互检，
    # 跨类型协议互检在此版本必失败（pyright 1.1.409 限制），业务侧以自身
    # 会话视图协议（如 L1 测试的 _SessionView）约束形状；生产实现分别为
    # CollectorProvider 产物与 StorageChunkLedger。
    collector: Any
    ledger: Any


class ConversationProcessor(Protocol):
    """业务处理步骤：消费一次快照的 session，异常语义由 module 统一分流。"""

    async def process(self, session: ProcessingSession) -> None: ...


class _CollectorProvider(Protocol):
    """观测钩子：创建与三态收尾 collector。collector 形状由 provider 决定。"""

    def create(self, group_id: str, processing_key: str) -> object | None: ...

    async def finalize(
        self,
        collector: Any,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> bool: ...


class StorageChunkLedger:
    """基于 storage 三动词的生产账本实现（manifest 读回逐字节比对门控内化）。"""

    def __init__(
        self,
        storage: _ProcessingStorage,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> None:
        self._storage = storage
        self._group_id = group_id
        self._processing_key = processing_key
        self._owner_token = owner_token

    async def initialize_manifest(self, manifest_json: str) -> str:
        stored = await self._storage.initialize_conversation_chunk_manifest(
            group_id=self._group_id,
            processing_key=self._processing_key,
            owner_token=self._owner_token,
            manifest_json=manifest_json,
        )
        # 冻结语义：manifest 门控内化，读回必须逐字节一致（F18）。
        if stored != manifest_json:
            raise _InvalidChunkLedgerError("manifest_mismatch")
        return stored

    async def get(self, field: str) -> str | None:
        return await self._storage.get_conversation_chunk_state(
            group_id=self._group_id,
            processing_key=self._processing_key,
            owner_token=self._owner_token,
            field=field,
        )

    async def set(self, field: str, value: str) -> None:
        await self._storage.set_conversation_chunk_state(
            group_id=self._group_id,
            processing_key=self._processing_key,
            owner_token=self._owner_token,
            field=field,
            value=value,
        )


def _raise_conversation_lease_lost(processing_key: str) -> Never:
    raise ConversationLeaseLostError(processing_key)


def _heartbeat_interval(lease_seconds: float) -> float:
    """按租约三分之一周期续租，下限 1 秒（F6）。"""
    return max(1.0, lease_seconds / 3)


async def _heartbeat_loop(
    *,
    storage: _ProcessingStorage,
    group_id: str,
    processing_key: str,
    owner_token: str,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
    interval: float,
) -> None:
    """租约心跳：每轮一次 wait_for(stop.wait())，靠超时推进续租（F10 冻结结构）。

    容错不对称（F7）：异常连续 2 次才置 lost；renewed=False 立即 lost；
    renewed=True 清零计数。
    """
    consecutive_errors = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
        else:
            return
        try:
            renewed = await storage.renew_processing_conversation_lease(
                group_id,
                processing_key,
                owner_token,
            )
        except Exception as error:
            consecutive_errors += 1
            logger.exception(
                "[KomariMemory] 对话 processing 续租异常: group={} key={} "
                "failures={} error_type={}",
                group_id,
                processing_key,
                consecutive_errors,
                type(error).__name__,
            )
            if consecutive_errors < 2:
                continue
        else:
            if renewed:
                consecutive_errors = 0
                continue
        lease_lost.set()
        return


async def _stop_processing_attempt(
    *,
    heartbeat_stop: asyncio.Event,
    heartbeat_task: asyncio.Task[None],
    processing_task: asyncio.Future[None],
) -> None:
    """停止处理与续租任务，并完整回收它们的异常。"""
    heartbeat_stop.set()
    if not processing_task.done():
        processing_task.cancel()
    await asyncio.gather(
        processing_task,
        heartbeat_task,
        return_exceptions=True,
    )


@retry_async(max_attempts=3, base_delay=1.0, exclude=(ConversationLeaseLostError,))
async def _run_processing_attempt(
    storage: _ProcessingStorage,
    group_id: str,
    processing_key: str,
    owner_token: str,
    collector: Any,
    processor: ConversationProcessor,
) -> None:
    """读缓冲 → 业务回调；每次尝试重建 session，幂等性依赖账本（F17/F21）。

    TSK-155：ConversationLeaseLostError 经 exclude 排除，首次出现即原样抛出
    （零 sleep、零重试日志），由外层按 lease-lost 分流（dead-letter / restore
    零副作用），普通异常仍按原语义重试恰好 3 次。
    """
    messages = await storage.get_processing_conversation_buffer(
        group_id,
        processing_key,
        owner_token,
    )
    if not messages:
        # F23：空缓冲 warning 早退，外层照常门控 + ack 消费快照，不算失败。
        logger.warning("[KomariMemory] 群组 {} 消息缓冲为空", group_id)
        return
    session = ProcessingSession(
        group_id=group_id,
        processing_key=processing_key,
        messages=messages,
        collector=collector,
        ledger=StorageChunkLedger(storage, group_id, processing_key, owner_token),
    )
    await processor.process(session)


class ConversationProcessingLifecycle:
    """对话 processing 生命周期深 module 双入口。"""

    def __init__(
        self,
        storage: _ProcessingStorage,
        collector_provider: _CollectorProvider,
    ) -> None:
        self._storage = storage
        self._collector_provider = collector_provider

    async def process_conversation_snapshot(
        self,
        group_id: str,
        processor: ConversationProcessor,
        *,
        existing_processing_key: str | None = None,
    ) -> bool:
        """发起一次快照处理。

        True=认领且走完生命周期（含空缓冲消费）；False=busy/empty 静默早退。

        claim 的存储异常直接向外传播；认领成功后的所有清理（dead-letter /
        restore / 停止心跳）都在本方法内完成，最终按分流规则 re-raise。
        """
        storage = self._storage
        owner_token = f"summary-{uuid4().hex}"
        # 效果前最后同步步骤：裁决关联群归属（ADR-0012 / AC1/AC2/AC3/AC8）。
        # 受限或归属失败的候选不领租约、不读正文、不耗 failure/retry、不进
        # dead-letter，直接休眠返回 False。
        if not memory_conversation_business_admitted(group_id=group_id):
            return False
        if existing_processing_key is None:
            claim = await storage.claim_conversation_buffer(
                group_id,
                owner_token,
                uuid4().hex[:8],
            )
        else:
            claim = await storage.claim_existing_conversation_processing(
                group_id,
                existing_processing_key,
                owner_token,
            )
        if claim.status != "claimed" or not claim.processing_key:
            return False
        processing_key = claim.processing_key
        collector = self._collector_provider.create(group_id, processing_key)
        # lease_seconds 每次处理尝试启动时读取，与 RedisManager.config 同形支持热重载。
        interval = _heartbeat_interval(
            storage.config.conversation_processing_lease_seconds
        )
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                storage=storage,
                group_id=group_id,
                processing_key=processing_key,
                owner_token=owner_token,
                stop=heartbeat_stop,
                lease_lost=lease_lost,
                interval=interval,
            )
        )
        processing_task = asyncio.ensure_future(
            _run_processing_attempt(
                storage,
                group_id,
                processing_key,
                owner_token,
                collector,
                processor,
            )
        )
        lease_lost_wait = asyncio.create_task(lease_lost.wait())

        try:
            # F8：处理与租约丢失竞速，lost 后取消处理任务再抛 ConversationLeaseLostError。
            await asyncio.wait(
                {processing_task, lease_lost_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_lost.is_set():
                processing_task.cancel()
                await asyncio.gather(processing_task, return_exceptions=True)
                _raise_conversation_lease_lost(processing_key)
            await processing_task
            # F9：完成门控四步——停心跳 → await → 显式 renew → ack。
            heartbeat_stop.set()
            await heartbeat_task
            if lease_lost.is_set() or not await storage.renew_processing_conversation_lease(
                group_id,
                processing_key,
                owner_token,
            ):
                _raise_conversation_lease_lost(processing_key)
            if not await storage.ack_processing_conversation_buffer(
                group_id,
                processing_key,
                owner_token,
            ):
                _raise_conversation_lease_lost(processing_key)
        except asyncio.CancelledError as error:
            # F10/F25：finalize(cancelled) 最先 → 停任务 → shield(restore) 恰好一次。
            await self._collector_provider.finalize(
                collector,
                status="cancelled",
                error=error,
            )
            await _stop_processing_attempt(
                heartbeat_stop=heartbeat_stop,
                heartbeat_task=heartbeat_task,
                processing_task=processing_task,
            )
            try:
                restored = await asyncio.shield(
                    storage.restore_processing_conversation_buffer(
                        group_id,
                        processing_key,
                        owner_token,
                    )
                )
            except Exception as cleanup_error:
                logger.exception(
                    "[KomariMemory] 取消总结后的快照恢复失败: group={} key={} "
                    "error_type={}",
                    group_id,
                    processing_key,
                    type(cleanup_error).__name__,
                )
            else:
                if not restored:
                    logger.warning(
                        "[KomariMemory] 取消总结后的快照恢复被拒绝，当前 worker 已失去 "
                        "owner: group={} key={}",
                        group_id,
                        processing_key,
                    )
            raise
        except Exception as error:
            # F25：error finalize 必须先于 dead-letter；lease-lost 零副作用（F11）。
            await self._collector_provider.finalize(
                collector,
                status="error",
                error=error,
            )
            await _stop_processing_attempt(
                heartbeat_stop=heartbeat_stop,
                heartbeat_task=heartbeat_task,
                processing_task=processing_task,
            )
            if not isinstance(error, ConversationLeaseLostError):
                # F12：attempt_count 透传 retry_async 的真实尝试次数（TSK-155）——
                # 普通异常穷尽重试后附着 max_attempts；未经过包装层的异常
                # （如门控续租/ack 失败）按至少 1 次兜底。
                dead_lettered = False
                try:
                    dead_lettered = await storage.dead_letter_processing_conversation_buffer(
                        group_id,
                        processing_key,
                        owner_token,
                        failure_code=type(error).__name__,
                        attempt_count=get_retry_attempts(error) or 1,
                    )
                except Exception as cleanup_error:
                    logger.exception(
                        "[KomariMemory] 对话快照移入 dead-letter 失败: group={} key={} "
                        "error_type={}",
                        group_id,
                        processing_key,
                        type(cleanup_error).__name__,
                    )
                if not dead_lettered:
                    # F13：dead-letter 失败/被拒 → restore 兜底，最终总是 re-raise。
                    try:
                        restored = await storage.restore_processing_conversation_buffer(
                            group_id,
                            processing_key,
                            owner_token,
                        )
                    except Exception as cleanup_error:
                        logger.exception(
                            "[KomariMemory] dead-letter 失败后的快照恢复失败: group={} "
                            "key={} error_type={}",
                            group_id,
                            processing_key,
                            type(cleanup_error).__name__,
                        )
                    else:
                        if not restored:
                            logger.warning(
                                "[KomariMemory] dead-letter 失败后的快照恢复被拒绝，"
                                "当前 worker 已失去 owner: group={} key={}",
                                group_id,
                                processing_key,
                            )
            raise
        else:
            # F25：success finalize 在 ack 之后。
            await self._collector_provider.finalize(collector, status="success")
        finally:
            heartbeat_stop.set()
            lease_lost_wait.cancel()
            await asyncio.gather(lease_lost_wait, return_exceptions=True)

        # F25：update_last_summary 仅在 ack 成功之后（try/finally 之外）。
        await storage.update_last_summary(group_id)
        return True

    async def run_worker_cycle(
        self,
        processor_factory: Callable[[], ConversationProcessor],
    ) -> None:
        """孤儿扫描 → 逐个接管 → resumed_groups 去重 → 活跃群触发；永不抛出。

        processor_factory 每个触发尝试调用一次；任何异常吞掉记 log 不中断。
        """
        storage = self._storage
        try:
            orphaned = await storage.get_orphaned_conversation_processing_keys()
        except Exception as error:
            logger.error(
                "[KomariMemory] 扫描可接管的对话 processing 快照失败: error_type={}",
                type(error).__name__,
            )
            orphaned = []
        resumed_groups: set[str] = set()
        for group_id, processing_key in orphaned:
            try:
                processor = processor_factory()
                await self.process_conversation_snapshot(
                    group_id,
                    processor,
                    existing_processing_key=processing_key,
                )
            except Exception as error:
                logger.error(
                    "[KomariMemory] 接管遗留对话快照失败: group={} key={} "
                    "error_type={}",
                    group_id,
                    processing_key,
                    type(error).__name__,
                )
            resumed_groups.add(group_id)

        try:
            group_ids = await storage.get_active_groups()
        except Exception as error:
            logger.error(
                "[KomariMemory] 获取活跃群组失败: error_type={}",
                type(error).__name__,
            )
            group_ids = []
        group_ids = [group_id for group_id in group_ids if group_id not in resumed_groups]
        for group_id in group_ids:
            try:
                if await storage.should_trigger_summary(group_id):
                    processor = processor_factory()
                    await self.process_conversation_snapshot(group_id, processor)
            except Exception as error:
                logger.error(
                    "[KomariMemory] 群组总结失败: group={} error_type={}",
                    group_id,
                    type(error).__name__,
                )
