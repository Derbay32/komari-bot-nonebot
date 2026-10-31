"""TSK-182 判定引擎测试支持（test-support，不收集为用例）。

只提供跨判定用例共享的真实外部资源夹具：

- 从门控库派生的一次性隔离 PostgreSQL 库（迁移到 head，用例结束删除）；
- 隔离 Redis 上**本用例创建的作用域键**（结束逐个删除，绝不 FLUSHDB）；
- 受控的官方 SDK 外部 transport 替身（``httpx2.AsyncBaseTransport``）；
- 组装与生产同构的 ``JevDecisionEngine``（真实仓储、真实 Redis 布局、
  真实 Prompt 加载器、真实 PG 会话工厂、可归因的 ``application_name``）；
- 真实多进程 worker：独立解释器、真实引擎、共享的进入/退出时间戳区间，
  由父进程用区间重叠计算真实并发峰值（不靠数据库名额标记推断）。

不替身的内容：引擎内部、锁形状、业务辅助函数、SQL 形状、Prompt 加载器与
答案语义。业务答案全部是手写字面量，不导入生产常量。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import uuid4

import httpx2
import pytest
import redis.asyncio as redis_asyncio

from komari_bot.decision.jev import (
    DecisionMessage,
    DecisionRequest,
    EventIdentity,
)
from tests.pg_support import reset_shared_orm_engine

# 说明：插件配置 Schema 与引擎实现只在真正组装时导入。子进程（spawn）会
# 先导入本支持模块再安装插件包 shim，模块顶层导入业务插件会触发插件入口
# 的 require() 副作用。
if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from komari_bot.plugins.komari_decision.services.jev_engine import (
        JevDecisionEngine,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[2]

GATE_POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
TEST_REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")

CONFIGURED_BASE_URL = "https://jev-decision.example.invalid/gateway"
CONFIGURED_API_KEY = "tsk-182-dummy-api-key"
CONFIGURED_MODEL = "jev-1.14.0"

PROMPT_TEXTS = {
    "role_description": "判定角色：你是群里的角色，需要判断是否参与对话。",
    "reply_eligibility": "回应资格：明确呼叫必须回应，随意参与可选，其他情况沉默。",
    "retention_purpose": "保留用途：有交流作用的闲聊可以成为记忆候选。",
    "summary_recognition": "总结识别：只有当消息要求总结本群聊天时才算总结请求。",
}


def skip_without_services() -> None:
    """缺少判定服务端点时跳过（绝不记为通过）。"""
    if not GATE_POSTGRES_URL:
        pytest.skip("未设置 KOMARI_TEST_POSTGRES_URL，跳过判定集成测试")
    if not TEST_REDIS_URL:
        pytest.skip("未设置 KOMARI_TEST_REDIS_URL，跳过判定集成测试")


# ------------------------------ 一次性隔离库 ------------------------------


async def create_scratch_database(
    suffix: str, *, migrate: bool = True
) -> str:
    """派生一次性隔离库，返回 SQLAlchemy URL（可选是否迁移到 head）。"""
    from tests.db.tsk197_gate_support import (
        recreate_scratch_database,
        scratch_url,
    )

    scratch = await recreate_scratch_database(suffix)
    url = scratch_url(str(scratch["database"]))
    if migrate:
        upgrade_database(url)
    return url


def upgrade_database(url: str) -> None:
    """把一次性隔离库迁移到 head（绝不动共享门控库）。"""
    from tests.db.tsk197_gate_support import run_bootstrap

    upgraded = run_bootstrap(url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    checked = run_bootstrap(url, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr


async def drop_scratch_database(url: str) -> None:
    from urllib.parse import urlparse

    from tests.db.tsk197_gate_support import drop_scratch_database as _drop

    await _drop(urlparse(url).path.lstrip("/"))


async def reset_decision_storage(engine: AsyncEngine) -> None:
    """清空判定自有表，保留迁移建好的结构与停用触发器。"""
    from sqlalchemy import text

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE komari_decision_dispatches, komari_decision_processes,"
                " komari_decision_groups, komari_decision_scheduler,"
                " komari_decision_logs, komari_decision_config RESTART IDENTITY"
            )
        )


async def write_config_row(
    engine: AsyncEngine,
    *,
    overrides: Mapping[str, Any] | None = None,
    revision: int = 1,
) -> None:
    """写入实时控制行（调度事务从这里读取开关与共享容量）。"""
    from sqlalchemy import text

    values = _config_schema(overrides).model_dump(mode="json")
    values.update(id=1, revision=revision, updated_at=datetime.now(UTC).isoformat())
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM komari_decision_config WHERE id = 1")
        )
        await connection.execute(
            text(
                "INSERT INTO komari_decision_config"
                " SELECT * FROM json_populate_record("
                "NULL::komari_decision_config, CAST(:payload AS json))"
            ),
            {"payload": json.dumps(values)},
        )


async def update_config_row(
    engine: AsyncEngine, overrides: Mapping[str, Any]
) -> None:
    """在既有行上更新指定列（用于实时容量/开关变化）。"""
    from sqlalchemy import text

    assignments = {"revision": "revision + 1", "updated_at": "now()"}
    parameters: dict[str, Any] = {}
    for index, (column, value) in enumerate(overrides.items()):
        assignments[column] = f":p{index}"
        parameters[f"p{index}"] = value
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE komari_decision_config SET "
                + ", ".join(
                    f"{column} = {expression}"
                    for column, expression in assignments.items()
                )
                + " WHERE id = 1"
            ),
            parameters,
        )


async def read_dispatch(
    engine: AsyncEngine, identity: EventIdentity
) -> dict[str, Any] | None:
    """读取事件身份行（持久占位/交接/撤销的观测面）。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT * FROM komari_decision_dispatches"
                        " WHERE protocol = :protocol AND account_id = :account_id"
                        " AND group_id = :group_id AND message_id = :message_id"
                    ),
                    {
                        "protocol": identity.protocol,
                        "account_id": identity.account_id,
                        "group_id": identity.group_id,
                        "message_id": identity.message_id,
                    },
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def count_requests_by_status(engine: AsyncEngine) -> dict[str, int]:
    """统计判定行的状态分布（跨进程容量/竞争观测面）。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT state, count(*) AS total"
                    " FROM komari_decision_dispatches GROUP BY state"
                )
            )
        ).mappings()
    return {str(row["state"]): int(row["total"]) for row in rows}


async def read_slot_rows(engine: AsyncEngine) -> int:
    """当前仍持有执行名额的行数（补充观测，不作为并发预言机）。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM komari_decision_dispatches"
                    " WHERE slot_held"
                )
            )
            or 0
        )


# ------------------------------ Prompt 行 ------------------------------


async def write_prompt(texts: Mapping[str, str] | None = None) -> int:
    """经公开 Prompt 存储入口写入当前判定 Prompt 修订（共享 ORM 库）。"""
    from komari_bot.config.prompt_storage import get_prompt_storage
    from komari_bot.plugins.komari_decision.prompt_schema import (
        RESOURCE_ID as DECISION_RESOURCE_ID,
    )

    await delete_prompt()
    stored = await get_prompt_storage().replace_if_revision_async(
        resource_id=DECISION_RESOURCE_ID,
        prompt_data=dict(texts or PROMPT_TEXTS),
        expected_revision=0,
    )
    assert stored is not None
    return stored.revision


async def delete_prompt() -> None:
    from sqlalchemy import delete

    from komari_bot.config import prompt_storage as storage_module
    from komari_bot.plugins.komari_decision.prompt_schema import (
        RESOURCE_ID as DECISION_RESOURCE_ID,
    )

    model_cls = storage_module.ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert model_cls is not None
    async with _shared_session() as session:
        await session.execute(delete(model_cls))
        await session.commit()


def _shared_session() -> AsyncSession:
    from nonebot import require

    require("nonebot_plugin_orm")
    from nonebot_plugin_orm import get_session

    return get_session()


def write_scratch_prompt_row(url: str) -> None:
    """把判定 Prompt 行写入一次性隔离库（供子进程真实加载器读取）。"""
    import asyncio as _asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from komari_bot.config.typed_config import ensure_typed_prompt_model

    async def _write() -> None:
        model_cls = ensure_typed_prompt_model("komari_decision")
        assert model_cls is not None
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await session.execute(delete(model_cls))
                await session.commit()
                session.add(
                    model_cls(
                        id=1,
                        revision=1,
                        updated_at=datetime.now(UTC),
                        **PROMPT_TEXTS,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    _asyncio.run(_write())


# ------------------------------ 配置与快照 ------------------------------


def _config_schema(overrides: Mapping[str, Any] | None = None) -> Any:
    from komari_bot.plugins.komari_decision.config_schema import (
        KomariDecisionConfigSchema,
    )

    values: dict[str, Any] = {
        "jev_base_url": CONFIGURED_BASE_URL,
        "jev_api_key": CONFIGURED_API_KEY,
        "jev_model": CONFIGURED_MODEL,
        "jev_http_timeout_seconds": 3.0,
        "jev_request_timeout_seconds": 3.0,
        "max_concurrent_decisions": 4,
        "max_queued_messages_per_group": 5,
        "queue_timeout_seconds": 3.0,
        "reference_timeout_seconds": 3.0,
        "optional_reply_probability": 0.5,
        "decision_log_retention_days": 7,
    }
    values.update(overrides or {})
    return KomariDecisionConfigSchema(**values)


def _snapshot(value: Any, revision: int) -> Any:
    from komari_bot.plugins import config_manager as config_manager_plugin

    return config_manager_plugin.ConfigSnapshot(
        value=value, revision=revision, updated_at=datetime.now(UTC)
    )


class ConfigSource:
    """可变测试配置源（引擎每次任务入口取一次原子快照）。"""

    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        self._overrides = dict(overrides or {})
        self.value = _config_schema(self._overrides)
        self.revision = 1

    def __call__(self) -> Any:
        return _snapshot(self.value, self.revision)

    def replace(self, overrides: Mapping[str, Any]) -> None:
        self._overrides.update(overrides)
        self.value = _config_schema(self._overrides)
        self.revision += 1


# ------------------------------ 受控 SDK transport ------------------------------

Responder = Callable[[httpx2.Request, int], Any]


def build_answers(
    questions: Mapping[str, Any],
    *,
    reply: str = "required",
    retention: str = "candidate",
    summary: float = 0.05,
    confidence: float = 0.91,
    actual_model: str = CONFIGURED_MODEL,
    usage: Mapping[str, int | None] | None = None,
) -> dict[str, Any]:
    """按请求里实际的问题类型生成响应体（测试自定义的字面量答案）。"""
    answers: dict[str, Any] = {}
    for name, question in questions.items():
        kind = question["type"]
        if kind == "choice":
            labels = list(question["criteria"])
            if name == "reply_eligibility":
                chosen = reply
            elif name == "retention_purpose":
                chosen = retention
            else:  # pragma: no cover - 判定只请求两个 Choice
                chosen = labels[0]
            distribution = dict.fromkeys(labels, 0.0)
            distribution[chosen] = 1.0
            answers[name] = {
                "type": "choice",
                "choice": chosen,
                "confidence": confidence,
                "probabilities": distribution,
            }
        elif kind == "noul":
            answers[name] = {"type": "noul", "noul": summary}
        else:  # pragma: no cover - 判定只请求两种问题
            answers[name] = {"type": kind}
    payload: dict[str, Any] = {"model": actual_model, "answers": answers}
    payload["usage"] = (
        dict(usage) if usage is not None else {"input_tokens": 128, "output_tokens": 9}
    )
    return payload


class ScriptedTransport(httpx2.AsyncBaseTransport):
    """官方 SDK 支持的外部 transport 替身：记录请求并回放脚本化响应。"""

    def __init__(self, responder: Responder | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.closed = False
        self._responder = responder or _default_responder

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        index = len(self.requests)
        self.requests.append(request)
        produced = self._responder(request, index)
        if asyncio.iscoroutine(produced) or hasattr(produced, "__await__"):
            produced = await cast("Awaitable[Any]", produced)
        if isinstance(produced, httpx2.Response):
            return produced
        return httpx2.Response(200, json=produced, request=request)

    async def aclose(self) -> None:
        self.closed = True

    def payload(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def question_ids(self, index: int = 0) -> list[str]:
        return sorted(self.payload(index)["questions"])

    def state(self, index: int = 0) -> dict[str, Any]:
        return self.payload(index)["state"]


def _default_responder(request: httpx2.Request, index: int) -> dict[str, Any]:
    del index
    body = json.loads(request.content)
    return build_answers(body["questions"])


# ------------------------------ 身份与请求 ------------------------------

_SCOPES: set[tuple[str, str, str]] = set()
"""本用例接触过的 Redis 作用域（结束逐个删除这些键）。"""


def note_scope(protocol: str, account_id: str, group_id: str) -> None:
    """登记一个本用例使用的 Redis 作用域，供结束时精确清理。"""
    _SCOPES.add((protocol, account_id, group_id))


async def clean_test_scopes(redis: Redis) -> None:
    """只删除本用例创建的作用域键（绝不 FLUSHDB、绝不广删前缀）。"""
    from komari_bot.plugins.komari_decision.services.jev_recent import scope_keys

    keys: list[str] = []
    for protocol, account_id, group_id in _SCOPES:
        keys.extend(scope_keys(protocol, account_id, group_id).all())
    if keys:
        await redis.delete(*keys)
    _SCOPES.clear()


def make_identity(
    *,
    protocol: str = "onebot_v11",
    account_id: str | None = None,
    group_id: str | None = None,
    message_id: str | None = None,
) -> EventIdentity:
    identity = EventIdentity(
        protocol=protocol,
        account_id=account_id or f"acct-{uuid4().hex[:8]}",
        group_id=group_id or f"group-{uuid4().hex[:8]}",
        message_id=message_id or f"msg-{uuid4().hex[:8]}",
    )
    note_scope(identity.protocol, identity.account_id, identity.group_id)
    return identity


def make_message(
    *,
    identity: EventIdentity | None = None,
    message_id: str | None = None,
    sender_id: str | None = None,
    text: str = "今天谁值班？",
    display_name: str = "群友",
    timestamp: datetime | None = None,
    reference_ids: Sequence[str] = (),
    mention_ids: Sequence[str] = (),
    image_present: bool = False,
    image_description: str | None = None,
    is_bot: bool = False,
    group_id: str | None = None,
) -> DecisionMessage:
    if identity is not None:
        note_scope(identity.protocol, identity.account_id, identity.group_id)
    return DecisionMessage(
        message_id=message_id
        or (identity.message_id if identity else None)
        or f"msg-{uuid4().hex[:8]}",
        group_id=group_id
        or (identity.group_id if identity else None)
        or f"group-{uuid4().hex[:8]}",
        sender_id=sender_id or f"user-{uuid4().hex[:6]}",
        display_name=display_name,
        text=text,
        timestamp=timestamp or datetime.now(UTC),
        is_bot=is_bot,
        reference_ids=tuple(reference_ids),
        mention_ids=tuple(mention_ids),
        image_present=image_present,
        image_description=image_description,
    )


def make_request(
    *,
    identity: EventIdentity | None = None,
    target: DecisionMessage | None = None,
    received_at: datetime | None = None,
    dispatch_id: str | None = None,
    known_references: Sequence[DecisionMessage] = (),
    real_platform_at: bool = False,
    reply_to_bot: bool = False,
    summary_candidate: bool = False,
    functional_command: bool = False,
    text: str = "今天谁值班？",
) -> DecisionRequest:
    event = identity or make_identity()
    message = target or make_message(identity=event, text=text)
    return DecisionRequest(
        event=event,
        target=message,
        received_at=received_at or message.timestamp + timedelta(seconds=1),
        dispatch_id=dispatch_id or str(uuid4()),
        known_references=tuple(known_references),
        real_platform_at=real_platform_at,
        reply_to_bot=reply_to_bot,
        summary_candidate=summary_candidate,
        functional_command=functional_command,
    )


# ------------------------------ 引擎夹具 ------------------------------


@dataclass
class HarnessOptions:
    """一个引擎夹具的可控外部依赖。"""

    config: Mapping[str, Any] = field(default_factory=dict)
    row_config: Mapping[str, Any] = field(default_factory=dict)
    responder: Responder | None = None
    transport: ScriptedTransport | None = None
    admission: Callable[[EventIdentity], bool] = lambda _identity: True
    reference_reader: (
        Callable[[EventIdentity, str], Awaitable[DecisionMessage | None]] | None
    ) = None
    random_values: Sequence[float] | None = None
    prompt_texts: Mapping[str, str] | None = None
    initialize: bool = True
    write_prompt_row: bool = True
    write_config_row: bool = True
    reset_storage: bool = True
    application_name: str | None = None
    redis_wrapper: Callable[[Any], Any] | None = None
    clean_redis_scopes: bool = True
    """结束本夹具时是否删除其作用域键（多实例共享同一作用域时由最后一个负责）。"""
    session_factory_wrapper: (
        Callable[[Callable[[], AsyncSession]], Callable[[], AsyncSession]] | None
    ) = None


class DecisionHarness:
    """真实 PG + Redis + Prompt 加载器 + 生产引擎的组合夹具。"""

    def __init__(
        self,
        database_url: str,
        options: HarnessOptions | None = None,
    ) -> None:
        self.database_url = database_url
        self.options = options or HarnessOptions()
        self.application_name = (
            self.options.application_name or f"jev-test-{uuid4().hex[:10]}"
        )
        self.transport = self.options.transport or ScriptedTransport(
            self.options.responder
        )
        self.config_source = ConfigSource(
            {**dict(self.options.config), **dict(self.options.row_config)}
        )
        self._random_values = list(self.options.random_values or ())
        self.random_draws = 0
        self.reference_calls: list[tuple[EventIdentity, str]] = []
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.redis: Redis | None = None
        self.decision: JevDecisionEngine | None = None

    async def __aenter__(self) -> Self:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        # 共享 ORM 引擎（Prompt 存储）跨事件循环必须在本循环内先释放连接池。
        await reset_shared_orm_engine()

        from komari_bot.config.prompt_storage import PromptTemplateLoader
        from komari_bot.plugins.komari_decision.prompt_schema import (
            DISPLAY_NAME,
            RESOURCE_ID,
        )
        from komari_bot.plugins.komari_decision.services.jev_engine import (
            JevDecisionEngine as EngineClass,
        )

        self.engine = create_async_engine(
            self.database_url,
            connect_args={
                "server_settings": {"application_name": self.application_name}
            },
        )
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        redis_client: Any = redis_asyncio.Redis.from_url(TEST_REDIS_URL)
        self.redis = (
            redis_client
            if self.options.redis_wrapper is None
            else self.options.redis_wrapper(redis_client)
        )
        engine_session_factory: Callable[[], AsyncSession] = self.session_factory
        if self.options.session_factory_wrapper is not None:
            engine_session_factory = self.options.session_factory_wrapper(
                engine_session_factory
            )
        if self.options.reset_storage:
            await reset_decision_storage(self.engine)
        if self.options.write_config_row:
            await write_config_row(
                self.engine, overrides=self.options.row_config or self.options.config
            )
        if self.options.write_prompt_row:
            await write_prompt(self.options.prompt_texts)
        loader = PromptTemplateLoader(
            resource_id=RESOURCE_ID,
            display_name=DISPLAY_NAME,
            log_prefix="[test]",
        )
        assert self.redis is not None
        self.decision = EngineClass(
            config_source=self.config_source,
            prompt_loader=loader,
            redis=self.redis,
            reference_reader=self._reference_reader,
            admission=self.options.admission,
            session_factory=engine_session_factory,
            transport_factory=lambda: self.transport,
            random_source=self._random,
        )
        if self.options.initialize:
            await self.decision.initialize()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.decision is not None:
            await self.decision.close()
        if self.redis is not None and self.options.clean_redis_scopes:
            await clean_test_scopes(self.redis)
        if self.options.redis_wrapper is not None and self.redis is not None:
            with suppress(Exception):
                await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()
        if self.options.write_prompt_row:
            await delete_prompt()
        # 同一事件循环内释放共享连接池：避免跨循环关闭连接池的噪声异常。
        await reset_shared_orm_engine()

    def _random(self) -> float:
        self.random_draws += 1
        if not self._random_values:
            return 0.99
        return self._random_values.pop(0)

    async def _reference_reader(
        self, identity: EventIdentity, message_id: str
    ) -> DecisionMessage | None:
        self.reference_calls.append((identity, message_id))
        if self.options.reference_reader is None:
            return None
        return await self.options.reference_reader(identity, message_id)

    def set_config(self, overrides: Mapping[str, Any]) -> None:
        """替换任务入口可见的配置快照（不影响在途任务的冻结值）。"""
        self.config_source.replace(overrides)


async def admin_engine(url: str, *, application_name: str | None = None) -> Any:
    """独立的管理/观测引擎（与被测引擎的连接归因分开）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        url,
        connect_args={
            "server_settings": {
                "application_name": application_name or f"jev-admin-{uuid4().hex[:8]}"
            }
        },
    )


async def advisory_session_lock_count(
    engine: AsyncEngine, application_name: str
) -> int:
    """某个应用名归属的连接当前持有的会话级 advisory lock 数量。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks l"
                    " JOIN pg_stat_activity a ON a.pid = l.pid"
                    " WHERE l.locktype = 'advisory' AND l.objsubid = 2"
                    " AND a.application_name = :app"
                ),
                {"app": application_name},
            )
            or 0
        )


async def kill_connections(engine: AsyncEngine, application_name: str) -> int:
    """终止某个应用名归属的连接（外部连接丢失故障的精确归因）。"""
    from sqlalchemy import text

    async with engine.connect() as connection:
        killed = await connection.scalar(
            text(
                "SELECT count(*) FROM ("
                " SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = :app AND pid <> pg_backend_pid()"
                ") AS terminated"
            ),
            {"app": application_name},
        )
    return int(killed or 0)


# ------------------------------ 真实并发区间预言机 ------------------------------


class FailingRedis:
    """外部 Redis 端点替身：按需让下一次脚本调用失败一次。

    只替换存储端点本身，不改变引擎内部逻辑；用于验证「SQL 已成功、Redis 定案
    失败」之后能否仅凭持久处置布尔值恢复。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.fail_next_eval = False
        self.failed_evals = 0

    def arms(self) -> None:
        """让下一次脚本调用失败（消费一次）。"""
        self.fail_next_eval = True

    async def eval(self, *args: Any) -> Any:
        if self.fail_next_eval:
            self.fail_next_eval = False
            self.failed_evals += 1
            msg = "synthetic redis script failure"
            raise ConnectionError(msg)
        return await cast("Awaitable[Any]", self._client.eval(*args))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


async def wait_until(
    predicate: Callable[[], bool], *, within_seconds: float = 5.0
) -> None:
    """在限定时间内等待测试条件成立（真实服务轮询的共用等待点）。"""
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "等待条件超时"
    raise AssertionError(msg)


def peak_overlap(intervals: Sequence[tuple[float, float]]) -> int:
    """由真实进入/退出时间戳计算最大同时占用数（测试侧预言机）。"""
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    current = 0
    peak = 0
    for _timestamp, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


# ------------------------------ 跨进程 worker（真实独立解释器） ------------------------------


def _install_package_shims() -> None:
    """按源文件路径加载判定实现，不执行插件包 ``__init__`` 的启动副作用。"""
    for dotted in (
        "komari_bot.plugins.komari_decision",
        "komari_bot.plugins.komari_decision.services",
        "komari_bot.plugins.komari_decision.repositories",
    ):
        if dotted in sys.modules:
            continue
        module = types.ModuleType(dotted)
        module.__path__ = [str(PROJECT_ROOT / dotted.replace(".", "/"))]
        sys.modules[dotted] = module


def _install_orm_session_shim(database_url: str) -> None:
    """把 Prompt 存储的**外部持久化**换成子进程自有会话工厂。

    引擎与 Prompt 加载器仍是生产实现；被替换的只是 nonebot-plugin-orm 的
    外部会话入口（子进程不启动 NoneBot）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    module = types.ModuleType("nonebot_plugin_orm")
    module.__path__ = []  # type: ignore[attr-defined]

    def get_session(*_args: object, **_kwargs: object) -> AsyncSession:
        return factory()

    module.get_session = get_session  # type: ignore[attr-defined]
    sys.modules["nonebot_plugin_orm"] = module


async def _no_reference(
    identity: EventIdentity, message_id: str
) -> DecisionMessage | None:
    del identity, message_id
    return None


def spawn_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """子进程入口：在自己的解释器与事件循环里运行一个真实判定引擎。

    意外的取消在进程边界显式转成失败（RuntimeError），让 Pool 立即上报而不是
    挂起；绝不把取消当成成功或放宽断言。
    """
    try:
        return asyncio.run(_spawn_worker_main(payload))
    except asyncio.CancelledError as exc:
        msg = "worker task cancelled unexpectedly"
        raise RuntimeError(msg) from exc


async def _spawn_worker_main(payload: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    _install_package_shims()
    _install_orm_session_shim(payload["database_url"])

    from komari_bot.config.prompt_storage import PromptTemplateLoader
    from komari_bot.config.typed_config import ensure_typed_config_model
    from komari_bot.decision.jev import DecisionRequest, EventIdentity
    from komari_bot.plugins.komari_decision.prompt_schema import (
        DISPLAY_NAME,
        RESOURCE_ID,
    )
    from komari_bot.plugins.komari_decision.services.jev_engine import (
        JevDecisionEngine,
    )

    schema = ensure_typed_config_model("komari_decision")
    assert schema is not None
    value = schema(**payload["config"])
    snapshot = _worker_snapshot(value)
    engine = create_async_engine(payload["database_url"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = redis_asyncio.Redis.from_url(payload["redis_url"])
    hold_seconds = float(payload["hold_seconds"])
    intervals = payload["intervals"]
    events_log = payload["events_log"]
    barrier = payload["barrier"]
    worker_id = str(payload["worker_id"])
    barrier_size = int(payload["barrier_size"])

    class ProbeTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx2.Request
        ) -> httpx2.Response:
            started = time.monotonic()
            try:
                await asyncio.sleep(hold_seconds)
                body = json.loads(request.content)
                return httpx2.Response(
                    200, json=build_answers(body["questions"]), request=request
                )
            finally:
                finished = time.monotonic()
                intervals.append(("sdk", started, finished))
                events_log.append(
                    {
                        "worker": worker_id,
                        "kind": "sdk",
                        "start": started,
                        "end": finished,
                        "message_id": str(request.url),
                    }
                )

        async def aclose(self) -> None:
            return None

    async def reference_reader(
        identity: EventIdentity, message_id: str
    ) -> DecisionMessage | None:
        started = time.monotonic()
        try:
            await asyncio.sleep(hold_seconds)
            return make_message(
                group_id=identity.group_id,
                message_id=message_id,
                text=f"引用原文 {message_id}",
                timestamp=datetime.now(UTC),
            )
        finally:
            finished = time.monotonic()
            intervals.append(("reference", started, finished))
            events_log.append(
                {
                    "worker": worker_id,
                    "kind": "reference",
                    "start": started,
                    "end": finished,
                    "message_id": message_id,
                }
            )

    loader = PromptTemplateLoader(
        resource_id=RESOURCE_ID,
        display_name=DISPLAY_NAME,
        log_prefix="[worker]",
    )
    decision = JevDecisionEngine(
        config_source=lambda: snapshot,
        prompt_loader=loader,
        redis=redis_client,
        reference_reader=reference_reader,
        admission=lambda _identity: True,
        session_factory=session_factory,
        transport_factory=ProbeTransport,
    )
    received: list[dict[str, Any]] = []
    try:
        await decision.initialize()
        # 启动屏障：等所有子进程都完成初始化再同时开始，绝不靠时间猜测。
        barrier[worker_id] = time.monotonic()
        deadline = time.monotonic() + 30.0
        while len(barrier) < barrier_size:
            if time.monotonic() > deadline:
                msg = f"启动屏障超时: {len(barrier)}/{barrier_size}"
                raise TimeoutError(msg)
            await asyncio.sleep(0.01)
        for item in payload["events"]:
            event = EventIdentity(**item["event"])
            request = DecisionRequest(
                event=event,
                target=make_message(
                    identity=event,
                    sender_id=item["sender_id"],
                    text=item["text"],
                    timestamp=datetime.fromisoformat(item["timestamp"]),
                    reference_ids=tuple(item.get("reference_ids") or ()),
                    display_name="子进程群友",
                ),
                received_at=datetime.fromisoformat(item["received_at"]),
                dispatch_id=item["dispatch_id"],
            )
            result = await decision.evaluate(request)
            handed_off = await decision.claim_handoff(request, result, "chat")
            received.append(
                {
                    "status": result.status,
                    "reason_code": result.reason_code,
                    "decision_id": result.decision_id,
                    "handoff": handed_off,
                }
            )
    finally:
        await decision.close()
        await redis_client.aclose()
        await engine.dispose()
    return {"results": received}


def _worker_snapshot(value: Any) -> Any:
    return types.SimpleNamespace(
        value=value, revision=1, updated_at=datetime.now(UTC)
    )


def worker_payload(
    database_url: str,
    *,
    config: Mapping[str, Any],
    events: Sequence[tuple[EventIdentity, str]],
    barrier: Any,
    intervals: Any,
    events_log: Any,
    barrier_size: int,
    worker_id: str,
    hold_seconds: float,
    reference_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """组装一个子进程载荷（显式带与主夹具相同的 SDK 配置）。"""
    full_config: dict[str, Any] = {
        "jev_base_url": CONFIGURED_BASE_URL,
        "jev_api_key": CONFIGURED_API_KEY,
        "jev_model": CONFIGURED_MODEL,
        "jev_http_timeout_seconds": 3.0,
        "jev_request_timeout_seconds": 3.0,
        "max_concurrent_decisions": 4,
        "max_queued_messages_per_group": 5,
        "queue_timeout_seconds": 3.0,
        "reference_timeout_seconds": 3.0,
        "optional_reply_probability": 0.5,
        "decision_log_retention_days": 7,
    }
    full_config.update(dict(config))
    return {
        "database_url": database_url,
        "redis_url": TEST_REDIS_URL,
        "config": full_config,
        "hold_seconds": hold_seconds,
        "barrier": barrier,
        "barrier_size": barrier_size,
        "worker_id": worker_id,
        "intervals": intervals,
        "events_log": events_log,
        "events": [
            {
                "event": {
                    "protocol": identity.protocol,
                    "account_id": identity.account_id,
                    "group_id": identity.group_id,
                    "message_id": identity.message_id,
                },
                "dispatch_id": dispatch_id,
                "sender_id": "worker-member",
                "text": f"跨进程消息 {identity.message_id}",
                "timestamp": datetime.now(UTC).isoformat(),
                "received_at": (
                    datetime.now(UTC) + timedelta(seconds=1)
                ).isoformat(),
                "reference_ids": list(reference_ids),
            }
            for identity, dispatch_id in events
        ],
    }


__all__ = [
    "CONFIGURED_API_KEY",
    "CONFIGURED_BASE_URL",
    "CONFIGURED_MODEL",
    "PROJECT_ROOT",
    "PROMPT_TEXTS",
    "TEST_REDIS_URL",
    "ConfigSource",
    "DecisionHarness",
    "FailingRedis",
    "HarnessOptions",
    "ScriptedTransport",
    "admin_engine",
    "advisory_session_lock_count",
    "build_answers",
    "clean_test_scopes",
    "count_requests_by_status",
    "create_scratch_database",
    "drop_scratch_database",
    "kill_connections",
    "make_identity",
    "make_message",
    "make_request",
    "note_scope",
    "peak_overlap",
    "read_dispatch",
    "read_slot_rows",
    "reset_decision_storage",
    "skip_without_services",
    "spawn_worker",
    "update_config_row",
    "upgrade_database",
    "wait_until",
    "worker_payload",
    "write_config_row",
    "write_prompt",
    "write_scratch_prompt_row",
]
