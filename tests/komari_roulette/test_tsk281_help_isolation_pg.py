# ruff: noqa: RUF001  # 定稿帮助文案含全角字符与「·」
"""TSK-281 阶段 B3/G4：真实 komari_help 查询命中轮盘元数据帮助，且零轮盘副作用。

本文件只补齐验收矩阵最后一个已知组合缺口（G4）：一次**真实**用户帮助查询
（真实 ``on_command("docs")`` matcher + 真实 ``HelpEngine`` + 真实 PostgreSQL
帮助存储）返回的正文必须来自真实
``komari_roulette.__plugin_meta__.usage``；并且在查询观察窗口内不得调用任何
轮盘命令 / 当前局读取 / 惰性推进 / 领取入口，也不得出现任何轮盘业务 SQL。

隔离与所有权（create-only）
--------------------------
用例在门控库派生的**本次 run 独有**的一次性隔离库（库名 = 门控库名前缀 +
``tsk281_help_g4`` 前缀 + 新 UUID，总长 ≤63 字节）内，按既有 Alembic
``upgrade head`` 流程重建 schema，用例结束即普通 ``DROP``。所有权是
**create-only**：只对本进程新建的精确库名执行 ``CREATE``；已存在则明确失败，
绝不 ``DROP``/``FORCE`` 预存库，也绝不 ``pg_terminate_backend``。只有 ``CREATE``
确实成功后才在 ``finally`` 清理该库，且先 ``dispose`` 本用例自己的 engine
再普通 ``DROP``；若有残留连接令 ``DROP`` 失败会显式报错，而不是杀其他 PID。
共享门控库绝不被本用例读写，也不运行任何面向共享库的全局 scanner。隔离库内
的 roulette 自动帮助行走真实 ``scan_and_sync``，但 ``get_loaded_plugins`` 收敛
为唯一真实轮盘插件，因此只会写入本用例自己的帮助行（不 ON CONFLICT 覆盖外来
plugin_name，不 TRUNCATE 任何表）。

替换缝（完整列出，不是只有前两类）
----------------------------------
* embedding 端口：``_RecordingHelpEngine._get_embedding`` 输出本地确定性
  512 维向量；真实 SQL 检索、真实关键词索引与真实渲染仍执行（不是"假 LLM
  固定答案"）；
* 帮助配置：``engine.get_config`` / ``rendering.get_config`` 换成本用例的
  ``DynamicConfigSchema`` 缺省实例（与既有 ``tests/komari_help`` 测试同一
  模式）；
* 准入裁决：``commands.adjudicate`` 放行为 BUSINESS、``commands.config_manager``
  返回 ``plugin_enable=True``（与既有 ``tests/komari_help/test_commands.py``
  同一模式）；帮助引擎实例经 ``commands.get_engine`` 注入；
* 连接来源：真实 ``SharedEngineConnectionPool`` 实例的 ``_shared_engine``
  指向隔离库引擎，并在外层包一个 raw asyncpg 语句记录代理；
* 平台传输：nonebug 假 OneBot bot 记录发送；真实 matcher / 渲染 / 消息构建
  仍在运行。

轮盘侧不替换任何业务对象：``RouletteCommandService`` / ``PostgresRouletteStorage``
/ ``RouletteQQHandler`` 仍为真实生产对象，只是在观察窗口内被 spy/deny seam
包裹，以证明"根本没有被调用"，并在窗口外用真实只读 ``load_current`` 证明该
seam 确实拦得住真实调用。
"""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import asyncpg
import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from komari_bot.db.orm_connection import SharedEngineConnectionPool
from komari_bot.onebot.onebot_messages import plain_text_message
from komari_bot.plugins.group_admission import AdmissionQualification
from komari_bot.plugins.komari_help import engine as help_engine_module
from komari_bot.plugins.komari_help import rendering as help_rendering_module
from komari_bot.plugins.komari_help import scanner as help_scanner_module
from komari_bot.plugins.komari_help.config_schema import DynamicConfigSchema
from komari_bot.plugins.komari_help.engine import HelpEngine
from komari_bot.plugins.komari_roulette import (
    GroupRef,
    PostgresRouletteStorage,
    RouletteCommandService,
)
from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler
from tests.character_binding.conftest import require_postgres
from tests.db.tsk197_gate_support import (
    parse_dsn,
    run_bootstrap,
    same_database,
    scratch_url,
)
from tests.komari_roulette.command_support import PG_REQUIRED, Scope
from tests.komari_roulette.tsk279_support import insert_waiting_game, seed_aged_receipt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nonebug import App
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

#: PostgreSQL 标识符硬上限（字节）。
_PG_IDENTIFIER_MAX_BYTES = 63

#: 本票专属隔离库前缀；每次创建都携带它 + 新 UUID，杜绝与外来库/并发 run 撞名。
_SCRATCH_DATABASE_PREFIX = "tsk281_help_g4"


def _gate_dsn() -> str:
    """门控库连接串（只用于建/删本次独占隔离库，绝不读写业务表）。"""
    return os.environ.get("KOMARI_TEST_POSTGRES_URL", "")


def _build_scratch_database_name() -> str:
    """生成本次 run 唯一的合法隔离库名（门控库名前缀 + 本票前缀 + UUID）。

    名字总字节数不超过 PostgreSQL 的 63 字节标识符上限；UUID 保证同一指针
    重复/并发运行也不会复用同一名字。
    """
    base_database = str(parse_dsn(_gate_dsn())["database"])
    suffix = f"_{_SCRATCH_DATABASE_PREFIX}_{uuid4().hex}"
    budget = _PG_IDENTIFIER_MAX_BYTES - len(suffix.encode("utf-8"))
    prefix = base_database
    if budget < len(prefix.encode("utf-8")):
        prefix = prefix.encode("utf-8")[: max(budget, 0)].decode("utf-8", "ignore")
    name = f"{prefix}{suffix}"
    assert len(name.encode("utf-8")) <= _PG_IDENTIFIER_MAX_BYTES, name
    assert _SCRATCH_DATABASE_PREFIX in name, name
    return name


async def _database_exists(database: str) -> bool:
    """该库名当前是否存在于门控 PostgreSQL（只读目录查询）。"""
    connection = await asyncpg.connect(**parse_dsn(_gate_dsn()))
    try:
        return (
            await connection.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1",
                database,
            )
            is not None
        )
    finally:
        await connection.close()


async def _create_scratch_database(database: str) -> None:
    """只 ``CREATE`` 本次独有的隔离库；已存在则明确失败。

    所有权是 create-only：这里绝不 ``DROP``/``FORCE`` 预存库，也绝不
    ``pg_terminate_backend``。``CREATE DATABASE`` 即原子所有权声明；即使
    目录预检与 ``CREATE`` 之间存在竞态，``DuplicateDatabaseError`` 也会
    转成显式失败而不是覆盖。
    """
    connection = await asyncpg.connect(**parse_dsn(_gate_dsn()))
    try:
        existing = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            database,
        )
        if existing is not None:
            msg = (
                f"隔离库 {database} 已存在；create-only 所有权拒绝覆盖"
                "（不 DROP、不 FORCE、不终止其他后端）"
            )
            raise RuntimeError(msg)
        try:
            await connection.execute(f'CREATE DATABASE "{database}"')
        except asyncpg.DuplicateDatabaseError as error:
            msg = (
                f"隔离库 {database} 在 CREATE 时已存在；"
                "create-only 所有权拒绝覆盖"
            )
            raise RuntimeError(msg) from error
    finally:
        await connection.close()


async def _drop_scratch_database(database: str) -> None:
    """普通 ``DROP`` 本次成功创建的精确库（无 ``FORCE``、不杀其他 PID）。

    调用方必须先关闭本次 run 的全部 engine/pool/连接；若仍有残留连接导致
    ``DROP`` 失败，异常会显式上抛，而不是终止别人的后端。
    """
    connection = await asyncpg.connect(**parse_dsn(_gate_dsn()))
    try:
        await connection.execute(f'DROP DATABASE "{database}"')
    finally:
        await connection.close()


#: 与 ``migrations`` 默认（``EMBEDDING_DIMENSION`` 未设时 512）以及真实
#: ``komari_help.embedding`` 列维度保持一致。
_EMBEDDING_DIMENSION = 512

#: 已确认的四份轮盘帮助文案关键片段（与 ``tests/komari_roulette/test_tsk278_help.py``
#: 的 ``ROULETTE_USAGE_MARKERS`` 同源，这里取足以证明"确为轮盘帮助"的一组）。
_ROULETTE_HELP_MARKERS = (
    "俄罗斯轮盘",
    "2～6 人参与",
    "@Bot /轮盘 开局",
    "俄罗斯轮盘 · 等候帮助",
    "俄罗斯轮盘 · 行动帮助",
    "俄罗斯轮盘 · 奖励选择帮助",
    "A＝放大镜",
    "奖励 替换",
)

#: 轮盘业务边界（命令服务 / 存储 / QQ 处理器）的真实调用入口。观察窗口内
#: 命中任意一个即视为"帮助查询产生了轮盘副作用"并立即失败。
_ROULETTE_BOUNDARIES: tuple[tuple[type, str], ...] = (
    (RouletteCommandService, "execute_group_command"),
    (RouletteCommandService, "observe_current"),
    (RouletteCommandService, "advance_expired"),
    (RouletteCommandService, "claim_fulfillment"),
    (RouletteCommandService, "mark_delivered"),
    (RouletteQQHandler, "handle"),
    (PostgresRouletteStorage, "load_current"),
    (PostgresRouletteStorage, "create_waiting"),
    (PostgresRouletteStorage, "save_transition"),
)


class _RecordingHelpEngine(HelpEngine):
    """真实 ``HelpEngine`` + 本地确定性 embedding 端口（测试替换缝）。

    只替换远程 embedding 端口；``search`` / 关键词索引 / 真实 SQL 检索 /
    结果构建全部走生产实现，并记录每次 embedding 调用以证明真实查询路径确实
    走到了向量层。
    """

    def __init__(self) -> None:
        super().__init__()
        self.embedding_calls: list[str] = []

    async def _get_embedding(self, text: str) -> list[float]:
        self.embedding_calls.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [
            digest[index % len(digest)] / 255.0
            for index in range(_EMBEDDING_DIMENSION)
        ]


class _SqlRecorder:
    """记录 raw asyncpg 语句（进入 ``HelpEngine`` 连接代理的每一条）。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def clear(self) -> None:
        self.statements.clear()


class _RecordingConnection:
    """asyncpg 兼容代理：记录并转发 fetch / fetchrow / fetchval / execute / executemany。"""

    def __init__(self, inner: Any, recorder: _SqlRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        self._recorder.statements.append(str(query))
        return await self._inner.fetch(query, *args, **kwargs)

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        self._recorder.statements.append(str(query))
        return await self._inner.fetchrow(query, *args, **kwargs)

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        self._recorder.statements.append(str(query))
        return await self._inner.fetchval(query, *args, **kwargs)

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        self._recorder.statements.append(str(query))
        return await self._inner.execute(query, *args, **kwargs)

    async def executemany(self, query: str, *args: Any, **kwargs: Any) -> Any:
        self._recorder.statements.append(str(query))
        return await self._inner.executemany(query, *args, **kwargs)


class _RecordingPool:
    """在生产 ``SharedEngineConnectionPool`` 外层记录 raw asyncpg 语句。

    ``acquire`` 逐字复用生产适配器语义（``engine.connect()`` →
    ``get_raw_connection().driver_connection``），只把每个借出的 raw 连接包一层
    记录代理，因此 SQLAlchemy 事件看不到的 raw 语句也能被可靠捕获。
    """

    def __init__(self, inner: SharedEngineConnectionPool, recorder: _SqlRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_RecordingConnection]:
        async with self._inner.acquire() as connection:
            yield _RecordingConnection(connection, self._recorder)

    async def probe(self) -> bool:
        return await self._inner.probe()

    async def close(self) -> None:
        await self._inner.close()


class _SqlStatementSink:
    """SQLAlchemy ``before_cursor_execute`` 监听（覆盖 ORM/Core 语句）。

    raw asyncpg 语句绕过该事件系统，因此它与 ``_RecordingPool`` 互为补充：
    两者同时为空才说明观察窗口内确实没有轮盘业务读写。
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def clear(self) -> None:
        self.statements.clear()

    def __call__(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,  # noqa: FBT001 — SQLAlchemy 监听回调位置参数
    ) -> None:
        self.statements.append(str(statement))


class _RouletteBoundaryGuard:
    """轮盘业务边界的记录型 spy/deny seam。

    每次调用都记录边界名；当观察窗口 ``activate()`` 后任何一次调用都会立即
    抛 ``AssertionError``，把"帮助查询触碰轮盘"变成确定性失败而不是静默通过。
    """

    def __init__(self) -> None:
        self._events: list[str] = []
        self._active = False

    @property
    def events(self) -> list[str]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def activate(self) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for owner, name in _ROULETTE_BOUNDARIES:
            original = getattr(owner, name)

            def _wrap(
                original: Any = original,
                owner: type = owner,
                name: str = name,
            ) -> Any:
                async def _seam(*args: Any, **kwargs: Any) -> Any:
                    self._events.append(f"{owner.__name__}.{name}")
                    if self._active:
                        msg = (
                            "帮助查询观察窗口内禁止调用轮盘边界 "
                            f"{owner.__name__}.{name}"
                        )
                        raise AssertionError(msg)
                    return await original(*args, **kwargs)

                return _seam

            monkeypatch.setattr(owner, name, _wrap())


class _StubConfigManager:
    """``commands.config_manager`` 替身：只提供 ``get_async`` 的插件开关读取。"""

    def __init__(self, config: object) -> None:
        self._config = config

    async def get_async(self) -> object:
        return self._config


def _adjudicate_business(*_args: object, **_kwargs: object) -> object:
    """返回获准裁决替身，供命令路径放行统一准入。"""
    return SimpleNamespace(qualification=AdmissionQualification.BUSINESS)


def _create_onebot_bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


def _build_group_event(message_text: str) -> GroupMessageEvent:
    message = Message(message_text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=1047195267,
        message_type="group",
        message_id=123,
        message=message,
        original_message=message,
        raw_message=message_text,
        font=14,
        sender=Sender.model_construct(
            user_id=1047195267,
            nickname="测试用户",
            card="",
        ),
        to_me=False,
        reply=None,
        group_id=114514,
        anonymous=None,
    )


@dataclass(frozen=True, slots=True)
class _ScratchHelpDatabase:
    """本次 run 独占的隔离库句柄；engine 生命周期由 fixture 拥有。"""

    database: str
    url: str
    engine: AsyncEngine
    session_factory: async_sessionmaker[Any]


@asynccontextmanager
async def _owned_scratch_database() -> AsyncIterator[str]:
    """create-only 所有权：只建本次唯一库名，成功后才在 ``finally`` 清理它。

    创建失败/撞名时不进入正文，也绝不调用删除；正常退出时只删除本次
    ``CREATE`` 成功的精确库名。
    """
    database = _build_scratch_database_name()
    created = False
    try:
        await _create_scratch_database(database)
        created = True
        yield database
    finally:
        if created:
            await _drop_scratch_database(database)


@asynccontextmanager
async def _scratch_help_database() -> AsyncIterator[_ScratchHelpDatabase]:
    """在本次独占隔离库内按既有 Alembic head 流程重建 schema，结束时安全清理。

    engine 由本 fixture 创建并在 ``finally`` 中 ``dispose``，随后才由
    create-only 所有权层普通 ``DROP`` 精确库名。
    """
    require_postgres()
    postgres_url = os.environ.get("KOMARI_TEST_POSTGRES_URL", "")
    sqlalchemy_url = os.environ.get("SQLALCHEMY_DATABASE_URL", "")
    assert same_database(postgres_url, sqlalchemy_url), (
        "KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 必须同库"
    )
    async with _owned_scratch_database() as database:
        url = scratch_url(database)
        upgrade = run_bootstrap(url, "upgrade", "head")
        assert upgrade.returncode == 0, (
            f"隔离库 {database} alembic upgrade head 失败:"
            f"\nstdout={upgrade.stdout}\nstderr={upgrade.stderr}"
        )
        engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=4,
            max_overflow=4,
        )
        try:
            yield _ScratchHelpDatabase(
                database=database,
                url=url,
                engine=engine,
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
            )
        finally:
            await engine.dispose()


async def _scope_canary(
    engine: AsyncEngine,
    *,
    app_id: str,
    group_openid: str,
) -> dict[str, Any]:
    """读取本 scope 的全部轮盘行（game / player / receipt / fulfillment）。"""

    params = {"app_id": app_id, "group": group_openid}
    async with engine.connect() as connection:
        games = (
            (
                await connection.execute(
                    text(
                        "SELECT * FROM komari_roulette_games "
                        "WHERE app_id = :app_id AND group_openid = :group "
                        "ORDER BY game_id"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        players = (
            (
                await connection.execute(
                    text(
                        "SELECT p.* FROM komari_roulette_players AS p "
                        "JOIN komari_roulette_games AS g ON g.game_id = p.game_id "
                        "WHERE g.app_id = :app_id AND g.group_openid = :group "
                        "ORDER BY p.game_id, p.join_seq"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        receipts = (
            (
                await connection.execute(
                    text(
                        "SELECT * FROM komari_roulette_command_receipts "
                        "WHERE app_id = :app_id AND group_openid = :group "
                        "ORDER BY receipt_id"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        fulfillments = (
            (
                await connection.execute(
                    text(
                        "SELECT f.* FROM komari_roulette_fulfillments AS f "
                        "JOIN komari_roulette_command_receipts AS r "
                        "ON r.receipt_id = f.receipt_id "
                        "WHERE r.app_id = :app_id AND r.group_openid = :group "
                        "ORDER BY f.receipt_id"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
    return {
        "games": [dict(row) for row in games],
        "players": [dict(row) for row in players],
        "receipts": [dict(row) for row in receipts],
        "fulfillments": [dict(row) for row in fulfillments],
    }


async def test_real_help_query_hits_roulette_usage_without_roulette_effects(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_postgres()

    commands_module = import_module("komari_bot.plugins.komari_help.commands")
    roulette_plugin = import_module("komari_bot.plugins.komari_roulette")
    roulette_meta = roulette_plugin.__plugin_meta__
    real_usage = str(roulette_meta.usage)
    assert real_usage, "轮盘插件必须通过 PluginMetadata.usage 提供帮助"
    for marker in _ROULETTE_HELP_MARKERS:
        assert marker in real_usage, f"真实轮盘 usage 缺少片段: {marker!r}"

    async with _scratch_help_database() as scratch:
        engine = scratch.engine
        session_factory = scratch.session_factory
        sql_sink = _SqlStatementSink()
        event.listen(engine.sync_engine, "before_cursor_execute", sql_sink)
        try:
            app_id = f"tsk281-g4-app-{uuid4().hex}"
            group_openid = f"tsk281-g4-group-{uuid4().hex}"
            member_openid = f"tsk281-g4-member-{uuid4().hex}"
            game_id = f"tsk281-g4-game-{uuid4().hex}"
            current = Scope(
                app_id=app_id,
                group_openid=group_openid,
                member_openid=member_openid,
            )

            # 真实当前游戏：waiting 且 deadline 已过、尚未被惰性推进。
            await insert_waiting_game(
                session_factory,
                game_id=game_id,
                app_id=app_id,
                group_openid=group_openid,
                member_openid=member_openid,
            )
            await seed_aged_receipt(
                session_factory,
                current,
                inbound_msg_id="tsk281-g4-inbound",
                age_seconds=8 * 24 * 3600,
                result_code="created",
                game_id=game_id,
            )

            state_before = await _scope_canary(
                engine,
                app_id=app_id,
                group_openid=group_openid,
            )
            assert state_before["games"], state_before
            assert state_before["players"], state_before
            assert state_before["receipts"], state_before

            # 帮助配置与引擎：真实 HelpEngine + 真实存储 + 记录代理连接池。
            config = DynamicConfigSchema()
            monkeypatch.setattr(help_engine_module, "get_config", lambda: config)
            monkeypatch.setattr(help_rendering_module, "get_config", lambda: config)

            recorder = _SqlRecorder()
            adapter = SharedEngineConnectionPool()
            monkeypatch.setattr(adapter, "_shared_engine", lambda: engine)
            help_engine = _RecordingHelpEngine()
            help_engine._pool = _RecordingPool(adapter, recorder)

            # 命令路径注入缝：真实 matcher/handler 保持不变，只放行准入裁决、
            # 打开插件开关并把命令引用的引擎换成上面的真实引擎。
            monkeypatch.setattr(commands_module, "adjudicate", _adjudicate_business)
            monkeypatch.setattr(
                commands_module,
                "config_manager",
                _StubConfigManager(SimpleNamespace(plugin_enable=True)),
            )
            monkeypatch.setattr(commands_module, "get_engine", lambda: help_engine)

            # 受控真实扫描：只允许真实轮盘插件参与，绝不触碰共享库或外来帮助行。
            monkeypatch.setattr(
                help_scanner_module,
                "get_loaded_plugins",
                lambda: [
                    SimpleNamespace(name="komari_roulette", metadata=roulette_meta)
                ],
            )
            updated = await help_scanner_module.scan_and_sync(help_engine)
            assert updated == 1
            setup_statements = list(recorder.statements)
            assert any(
                "INSERT INTO komari_help" in statement
                for statement in setup_statements
            ), setup_statements

            # 正向证据：真实查询真正检索到真实轮盘帮助（含真实 SQL + 向量层）。
            recorder.clear()
            results = await help_engine.search(
                "轮盘",
                limit=help_rendering_module.get_search_result_limit(),
            )
            assert results, "真实帮助查询必须命中轮盘帮助"
            hit = next(
                (item for item in results if item.plugin_name == "komari_roulette"),
                None,
            )
            assert hit is not None, results
            assert hit.title == "俄罗斯轮盘"
            assert hit.source == "keyword"
            for marker in _ROULETTE_HELP_MARKERS:
                assert marker in hit.content, f"命中帮助缺少片段: {marker!r}"
            assert any(
                "komari_help" in statement.lower()
                for statement in recorder.statements
            ), recorder.statements
            assert help_engine.embedding_calls, "真实查询必须走到向量层 embedding 端口"
            expected_message = plain_text_message(
                help_rendering_module.format_results(results)
            )

            # 观察窗口外的正向对照：真实只读 load_current 必须被 seam 记录，
            # 且其 SQL 必须落到隔离库（证明 seam 拦得住真实调用、SQL 可观测）。
            guard = _RouletteBoundaryGuard()
            guard.install(monkeypatch)
            group = GroupRef(app_id=app_id, group_openid=group_openid)
            control_offset = len(sql_sink.statements)
            async with session_factory() as session:
                snapshot = await PostgresRouletteStorage(session).load_current(group)
            assert snapshot is not None
            assert snapshot.lifecycle == "waiting"
            assert "PostgresRouletteStorage.load_current" in guard.events
            assert any(
                "komari_roulette_games" in statement.lower()
                for statement in sql_sink.statements[control_offset:]
            ), sql_sink.statements[control_offset:]

            # 打开观察窗口：真实 .docs 帮助查询必须正常回复且零轮盘副作用。
            window_embedding_before = len(help_engine.embedding_calls)
            recorder.clear()
            sql_sink.clear()
            guard.clear()
            guard.activate()
            try:
                async with app.test_matcher(commands_module.help_cmd) as ctx:
                    bot = _create_onebot_bot(ctx)
                    help_event = _build_group_event(".docs 轮盘")
                    ctx.receive_event(bot, help_event)
                    ctx.should_ignore_permission(matcher=commands_module.help_cmd)
                    ctx.should_pass_rule(matcher=commands_module.help_cmd)
                    ctx.should_call_send(help_event, expected_message, bot=bot)
                    ctx.should_finished()
            finally:
                guard.deactivate()

            # 1) 没有任何轮盘业务边界被调用（含命令/读取/惰性推进/领取）。
            assert guard.events == [], guard.events
            # 2) 观察窗口内没有 raw asyncpg 轮盘语句，但帮助 SQL 确实执行了。
            roulette_raw = [
                statement
                for statement in recorder.statements
                if "roulette" in statement.lower()
            ]
            assert roulette_raw == [], roulette_raw
            # 观察窗口内 handler 真实检索确实执行了帮助 SQL（不是预先算好的那次
            # 搜索），且真的走到了 embedding 端口（向量层）。
            window_help_queries = [
                statement
                for statement in recorder.statements
                if "from komari_help" in statement.lower()
            ]
            assert window_help_queries, (
                "观察窗口内必须实际执行 handler 的帮助检索 SQL，而不是只执行"
                f"预先算好的搜索: {recorder.statements!r}"
            )
            assert len(help_engine.embedding_calls) > window_embedding_before, (
                "观察窗口内 handler 的真实检索必须走到 embedding 端口（向量层）"
            )
            # 3) 观察窗口内没有经 SQLAlchemy 引擎执行的轮盘语句。
            roulette_orm = [
                statement
                for statement in sql_sink.statements
                if "roulette" in statement.lower()
            ]
            assert roulette_orm == [], roulette_orm

            # 4) 真实游戏状态逐行不变（state_revision / phase / deadline / 库存…）。
            state_after = await _scope_canary(
                engine,
                app_id=app_id,
                group_openid=group_openid,
            )
            assert state_after == state_before
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", sql_sink)


# ---------------------------------------------------------------------------
# create-only 所有权边界验证（受控失败/记录代理 + 真实撞名）
# ---------------------------------------------------------------------------


async def test_create_only_scratch_database_rejects_existing_without_dropping() -> None:
    """真实撞名：已存在库必须显式失败，且失败路径绝不删除该库。"""
    require_postgres()

    database = _build_scratch_database_name()
    assert _SCRATCH_DATABASE_PREFIX in database
    assert len(database.encode("utf-8")) <= _PG_IDENTIFIER_MAX_BYTES

    # 第一次 CREATE 成功即取得所有权；若这里失败，绝不进入下面的 finally。
    await _create_scratch_database(database)
    try:
        assert await _database_exists(database)
        with pytest.raises(RuntimeError, match="create-only"):
            await _create_scratch_database(database)
        assert await _database_exists(database), (
            "create-only 撞名失败后不得删除已存在库"
        )
    finally:
        await _drop_scratch_database(database)
    assert not await _database_exists(database)


async def test_owned_scratch_database_never_drops_when_create_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受控创建失败：fixture 不进入正文，也绝不调用任何删除。"""
    module = sys.modules[__name__]
    created_names: list[str] = []
    dropped_names: list[str] = []

    async def _failing_create(database: str) -> None:
        created_names.append(database)
        msg = "injected scratch CREATE failure"
        raise RuntimeError(msg)

    async def _recording_drop(database: str) -> None:
        dropped_names.append(database)

    monkeypatch.setattr(module, "_create_scratch_database", _failing_create)
    monkeypatch.setattr(module, "_drop_scratch_database", _recording_drop)

    with pytest.raises(RuntimeError, match="injected scratch CREATE failure"):
        async with _owned_scratch_database():
            pytest.fail("创建失败时不得进入隔离库正文")

    assert len(created_names) == 1, created_names
    assert _SCRATCH_DATABASE_PREFIX in created_names[0]
    assert len(created_names[0].encode("utf-8")) <= _PG_IDENTIFIER_MAX_BYTES
    assert dropped_names == [], dropped_names


async def test_owned_scratch_database_drops_exact_created_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常退出：只删除本次 CREATE 成功的精确库名，且恰一次。"""
    module = sys.modules[__name__]
    created_names: list[str] = []
    dropped_names: list[str] = []

    async def _recording_create(database: str) -> None:
        created_names.append(database)

    async def _recording_drop(database: str) -> None:
        dropped_names.append(database)

    monkeypatch.setattr(module, "_create_scratch_database", _recording_create)
    monkeypatch.setattr(module, "_drop_scratch_database", _recording_drop)

    async with _owned_scratch_database() as database:
        assert database == created_names[-1]

    assert created_names == [database], created_names
    assert dropped_names == [database], dropped_names
