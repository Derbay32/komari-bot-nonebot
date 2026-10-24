"""常驻判定运行时（TSK-182 standby）：自有资源、被动健康、刷新与日志清理。

本模块是判定插件唯一的**进程内运行时单例**：它自己持有可重试的判定前置条件
（顶层 config_manager 的版本化配置管理器、共享 ORM 会话工厂、自有 Redis 客户端、
一个 Prompt 加载器、一个 ``DecisionLogRepository``），并按需构建**真实**的
``JevDecisionEngine``。它不接收真实流量：聊天/总结调用方直到 TSK-183 仍走既有
``get_decision_engine``；本模块也不导出第二个公共引擎 getter。

边界（与 182 运行时装配契约一致）：

- 配置只经**顶层** ``config_manager.get_config_manager`` 注册表取得（与
  ``config_interface`` 同一实例），刷新经既有 ``get_async`` 修订机制；
  引擎的 ``config_source`` 只原子读取 ``get_cached_versioned_snapshot``，那里
  没有任何 I/O、回退或默认快照。
- Prompt 在运行时生命周期内只用一个 ``PromptTemplateLoader``（复用既有 1 秒
  缓存 / 本地失效 / PG 修订传播）；重试不重建加载器，也不新建第二套轮询。
- 会话工厂惰性调用 ``nonebot_plugin_orm.get_session(expire_on_commit=False)``：
  不新建连接池、不在关闭时处置共享资源。
- Redis 是**自有**客户端（``get_shared_redis_config`` 的 host/port/password，
  生产 DB0，连接/读取超时 3 秒）；构造引擎时所有权转移给引擎（``close`` 由
  引擎负责），构造失败则就地释放，绝不把失败/被替换的客户端留在身后。
- ``DecisionLogRepository`` 在构造时就建立，且**不依赖**引擎可用性：判定关闭、
  初始化失败或存储故障时查询路由仍能按既有 503 语义回答。保留期 provider 每次
  操作实时读取同一份配置快照，没有秘密、没有默认回退。
- 健康读取只返回被动快照：不做配置/Redis/DB/模型 I/O；未知请求观测保持
  ``unknown``/``null``；引擎外部的前置失败只**叠加**在引擎真实计数之上，只有
  真正成功的前置刷新才清除它。
- 每次自有初始化/刷新/清理尝试受 ``asyncio.timeout(5)`` 约束；这是内部生命
  周期上限，不是另一个 SDK 预算或配置字段。意外失败/超时只记录固定安全文本与
  异常**类型**，绝不向角色服务启动冒泡；``CancelledError`` 等 ``BaseException``
  不在这里被吞掉。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from nonebot import logger

from komari_bot.decision.jev import (
    DecisionHealthSnapshot,
    DecisionReason,
    EventIdentity,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    DISPLAY_NAME as DECISION_PROMPT_DISPLAY_NAME,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    RESOURCE_ID as DECISION_PROMPT_RESOURCE_ID,
)
from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
    DecisionLogRepository,
)
from komari_bot.plugins.komari_decision.services.jev_engine import JevDecisionEngine
from komari_bot.plugins.komari_decision.services.jev_onebot import (
    PROTOCOL as ONEBOT_PROTOCOL,
)
from komari_bot.plugins.komari_decision.services.jev_onebot import (
    OneBotReferenceReader,
    canonical_platform_id,
    group_business_admitted,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx2
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

    from komari_bot.config.prompt_storage import PromptTemplateLoader
    from komari_bot.plugins.config_manager import ConfigManager, ConfigSnapshot

REFRESH_JOB_ID: Final = "komari_decision_refresh"
"""每 1 秒的本地前置条件刷新 job 固定 id。"""

PRUNE_JOB_ID: Final = "komari_decision_prune"
"""每 300 秒的有界日志清理 job 固定 id。"""

REFRESH_INTERVAL_SECONDS: Final = 1
PRUNE_INTERVAL_SECONDS: Final = 300
PRUNE_BATCH_LIMIT: Final = 1000
LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS: Final = 5.0
"""单次自有初始化/刷新/清理尝试的内部上限（不是 SDK 预算/配置字段）。"""

_REDIS_TIMEOUT_SECONDS: Final = 3.0
_PROTOCOL: Final = ONEBOT_PROTOCOL


class _PrerequisiteError(RuntimeError):
    """某个外部前置条件不可用（只承载安全原因码，不承载原始异常）。"""

    def __init__(self, reason: DecisionReason) -> None:
        super().__init__(reason)
        self.reason: DecisionReason = reason


class DecisionRuntime:
    """判定常驻运行时：自有资源、可重试前置条件与被动健康投影。"""

    def __init__(
        self,
        *,
        config_manager: ConfigManager | None = None,
        session_factory: Callable[[], AsyncSession] | None = None,
        redis_factory: Callable[[], Redis] | None = None,
        prompt_loader: PromptTemplateLoader | None = None,
        transport_factory: Callable[[], httpx2.AsyncBaseTransport] | None = None,
        bot_resolver: Callable[[str], object | None] | None = None,
        group_admitted: Callable[[str], bool] | None = None,
    ) -> None:
        """构造运行时。

        关键字参数只用于替换**外部 I/O 边界**（配置管理器、共享会话工厂、自有
        Redis 客户端工厂、Prompt 加载器、SDK transport、已连接 Bot 解析与群准入
        裁决）供测试注入；业务引擎、遍历、健康/清理语义全部固定在本模块。
        """
        self._config_manager = config_manager
        self._session_factory = session_factory
        self._redis_factory = redis_factory
        self._prompt_loader = prompt_loader
        self._transport_factory = transport_factory
        self._bot_resolver = bot_resolver
        self._group_admitted = group_admitted or group_business_admitted

        # 日志仓储与引擎可用性无关：构造即建立，查询路由随时可用。
        self._log_repository = DecisionLogRepository(
            self._open_session, self._retention_days
        )
        self._reference_reader = OneBotReferenceReader(
            bot_resolver=self._bot_resolver,
            group_admitted=self._admit_group,
        )
        self._engine: JevDecisionEngine | None = None
        self._redis: Redis | None = None
        """尚未转移给引擎的自有 Redis 客户端（转移后为 ``None``）。"""
        self._engine_redis: Redis | None = None
        """转移给引擎的**借用**引用：只用于本地连通性复核，关闭由引擎负责。"""
        self._refresh_task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[int] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._owned: set[asyncio.Task[Any]] = set()
        self._startup_attempted = False
        self._closed = False
        self._failure_reason: DecisionReason | None = None
        self._lifecycle_failure: DecisionReason | None = None
        self._final_snapshot: DecisionHealthSnapshot | None = None
        self._explicit_disabled_known = False
        self._observed_at = datetime.now(UTC)

    # ------------------------------ 对外读取面 ------------------------------

    @property
    def log_repository(self) -> DecisionLogRepository:
        """稳定的判定日志仓储（与引擎共享同一个实例）。"""
        return self._log_repository

    @property
    def engine(self) -> JevDecisionEngine | None:
        """内部持有的真实引擎（供 TSK-183 交接使用，不是公共 getter）。"""
        return None if self._closed else self._engine

    def get_health(self) -> DecisionHealthSnapshot:
        """返回被动健康快照：零配置查找、零 Redis/DB/模型 I/O。"""
        final = self._final_snapshot
        if final is not None:
            # 已关闭：保留关闭前最后一次真实快照的请求统计，投影 failed/interrupted。
            return final
        engine = self._engine
        if engine is None:
            status, reason = self._pre_engine_state()
            return DecisionHealthSnapshot(
                runtime_status=status,
                reason_code=reason,
                observed_at=self._observed_at,
            )
        snapshot = engine.get_health()
        failure = self._lifecycle_failure or self._failure_reason
        if failure is None or snapshot.runtime_status == "disabled":
            # 显式关闭永远保持 disabled；引擎自己的请求计数/时间戳原样保留。
            return snapshot
        return replace(snapshot, runtime_status="failed", reason_code=failure)

    def report_lifecycle_failure(self, reason: DecisionReason) -> None:
        """投影一次插件级生命周期失败（普通刷新成功**不**清除它）。"""
        self._lifecycle_failure = reason
        self._observed_at = datetime.now(UTC)

    def clear_lifecycle_failure(self) -> None:
        """只在对应生命周期步骤真正成功之后清除该投影。"""
        if self._lifecycle_failure is None:
            return
        self._lifecycle_failure = None
        self._observed_at = datetime.now(UTC)

    # ------------------------------ 生命周期入口 ------------------------------

    async def startup(self) -> None:
        """一次初始化尝试（幂等）；失败只投影 failed，绝不向 driver 冒泡。"""
        if self._closed or self._startup_attempted:
            return
        self._startup_attempted = True
        await self._run_refresh()

    async def refresh(self) -> None:
        """本地配置/Prompt/存储前置条件刷新（并发请求合并到同一任务）。"""
        if self._closed:
            return
        await self._run_refresh()

    async def prune(self) -> int:
        """一次有界日志清理；与判定开关无关，失败安全返回 0。"""
        if self._closed:
            return 0
        return await self._run_prune()

    async def close(self) -> None:
        """关闭自有资源；重复取消仍只等待同一个已跟踪关闭任务。"""
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._close_owned(), name="jev-runtime-close")
            self._close_task = task
        await _await_owned(task)

    # ------------------------------ 自有任务归属 ------------------------------

    async def _run_refresh(self) -> None:
        if self._closed:
            return
        task = self._refresh_task
        if task is None or task.done():
            task = self._track(
                asyncio.create_task(self._guarded_refresh(), name="jev-runtime-refresh")
            )
            self._refresh_task = task
        await _await_owned(task)

    async def _run_prune(self) -> int:
        if self._closed:
            return 0
        task = self._prune_task
        if task is None or task.done():
            task = self._track(
                asyncio.create_task(self._guarded_prune(), name="jev-runtime-prune")
            )
            self._prune_task = task
        await _await_owned(task)
        return int(task.result())

    def _track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._owned.add(task)
        task.add_done_callback(self._owned.discard)
        return task

    async def _guarded_refresh(self) -> None:
        """一次受上限约束的刷新；失败只留安全诊断与投影失败原因。"""
        try:
            async with asyncio.timeout(LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS):
                await self._refresh_once()
        except TimeoutError:
            # 引擎外的超时是前置条件失败，不是「配置非法」的证据。
            self._record_failure("storage_unavailable")
            logger.warning(
                "[KomariDecision] Jev 判定前置条件刷新超时: error={}",
                "TimeoutError",
            )
        except _PrerequisiteError as error:
            self._record_failure(error.reason)
            logger.warning(
                "[KomariDecision] Jev 判定前置条件不可用: error={}",
                type(error).__name__,
            )
        except Exception as exc:
            self._record_failure("storage_unavailable")
            logger.warning(
                "[KomariDecision] Jev 判定前置条件刷新失败: error={}",
                type(exc).__name__,
            )

    async def _guarded_prune(self) -> int:
        """一次受上限约束的有界清理；失败不触碰模型观测，返回 0。"""
        try:
            async with asyncio.timeout(LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS):
                return await self._prune_once()
        except TimeoutError:
            logger.warning(
                "[KomariDecision] Jev 判定日志清理超时: error={}", "TimeoutError"
            )
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev 判定日志清理失败: error={}", type(exc).__name__
            )
        return 0

    # ------------------------------ 刷新主体 ------------------------------

    async def _refresh_once(self) -> None:
        """先建/初始化引擎，再读配置并刷新引擎（配置源允许安全抛错）。

        配置读取**不得**先于引擎构建：卡住或不可用的配置源否则会反复耗尽每次
        尝试的上限，让健康 PG/Redis 下的确定性非模型旁路永久失去引擎。
        """
        if self._closed:
            return
        manager = self._require_manager()
        engine = await self._ensure_engine()
        if engine is None:
            return
        if not await self._recheck_engine_redis():
            self._record_failure("storage_unavailable")
            return
        if not await self._refresh_config(manager):
            self._record_failure("configuration_invalid")
            return
        # 后续 tick 只刷新一次（初始化只发生在新引擎上，见 _ensure_engine）。
        await engine.refresh()
        # 只有真正成功的前置刷新才清除外部失败投影。
        self._failure_reason = None
        self._observed_at = datetime.now(UTC)

    def _require_manager(self) -> ConfigManager:
        manager = self._config_manager
        if manager is not None:
            return manager
        try:
            from komari_bot.plugins.config_manager import get_config_manager
            from komari_bot.plugins.komari_decision.config_schema import (
                KomariDecisionConfigSchema as Schema,
            )

            manager = get_config_manager("komari_decision", Schema)
        except Exception as exc:
            raise _PrerequisiteError("configuration_invalid") from exc
        self._config_manager = manager
        return manager

    async def _refresh_config(self, manager: ConfigManager) -> bool:
        """经既有版本机制刷新配置；不可读只报告，不阻止引擎构建。"""
        try:
            await manager.get_async()
            snapshot = manager.get_cached_versioned_snapshot()
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev 判定配置暂不可用: error={}",
                type(exc).__name__,
            )
            return False
        self._explicit_disabled_known = snapshot.value.plugin_enable is False
        return True

    async def _ensure_engine(self) -> JevDecisionEngine | None:
        """构建一次真实引擎并 ``initialize``；已存在时只返回同一实例。"""
        if self._closed:
            return None
        engine = self._engine
        if engine is not None:
            return engine
        redis = await self._acquire_redis()
        try:
            built = JevDecisionEngine(
                config_source=self._snapshot,
                prompt_loader=self._prompt_loader_value(),
                redis=redis,
                reference_reader=self._reference_reader,
                admission=self._admission,
                session_factory=self._open_session,
                transport_factory=self._transport_factory,
                log_repository=self._log_repository,
            )
        except BaseException:
            # 构造失败（含取消）：绝不把失败客户端留在身后。
            await self._discard_redis(redis)
            raise
        # 所有权转移给引擎：引擎 close 负责关闭该客户端；运行时只保留借用引用。
        self._engine_redis = redis
        self._redis = None
        self._engine = built
        self._observed_at = datetime.now(UTC)
        await built.initialize()
        return built

    def _snapshot(self) -> ConfigSnapshot:
        """引擎配置源：只原子读取进程内版本化快照（无 I/O、无回退）。"""
        return self._require_manager().get_cached_versioned_snapshot()

    def _retention_days(self) -> int:
        """日志保留期：每次操作实时读同一份配置快照，不可用即抛（无默认值）。"""
        return int(self._snapshot().value.decision_log_retention_days)

    def _open_session(self) -> AsyncSession:
        """共享 ORM 会话工厂：惰性调用，绝不新建连接池。"""
        factory = self._session_factory
        if factory is not None:
            return factory()
        from nonebot_plugin_orm import get_session

        return get_session(expire_on_commit=False)

    def _prompt_loader_value(self) -> PromptTemplateLoader:
        """运行时生命周期内唯一的 Prompt 加载器。"""
        loader = self._prompt_loader
        if loader is not None:
            return loader
        from komari_bot.config.prompt_storage import PromptTemplateLoader

        loader = PromptTemplateLoader(
            resource_id=DECISION_PROMPT_RESOURCE_ID,
            display_name=DECISION_PROMPT_DISPLAY_NAME,
            log_prefix="[KomariDecision]",
        )
        self._prompt_loader = loader
        return loader

    # ------------------------------ 自有 Redis ------------------------------

    async def _acquire_redis(self) -> Redis:
        """取自有 Redis 客户端：新建时先 ping，任何退出都不遗留客户端。"""
        client = self._redis
        if client is not None:
            # 上次清理失败的客户端仍归本运行时，不能直接交给新引擎。
            await self._discard_redis(client)
        created = self._make_redis()
        # 所有权在任何 await 之前登记：ping 期间被取消也不会让客户端脱离清理。
        self._redis = created
        cancellation: asyncio.CancelledError | None = None
        try:
            # redis-py 的 ``ping`` 同时暴露同步/异步标注；这里只关心异常。
            await cast("Awaitable[Any]", created.ping())
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception as exc:
            await self._discard_redis(created)
            raise _PrerequisiteError("storage_unavailable") from exc
        if cancellation is not None:
            # 清理失败仍保留资源引用；它不能把原始取消改成正常返回。
            try:
                await self._discard_redis(created)
            except Exception as error:
                logger.warning(
                    "[KomariDecision] Jev 判定取消后资源清理失败: error={}",
                    type(error).__name__,
                )
            raise cancellation
        return created

    async def _recheck_engine_redis(self) -> bool:
        """本地复核引擎已持有的同一客户端（借用引用，不新建/不关闭）。"""
        client = self._engine_redis
        if client is None:
            return True
        try:
            await cast("Awaitable[Any]", client.ping())
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev 判定自有 Redis 暂不可用: error={}",
                type(exc).__name__,
            )
            return False
        return True

    def _make_redis(self) -> Redis:
        factory = self._redis_factory
        if factory is not None:
            return factory()
        import redis.asyncio as redis_asyncio

        from komari_bot.config.redis_config import get_shared_redis_config

        shared = get_shared_redis_config()
        return redis_asyncio.Redis(
            host=shared.redis_host,
            port=shared.redis_port,
            password=shared.redis_password or None,
            db=0,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
        )

    async def _discard_redis(self, client: Redis) -> None:
        """等待实际释放；失败保留所有权，成功后才清空客户端引用。"""
        release = self._track(
            asyncio.create_task(client.aclose(), name="jev-runtime-redis-release")
        )
        try:
            await _await_owned(release)
        finally:
            if (
                release.done()
                and not release.cancelled()
                and release.exception() is None
                and self._redis is client
            ):
                self._redis = None

    # ------------------------------ 清理主体 ------------------------------

    async def _prune_once(self) -> int:
        if self._closed:
            return 0
        return await self._log_repository.prune(limit=PRUNE_BATCH_LIMIT)

    # ------------------------------ 关闭主体 ------------------------------

    async def _close_owned(self) -> None:
        """停新工作 → 取消并等待自有任务 → 关引擎 → 释放未转移资源。"""
        self._closed = True
        engine = self._engine
        engine_closed = engine is None
        try:
            workers: list[asyncio.Task[Any]] = [
                task
                for task in (self._refresh_task, self._prune_task)
                if task is not None
            ]
            for task in workers:
                if not task.done():
                    task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            if engine is not None:
                await engine.close()
                engine_closed = True
        finally:
            try:
                if self._redis is not None:
                    await self._discard_redis(self._redis)
            finally:
                leftovers = [task for task in self._owned if not task.done()]
                if leftovers:
                    await asyncio.gather(*leftovers, return_exceptions=True)
                # 收尾期间仍可能产生真实请求观测；异常也不能让健康回到初始化。
                base = None if engine is None else engine.get_health()
                self._observed_at = datetime.now(UTC)
                self._final_snapshot = _stopped_snapshot(base, self._observed_at)
                if engine_closed:
                    self._engine = None
                    self._engine_redis = None

    # ------------------------------ 内部投影 ------------------------------

    def _pre_engine_state(
        self,
    ) -> tuple[Literal["ready", "disabled", "failed"], DecisionReason]:
        """引擎尚未可用时的被动三态（不读取任何外部状态）。"""
        if self._explicit_disabled_known:
            return ("disabled", "disabled")
        reason = self._lifecycle_failure or self._failure_reason
        if reason is not None:
            return ("failed", reason)
        return ("failed", "initializing")

    def _record_failure(self, reason: DecisionReason) -> None:
        self._failure_reason = reason
        self._observed_at = datetime.now(UTC)

    def _admit_group(self, group_id: str) -> bool:
        """群级业务准入（引用补取适配器使用的同一条同步裁决）。"""
        return bool(self._group_admitted(group_id))

    def _admission(self, identity: EventIdentity) -> bool:
        """引擎准入回调：协议与真实数值群号都合格才做一次同步裁决。"""
        if identity.protocol != _PROTOCOL:
            return False
        group_id = canonical_platform_id(identity.group_id)
        if group_id is None:
            return False
        return self._admit_group(str(group_id))


def _stopped_snapshot(
    base: DecisionHealthSnapshot | None, observed_at: datetime
) -> DecisionHealthSnapshot:
    """已停止运行时的最终快照：保留真实请求统计，投影 failed/interrupted。"""
    if base is None:
        return DecisionHealthSnapshot(
            runtime_status="failed",
            reason_code="interrupted",
            observed_at=observed_at,
        )
    return replace(
        base,
        runtime_status="failed",
        reason_code="interrupted",
        observed_at=observed_at,
    )


async def _await_owned(task: asyncio.Task[Any]) -> None:
    """等待一个自有任务真正结束；结果/异常绝不当作成功被吞掉。

    - 等待期间的**调用方**取消只在任务真正结束后原样传播；
    - 没有调用方取消时，任务自身的异常（含 ``BaseException``）向上传播，绝不
      用 ``suppress`` 把失败关闭当成成功关闭。
    """
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException:  # 任务异常在下面统一读取
            break
    try:
        task.result()
    except BaseException:
        if cancellation is None:
            raise
    if cancellation is not None:
        raise cancellation


_runtime = DecisionRuntime()
"""进程内判定运行时单例（standby；TSK-183 消费其内部引擎）。"""


def get_decision_runtime() -> DecisionRuntime:
    """返回进程内判定运行时单例（不是公共引擎 getter）。"""
    return _runtime


__all__ = [
    "LIFECYCLE_ATTEMPT_TIMEOUT_SECONDS",
    "PRUNE_BATCH_LIMIT",
    "PRUNE_INTERVAL_SECONDS",
    "PRUNE_JOB_ID",
    "REFRESH_INTERVAL_SECONDS",
    "REFRESH_JOB_ID",
    "DecisionRuntime",
    "get_decision_runtime",
]
