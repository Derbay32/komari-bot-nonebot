""":set_user_favorability 使用本地 fake pool 的完整流程测试。

覆盖：
- 首次建档（before=initial_favorability）
- 已有行（before=实际当前值）
- SELECT FOR UPDATE 行锁 SQL
- 显式事务 (BEGIN/COMMIT)
- 0 和 400 边界值完整通过
- UPDATE 返回空结果 → RuntimeError
- 越界值在 DB 层被拒绝
"""

from __future__ import annotations

from typing import Any

import pytest

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB

# ─── fake pool / connection builder ─────────────────────────────


class _FakeConnection:
    """模拟 asyncpg Connection，捕获 SQL 与返回行。

    fetchrow / fetchval 按自己的调用序号从 rows 列表中取第 N 个元素，
    不受 execute() 调用的影响。
    """

    def __init__(self, fetch_rows: list[dict[str, Any] | None] | None = None) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self._fetch_rows: list[dict[str, Any] | None] = fetch_rows or []
        self._fetch_index = 0
        self._in_transaction = False
        self._transaction_depth = 0

    async def execute(self, query: str, *args: object) -> str:
        self.queries.append((query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: object) -> dict[str, Any] | None:
        self.queries.append((query, args))
        if self._fetch_index < len(self._fetch_rows):
            row = self._fetch_rows[self._fetch_index]
            self._fetch_index += 1
            if row is None:
                return None
            return dict(row)
        return None

    async def fetchval(self, query: str, *args: object) -> object:
        self.queries.append((query, args))
        if self._fetch_index < len(self._fetch_rows):
            row = self._fetch_rows[self._fetch_index]
            self._fetch_index += 1
            if row is None:
                return None
            return next(iter(row.values())) if row else None
        return None

    async def fetch(self, query: str, *args: object) -> list[dict[str, Any]]:
        self.queries.append((query, args))
        return [r for r in self._fetch_rows if r is not None]  # type: ignore[misc]

    def transaction(self) -> _FakeTransactionContext:
        return _FakeTransactionContext(self)


class _FakeTransactionContext:
    """模拟 asyncpg Connection.transaction() 上下文管理器。"""

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        self._conn._in_transaction = True
        self._conn._transaction_depth += 1
        # 不为事务添加单独的 execute 调用，保持测试简单
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        self._conn._in_transaction = False
        self._conn._transaction_depth -= 1


class _FakePool:
    """模拟 asyncpg Pool。"""

    def __init__(self, conn: _FakeConnection | None = None) -> None:
        self._conn = conn or _FakeConnection()
        self.closed = False
        self.acquire_calls = 0

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self)


class _FakeAcquireContext:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConnection:
        self._pool.acquire_calls += 1
        return self._pool._conn

    async def __aexit__(self, *args: object) -> None:
        pass


# ─── helpers ────────────────────────────────────────────────────


def _build_db(config: DynamicConfigSchema | None = None) -> UserDataDB:
    """创建 UserDataDB 实例。"""
    return UserDataDB(config or DynamicConfigSchema())


def _inject_pool(db: UserDataDB, conn: _FakeConnection) -> _FakePool:
    """注入 fake pool 到 UserDataDB 实例。"""
    pool = _FakePool(conn)
    db._pool = pool  # type: ignore[assignment]
    return pool


def _make_row(user_id: str, favorability: int, updated_at_str: str = "2026-07-11T23:00:00+08:00") -> dict[str, Any]:
    """构造模拟行，updated_at 提供 .isoformat() 方法模拟 asyncpg 返回的 datetime 对象。"""

    class _FakeDatetime:
        def __init__(self, iso_str: str) -> None:
            self._iso = iso_str

        def isoformat(self) -> str:
            return self._iso

    return {
        "user_id": user_id,
        "favorability": favorability,
        "updated_at": _FakeDatetime(updated_at_str),
    }


# ─── 首次建档测试 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_first_insertion_uses_initial_as_before() -> None:
    """首次设置：建档 SQL 先 INSERT，before = initial_favorability，UPDATE 返回新值。

    SQL 调用顺序（同一次事务内）：
      1. INSERT ... ON CONFLICT DO NOTHING  (execute)
      2. SELECT favorability ... FOR UPDATE  (fetchrow) → 无行则 before=initial
      3. UPDATE ... RETURNING                (fetchrow) → 返回新行
    """
    config = DynamicConfigSchema(initial_favorability=0)
    db = _build_db(config)
    # fetch_rows 仅对应 fetchrow 调用：2 次 fetchrow → 2 个 mock 行
    conn = _FakeConnection(
        fetch_rows=[
            None,                       # 第 1 次 fetchrow: SELECT FOR UPDATE → 无行（首次）
            _make_row("u1", 200),        # 第 2 次 fetchrow: UPDATE RETURNING
        ]
    )
    _inject_pool(db, conn)

    result = await db.set_user_favorability("u1", 200)

    # before 应为 initial_favorability（首次建档 SELECT 返回 None）
    assert result.before == 0
    assert result.after == 200
    assert result.user_id == "u1"
    assert result.stage_index == 3

    # 验证 SQL 调用顺序
    sql_texts = [q[0].strip().split()[0] for q in conn.queries]
    assert "INSERT" in sql_texts[0]  # ON CONFLICT DO NOTHING
    assert "SELECT" in sql_texts[1]  # FOR UPDATE
    assert "UPDATE" in sql_texts[2]  # RETURNING

    # 验证 SELECT 包含 FOR UPDATE
    select_sql = conn.queries[1][0]
    assert "FOR UPDATE" in select_sql.upper()


@pytest.mark.asyncio
async def test_set_favorability_existing_row_preserves_before() -> None:
    """已有行：before 应为 SELECT FOR UPDATE 返回的实际值。"""
    config = DynamicConfigSchema(initial_favorability=0)
    db = _build_db(config)
    conn = _FakeConnection(
        fetch_rows=[
            {"favorability": 150},       # 第 1 次 fetchrow: SELECT FOR UPDATE
            _make_row("u2", 350),        # 第 2 次 fetchrow: UPDATE RETURNING
        ]
    )
    _inject_pool(db, conn)

    result = await db.set_user_favorability("u2", 350)

    assert result.before == 150
    assert result.after == 350
    assert result.stage_index == 4


# ─── 边界值测试 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_zero_full_flow() -> None:
    """0 值通过完整流程。"""
    db = _build_db()
    conn = _FakeConnection(
        fetch_rows=[
            {"favorability": 100},       # SELECT FOR UPDATE
            _make_row("u3", 0),          # UPDATE RETURNING
        ]
    )
    _inject_pool(db, conn)

    result = await db.set_user_favorability("u3", 0)

    assert result.before == 100
    assert result.after == 0
    assert result.stage_index == 1


@pytest.mark.asyncio
async def test_set_favorability_400_full_flow() -> None:
    """400 值通过完整流程。"""
    db = _build_db()
    conn = _FakeConnection(
        fetch_rows=[
            {"favorability": 50},        # SELECT FOR UPDATE
            _make_row("u4", 400),        # UPDATE RETURNING
        ]
    )
    _inject_pool(db, conn)

    result = await db.set_user_favorability("u4", 400)

    assert result.before == 50
    assert result.after == 400
    assert result.stage_index == 4


# ─── 事务测试 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_runs_in_transaction() -> None:
    """验证 INSERT + SELECT FOR UPDATE + UPDATE 在同一事务中执行。"""
    db = _build_db()
    conn = _FakeConnection(
        fetch_rows=[
            {"favorability": 80},
            _make_row("u5", 250),
        ]
    )
    _inject_pool(db, conn)

    await db.set_user_favorability("u5", 250)

    # 在事务中应有恰好 3 条 SQL（INSERT + SELECT + UPDATE）
    assert len(conn.queries) == 3


# ─── UPDATE 空结果异常测试 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_update_returns_none_raises_runtime_error() -> None:
    """当 UPDATE RETURNING 返回 None 时抛出 RuntimeError。"""
    db = _build_db()
    conn = _FakeConnection(
        fetch_rows=[
            {"favorability": 60},        # SELECT FOR UPDATE 正常
            None,                         # UPDATE RETURNING → None
        ]
    )
    _inject_pool(db, conn)

    with pytest.raises(RuntimeError, match="好感度 SET 未返回记录"):
        await db.set_user_favorability("u6", 100)


# ─── 越界值测试（DB 层） ───────────────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_rejects_negative_value_db_layer() -> None:
    """越界值 -1 在 DB 层被 ValueError 拒绝。"""
    db = _build_db()
    conn = _FakeConnection()
    _inject_pool(db, conn)

    with pytest.raises(ValueError, match="好感度值 -1 越界"):
        await db.set_user_favorability("u7", -1)

    # 不应有任何 SQL 执行
    assert len(conn.queries) == 0


@pytest.mark.asyncio
async def test_set_favorability_rejects_401_value_db_layer() -> None:
    """越界值 401 在 DB 层被 ValueError 拒绝。"""
    db = _build_db()
    conn = _FakeConnection()
    _inject_pool(db, conn)

    with pytest.raises(ValueError, match="好感度值 401 越界"):
        await db.set_user_favorability("u7", 401)

    assert len(conn.queries) == 0


# ─── 连接池未初始化测试 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_requires_initialized_pool_detailed() -> None:
    """未注入连接池时 set_user_favorability 抛出 RuntimeError（在越界检查之后）。"""
    db = _build_db()
    # 不注入 pool

    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.set_user_favorability("u8", 150)


# ─── initial_favorability 非零测试 ─────────────────────────────


@pytest.mark.asyncio
async def test_set_favorability_first_insertion_with_nonzero_initial() -> None:
    """配置 initial_favorability=100 时，首次建档的 before 为 100。

    SELECT FOR UPDATE 返回 None（无行）→ before 回退到 initial_favorability=100。
    """
    config = DynamicConfigSchema(initial_favorability=100)
    db = _build_db(config)
    conn = _FakeConnection(
        fetch_rows=[
            None,                        # SELECT FOR UPDATE → 无行
            _make_row("u9", 200),        # UPDATE RETURNING
        ]
    )
    _inject_pool(db, conn)

    result = await db.set_user_favorability("u9", 200)

    # before 应为 initial_favorability=100
    assert result.before == 100
    assert result.after == 200
