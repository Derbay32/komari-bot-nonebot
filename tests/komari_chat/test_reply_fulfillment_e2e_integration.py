"""回复履约跨边界端到端集成验收（TSK-88）。

以 workflow 为唯一核心 seam，真实 PostgreSQL（履约父子表 + 好感度
账本）与真实 Redis（幂等证据键 + 主动预占）参与；OneBot 发送边界以
受控 sender 注入，平台结果只经 ``ReplyDeliveryResult`` 进入领域。

门控纪律（验收项 1）：
- 缺 ``KOMARI_TEST_POSTGRES_URL`` / ``SQLALCHEMY_DATABASE_URL`` 或两者
  不指向同一库时模块级安全 skip，绝不隐式连接本地默认库；
- 每个用例经 ``_assert_live_gate`` 做活连接校验：asyncpg 直连与
  nonebot-plugin-orm 共享引擎的 ``current_database()`` 必须都等于
  门控 DSN 声明的库名，防止环境变量与运行时连接漂移；
- Redis 由 ``KOMARI_TEST_REDIS_URL`` / ``KOMARI_TEST_REDIS_SOCKET``
  门控，缺省时同样安全 skip。
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
    from collections.abc import AsyncIterator, Awaitable

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


async def _cleanup_favorability(operation_id: str) -> None:
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
            {"user_id": _E2E_USER_ID},
        )
        await session.commit()
    finally:
        await session.close()


def _pending_reply(
    run_id: str,
    *,
    reservation: proactive_module.Reservation | None,
    reservation_id: str | None = None,
) -> Any:
    fulfillment_id = build_reply_fulfillment_id(
        group_id=_E2E_GROUP_ID,
        trigger_message_id=f"message-{run_id}",
        trigger_user_id=_E2E_USER_ID,
    )
    message = MessageSchema(
        user_id=_E2E_USER_ID,
        user_nickname="端到端用户",
        group_id=_E2E_GROUP_ID,
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
