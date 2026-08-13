"""回复履约跨边界端到端集成验收（TSK-88 / TSK-101）。

以 workflow 为唯一核心 seam，真实 PostgreSQL（履约父子表 + 好感度
账本）与真实 Redis（幂等证据键 + 主动预占）参与；OneBot 发送边界以
受控 sender 注入，平台结果只经 ``ReplyDeliveryResult`` 进入领域。

TSK-101 补齐发送前恢复路径的真实组件证据缺口：既有 3 个用例装配时
恢复 sender 映射为空（``dict``），本文件新增用例注入模拟真实 OneBot
发送边界的恢复 sender，端到端闭环验收四个恢复窗口——持久准备后崩溃
补发、登记发送开始后崩溃不重复发送、恢复结果未知保守待确认、Bot
身份不精确匹配不领取、满时效过期终止释放真实预占。

门控纪律（验收项 1 / 验收项 5）：
- 缺 ``KOMARI_TEST_POSTGRES_URL`` / ``SQLALCHEMY_DATABASE_URL`` 或两者
  不指向同一库时模块级安全 skip，绝不隐式连接本地默认库；
- 每个用例经 ``_assert_live_gate`` 做活连接校验：asyncpg 直连与
  nonebot-plugin-orm 共享引擎的 ``current_database()`` 必须都等于
  门控 DSN 声明的库名，防止环境变量与运行时连接漂移；
- Redis 由 ``KOMARI_TEST_REDIS_URL`` / ``KOMARI_TEST_REDIS_SOCKET``
  门控，缺省时同样安全 skip；
- 数据隔离：每个用例用 uuid 生成 group/user 标识，用例结束清理自建
  行与自建 Redis 键，不碰门控库其他数据。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text

from komari_bot.plugins.komari_chat.handlers import message_handler as handler_module
from komari_bot.plugins.komari_chat.repositories.reply_fulfillment_repository import (
    ReplyFulfillmentRepository,
)
from komari_bot.plugins.komari_chat.services import (
    proactive_reservation as proactive_module,
)
from komari_bot.plugins.komari_chat.services.reply_commitment_workflow import (
    ReplyCommitmentWorkflow,
)
from komari_bot.plugins.komari_chat.services.reply_delivery_onebot import (
    ReplyDeliveryResult,
)
from komari_bot.plugins.komari_chat.services.reply_fulfillment_alert import (
    ReplyFulfillmentAlertService,
)
from komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow import (
    ReplyFulfillmentWorkflow,
    build_reply_fulfillment_id,
)
from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services.redis_keys import RedisKeys
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)
from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB
from komari_bot.plugins.user_data.orm_models import (
    UserFavorabilityAdjustmentLedgerRow,
    UserFavorabilityRow,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Mapping

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")
REDIS_SOCKET = os.getenv("KOMARI_TEST_REDIS_SOCKET", "")


def _database_identity(url: str) -> tuple[str, int | None, str]:
    parsed = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    return parsed.hostname or "", parsed.port, parsed.path


DATABASE_CONFIGURED = bool(
    POSTGRES_URL
    and SQLALCHEMY_URL
    and _database_identity(POSTGRES_URL) == _database_identity(SQLALCHEMY_URL)
)
REDIS_CONFIGURED = bool(REDIS_URL or REDIS_SOCKET)

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_CONFIGURED,
        reason="KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 未指向同一真实库",
    ),
    pytest.mark.skipif(
        not REDIS_CONFIGURED,
        reason="未配置真实 Redis 测试连接",
    ),
    pytest.mark.asyncio,
]

_E2E_USER_ID = "e2e-user-1"
_E2E_GROUP_ID = "e2e-group-1"


def _asyncpg_url() -> str:
    return POSTGRES_URL.replace("postgresql+asyncpg://", "postgresql://")


def _database_name() -> str:
    return _database_identity(POSTGRES_URL)[2].lstrip("/")


def _redis_client() -> aioredis.Redis:
    if REDIS_URL:
        return aioredis.from_url(REDIS_URL, decode_responses=True)
    return aioredis.Redis(unix_socket_path=REDIS_SOCKET, decode_responses=True)


async def _reset_shared_orm_engine() -> None:
    """清空 nonebot-plugin-orm 共享引擎连接池（每个用例独立事件循环）。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


async def _assert_live_gate(pool: asyncpg.Pool) -> None:
    """活连接校验：直连与 ORM 共享引擎必须都落在门控声明的库。

    只比较 ``current_database()`` 库名——主机/端口已由模块级静态
    门控约束；任何漂移立即失败，而不是在错误库上静默执行。
    """
    expected = _database_name()
    async with pool.acquire() as connection:
        direct_name = await connection.fetchval("SELECT current_database()")
    assert direct_name == expected

    from nonebot_plugin_orm import get_session

    session = get_session(expire_on_commit=False)
    try:
        orm_name = (
            await session.execute(text("SELECT current_database()"))
        ).scalar_one()
    finally:
        await session.close()
    assert orm_name == expected


class _E2EStack:
    """一套真实组件装配：单 Repository 被 workflow/承诺/告警三方共享。"""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
        redis_manager: RedisManager,
        user_data: UserDataDB,
        reservation_service: proactive_module.ProactiveReservationService,
        config: SimpleNamespace,
    ) -> None:
        self.pool = pool
        self.redis_client = redis_client
        self.redis = redis_manager
        self.user_data = user_data
        self.proactive = reservation_service
        self.config = config
        self.repository = ReplyFulfillmentRepository(pool)
        self.commitments = ReplyCommitmentWorkflow(
            repository=self.repository,
            redis=redis_manager,
            proactive_reservation=reservation_service,
            user_data=user_data,
            config_getter=lambda: config,
        )
        self.alerts = ReplyFulfillmentAlertService(
            repository=self.repository,
            bots_provider=list,
            superusers_provider=tuple,
        )
        self.workflow = ReplyFulfillmentWorkflow(
            repository=self.repository,
            proactive_reservation=reservation_service,
            config_getter=lambda: config,
            recovery_senders_getter=dict,
            commitment_workflow=self.commitments,
            alert_service=self.alerts,
        )


def _e2e_config() -> SimpleNamespace:
    return SimpleNamespace(
        proactive_cooldown=300,
        global_interaction_enabled=True,
        global_interaction_trigger_size=20,
        reply_fulfillment_worker_interval_seconds=5,
        reply_fulfillment_batch_size=20,
        reply_fulfillment_lease_seconds=120,
        reply_fulfillment_max_attempts=3,
        reply_fulfillment_retry_base_seconds=1,
        reply_fulfillment_retry_max_seconds=60,
        reply_fulfillment_tombstone_retention_days=30,
        reply_fulfillment_freshness_seconds=120,
    )


@asynccontextmanager
async def _stack(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_E2EStack]:
    """装配真实组件并保证用例结束后清理连接与 ORM 引擎。"""
    monkeypatch.setattr(
        proactive_module,
        "get_config",
        lambda: SimpleNamespace(
            proactive_cooldown=300,
            proactive_max_per_hour=400,
            proactive_reservation_ttl_seconds=360,
        ),
    )
    pool = await asyncpg.create_pool(_asyncpg_url(), min_size=1, max_size=4)
    redis_client = _redis_client()
    await _reset_shared_orm_engine()
    user_data = UserDataDB(DynamicConfigSchema(initial_favorability=100))
    await user_data.initialize()
    try:
        await _assert_live_gate(pool)
        redis_manager = RedisManager(KomariMemoryConfigSchema())
        redis_manager._redis = cast("Any", redis_client)
        reservation_service = proactive_module.ProactiveReservationService(
            cast("Any", redis_client)
        )
        yield _E2EStack(
            pool=pool,
            redis_client=redis_client,
            redis_manager=redis_manager,
            user_data=user_data,
            reservation_service=reservation_service,
            config=_e2e_config(),
        )
    finally:
        await user_data.close()
        await redis_client.aclose()
        await pool.close()
        await _reset_shared_orm_engine()


async def _cleanup_rows(pool: asyncpg.Pool, fulfillment_ids: list[str]) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            DELETE FROM komari_chat_reply_fulfillments
            WHERE fulfillment_id = ANY($1::text[])
            """,
            fulfillment_ids,
        )


async def _cleanup_favorability(
    operation_id: str,
    user_id: str = _E2E_USER_ID,
) -> None:
    from nonebot_plugin_orm import get_session

    session = get_session(expire_on_commit=False)
    try:
        await session.execute(
            text(
                "DELETE FROM "
                f"{UserFavorabilityAdjustmentLedgerRow.__tablename__} "
                "WHERE operation_id = :operation_id"
            ),
            {"operation_id": operation_id},
        )
        await session.execute(
            text(
                f"DELETE FROM {UserFavorabilityRow.__tablename__} "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )
        await session.commit()
    finally:
        await session.close()


def _reservation_keys(group_id: str) -> tuple[str, str]:
    """主动预占的冷却键与名额槽键（前缀与 proactive_reservation 模块一致）。"""
    return (
        f"komari_chat:proactive:cd:{group_id}",
        f"komari_chat:proactive:slots:{group_id}",
    )


def _restarted_workflow(
    stack: _E2EStack,
    recovery_senders: Mapping[tuple[str, str], Any],
) -> ReplyFulfillmentWorkflow:
    """模拟进程重启：新 workflow 实例共享同一仓储/承诺/告警。

    恢复 sender 由调用方注入（模拟真实 OneBot 发送边界的 async
    callable，记录每次调用的发送载荷并返回 ``ReplyDeliveryResult``）；
    各服务仍共享 stack 上同一个 ``ReplyFulfillmentRepository``。
    """
    return ReplyFulfillmentWorkflow(
        repository=stack.repository,
        proactive_reservation=stack.proactive,
        config_getter=lambda: stack.config,
        recovery_senders_getter=lambda: dict(recovery_senders),
        commitment_workflow=stack.commitments,
        alert_service=stack.alerts,
    )


def _pending_reply(
    run_id: str,
    *,
    reservation: proactive_module.Reservation | None,
    reservation_id: str | None = None,
    group_id: str = _E2E_GROUP_ID,
    user_id: str = _E2E_USER_ID,
) -> Any:
    fulfillment_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=user_id,
    )
    message = MessageSchema(
        user_id=user_id,
        user_nickname="端到端用户",
        group_id=group_id,
        content="用户正文",
        timestamp=1.0,
        message_id=f"message-{run_id}",
    )
    return handler_module.PendingReply(
        reply="端到端角色回复",
        reply_to_message_id=f"message-{run_id}",
        message=message,
        reply_result=handler_module.ReplyResult(
            content="端到端角色回复",
            interaction_history={
                "event": "发言",
                "result": "回复",
                "emotion": "平静",
            },
            favorability_delta=5,
            favorability_reason="端到端互动",
        ),
        force_reply=False,
        bot_nickname="小鞠",
        bot_self_id="bot-e2e",
        adapter_name="onebot.v11",
        reason="score",
        reply_score=0.9,
        operation_id=fulfillment_id,
        request_trace_id=f"trace-{run_id}",
        reply_timestamp=2.0,
        proactive_reservation_id=(
            reservation_id
            if reservation_id is not None
            else (reservation.reservation_id if reservation is not None else None)
        ),
        proactive_reservation=reservation,
    )


def _evidence_keys(fulfillment_id: str) -> tuple[str, str]:
    return (
        RedisKeys.chat_commit_step(fulfillment_id, "ai_history"),
        RedisKeys.chat_commit_step(fulfillment_id, "interaction"),
    )


async def _commitment_states(
    pool: asyncpg.Pool,
    fulfillment_id: str,
) -> dict[str, str]:
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT commitment_type, state
            FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id = $1
            """,
            fulfillment_id,
        )
    return {str(row["commitment_type"]): str(row["state"]) for row in rows}


async def _age_terminal_beyond_protection(
    pool: asyncpg.Pool,
    fulfillment_id: str,
) -> None:
    """把已解决终态的保护期起点改到 31 天前（配置保护期 30 天）。

    只移动实际非空的保护期起点列：DELIVERED 行改 ``completed_at``、
    NOT_DELIVERED 行改 ``not_delivered_at``；另一列必须保持 NULL，
    否则违反 ``ck_reply_fulfillment_delivery_timestamps`` 状态组合约束。
    """
    async with pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE komari_chat_reply_fulfillments
            SET completed_at = CASE
                    WHEN completed_at IS NOT NULL
                    THEN completed_at - INTERVAL '31 days'
                END,
                not_delivered_at = CASE
                    WHEN not_delivered_at IS NOT NULL
                    THEN not_delivered_at - INTERVAL '31 days'
                END,
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE fulfillment_id = $1
            """,
            fulfillment_id,
        )


async def test_e2e_delivered_reply_full_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端全链：准备→发送→四承诺→终态最小化→保护期防重→两阶段清理。

    真实 PG/Redis/好感度参与；送达后四承诺在同一父租约内完成，终态
    最小化抹除正文与载荷；保护期内同身份拒绝重发，超期清理删除全部
    幂等证据后身份释放。
    """
    run_id = uuid4().hex
    fulfillment_id = build_reply_fulfillment_id(
        group_id=_E2E_GROUP_ID,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=_E2E_USER_ID,
    )
    async with _stack(monkeypatch) as stack:
        try:
            reservation = await stack.proactive.reserve(
                _E2E_GROUP_ID, f"reservation-{run_id}"
            )
            assert isinstance(reservation, proactive_module.Reservation)

            async def _send(_pending: object) -> ReplyDeliveryResult:
                return ReplyDeliveryResult.delivered(
                    platform_message_id=f"platform-{run_id}"
                )

            pending = _pending_reply(run_id, reservation=reservation)
            assert await stack.workflow.fulfill(pending, send_reply=_send) is True

            # 父终态完成且载荷最小化；平台消息 ID 持久化
            async with stack.pool.acquire() as connection:
                parent = await connection.fetchrow(
                    """
                    SELECT delivery_state, completed_at, reply_content,
                           platform_message_id
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert parent is not None
            assert parent["delivery_state"] == "DELIVERED"
            assert parent["completed_at"] is not None
            assert parent["reply_content"] is None
            assert parent["platform_message_id"] == f"platform-{run_id}"
            states = await _commitment_states(stack.pool, fulfillment_id)
            assert states == {
                "proactive_reply_confirmation": "COMPLETED",
                "favorability_adjustment": "COMPLETED",
                "assistant_reply_history": "COMPLETED",
                "interaction_history": "COMPLETED",
            }
            # 子项载荷已最小化
            async with stack.pool.acquire() as connection:
                non_null_payloads = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM komari_chat_reply_fulfillment_commitments
                    WHERE fulfillment_id = $1 AND payload IS NOT NULL
                    """,
                    fulfillment_id,
                )
            assert non_null_payloads == 0

            # 真实 Redis：两枚幂等证据键持久（TTL=-1，超越旧固定 TTL 语义）
            ai_key, interaction_key = _evidence_keys(fulfillment_id)
            assert await stack.redis_client.ttl(ai_key) == -1
            assert await stack.redis_client.ttl(interaction_key) == -1
            # 主动预占已转已送达：冷却键存在
            cooldown_key = f"komari_chat:proactive:cd:{_E2E_GROUP_ID}"
            assert await stack.redis_client.exists(cooldown_key) == 1

            # 真实好感度：+5 且账本唯一
            score = await stack.user_data.get_user_favorability(_E2E_USER_ID)
            assert score.favorability == 105
            from nonebot_plugin_orm import get_session

            session = get_session(expire_on_commit=False)
            try:
                ledger_count = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            f"{UserFavorabilityAdjustmentLedgerRow.__tablename__} "
                            "WHERE operation_id = :operation_id"
                        ),
                        {"operation_id": f"{fulfillment_id}:favorability"},
                    )
                ).scalar_one()
            finally:
                await session.close()
            assert ledger_count == 1

            # 保护期内同身份同载荷重投被幂等拒绝（终态 tombstone 防重）；
            # 载荷必须与首次完全一致（含承诺子集），不同载荷属冲突语义
            duplicate = _pending_reply(
                run_id,
                reservation=None,
                reservation_id=f"reservation-{run_id}",
            )
            assert (
                await stack.workflow.fulfill(
                    duplicate,
                    send_reply=_send,
                )
                is False
            )

            # 超过保护期：两阶段清理删除证据、tombstone 与子行
            await _age_terminal_beyond_protection(stack.pool, fulfillment_id)
            assert await stack.commitments.cleanup_terminal_fulfillments() == 1
            assert await stack.redis_client.exists(ai_key) == 0
            assert await stack.redis_client.exists(interaction_key) == 0
            session = get_session(expire_on_commit=False)
            try:
                ledger_after = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            f"{UserFavorabilityAdjustmentLedgerRow.__tablename__} "
                            "WHERE operation_id = :operation_id"
                        ),
                        {"operation_id": f"{fulfillment_id}:favorability"},
                    )
                ).scalar_one()
            finally:
                await session.close()
            assert ledger_after == 0
            async with stack.pool.acquire() as connection:
                remaining = await connection.fetchval(
                    """
                    SELECT COUNT(*) FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
                remaining_children = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM komari_chat_reply_fulfillment_commitments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert remaining == 0
            assert remaining_children == 0
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await _cleanup_favorability(f"{fulfillment_id}:favorability")
            await stack.redis_client.delete(
                *_evidence_keys(fulfillment_id),
                f"komari_chat:proactive:cd:{_E2E_GROUP_ID}",
                f"komari_chat:proactive:slots:{_E2E_GROUP_ID}",
            )


async def test_e2e_unresolved_fulfillment_keeps_persistent_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未解决履约：证据键持久保留，恢复时已兑现承诺不重复。

    好感度承诺注入一次「外部结果未知」故障：其余承诺先完成并留下
    持久证据；故障解除后恢复只补缺失项——AI 历史与互动缓冲不重复
    写入、好感度账目不二次应用。
    """
    run_id = uuid4().hex
    fulfillment_id = build_reply_fulfillment_id(
        group_id=_E2E_GROUP_ID,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=_E2E_USER_ID,
    )
    async with _stack(monkeypatch) as stack:
        try:
            # 注入一次好感度「外部结果未知」：adjust 抛连接错误
            original_adjust = cast("Any", stack.user_data.adjust_user_favorability)
            injected = True

            async def _flaky_adjust(*args: object, **kwargs: object) -> Any:
                nonlocal injected
                if injected:
                    injected = False
                    raise ConnectionError("好感度写入结果未知")
                return await original_adjust(*args, **kwargs)

            stack.user_data.adjust_user_favorability = _flaky_adjust  # type: ignore[method-assign]

            async def _send(_pending: object) -> ReplyDeliveryResult:
                return ReplyDeliveryResult.delivered(
                    platform_message_id=f"platform-{run_id}"
                )

            pending = _pending_reply(run_id, reservation=None)
            assert await stack.workflow.fulfill(pending, send_reply=_send) is True

            states = await _commitment_states(stack.pool, fulfillment_id)
            assert states["favorability_adjustment"] == "RETRY_WAIT"
            assert states["assistant_reply_history"] == "COMPLETED"
            assert states["interaction_history"] == "COMPLETED"
            assert "proactive_reply_confirmation" not in states

            # 证据键持久：即使超过任何旧固定 TTL 窗口也不会自动消失
            ai_key, interaction_key = _evidence_keys(fulfillment_id)
            assert await stack.redis_client.ttl(ai_key) == -1
            assert await stack.redis_client.ttl(interaction_key) == -1
            buffer_before = await cast(
                "Awaitable[int]",
                stack.redis_client.llen(RedisKeys.buffer(_E2E_GROUP_ID)),
            )
            interaction_before = await cast(
                "Awaitable[int]",
                stack.redis_client.llen(RedisKeys.global_interaction(_E2E_USER_ID)),
            )

            # 恢复：好感度补兑现；已完成两项由证据键幂等跳过
            async with stack.pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE komari_chat_reply_fulfillment_commitments
                    SET next_retry_at = NOW() - INTERVAL '1 second'
                    WHERE fulfillment_id = $1 AND state = 'RETRY_WAIT'
                    """,
                    fulfillment_id,
                )
            assert await stack.commitments.recover_fulfillment(fulfillment_id) is True

            states = await _commitment_states(stack.pool, fulfillment_id)
            assert set(states.values()) == {"COMPLETED"}
            assert await cast(
                "Awaitable[int]",
                stack.redis_client.llen(RedisKeys.buffer(_E2E_GROUP_ID)),
            ) == buffer_before
            assert await cast(
                "Awaitable[int]",
                stack.redis_client.llen(RedisKeys.global_interaction(_E2E_USER_ID)),
            ) == interaction_before
            score = await stack.user_data.get_user_favorability(_E2E_USER_ID)
            assert score.favorability == 105
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await _cleanup_favorability(f"{fulfillment_id}:favorability")
            await stack.redis_client.delete(*_evidence_keys(fulfillment_id))
            await stack.redis_client.delete(RedisKeys.buffer(_E2E_GROUP_ID))
            await stack.redis_client.delete(
                RedisKeys.global_interaction(_E2E_USER_ID)
            )


async def test_e2e_cleanup_never_deletes_tombstone_before_all_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理顺序铁律：任一证据删除失败都必须保留 tombstone 与剩余证据。

    第一轮注入 Redis 证据删除失败：tombstone 保留、marker 不落、
    好感度账本仍在；第二轮注入好感度删除失败：Redis 证据已删但
    tombstone 仍保留；第三轮全恢复：marker 落且 tombstone 删除。
    """
    run_id = uuid4().hex
    fulfillment_id = build_reply_fulfillment_id(
        group_id=_E2E_GROUP_ID,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=_E2E_USER_ID,
    )
    async with _stack(monkeypatch) as stack:
        try:
            async def _send(_pending: object) -> ReplyDeliveryResult:
                return ReplyDeliveryResult.delivered(
                    platform_message_id=f"platform-{run_id}"
                )

            pending = _pending_reply(run_id, reservation=None)
            assert await stack.workflow.fulfill(pending, send_reply=_send) is True
            await _age_terminal_beyond_protection(stack.pool, fulfillment_id)
            ai_key, interaction_key = _evidence_keys(fulfillment_id)

            # 第一轮：Redis 证据删除失败 → tombstone 必须保留
            original_redis_delete = stack.redis.delete_chat_commit_evidence
            original_ledger_delete = stack.user_data.delete_favorability_operation

            async def _fail_redis_delete(_operation_id: str) -> int:
                raise ConnectionError("Redis暂不可用")

            stack.redis.delete_chat_commit_evidence = _fail_redis_delete  # type: ignore[method-assign]
            assert await stack.commitments.cleanup_terminal_fulfillments() == 0
            async with stack.pool.acquire() as connection:
                parent = await connection.fetchrow(
                    """
                    SELECT idempotency_evidence_cleared_at
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert parent is not None
            assert parent["idempotency_evidence_cleared_at"] is None

            # 第二轮：Redis 证据删除成功、好感度删除失败 → tombstone 仍保留
            #（显式恢复原方法，不用 monkeypatch.undo() 以免误伤 stack 装配）
            stack.redis.delete_chat_commit_evidence = original_redis_delete  # type: ignore[method-assign]

            async def _fail_ledger_delete(_operation_id: str) -> bool:
                raise ConnectionError("好感度账本删除结果未知")

            stack.user_data.delete_favorability_operation = _fail_ledger_delete  # type: ignore[method-assign]
            assert await stack.commitments.cleanup_terminal_fulfillments() == 0
            assert await stack.redis_client.exists(ai_key) == 0
            assert await stack.redis_client.exists(interaction_key) == 0
            async with stack.pool.acquire() as connection:
                parent = await connection.fetchrow(
                    """
                    SELECT idempotency_evidence_cleared_at
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert parent is not None
            assert parent["idempotency_evidence_cleared_at"] is None
            score = await stack.user_data.get_user_favorability(_E2E_USER_ID)
            assert score.favorability == 105

            # 第三轮：全部恢复 → marker 落、tombstone 删除
            stack.user_data.delete_favorability_operation = original_ledger_delete  # type: ignore[method-assign]
            assert await stack.commitments.cleanup_terminal_fulfillments() == 1
            async with stack.pool.acquire() as connection:
                remaining = await connection.fetchval(
                    """
                    SELECT COUNT(*) FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert remaining == 0
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await _cleanup_favorability(f"{fulfillment_id}:favorability")
            await stack.redis_client.delete(*_evidence_keys(fulfillment_id))
            await stack.redis_client.delete(RedisKeys.buffer(_E2E_GROUP_ID))
            await stack.redis_client.delete(
                RedisKeys.global_interaction(_E2E_USER_ID)
            )


async def test_e2e_recovery_after_prepare_crash_resends_and_advances_all_commitments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久准备后崩溃→重启恢复补发→送达事实持久化→四项承诺推进（TSK-101 验收项 1）。

    真实组件闭环：``_prepare`` 完成持久准备后直接抛弃该次执行（从不
    调用平台发送）模拟进程崩溃；新 workflow 实例（模拟重启）注入模拟
    真实 OneBot 发送边界的恢复 sender 跑恢复循环——精确身份领取
    NOT_STARTED 行、补发、送达事实持久化，四项承诺在同一父租约内
    推进（proactive confirm / 好感度 adjust / AI 历史存储 / 互动历史
    写入），断言方式与既有 e2e 用例一致。
    """
    run_id = uuid4().hex
    group_id = f"e2e-recover-group-{run_id}"
    user_id = f"e2e-recover-user-{run_id}"
    fulfillment_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=user_id,
    )
    cooldown_key, slots_key = _reservation_keys(group_id)
    async with _stack(monkeypatch) as stack:
        try:
            reservation = await stack.proactive.reserve(
                group_id, f"reservation-{run_id}"
            )
            assert isinstance(reservation, proactive_module.Reservation)

            pending = _pending_reply(
                run_id,
                reservation=reservation,
                group_id=group_id,
                user_id=user_id,
            )
            # 模拟崩溃窗口：持久准备完成，发送前进程崩溃（不调用发送）
            assert await stack.workflow._prepare(pending) is True
            async with stack.pool.acquire() as connection:
                before = await connection.fetchrow(
                    """
                    SELECT delivery_state, reply_content
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert before is not None
            assert before["delivery_state"] == "NOT_STARTED"
            assert before["reply_content"] == "端到端角色回复"

            sent: list[Any] = []

            async def _recovery_send(recovered_reply: object) -> ReplyDeliveryResult:
                sent.append(recovered_reply)
                return ReplyDeliveryResult.delivered(f"platform-{run_id}")

            restarted = _restarted_workflow(
                stack,
                {("bot-e2e", "onebot.v11"): _recovery_send},
            )
            assert await restarted.recover_pending() == 1

            # 恢复 sender 收到真实投影载荷且只被调用一次
            assert len(sent) == 1
            recovered = sent[0]
            assert recovered.group_id == group_id
            assert recovered.reply == "端到端角色回复"
            assert recovered.reply_to_message_id == f"message-{run_id}"

            # 送达事实持久化：DELIVERED、平台消息 ID、父正文最小化
            async with stack.pool.acquire() as connection:
                parent = await connection.fetchrow(
                    """
                    SELECT delivery_state, completed_at, reply_content,
                           platform_message_id
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert parent is not None
            assert parent["delivery_state"] == "DELIVERED"
            assert parent["completed_at"] is not None
            assert parent["reply_content"] is None
            assert parent["platform_message_id"] == f"platform-{run_id}"

            # 四项承诺全部推进（复用既有 e2e 断言方式）
            states = await _commitment_states(stack.pool, fulfillment_id)
            assert states == {
                "proactive_reply_confirmation": "COMPLETED",
                "favorability_adjustment": "COMPLETED",
                "assistant_reply_history": "COMPLETED",
                "interaction_history": "COMPLETED",
            }
            ai_key, interaction_key = _evidence_keys(fulfillment_id)
            assert await stack.redis_client.ttl(ai_key) == -1
            assert await stack.redis_client.ttl(interaction_key) == -1
            # 主动预占已确认：冷却键存在
            assert await stack.redis_client.exists(cooldown_key) == 1

            # 真实好感度：+5 且账本唯一
            score = await stack.user_data.get_user_favorability(user_id)
            assert score.favorability == 105
            from nonebot_plugin_orm import get_session

            session = get_session(expire_on_commit=False)
            try:
                ledger_count = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            f"{UserFavorabilityAdjustmentLedgerRow.__tablename__} "
                            "WHERE operation_id = :operation_id"
                        ),
                        {"operation_id": f"{fulfillment_id}:favorability"},
                    )
                ).scalar_one()
            finally:
                await session.close()
            assert ledger_count == 1

            # AI 历史与互动缓冲各写入一条
            assert await cast(
                "Awaitable[int]",
                stack.redis_client.llen(RedisKeys.buffer(group_id)),
            ) == 1
            assert await cast(
                "Awaitable[int]",
                stack.redis_client.llen(RedisKeys.global_interaction(user_id)),
            ) == 1
            # 子项载荷已最小化
            async with stack.pool.acquire() as connection:
                non_null_payloads = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM komari_chat_reply_fulfillment_commitments
                    WHERE fulfillment_id = $1 AND payload IS NOT NULL
                    """,
                    fulfillment_id,
                )
            assert non_null_payloads == 0
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await _cleanup_favorability(
                f"{fulfillment_id}:favorability",
                user_id=user_id,
            )
            await stack.redis_client.delete(
                *_evidence_keys(fulfillment_id),
                cooldown_key,
                slots_key,
                RedisKeys.buffer(group_id),
                RedisKeys.global_interaction(user_id),
            )


async def test_e2e_crash_after_send_start_registered_is_never_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记发送开始后崩溃：保守待确认，恢复路径不重复发送（TSK-101 验收项 2）。

    直接路径 sender 以「平台侧已送达但本地未登记送达事实」语义返回
    待确认，行进入 PENDING_CONFIRMATION 类终态；重启后的恢复循环只
    领取 NOT_STARTED 行，该行不被领取、不重复发送，真实 Redis 预占
    保持占用（既不确认也不释放）。
    """
    run_id = uuid4().hex
    group_id = f"e2e-crash-group-{run_id}"
    user_id = f"e2e-crash-user-{run_id}"
    fulfillment_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=user_id,
    )
    cooldown_key, slots_key = _reservation_keys(group_id)
    async with _stack(monkeypatch) as stack:
        try:
            reservation = await stack.proactive.reserve(
                group_id, f"reservation-{run_id}"
            )
            assert isinstance(reservation, proactive_module.Reservation)
            reservation_member = f"pending:{reservation.reservation_id}"

            async def _send_unknown(_pending: object) -> ReplyDeliveryResult:
                return ReplyDeliveryResult.pending_confirmation()

            pending = _pending_reply(
                run_id,
                reservation=reservation,
                group_id=group_id,
                user_id=user_id,
            )
            assert (
                await stack.workflow.fulfill(pending, send_reply=_send_unknown)
                is False
            )
            async with stack.pool.acquire() as connection:
                parent = await connection.fetchrow(
                    """
                    SELECT delivery_state, send_started_at, platform_message_id
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert parent is not None
            assert parent["delivery_state"] == "PENDING_CONFIRMATION"
            assert parent["send_started_at"] is not None
            assert parent["platform_message_id"] is None
            # 崩溃后预占仍真实占用
            assert (
                await stack.redis_client.get(cooldown_key)
                == reservation.reservation_id
            )
            assert (
                await stack.redis_client.zscore(slots_key, reservation_member)
                is not None
            )

            recovery_send_count = 0

            async def _recovery_send(_reply: object) -> ReplyDeliveryResult:
                nonlocal recovery_send_count
                recovery_send_count += 1
                return ReplyDeliveryResult.delivered(f"platform-{run_id}")

            restarted = _restarted_workflow(
                stack,
                {("bot-e2e", "onebot.v11"): _recovery_send},
            )
            assert await restarted.recover_pending() == 0

            # 恢复路径不重复发送、不释放预占、行保持待确认
            assert recovery_send_count == 0
            async with stack.pool.acquire() as connection:
                after = await connection.fetchrow(
                    """
                    SELECT delivery_state
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert after is not None
            assert after["delivery_state"] == "PENDING_CONFIRMATION"
            assert (
                await stack.redis_client.get(cooldown_key)
                == reservation.reservation_id
            )
            assert (
                await stack.redis_client.zscore(slots_key, reservation_member)
                is not None
            )
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await stack.redis_client.delete(cooldown_key, slots_key)


async def test_e2e_recovery_sender_unknown_result_keeps_pending_without_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复发送结果未知：保守待确认、不重复发送、预占不释放（TSK-101 验收项 2）。

    恢复 sender 返回待确认，走 ``_finish_recovered`` 保守分支：行被
    领取后进入 PENDING_CONFIRMATION 并保持；下一轮恢复不再领取、不再
    调用发送；真实 Redis 预占既不确认也不释放。
    """
    run_id = uuid4().hex
    group_id = f"e2e-unknown-group-{run_id}"
    user_id = f"e2e-unknown-user-{run_id}"
    fulfillment_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=user_id,
    )
    cooldown_key, slots_key = _reservation_keys(group_id)
    async with _stack(monkeypatch) as stack:
        try:
            reservation = await stack.proactive.reserve(
                group_id, f"reservation-{run_id}"
            )
            assert isinstance(reservation, proactive_module.Reservation)
            reservation_member = f"pending:{reservation.reservation_id}"

            pending = _pending_reply(
                run_id,
                reservation=reservation,
                group_id=group_id,
                user_id=user_id,
            )
            # 模拟崩溃窗口：持久准备完成，发送前进程崩溃
            assert await stack.workflow._prepare(pending) is True

            recovery_send_count = 0

            async def _recovery_send(_reply: object) -> ReplyDeliveryResult:
                nonlocal recovery_send_count
                recovery_send_count += 1
                return ReplyDeliveryResult.pending_confirmation()

            restarted = _restarted_workflow(
                stack,
                {("bot-e2e", "onebot.v11"): _recovery_send},
            )
            assert await restarted.recover_pending() == 0
            assert recovery_send_count == 1
            # 保守待确认：预占仍真实占用
            assert (
                await stack.redis_client.get(cooldown_key)
                == reservation.reservation_id
            )
            assert (
                await stack.redis_client.zscore(slots_key, reservation_member)
                is not None
            )

            # 下一轮恢复：已非 NOT_STARTED，不重复发送
            assert await restarted.recover_pending() == 0
            assert recovery_send_count == 1
            async with stack.pool.acquire() as connection:
                after = await connection.fetchrow(
                    """
                    SELECT delivery_state
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert after is not None
            assert after["delivery_state"] == "PENDING_CONFIRMATION"
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await stack.redis_client.delete(cooldown_key, slots_key)


async def test_e2e_recovery_only_claims_exact_original_bot_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复约束：Bot 身份不精确匹配的行不被领取（TSK-101 验收项 3）。

    两条 NOT_STARTED 行：一条原身份（bot-e2e / onebot.v11），一条改
    适配器（bot-e2e / other-adapter）。错误 Bot 与错误适配器的恢复
    sender 均不领取；匹配原 Bot 的 sender 只领取并补发原身份行，
    适配器不符行保持 NOT_STARTED。
    """
    run_id = uuid4().hex
    group_id = f"e2e-identity-group-{run_id}"
    user_id = f"e2e-identity-user-{run_id}"
    match_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=user_id,
    )
    mismatch_run_id = uuid4().hex
    mismatch_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{mismatch_run_id}",
        trigger_user_id=user_id,
    )
    async with _stack(monkeypatch) as stack:
        try:
            pending_match = _pending_reply(
                run_id,
                reservation=None,
                group_id=group_id,
                user_id=user_id,
            )
            pending_mismatch = _pending_reply(
                mismatch_run_id,
                reservation=None,
                group_id=group_id,
                user_id=user_id,
            )
            assert await stack.workflow._prepare(pending_match) is True
            assert await stack.workflow._prepare(pending_mismatch) is True
            async with stack.pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE komari_chat_reply_fulfillments
                    SET adapter_name = 'other-adapter'
                    WHERE fulfillment_id = $1
                    """,
                    mismatch_id,
                )

            wrong_send_count = 0

            async def _wrong_sender(_reply: object) -> ReplyDeliveryResult:
                nonlocal wrong_send_count
                wrong_send_count += 1
                return ReplyDeliveryResult.delivered("wrong")

            # 错误 Bot / 错误适配器都不领取，两行都保持 NOT_STARTED
            restarted = _restarted_workflow(
                stack,
                {
                    ("bot-e2e-other", "onebot.v11"): _wrong_sender,
                    ("bot-e2e", "onebot.v11-other"): _wrong_sender,
                },
            )
            assert await restarted.recover_pending() == 0
            assert wrong_send_count == 0
            async with stack.pool.acquire() as connection:
                wrong_states = await connection.fetch(
                    """
                    SELECT delivery_state
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = ANY($1::text[])
                    """,
                    [match_id, mismatch_id],
                )
            assert {str(row["delivery_state"]) for row in wrong_states} == {
                "NOT_STARTED"
            }

            # 匹配原 Bot：只领取原身份行并补发
            match_send_count = 0

            async def _match_sender(_reply: object) -> ReplyDeliveryResult:
                nonlocal match_send_count
                match_send_count += 1
                return ReplyDeliveryResult.delivered(f"platform-{run_id}")

            restarted = _restarted_workflow(
                stack,
                {("bot-e2e", "onebot.v11"): _match_sender},
            )
            assert await restarted.recover_pending() == 1
            assert match_send_count == 1
            async with stack.pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT fulfillment_id, delivery_state
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = ANY($1::text[])
                    """,
                    [match_id, mismatch_id],
                )
            by_id = {
                str(row["fulfillment_id"]): str(row["delivery_state"])
                for row in rows
            }
            assert by_id[match_id] == "DELIVERED"
            assert by_id[mismatch_id] == "NOT_STARTED"
        finally:
            await _cleanup_rows(stack.pool, [match_id, mismatch_id])
            await _cleanup_favorability(
                f"{match_id}:favorability",
                user_id=user_id,
            )
            await stack.redis_client.delete(
                *_evidence_keys(match_id),
                RedisKeys.buffer(group_id),
                RedisKeys.global_interaction(user_id),
            )


async def test_e2e_recovery_expiration_releases_real_reservation_and_minimizes_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期终止：真实 Redis 预占被真实释放，父正文与子 payload 已清除（TSK-101 验收项 4）。

    持久准备后发送前崩溃且超过时效：恢复循环把 NOT_STARTED 行转为
    未送达终态，父正文与全部子 payload 清除；预占身份经子 payload
    投影键驱动真实 Redis 释放——冷却键删除、pending 名额移除，重复
    释放返回 False（不可二次释放）。
    """
    run_id = uuid4().hex
    group_id = f"e2e-expire-group-{run_id}"
    user_id = f"e2e-expire-user-{run_id}"
    fulfillment_id = build_reply_fulfillment_id(
        group_id=group_id,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=user_id,
    )
    cooldown_key, slots_key = _reservation_keys(group_id)
    async with _stack(monkeypatch) as stack:
        try:
            reservation = await stack.proactive.reserve(
                group_id, f"reservation-{run_id}"
            )
            assert isinstance(reservation, proactive_module.Reservation)
            reservation_member = f"pending:{reservation.reservation_id}"
            assert (
                await stack.redis_client.get(cooldown_key)
                == reservation.reservation_id
            )
            assert (
                await stack.redis_client.zscore(slots_key, reservation_member)
                is not None
            )

            pending = _pending_reply(
                run_id,
                reservation=reservation,
                group_id=group_id,
                user_id=user_id,
            )
            # 模拟崩溃窗口：持久准备完成，发送前进程崩溃
            assert await stack.workflow._prepare(pending) is True
            # 超过时效（freshness 120 秒）
            async with stack.pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE komari_chat_reply_fulfillments
                    SET prepared_at = NOW() - INTERVAL '121 seconds'
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )

            restarted = _restarted_workflow(stack, {})
            assert await restarted.recover_pending() == 0

            # 未送达终态：父正文与全部子 payload 已清除
            async with stack.pool.acquire() as connection:
                parent = await connection.fetchrow(
                    """
                    SELECT delivery_state, send_started_at, not_delivered_at,
                           reply_content
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
                children = await connection.fetch(
                    """
                    SELECT state, payload
                    FROM komari_chat_reply_fulfillment_commitments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
            assert parent is not None
            assert parent["delivery_state"] == "NOT_DELIVERED"
            assert parent["send_started_at"] is None
            assert parent["not_delivered_at"] is not None
            assert parent["reply_content"] is None
            assert len(children) == 4
            assert all(str(row["state"]) == "PENDING" for row in children)
            assert all(row["payload"] is None for row in children)

            # 真实 Redis：预占被真实释放，重复释放不再生效
            assert await stack.redis_client.exists(cooldown_key) == 0
            assert (
                await stack.redis_client.zscore(slots_key, reservation_member)
                is None
            )
            assert (
                await stack.proactive.release(
                    group_id, reservation.reservation_id
                )
                is False
            )
        finally:
            await _cleanup_rows(stack.pool, [fulfillment_id])
            await stack.redis_client.delete(cooldown_key, slots_key)
