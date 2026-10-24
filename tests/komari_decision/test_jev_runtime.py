"""TSK-182 常驻判定运行时验收：standby 资源、被动健康、恢复与清理。

全部断言穿过真实生产组件：真实 ``DecisionRuntime`` / 真实 ``JevDecisionEngine`` /
真实 ``ConfigManager`` / 真实 ``PromptTemplateLoader`` / 真实
``DecisionLogRepository`` / 真实 group_admission 运行时裁决 / 官方 SDK
``httpx2.AsyncBaseTransport``。被替换的只有外部 I/O 边界：

- 共享 ORM 会话工厂（指向本次派生的一次性隔离库，绝不写门控库）；
- 自有 Redis 客户端工厂（隔离 Redis 的 DB15）；
- 官方 SDK transport（``ScriptedTransport``，零网络）；
- 配置存储工厂（故障注入用，仍是真实 ``ConfigStorage`` 语义）。

期望值来自 182 运行时装配契约（手写字面量），不导入生产常量。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import redis.asyncio as redis_asyncio

from komari_bot.decision.jev import (
    DecisionLogMetadata,
    DecisionResult,
    EventIdentity,
)
from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.services import (
    jev_runtime as jev_runtime_module,
)
from komari_bot.plugins.komari_decision.services.jev_runtime import DecisionRuntime
from tests.komari_decision.jev_test_support import (
    CONFIGURED_API_KEY,
    TEST_REDIS_URL,
    ScriptedTransport,
    clean_test_scopes,
    create_scratch_database,
    delete_prompt,
    drop_scratch_database,
    make_identity,
    make_request,
    read_dispatch,
    reset_decision_storage,
    skip_without_services,
    write_config_row,
    write_prompt,
)
from tests.pg_support import reset_shared_orm_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


# ------------------------------ 一次性隔离库 ------------------------------


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    """派生一个迁移到 head 的一次性隔离库（模块级共享，用例内清表）。"""
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevr{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


def _shared_session_factory(
    engine: AsyncEngine,
) -> Callable[[], AsyncSession]:
    """把共享 ORM 会话工厂指向隔离库（外部持久化边界替换）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    return lambda: factory()


async def _start_admission(monkeypatch: pytest.MonkeyPatch) -> Any:
    """用真实 ConfigManager 拉起**真实**群准入运行时（默认空黑名单=全部获准）。"""
    from komari_bot.plugins.group_admission import runtime as admission_runtime_module
    from komari_bot.plugins.group_admission.config_schema import (
        GroupAdmissionConfigSchema,
    )

    manager = ConfigManager("group_admission", GroupAdmissionConfigSchema)
    runtime = admission_runtime_module._AdmissionRuntime()
    await runtime.start(manager)
    monkeypatch.setattr(admission_runtime_module, "_runtime", runtime)
    return runtime


class _CountingConfigManager(ConfigManager):
    """真实 ConfigManager 的调用计数外壳（只做计数，不改变语义）。"""

    get_async_calls: int

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.get_async_calls = 0

    async def get_async(self) -> Any:
        self.get_async_calls += 1
        return await super().get_async()


class _RedisCounter:
    """自有 Redis 客户端工厂的**可观测**调用计数。"""

    def __init__(self) -> None:
        self.calls = 0


class _ObservedRedis:
    """真实 ``redis.asyncio.Redis`` 的 I/O 边界观测外壳。

    只记录 ``ping`` / ``aclose`` 并支持故障注入；不改变客户端行为，也不替换
    判定业务语义。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.ping_calls = 0
        self.close_calls = 0
        self.ping_gate: asyncio.Event | None = None
        self.ping_error: Exception | None = None

    async def ping(self) -> Any:
        self.ping_calls += 1
        if self.ping_gate is not None:
            await self.ping_gate.wait()
        if self.ping_error is not None:
            raise self.ping_error
        return await self._client.ping()

    async def aclose(self) -> None:
        self.close_calls += 1
        await self._client.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@dataclass
class _RuntimeEnv:
    """一个用例的真实运行时与其外部边界观测面。"""

    runtime: DecisionRuntime
    manager: _CountingConfigManager
    transport: ScriptedTransport
    redis: Redis
    redis_observed: _ObservedRedis
    redis_counter: _RedisCounter
    engine: AsyncEngine
    session_factory: Callable[[], AsyncSession]
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    _original_storage_factory: Any = None
    _original_get_async: Any = None

    def stall_config_read(self) -> asyncio.Event:
        """让配置修订读取真实阻塞（外部配置边界，可安全取消）。"""
        gate = asyncio.Event()
        self._original_get_async = self.manager.get_async

        async def _stalled() -> Any:
            await gate.wait()
            return await self._original_get_async()

        self.manager.get_async = _stalled  # type: ignore[method-assign]
        return gate

    def resume_config_read(self) -> None:
        """恢复真实配置读取（只恢复这一处边界）。"""
        original = self._original_get_async
        if original is not None:
            self.manager.get_async = original  # type: ignore[method-assign]
            self._original_get_async = None

    def break_config_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """让下一次配置存储读取失败（真实 ConfigStorage 边界故障）。"""

        def _broken() -> Any:
            msg = "synthetic config storage outage"
            raise ConnectionError(msg)

        self._original_storage_factory = manager_module.get_config_storage
        monkeypatch.setattr(manager_module, "get_config_storage", _broken)
        self.manager._last_revision_checked_at = 0.0

    def restore_config_storage(self) -> None:
        """只恢复配置存储工厂，绝不撤销本用例其它边界替换。"""
        if self._original_storage_factory is not None:
            manager_module.get_config_storage = self._original_storage_factory
            self._original_storage_factory = None
        self.manager._last_revision_checked_at = 0.0


@pytest.fixture
async def runtime_env(
    decision_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_RuntimeEnv]:
    """真实运行时 + 真实引擎 + 隔离库/Redis/官方 SDK transport。"""
    from nonebot import require as nonebot_require

    # 仓库既有模式：先 require 再 import（否则 localstore 无法识别调用插件）。
    nonebot_require("nonebot_plugin_orm")
    import nonebot_plugin_orm
    from sqlalchemy.ext.asyncio import create_async_engine

    from komari_bot.config.prompt_storage import PromptTemplateLoader
    from komari_bot.plugins.komari_decision.prompt_schema import (
        DISPLAY_NAME,
        RESOURCE_ID,
    )

    await reset_shared_orm_engine()
    engine = create_async_engine(decision_database)
    session_factory = _shared_session_factory(engine)

    def _shared_get_session(*_args: object, **_kwargs: object) -> AsyncSession:
        return session_factory()

    # 共享 ORM 会话工厂（config/Prompt 存储的外部持久化）指向隔离库。
    monkeypatch.setattr(nonebot_plugin_orm, "get_session", _shared_get_session)
    admission_runtime = await _start_admission(monkeypatch)
    await reset_decision_storage(engine)
    await write_config_row(engine, overrides={"plugin_enable": True})
    await write_prompt()

    manager = _CountingConfigManager("komari_decision", KomariDecisionConfigSchema)
    transport = ScriptedTransport()
    client = redis_asyncio.Redis.from_url(TEST_REDIS_URL)
    counter = _RedisCounter()
    observed = _ObservedRedis(client)

    def _redis_factory() -> Redis:
        counter.calls += 1
        return observed  # type: ignore[return-value]

    runtime = DecisionRuntime(
        config_manager=manager,
        session_factory=session_factory,
        redis_factory=_redis_factory,
        prompt_loader=PromptTemplateLoader(
            resource_id=RESOURCE_ID,
            display_name=DISPLAY_NAME,
            log_prefix="[test]",
        ),
        transport_factory=lambda: transport,
        bot_resolver=lambda _account_id: None,
    )
    env = _RuntimeEnv(
        runtime=runtime,
        manager=manager,
        transport=transport,
        redis=client,
        redis_observed=observed,
        redis_counter=counter,
        engine=engine,
        session_factory=session_factory,
    )
    try:
        yield env
    finally:
        with suppress(BaseException):
            await runtime.close()
        with suppress(Exception):
            await admission_runtime.close()
        # 只删除本用例自有作用域的 Redis 键（不 FLUSH、不广删）。
        with suppress(Exception):
            await clean_test_scopes(client)
        with suppress(Exception):
            await client.aclose()
        await delete_prompt()
        await engine.dispose()
        await reset_shared_orm_engine()


def _numeric_identity() -> EventIdentity:
    """每个用例自有的真实数值平台标识（避免共享作用域键）。"""
    seed = int(uuid4().int % 900_000) + 100_000
    return make_identity(
        protocol="onebot_v11",
        account_id=f"1{seed}",
        group_id=f"2{seed}",
        message_id=str(seed),
    )


# ------------------------------ 1. standby 启动 ------------------------------


async def test_startup_builds_one_engine_and_is_idempotent(
    runtime_env: _RuntimeEnv,
) -> None:
    """真实 startup 建一个自有引擎；重复 startup/refresh 不重建、零 SDK 调用。"""
    runtime = runtime_env.runtime
    assert runtime.engine is None

    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    await runtime.startup()
    await runtime.refresh()
    assert runtime.engine is engine, "重复 startup/refresh 不得重建引擎"
    assert runtime_env.redis_counter.calls == 1, "自有 Redis 只建一次"
    assert runtime_env.transport.requests == [], "standby 期间不得发起模型请求"

    health = runtime.get_health()
    assert health.runtime_status == "ready"
    assert health.reason_code == "ready"
    assert health.last_request_status == "unknown"
    assert health.last_success_at is None
    assert health.last_failure_at is None
    assert health.consecutive_failures == 0


# ------------------------------ 2. 被动健康 ------------------------------


async def test_health_before_ready_is_passive_failed_initializing(
    decision_database: str,
) -> None:
    """尚未初始化的运行时只投影 failed/initializing，且读取零外部 I/O。"""
    await reset_shared_orm_engine()
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(decision_database)
    session_factory = _shared_session_factory(engine)
    counter = {"calls": 0}

    def _redis_factory() -> Redis:
        counter["calls"] += 1
        return redis_asyncio.Redis.from_url(TEST_REDIS_URL)

    runtime = DecisionRuntime(
        config_manager=_CountingConfigManager(
            "komari_decision", KomariDecisionConfigSchema
        ),
        session_factory=session_factory,
        redis_factory=_redis_factory,
        transport_factory=lambda: ScriptedTransport(),
        bot_resolver=lambda _account_id: None,
    )
    try:
        observed = [runtime.get_health() for _ in range(5)]
        assert {item.runtime_status for item in observed} == {"failed"}
        assert {item.reason_code for item in observed} == {"initializing"}
        assert {item.last_request_status for item in observed} == {"unknown"}
        assert {item.in_flight_count for item in observed} == {None}
        assert {item.queued_count for item in observed} == {None}
        assert {item.observed_at for item in observed} == {observed[0].observed_at}
        assert counter["calls"] == 0, "健康读取不得创建 Redis 客户端"
    finally:
        await runtime.close()
        await engine.dispose()
        await reset_shared_orm_engine()


# ------------------------------ 3. 配置不可读 → 修复 ------------------------------


async def test_unreadable_config_still_builds_engine_and_recovers_without_restart(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置不可读不阻止构建引擎；修复后经真实刷新恢复，无需重启、不探测模型。"""
    runtime = runtime_env.runtime
    runtime_env.break_config_storage(monkeypatch)
    await runtime.startup()

    engine = runtime.engine
    assert engine is not None, "配置不可读不得永久阻止构建引擎"
    health = runtime.get_health()
    assert health.runtime_status == "failed"
    assert health.reason_code == "configuration_invalid"
    assert health.last_request_status == "unknown"

    runtime_env.restore_config_storage()
    await runtime.refresh()
    health = runtime.get_health()
    assert health.runtime_status == "ready"
    assert health.reason_code == "ready"
    assert runtime.engine is engine
    assert runtime_env.transport.requests == []


# ------------------------------ 4. Prompt 缺失 → 修复 ------------------------------


async def test_missing_prompt_then_repaired_becomes_ready_without_model_probe(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await delete_prompt()
    await runtime.startup()
    health = runtime.get_health()
    assert health.runtime_status == "failed"
    assert health.reason_code == "prompt_unavailable"

    await write_prompt()
    await runtime.refresh()
    health = runtime.get_health()
    assert health.runtime_status == "ready"
    assert runtime_env.transport.requests == []


# ------------------------------ 5. 凭据非法 → 修正 ------------------------------


async def test_invalid_credentials_then_corrected_recovers(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    assert runtime.get_health().runtime_status == "ready"

    await runtime_env.manager.update_field_async("jev_api_key", "")
    await runtime.refresh()
    health = runtime.get_health()
    assert health.runtime_status == "failed"
    assert health.reason_code == "configuration_invalid"

    await runtime_env.manager.update_field_async("jev_api_key", CONFIGURED_API_KEY)
    await runtime.refresh()
    assert runtime.get_health().runtime_status == "ready"
    assert runtime_env.transport.requests == []


# ------------------------------ 6. 显式关闭保持关闭 ------------------------------


async def test_explicit_disable_stays_disabled_across_edits_and_retries(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    await runtime_env.manager.update_field_async(
        "plugin_enable",
        False,  # noqa: FBT003 - 字段值本身是布尔
    )
    await runtime.refresh()
    health = runtime.get_health()
    assert health.runtime_status == "disabled"
    assert health.reason_code == "disabled"

    # 普通配置编辑 + 重复刷新/启动都不得把显式关闭变成 ready。
    await runtime_env.manager.update_field_async("bot_aliases", ["小鞠"])
    await runtime.refresh()
    await runtime.startup()
    health = runtime.get_health()
    assert health.runtime_status == "disabled"
    assert health.reason_code == "disabled"
    assert runtime_env.transport.requests == []


# ------------------------------ 7. 普通编辑不替换引擎/不取消在途任务 ------------------------------


async def test_normal_config_edit_keeps_engine_and_inflight_task(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None

    entered = asyncio.Event()

    async def _hold(request: Any, index: int) -> Any:
        del index
        entered.set()
        await runtime_env.gate.wait()
        return _success_payload(request)

    runtime_env.transport._responder = _hold
    identity = _numeric_identity()
    request = make_request(identity=identity, text="今晚谁值班？")
    evaluate = asyncio.create_task(engine.evaluate(request))
    await asyncio.wait_for(entered.wait(), timeout=5)

    await runtime_env.manager.update_field_async("optional_reply_probability", 0.9)
    await runtime.refresh()
    assert runtime.engine is engine, "普通配置编辑不得替换引擎"

    runtime_env.gate.set()
    result = await asyncio.wait_for(evaluate, timeout=10)
    assert result.status == "success"
    assert result.config_revision is not None
    assert runtime.get_health().last_request_status == "success"


# ------------------------------ 8. 健康读取零 I/O 且保留真实统计 ------------------------------


async def test_health_reads_stay_passive_and_preserve_request_stats(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    identity = _numeric_identity()
    result = await engine.evaluate(make_request(identity=identity, text="今晚谁值班？"))
    assert result.status == "success"

    before = runtime.get_health()
    assert before.last_request_status == "success"
    assert before.last_success_at is not None

    # 外部配置读取故障：生命周期失败投影不得抹掉引擎真实请求观测。
    runtime_env.break_config_storage(monkeypatch)
    await runtime.refresh()
    after = runtime.get_health()
    assert after.runtime_status == "failed"
    assert after.reason_code == "configuration_invalid"
    assert after.last_request_status == "success"
    assert after.last_success_at == before.last_success_at
    assert after.consecutive_failures == before.consecutive_failures

    # 健康读取本身不访问配置/Redis/数据库/模型。
    for _ in range(3):
        repeated = runtime.get_health()
        assert repeated == after
    assert runtime_env.transport.requests, "上一步只有那一次真实模型调用"

    runtime_env.restore_config_storage()
    await runtime.refresh()
    recovered = runtime.get_health()
    assert recovered.runtime_status == "ready"
    assert recovered.last_request_status == "success"
    assert recovered.last_success_at == before.last_success_at


# ------------------------------ 9. 日志清理 ------------------------------


def _log_row(
    *, decision_id: str, identity: EventIdentity, created_at: datetime
) -> DecisionLogMetadata:
    return DecisionLogMetadata(
        decision_id=decision_id,
        event=identity,
        created_at=created_at,
        finished_at=created_at,
        result=DecisionResult(
            decision_id=decision_id,
            status="success",
            reason_code="classified",
        ),
    )


async def _log_ids(engine: AsyncEngine) -> set[str]:
    from sqlalchemy import text

    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT id FROM komari_decision_logs"))
    return {str(row[0]) for row in rows}


async def test_prune_while_disabled_removes_only_expired_logs(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    identity = _numeric_identity()
    result = await engine.evaluate(make_request(identity=identity, text="今晚谁值班？"))
    assert result.status == "success"

    expired_id = str(uuid4())
    fresh_id = str(uuid4())
    old = datetime.now(UTC) - timedelta(days=10)
    repository = runtime.log_repository
    await repository.record(
        _log_row(decision_id=expired_id, identity=identity, created_at=old),
        body=None,
    )
    await repository.record(
        _log_row(decision_id=fresh_id, identity=identity, created_at=datetime.now(UTC)),
        body=None,
    )
    before_identity = await read_dispatch(runtime_env.engine, identity)
    assert before_identity is not None
    identity_facts = {
        key: before_identity[key]
        for key in (
            "id",
            "protocol",
            "account_id",
            "group_id",
            "message_id",
            "dispatch_id",
        )
    }

    # 判定显式关闭时清理照常运行。
    await runtime_env.manager.update_field_async(
        "plugin_enable",
        False,  # noqa: FBT003 - 字段值本身是布尔
    )
    await runtime.refresh()
    assert runtime.get_health().runtime_status == "disabled"
    removed = await runtime.prune()
    assert removed >= 1

    remaining = await _log_ids(runtime_env.engine)
    assert expired_id not in remaining
    assert fresh_id in remaining
    assert result.decision_id in remaining
    after_identity = await read_dispatch(runtime_env.engine, identity)
    assert after_identity is not None, "清理不得删除身份/调度事实"
    assert {key: after_identity[key] for key in identity_facts} == identity_facts, (
        "清理不得改写身份/分发事实"
    )


async def test_prune_failure_is_safe_and_does_not_touch_request_observations(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    await engine.evaluate(
        make_request(identity=_numeric_identity(), text="今晚谁值班？")
    )
    before = runtime.get_health()

    class _BrokenSessionFactory:
        def __call__(self) -> Any:
            msg = "synthetic session outage"
            raise ConnectionError(msg)

    broken = DecisionRuntime(
        config_manager=runtime_env.manager,
        session_factory=_BrokenSessionFactory(),  # type: ignore[arg-type]
        redis_factory=lambda: runtime_env.redis,
        transport_factory=lambda: runtime_env.transport,
        bot_resolver=lambda _account_id: None,
    )
    try:
        assert await broken.prune() == 0
    finally:
        await broken.close()

    after = runtime.get_health()
    assert after.last_request_status == before.last_request_status
    assert after.last_failure_at == before.last_failure_at
    assert after.consecutive_failures == before.consecutive_failures


# ------------------------------ 10. 关闭等待在途工作 ------------------------------


async def test_shutdown_waits_inflight_evaluate_and_survives_repeated_cancel(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    entered = asyncio.Event()

    async def _hold(request: Any, index: int) -> Any:
        del index
        entered.set()
        await runtime_env.gate.wait()
        return _success_payload(request)

    runtime_env.transport._responder = _hold
    evaluate = asyncio.create_task(
        engine.evaluate(make_request(identity=_numeric_identity(), text="今晚谁值班？"))
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    closer = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    closer.cancel()
    await asyncio.sleep(0)
    closer.cancel()
    runtime_env.gate.set()
    outcome = (await asyncio.gather(closer, return_exceptions=True))[0]
    # 调用方取消不是任意异常：它必须是取消本身，且在真正清理完成后才传播。
    assert isinstance(outcome, asyncio.CancelledError), outcome
    assert closer.done()

    assert runtime.engine is None, "关闭必须真正结束自有引擎"
    await asyncio.gather(evaluate, return_exceptions=True)
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    leaked = [
        task
        for task in pending
        if task.get_name().startswith(("jev-runtime-", "jev-decision-"))
    ]
    assert leaked == [], f"关闭后不得遗留自有任务: {leaked}"

    # 共享 ORM/会话资源不被处置：隔离库连接仍可用。
    from sqlalchemy import text

    async with runtime_env.engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1


# ------------------------------ 11. 刷新请求合并 ------------------------------


async def test_concurrent_refresh_requests_coalesce_into_one_attempt(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    manager = runtime_env.manager
    baseline = manager.get_async_calls

    await asyncio.gather(*(runtime.refresh() for _ in range(5)))
    assert manager.get_async_calls - baseline == 1, "并发刷新必须合并到一次真实前置刷新"


# ------------------------------ 12. 关闭后的健康与拒绝新工作 ------------------------------


async def test_close_retains_final_snapshot_and_refuses_new_work(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    result = await engine.evaluate(
        make_request(identity=_numeric_identity(), text="今晚谁值班？")
    )
    assert result.status == "success"

    await runtime.close()
    final = runtime.get_health()
    assert final.runtime_status == "failed"
    assert final.reason_code == "interrupted"
    assert final.last_request_status == "success", "关闭后不得伪造新的未知观测"

    # 关闭后不再创建任务/引擎/客户端。
    await runtime.refresh()
    assert await runtime.prune() == 0
    await runtime.startup()
    assert runtime.engine is None
    assert runtime_env.redis_counter.calls == 1
    assert runtime.get_health() == final


# ------------------------------ 13. 关闭失败不得被当成成功 ------------------------------


class _FailingCloseRuntime(DecisionRuntime):
    """关闭实现确定失败的运行时（只替换清理边界，不触碰引擎业务方法）。"""

    def __init__(self) -> None:
        super().__init__()
        self.close_attempts = 0

    async def _close_owned(self) -> None:
        self.close_attempts += 1
        self._closed = True
        msg = "synthetic cleanup failure"
        raise RuntimeError(msg)


async def test_failed_close_is_not_reported_as_success() -> None:
    runtime = _FailingCloseRuntime()
    with pytest.raises(RuntimeError):
        await runtime.close()
    # 重复关闭仍等待同一个已跟踪关闭任务，并如实再次暴露失败。
    with pytest.raises(RuntimeError):
        await runtime.close()
    assert runtime.close_attempts == 1, "重复关闭不得启动第二个关闭任务"


# ------------------------------ 14. 引擎前 ping 取消释放客户端 ------------------------------


async def test_pre_engine_ping_timeout_releases_owned_client(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _ObservedRedis(redis_asyncio.Redis.from_url(TEST_REDIS_URL))
    observed.ping_gate = asyncio.Event()
    runtime = DecisionRuntime(
        config_manager=runtime_env.manager,
        session_factory=runtime_env.session_factory,
        redis_factory=lambda: observed,  # type: ignore[arg-type,return-value]
        transport_factory=lambda: runtime_env.transport,
        bot_resolver=lambda _account_id: None,
    )
    monkeypatch.setattr(jev_runtime_module, "LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    try:
        await runtime.startup()
        health = runtime.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "storage_unavailable", "引擎外超时是前置条件失败"
        assert runtime.engine is None
        assert observed.close_calls == 1, "ping 期间的取消不得泄漏自有客户端"
    finally:
        with suppress(BaseException):
            await runtime.close()
        await observed.aclose()


# ------------------------------ 15. 配置读取阻塞时引擎仍可用 ------------------------------


async def test_stalled_config_read_keeps_engine_and_non_model_bypass(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_env.runtime
    gate = runtime_env.stall_config_read()
    monkeypatch.setattr(jev_runtime_module, "LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS", 0.1)
    try:
        await runtime.startup()
        engine = runtime.engine
        assert engine is not None, "配置源卡住不得阻止构建引擎（PG/Redis 健康）"
        assert runtime.get_health().reason_code == "storage_unavailable"

        # 确定性平台召唤（非总结候选）即使 Jev 配置不可读也必须照样直通。
        bypass = await engine.evaluate(
            make_request(identity=_numeric_identity(), real_platform_at=True)
        )
        assert bypass.status == "bypass"
        assert bypass.route == "chat"
        assert runtime_env.transport.requests == [], "旁路不得发起模型请求"
    finally:
        gate.set()
        runtime_env.resume_config_read()
        monkeypatch.setattr(
            jev_runtime_module, "LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS", 5.0
        )
    await runtime.refresh()
    assert runtime.get_health().runtime_status == "ready", "修复后无需重启即可恢复"


# ------------------------------ 16. 本地 Redis 故障不得留下陈旧 ready ------------------------------


async def test_local_redis_outage_does_not_leave_stale_ready(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    result = await engine.evaluate(
        make_request(identity=_numeric_identity(), text="今晚谁值班？")
    )
    assert result.status == "success"
    before = runtime.get_health()

    runtime_env.redis_observed.ping_error = ConnectionError("synthetic redis outage")
    await runtime.refresh()
    degraded = runtime.get_health()
    assert degraded.runtime_status == "failed"
    assert degraded.reason_code == "storage_unavailable"
    assert degraded.last_request_status == "success"
    assert degraded.last_success_at == before.last_success_at

    runtime_env.redis_observed.ping_error = None
    await runtime.refresh()
    assert runtime.get_health().runtime_status == "ready"
    assert runtime_env.redis_counter.calls == 1, "恢复复用同一个自有客户端"


# ------------------------------ 17. 生命周期失败只在真正成功时清除 ------------------------------


async def test_lifecycle_failure_survives_ordinary_refresh(
    runtime_env: _RuntimeEnv,
) -> None:
    runtime = runtime_env.runtime
    await runtime.startup()
    runtime.report_lifecycle_failure("internal_error")
    assert runtime.get_health().reason_code == "internal_error"

    await runtime.refresh()
    health = runtime.get_health()
    assert health.runtime_status == "failed", "普通刷新不得清除生命周期失败"
    assert health.reason_code == "internal_error"

    runtime.clear_lifecycle_failure()
    assert runtime.get_health().runtime_status == "ready"


def _success_payload(request: Any) -> dict[str, Any]:
    """按请求里实际的问题类型回放手写答案（不导入生产常量）。"""
    import json

    body = json.loads(request.content)
    answers: dict[str, Any] = {}
    for name, question in body["questions"].items():
        if question["type"] == "choice":
            label = "required" if name == "reply_eligibility" else "candidate"
            answers[name] = {
                "type": "choice",
                "choice": label,
                "confidence": 0.9,
                "probabilities": {
                    **dict.fromkeys(question["criteria"], 0.0),
                    label: 1.0,
                },
            }
        else:
            answers[name] = {"type": "noul", "noul": 0.05}
    return {
        "model": "jev-1.14.0",
        "answers": answers,
        "usage": {"input_tokens": 64, "output_tokens": 8},
    }


class _FailingRedisRelease:
    """外部 Redis 清理失败；不替换运行时或引擎的实现。"""

    def __init__(self) -> None:
        self.close_calls = 0

    async def ping(self) -> None:
        message = "synthetic ping failure"
        raise ConnectionError(message)

    async def aclose(self) -> None:
        self.close_calls += 1
        message = "synthetic redis close failure"
        raise RuntimeError(message)


async def test_untransferred_client_release_failure_remains_observable() -> None:
    client = _FailingRedisRelease()
    runtime = DecisionRuntime(
        config_manager=ConfigManager("komari_decision", KomariDecisionConfigSchema),
        redis_factory=lambda: client,  # type: ignore[arg-type,return-value]
    )
    await runtime.startup()
    assert runtime.engine is None
    assert client.close_calls == 1
    assert runtime.get_health().reason_code == "storage_unavailable"
    with pytest.raises(RuntimeError, match="synthetic redis close failure"):
        await runtime.close()
    assert client.close_calls == 2, "失败资源必须保留到关闭阶段再次清理"
    assert runtime.get_health().reason_code == "interrupted"
    with pytest.raises(RuntimeError, match="synthetic redis close failure"):
        await runtime.close()
    assert client.close_calls == 2, "重复关闭必须读取同一关闭任务的失败"


async def test_refresh_cannot_reuse_client_whose_release_failed() -> None:
    client = _FailingRedisRelease()
    runtime = DecisionRuntime(
        config_manager=ConfigManager("komari_decision", KomariDecisionConfigSchema),
        redis_factory=lambda: client,  # type: ignore[arg-type,return-value]
    )
    try:
        await runtime.startup()
        await runtime.refresh()
        assert runtime.engine is None
        assert client.close_calls == 2, "刷新必须先处理尚未释放的客户端"
    finally:
        with pytest.raises(RuntimeError, match="synthetic redis close failure"):
            await runtime.close()


async def test_real_scheduler_runs_passive_jobs_and_shutdown_removes_them(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_SCHEDULER_SHUTDOWN
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from komari_bot.plugins.komari_decision.services import jev_lifecycle

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    completed: set[str] = set()
    executed = asyncio.Event()
    stopped = asyncio.Event()

    def observe(event: Any) -> None:
        completed.add(event.job_id)
        if completed == {"komari_decision_refresh", "komari_decision_prune"}:
            executed.set()

    scheduler.add_listener(observe, EVENT_JOB_EXECUTED)
    scheduler.add_listener(lambda _event: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    monkeypatch.setattr(jev_lifecycle, "scheduler", scheduler)
    monkeypatch.setattr(
        jev_lifecycle, "get_decision_runtime", lambda: runtime_env.runtime
    )
    scheduler.start(paused=True)
    try:
        await jev_lifecycle._startup()
        engine = runtime_env.runtime.engine
        assert engine is not None
        await jev_lifecycle._startup()
        jobs = scheduler.get_jobs()
        assert {job.id for job in jobs} == {
            "komari_decision_refresh",
            "komari_decision_prune",
        }
        assert len(jobs) == 2
        assert runtime_env.runtime.engine is engine
        assert runtime_env.redis_counter.calls == 1
        for job in jobs:
            job.modify(next_run_time=datetime.now(UTC))
        scheduler.resume()
        await asyncio.wait_for(executed.wait(), timeout=3)
        assert runtime_env.transport.requests == []
        await jev_lifecycle._shutdown()
        assert scheduler.get_jobs() == []
        assert runtime_env.runtime.engine is None
        assert runtime_env.redis_observed.close_calls == 1
    finally:
        await jev_lifecycle._shutdown()
        scheduler.shutdown(wait=True)
        await asyncio.wait_for(stopped.wait(), timeout=3)


async def test_shutdown_waits_for_real_prune_session_cleanup(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from komari_bot.plugins.komari_decision.services import jev_lifecycle
    from tests.pg_support import backend_pid, wait_for_blocked

    await runtime_env.runtime.startup()
    closing = asyncio.Event()
    release = asyncio.Event()

    class DelayedCloseSession(AsyncSession):
        async def close(self) -> None:
            closing.set()
            await release.wait()
            await super().close()

    runtime = DecisionRuntime(
        config_manager=runtime_env.manager,
        session_factory=lambda: DelayedCloseSession(runtime_env.engine),
    )
    monkeypatch.setattr(jev_lifecycle, "get_decision_runtime", lambda: runtime)
    blocker = runtime_env.session_factory()
    pruning: asyncio.Task[int] | None = None
    shutdown: asyncio.Task[None] | None = None
    try:
        await blocker.execute(
            text("LOCK TABLE komari_decision_logs IN ACCESS EXCLUSIVE MODE")
        )
        pid = await backend_pid(blocker)
        pruning = asyncio.create_task(runtime.prune())
        await wait_for_blocked(
            async_sessionmaker(runtime_env.engine, expire_on_commit=False), pid
        )
        shutdown = asyncio.create_task(jev_lifecycle._shutdown())
        await asyncio.wait_for(closing.wait(), timeout=3)
        assert not shutdown.done(), "真实清理会话尚未归还时，关闭不得完成"
        assert await runtime.prune() == 0, "关闭期间不能再创建清理工作"
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done(), "重复取消不能提前结束资源清理"
        release.set()
        outcome = (await asyncio.gather(shutdown, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        result = (await asyncio.gather(pruning, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert runtime.get_health().reason_code == "interrupted"
        assert runtime_env.transport.requests == []
    finally:
        release.set()
        await blocker.rollback()
        await blocker.close()
        await runtime.close()
        if pruning is not None:
            await asyncio.gather(pruning, return_exceptions=True)
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)


async def test_ping_cancellation_survives_external_release_failure() -> None:
    entered = asyncio.Event()

    class CancelledPingRedis(_FailingRedisRelease):
        async def ping(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    client = CancelledPingRedis()
    runtime = DecisionRuntime(
        config_manager=ConfigManager("komari_decision", KomariDecisionConfigSchema),
        redis_factory=lambda: client,  # type: ignore[arg-type,return-value]
    )
    startup = asyncio.create_task(runtime.startup())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(RuntimeError, match="synthetic redis close failure"):
            await runtime.close()
        result = (await asyncio.gather(startup, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError), (
            "清理故障不能把已取消的初始化改成成功"
        )
    finally:
        with suppress(RuntimeError):
            await runtime.close()
        await asyncio.gather(startup, return_exceptions=True)


async def test_transferred_redis_close_failure_remains_observable(
    runtime_env: _RuntimeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the failed owner and expose the same failure to every close caller."""
    runtime = runtime_env.runtime
    await runtime.startup()
    engine = runtime.engine
    assert engine is not None
    attempts = 0

    async def fail_release() -> None:
        nonlocal attempts
        attempts += 1
        message = "synthetic transferred Redis close failure"
        raise ConnectionError(message)

    monkeypatch.setattr(runtime_env.redis_observed, "aclose", fail_release)
    with pytest.raises(ConnectionError, match="synthetic transferred"):
        await runtime.close()
    assert runtime.engine is None
    assert runtime._engine is engine  # pyright: ignore[reportPrivateUsage]
    assert runtime.get_health().runtime_status == "failed"
    assert runtime.get_health().reason_code == "interrupted"
    outcomes = await asyncio.gather(
        runtime.close(), runtime.close(), engine.close(), return_exceptions=True
    )
    assert all(isinstance(outcome, ConnectionError) for outcome in outcomes)
    assert attempts == 1, "Repeated callers observe one failed shutdown attempt."
    assert runtime.engine is None
    assert runtime._engine is engine  # pyright: ignore[reportPrivateUsage]
    await runtime.startup()
    await runtime.refresh()
    assert attempts == 1
    assert runtime_env.transport.requests == []
    # Fixture teardown releases the actual external client, not a retry in production.
