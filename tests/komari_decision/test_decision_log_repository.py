"""TSK-182 判定日志仓储验收：元数据/正文边界、脱敏、保留期与清理。

所有断言都穿过公开仓储接口与**外部 SQL 观测**（直接读取
``komari_decision_logs`` / ``komari_decision_dispatches`` 的原始列），不绑定
私有辅助函数或 SQL 文本形状；存储使用门控库派生的一次性隔离库（迁移到
head，用例结束 DROP）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from komari_bot.decision.jev import (
    DecisionLogMetadata,
    DecisionReason,
    DecisionResult,
    DecisionStatus,
    DecisionUsage,
    EventIdentity,
    PhaseDurations,
    ReferenceSummary,
    ReplyChoice,
    RetentionChoice,
)
from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
    STORAGE_UNAVAILABLE_MESSAGE,
    DecisionLogBody,
    DecisionLogCorruptRowError,
    DecisionLogPage,
    DecisionLogRepository,
    DecisionLogRequestError,
    DecisionLogRetentionError,
    DecisionLogStorageUnavailable,
)
from tests.komari_decision.jev_test_support import (
    create_scratch_database,
    drop_scratch_database,
    skip_without_services,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_REDACTED = "[已移除凭据]"

#: 三条时间列使用的固定判定标识（小写规范 UUID，便于断言 id 升序）。
_ID_A = "00000000-0000-0000-0000-00000000000a"
_ID_B = "00000000-0000-0000-0000-00000000000b"
_ID_C = "00000000-0000-0000-0000-00000000000c"
_ID_D = "00000000-0000-0000-0000-00000000000d"
_ID_E = "00000000-0000-0000-0000-00000000000e"


# ------------------------------ 夹具 ------------------------------


@pytest.fixture(scope="module")
def log_database() -> Iterator[str]:
    """门控库派生的一次性隔离库（迁移到 head，结束即删除）。"""
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevlog{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


@pytest.fixture
async def engine(log_database: str) -> AsyncIterator[AsyncEngine]:
    from sqlalchemy.ext.asyncio import create_async_engine

    created = create_async_engine(log_database)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean_decision_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """每个用例从空表开始（只清判定自有表，不触碰其它资源）。"""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE komari_decision_logs, komari_decision_dispatches,"
                " komari_decision_processes RESTART IDENTITY"
            )
        )
    yield


class RetentionProvider:
    """可控保留期 provider：记录读取次数，可切换天数。"""

    def __init__(self, days: int = 7) -> None:
        self.days = days
        self.calls = 0
        self.failure: Exception | None = None

    def __call__(self) -> int:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.days


def _repository(
    session_factory: async_sessionmaker[AsyncSession],
    retention: RetentionProvider | None = None,
) -> DecisionLogRepository:
    return DecisionLogRepository(
        session_factory,
        retention if retention is not None else RetentionProvider(),
    )


# ------------------------------ 元数据字面量 ------------------------------


def _identity(**overrides: str) -> EventIdentity:
    values = {
        "protocol": "onebot_v11",
        "account_id": "acct-1001",
        "group_id": "group-42",
        "message_id": "msg-9001",
    }
    values.update(overrides)
    return EventIdentity(**values)


def _result(
    decision_id: str,
    *,
    status: DecisionStatus = "success",
    reason_code: DecisionReason = "classified",
    **overrides: Any,
) -> DecisionResult:
    """手写的完整判定结果（3x2 之外的可观测字段全部显式给出）。"""
    values: dict[str, Any] = {
        "decision_id": decision_id,
        "status": status,
        "reason_code": reason_code,
        "route": "chat",
        "force_reply": True,
        "reply_choice": ReplyChoice(
            value="required",
            confidence=0.91,
            probabilities=(("required", 0.91), ("optional", 0.06), ("silent", 0.03)),
        ),
        "retention_choice": RetentionChoice(
            value="candidate",
            confidence=0.72,
            probabilities=(("candidate", 0.72), ("noise", 0.28)),
        ),
        "summary_probability": 0.12,
        "effective_reply": "required",
        "retain_recent": True,
        "memory_candidate": True,
        "optional_selected": None,
        "references": ReferenceSummary(
            complete=False,
            resolved_count=2,
            missing_count=1,
            cycle_count=1,
            timed_out=True,
        ),
        "durations": PhaseDurations(
            queue_ms=12.5,
            references_ms=None,
            request_ms=310.25,
        ),
        "usage": DecisionUsage(input_tokens=None, output_tokens=None),
        "config_revision": 7,
        "prompt_revision": 3,
        "requested_model": "jev-requested-1.14.0",
        "actual_model": "jev-actual-1.14.1",
    }
    values.update(overrides)
    return DecisionResult(**values)


def _metadata(
    *,
    decision_id: str = _ID_A,
    identity: EventIdentity | None = None,
    result: DecisionResult | None = None,
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> DecisionLogMetadata:
    created = created_at or datetime.now(UTC) - timedelta(minutes=5)
    return DecisionLogMetadata(
        decision_id=decision_id,
        event=identity or _identity(),
        created_at=created,
        finished_at=finished_at or created + timedelta(seconds=1),
        result=result or _result(decision_id),
    )


# ------------------------------ 外部 SQL 观测 ------------------------------


async def _raw_log_row(engine: AsyncEngine, decision_id: str) -> dict[str, Any] | None:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT * FROM komari_decision_logs WHERE id = :id"),
                    {"id": decision_id},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _raw_log_text(engine: AsyncEngine, decision_id: str) -> str:
    """整行 JSON 文本：用于证明任何列都没有残留原始凭据/媒体。"""
    async with engine.connect() as connection:
        raw = await connection.scalar(
            text(
                "SELECT to_jsonb(komari_decision_logs)::text"
                " FROM komari_decision_logs WHERE id = :id"
            ),
            {"id": decision_id},
        )
    assert raw is not None, "外部 SQL 未读到已落库的行"
    return str(raw)


async def _raw_count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


async def _raw_log_ids(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(text("SELECT id FROM komari_decision_logs"))
        ).scalars()
    return sorted(str(row) for row in rows)


async def _insert_dispatch(engine: AsyncEngine, decision_id: str) -> int:
    """外部写入一条事件占位行（去重身份），返回其 id。"""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO komari_decision_processes (id) VALUES (1)"
                " ON CONFLICT (id) DO NOTHING"
            )
        )
        dispatch_row_id = await connection.scalar(
            text(
                "INSERT INTO komari_decision_dispatches"
                " (decision_id, protocol, account_id, group_id, message_id,"
                "  owner_process_id, dispatch_id, generation, state, reason_code,"
                "  enqueued_at, queue_deadline)"
                " VALUES (:decision_id, 'onebot_v11', 'acct-1001', 'group-42',"
                "  :message_id, 1, :dispatch_id, 0, 'success', 'classified',"
                "  now(), now())"
                " RETURNING id"
            ),
            {
                "decision_id": decision_id,
                "message_id": f"msg-{decision_id}",
                "dispatch_id": str(uuid4()),
            },
        )
    assert dispatch_row_id is not None
    return int(dispatch_row_id)


# ------------------------------ 往返与投影 ------------------------------


async def test_complete_metadata_and_body_roundtrip(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：完整类型化元数据/正文往返；缺失用量保持 null；请求/实际模型分开。"""
    repository = _repository(session_factory)
    metadata = _metadata()
    body = {
        "state": {"target": {"message_id": "msg-9001", "text": "今天谁值班？"}},
        "response": {"answers": {"reply_eligibility": "required"}},
    }

    await repository.record(metadata, body=body)

    stored = await repository.get_metadata(metadata.decision_id)
    assert stored == metadata
    assert stored is not None
    assert stored.result.usage.input_tokens is None
    assert stored.result.usage.output_tokens is None
    assert stored.result.requested_model == "jev-requested-1.14.0"
    assert stored.result.actual_model == "jev-actual-1.14.1"

    captured = await repository.get_body(metadata.decision_id)
    assert captured == DecisionLogBody(decision_id=metadata.decision_id, body=body)

    # 外部 SQL：details 只有 DecisionResult 字段，没有请求/响应转储或预览字段。
    raw = await _raw_log_row(engine, metadata.decision_id)
    assert raw is not None
    assert raw["body"] == body
    assert set(raw["details"]) == {
        "decision_id",
        "status",
        "reason_code",
        "route",
        "force_reply",
        "reply_choice",
        "retention_choice",
        "summary_probability",
        "effective_reply",
        "retain_recent",
        "memory_candidate",
        "optional_selected",
        "references",
        "durations",
        "usage",
        "config_revision",
        "prompt_revision",
        "requested_model",
        "actual_model",
    }
    assert raw["status"] == "success"
    assert raw["reason_code"] == "classified"
    assert raw["requested_model"] == "jev-requested-1.14.0"
    assert raw["actual_model"] == "jev-actual-1.14.1"


async def test_uppercase_decision_id_is_normalized_and_roundtrips(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：非规范大小写的 UUID 被规范化，细节行 id 与专用列始终一致。"""
    repository = _repository(session_factory)
    uppercase = _ID_A.upper()
    await repository.record(
        _metadata(decision_id=uppercase, result=_result(uppercase)),
        body={"state": "x"},
    )

    stored = await repository.get_metadata(_ID_A)
    assert stored is not None
    assert stored.decision_id == _ID_A
    captured = await repository.get_body(uppercase)
    assert captured == DecisionLogBody(decision_id=_ID_A, body={"state": "x"})
    assert await repository.get_body(_ID_A) == captured


async def test_configured_and_regex_and_media_secrets_are_sanitized_in_persisted_row(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：配置秘密、正则凭据与媒体载荷在落库正文中脱敏（外部 SQL 断言）。"""
    configured = "configured-secret-9f8e7d6c5b4a"
    repository = _repository(session_factory)
    metadata = _metadata(
        identity=_identity(message_id=f"msg-{configured}"),
    )
    media_payload = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    body = {
        "request": {"api_key": configured, "text": "今天谁值班？"},
        "note": f"作者备注 password=hunter2hunter2 以及 {configured}",
        "image": {
            "type": "image",
            "image_url": {"url": f"data:image/png;base64,{media_payload}"},
        },
        "payload_base64": media_payload,
    }

    await repository.record(metadata, body=body, known_secrets=(configured,))

    raw_text = await _raw_log_text(engine, metadata.decision_id)
    assert configured not in raw_text
    assert "hunter2hunter2" not in raw_text
    assert media_payload not in raw_text
    assert _REDACTED in raw_text
    assert "redacted_binary" in raw_text

    captured = await repository.get_body(metadata.decision_id)
    assert captured is not None
    assert captured.body is not None
    persisted = captured.body
    assert persisted["request"]["api_key"] == _REDACTED
    assert persisted["request"]["text"] == "今天谁值班？"
    assert configured not in str(persisted)
    assert persisted["image"]["image_url"]["url"]["redacted_binary"] is True
    assert persisted["payload_base64"]["redacted_binary"] is True

    stored = await repository.get_metadata(metadata.decision_id)
    assert stored is not None
    assert configured not in stored.event.message_id


async def test_filtered_and_duplicate_statuses_force_null_body(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：过滤/重投事件即使误传正文也只落安全元数据。"""
    repository = _repository(session_factory)

    cases: tuple[tuple[str, DecisionStatus, DecisionReason], ...] = (
        (_ID_A, "filtered", "filtered"),
        (_ID_B, "duplicate", "duplicate_event"),
    )
    for decision_id, row_status, reason_code in cases:
        metadata = _metadata(
            decision_id=decision_id,
            result=_result(
                decision_id,
                status=row_status,
                reason_code=reason_code,
                route="none",
                force_reply=False,
                reply_choice=None,
                retention_choice=None,
                summary_probability=None,
                effective_reply=None,
                retain_recent=None,
                memory_candidate=None,
            ),
        )
        await repository.record(
            metadata, body={"should": "not be stored"}, known_secrets=()
        )
        raw = await _raw_log_row(engine, decision_id)
        assert raw is not None
        assert raw["body"] is None
        captured = await repository.get_body(decision_id)
        assert captured == DecisionLogBody(decision_id=decision_id, body=None)


async def test_repeated_record_cannot_overwrite_observation_or_body(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：同 decision_id 重复记录只保留第一次观测与正文。"""
    repository = _repository(session_factory)
    first = _metadata()
    await repository.record(first, body={"attempt": "first"})

    second = _metadata(
        identity=_identity(message_id="msg-other", group_id="group-99"),
        result=_result(
            _ID_A,
            status="failed",
            reason_code="provider_error",
            route="none",
            force_reply=False,
            reply_choice=None,
            retention_choice=None,
            summary_probability=None,
            effective_reply=None,
            retain_recent=None,
            memory_candidate=None,
            requested_model="other-requested",
            actual_model="other-actual",
        ),
        created_at=datetime.now(UTC),
    )
    await repository.record(second, body={"attempt": "second"})

    stored = await repository.get_metadata(_ID_A)
    assert stored == first
    raw = await _raw_log_row(engine, _ID_A)
    assert raw is not None
    assert raw["body"] == {"attempt": "first"}
    assert raw["message_id"] == "msg-9001"
    assert raw["status"] == "success"
    assert await _raw_count(engine, "komari_decision_logs") == 1


async def test_metadata_queries_never_select_body(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：列表/详情 SQL 只取显式元数据列，正文只能由独立路径读取。"""
    repository = _repository(session_factory)
    await repository.record(_metadata(), body={"state": {"target": "正文"}})

    statements: list[str] = []

    def _capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        page = await repository.list_metadata()
        detail = await repository.get_metadata(_ID_A)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert page.total == 1
    assert detail is not None
    assert statements, "元数据查询必须真的发出 SQL"
    for statement in statements:
        assert "body" not in statement.lower(), statement
    assert any(
        "komari_decision_logs.id" in statement
        and "komari_decision_logs.details" in statement
        for statement in statements
    )
    assert not any("komari_decision_logs.*" in statement for statement in statements)

    # 正向对照：正文路径确实会被观察器捕获（证明上面的断言不是空转）。
    body_statements: list[str] = []

    def _capture_body(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        body_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture_body)
    try:
        captured = await repository.get_body(_ID_A)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture_body)
    assert captured == DecisionLogBody(decision_id=_ID_A, body={"state": {"target": "正文"}})
    assert any("body" in statement.lower() for statement in body_statements)


# ------------------------------ 分页与过滤 ------------------------------


async def test_pagination_filters_and_deterministic_tie_break(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：分页、每一种过滤、以及同一时间的 id 升序确定性。"""
    repository = _repository(session_factory)
    base = datetime.now(UTC) - timedelta(minutes=10)
    rows: tuple[
        tuple[str, datetime, DecisionStatus, DecisionReason, str | None, str | None],
        ...,
    ] = (
        (_ID_A, base, "success", "classified", "req-a", "act-a"),
        (_ID_B, base, "filtered", "filtered", None, None),
        (
            _ID_C,
            base - timedelta(minutes=1),
            "failed",
            "provider_error",
            "req-shared",
            None,
        ),
        (_ID_D, base - timedelta(minutes=2), "success", "classified", None, "act-shared"),
        (_ID_E, base - timedelta(minutes=3), "bypass", "platform_bypass", None, None),
    )
    for decision_id, created_at, row_status, reason_code, requested, actual in rows:
        await repository.record(
            _metadata(
                decision_id=decision_id,
                identity=_identity(message_id=f"msg-{decision_id[-1]}"),
                result=_result(
                    decision_id,
                    status=row_status,
                    reason_code=reason_code,
                    requested_model=requested,
                    actual_model=actual,
                ),
                created_at=created_at,
                finished_at=created_at + timedelta(seconds=1),
            ),
            body=None,
        )

    first = await repository.list_metadata()
    assert (first.total, first.page, first.page_size) == (5, 1, 50)
    assert [item.decision_id for item in first.items] == [
        _ID_A,
        _ID_B,
        _ID_C,
        _ID_D,
        _ID_E,
    ]

    second_page = await repository.list_metadata(page=2, page_size=2)
    assert [item.decision_id for item in second_page.items] == [_ID_C, _ID_D]
    assert second_page.total == 5
    third_page = await repository.list_metadata(page=3, page_size=2)
    assert [item.decision_id for item in third_page.items] == [_ID_E]
    empty_page = await repository.list_metadata(page=4, page_size=2)
    assert empty_page == DecisionLogPage(items=(), total=5, page=4, page_size=2)

    by_status = await repository.list_metadata(status="success")
    assert [item.decision_id for item in by_status.items] == [_ID_A, _ID_D]

    by_requested = await repository.list_metadata(model="req-shared")
    assert [item.decision_id for item in by_requested.items] == [_ID_C]
    by_actual = await repository.list_metadata(model="act-shared")
    assert [item.decision_id for item in by_actual.items] == [_ID_D]

    lower_bound = await repository.list_metadata(
        created_from=base - timedelta(minutes=2)
    )
    assert [item.decision_id for item in lower_bound.items] == [
        _ID_A,
        _ID_B,
        _ID_C,
        _ID_D,
    ]
    upper_bound = await repository.list_metadata(
        created_before=base - timedelta(minutes=1)
    )
    assert [item.decision_id for item in upper_bound.items] == [_ID_D, _ID_E]

    combined = await repository.list_metadata(
        status="success",
        model="act-shared",
        created_from=base - timedelta(minutes=3),
        created_before=base,
    )
    assert [item.decision_id for item in combined.items] == [_ID_D]
    assert combined.total == 1


# ------------------------------ 保留期 ------------------------------


async def test_current_retention_hides_and_restores_unpruned_rows(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：当前保留期收缩/放宽时未清理行的可见性随之变化。"""
    retention = RetentionProvider(7)
    repository = _repository(session_factory, retention)
    old_created = datetime.now(UTC) - timedelta(days=10)
    await repository.record(
        _metadata(
            decision_id=_ID_A,
            created_at=old_created,
            finished_at=old_created + timedelta(seconds=1),
        ),
        body={"state": "旧的正文"},
    )
    await repository.record(_metadata(decision_id=_ID_B), body={"state": "新正文"})

    assert retention.calls == 0
    short = await repository.list_metadata()
    assert [item.decision_id for item in short.items] == [_ID_B]
    assert short.total == 1
    assert await repository.get_metadata(_ID_A) is None
    assert await repository.get_body(_ID_A) is None
    assert retention.calls > 0
    # 过期行在物理清理前仍留在表里：不可见不等于已删除。
    assert await _raw_count(engine, "komari_decision_logs") == 2

    retention.days = 30
    reopened = await repository.list_metadata()
    assert [item.decision_id for item in reopened.items] == [_ID_B, _ID_A]
    assert reopened.total == 2
    detail = await repository.get_metadata(_ID_A)
    assert detail is not None
    assert detail.created_at == old_created
    captured = await repository.get_body(_ID_A)
    assert captured == DecisionLogBody(decision_id=_ID_A, body={"state": "旧的正文"})


async def test_exact_retention_boundary_row_stays_visible(
    engine: AsyncEngine,
) -> None:
    """AC：created_at 正好等于保留期下界仍可见（同一数据库事务内取时间）。"""
    retention = RetentionProvider(7)
    async with engine.connect() as connection:
        outer = await connection.begin()
        try:
            cutoff = await connection.scalar(
                text("SELECT CURRENT_TIMESTAMP - make_interval(0, 0, 0, 7)")
            )
            assert isinstance(cutoff, datetime)

            def _bound_factory() -> AsyncSession:
                from sqlalchemy.ext.asyncio import AsyncSession as SessionClass

                return SessionClass(bind=connection, expire_on_commit=False)

            repository = DecisionLogRepository(_bound_factory, retention)
            await repository.record(
                _metadata(
                    decision_id=_ID_A,
                    created_at=cutoff,
                    finished_at=cutoff,
                ),
                body={"boundary": "exact"},
            )
            await repository.record(
                _metadata(
                    decision_id=_ID_B,
                    created_at=cutoff - timedelta(microseconds=1),
                    finished_at=cutoff,
                ),
                body={"boundary": "just_below"},
            )

            page = await repository.list_metadata()
            assert [item.decision_id for item in page.items] == [_ID_A]
            assert page.total == 1
            visible = await repository.get_metadata(_ID_A)
            assert visible is not None
            assert visible.created_at == cutoff
            assert await repository.get_metadata(_ID_B) is None
            boundary_body = await repository.get_body(_ID_A)
            assert boundary_body == DecisionLogBody(
                decision_id=_ID_A, body={"boundary": "exact"}
            )
            assert await repository.get_body(_ID_B) is None
        finally:
            await outer.rollback()


# ------------------------------ 清理 ------------------------------


async def test_prune_removes_only_expired_logs_in_bounded_batches(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：清理只删过期日志、保留去重身份，且按批次上限删除。"""
    repository = _repository(session_factory, RetentionProvider(7))
    dispatch_row_id = await _insert_dispatch(engine, _ID_A)
    expired_created = datetime.now(UTC) - timedelta(days=10)
    await repository.record(
        _metadata(
            decision_id=_ID_A,
            created_at=expired_created,
            finished_at=expired_created + timedelta(seconds=1),
        ),
        body={"state": "过期正文"},
    )
    raw = await _raw_log_row(engine, _ID_A)
    assert raw is not None
    assert raw["dispatch_row_id"] == dispatch_row_id

    for decision_id in (_ID_B, _ID_C):
        await repository.record(
            _metadata(
                decision_id=decision_id,
                identity=_identity(message_id=f"msg-{decision_id[-1]}"),
                created_at=expired_created,
                finished_at=expired_created + timedelta(seconds=1),
            ),
            body={"state": "过期正文"},
        )
    await repository.record(_metadata(decision_id=_ID_D), body={"state": "新正文"})

    assert await _raw_count(engine, "komari_decision_logs") == 4
    assert await repository.prune(limit=2) == 2
    assert await _raw_count(engine, "komari_decision_logs") == 2
    # 未清理的过期行仍然不可见。
    page = await repository.list_metadata()
    assert [item.decision_id for item in page.items] == [_ID_D]

    assert await repository.prune(limit=2) == 1
    assert await _raw_count(engine, "komari_decision_logs") == 1
    assert await repository.prune(limit=2) == 0

    # 去重身份与进程行绝不被清理删除。
    assert await _raw_count(engine, "komari_decision_dispatches") == 1
    assert await _raw_count(engine, "komari_decision_processes") == 1
    async with engine.connect() as connection:
        remaining_decision_id = await connection.scalar(
            text("SELECT decision_id FROM komari_decision_dispatches")
        )
    assert str(remaining_decision_id) == _ID_A


# ------------------------------ 失败面 ------------------------------


async def test_storage_failure_raises_unavailable_never_empty_results(
    log_database: str,
) -> None:
    """AC：存储不可用统一抛固定文案异常，绝不返回空页/不存在。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    missing_database = log_database.rsplit("/", 1)[0] + "/komari_missing_log_db"
    broken_engine = create_async_engine(missing_database)
    try:
        factory = async_sessionmaker(broken_engine, expire_on_commit=False)
        repository = DecisionLogRepository(factory, RetentionProvider(7))
        metadata = _metadata()

        with pytest.raises(DecisionLogStorageUnavailable) as list_error:
            await repository.list_metadata()
        with pytest.raises(DecisionLogStorageUnavailable):
            await repository.get_metadata(_ID_A)
        with pytest.raises(DecisionLogStorageUnavailable):
            await repository.get_body(_ID_A)
        with pytest.raises(DecisionLogStorageUnavailable):
            await repository.record(metadata, body={"state": "x"})
        with pytest.raises(DecisionLogStorageUnavailable):
            await repository.prune(limit=10)

        message = str(list_error.value)
        assert message == STORAGE_UNAVAILABLE_MESSAGE
        assert "komari_decision_logs" not in message
        assert "SELECT" not in message
    finally:
        await broken_engine.dispose()


async def test_invalid_retention_provider_never_falls_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：保留期不可用或越界都不静默回退默认值。"""
    for days in (0, 31, -1):
        retention = RetentionProvider(days)
        repository = _repository(session_factory, retention)
        with pytest.raises(DecisionLogRetentionError):
            await repository.list_metadata()
        with pytest.raises(DecisionLogRetentionError):
            await repository.get_metadata(_ID_A)
        with pytest.raises(DecisionLogRetentionError):
            await repository.prune(limit=1)
        assert retention.calls > 0

    failing = RetentionProvider()
    failing.failure = RuntimeError("保留期来源不可用")
    with pytest.raises(DecisionLogRetentionError):
        await _repository(session_factory, failing).list_metadata()

    def _non_integer() -> int:
        return "7"  # type: ignore[return-value]

    with pytest.raises(DecisionLogRetentionError):
        await DecisionLogRepository(session_factory, _non_integer).list_metadata()


async def test_invalid_query_and_metadata_arguments_are_rejected(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：非法分页/时间/批次与非法元数据一律显式拒绝。"""
    repository = _repository(session_factory)

    with pytest.raises(DecisionLogRequestError):
        await repository.list_metadata(page=0)
    with pytest.raises(DecisionLogRequestError):
        await repository.list_metadata(page_size=0)
    with pytest.raises(DecisionLogRequestError):
        await repository.list_metadata(page_size=101)
    with pytest.raises(DecisionLogRequestError):
        # 无时区时间必须被拒绝（测试对象本身就是无时区输入）。
        await repository.list_metadata(
            created_from=datetime(2026, 1, 1)  # noqa: DTZ001
        )
    with pytest.raises(DecisionLogRequestError):
        await repository.list_metadata(
            created_from=datetime.now(UTC),
            created_before=datetime.now(UTC) - timedelta(days=1),
        )
    with pytest.raises(DecisionLogRequestError):
        await repository.list_metadata(status="not-a-status")  # type: ignore[arg-type]
    with pytest.raises(DecisionLogRequestError):
        await repository.prune(limit=0)

    mismatched = _metadata(result=_result(_ID_B))
    with pytest.raises(DecisionLogRequestError):
        await repository.record(mismatched, body=None)
    malformed = _metadata(decision_id="not-a-uuid")
    with pytest.raises(DecisionLogRequestError):
        await repository.record(malformed, body=None)
    assert await _raw_count(engine, "komari_decision_logs") == 0


async def test_corrupt_or_foreign_details_are_rejected_not_returned(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：details 出现未知字段或与他列不一致时拒绝，绝不返回任意 JSON。"""
    repository = _repository(session_factory)
    await repository.record(_metadata(), body={"state": "x"})

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE komari_decision_logs"
                " SET details = details || '{\"preview\": \"泄露预览\"}'::jsonb"
                " WHERE id = :id"
            ),
            {"id": _ID_A},
        )

    with pytest.raises(DecisionLogCorruptRowError):
        await repository.get_metadata(_ID_A)
    with pytest.raises(DecisionLogCorruptRowError):
        await repository.list_metadata()

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE komari_decision_logs"
                " SET details = (details - 'preview')"
                " || jsonb_build_object('decision_id', CAST(:other AS text))"
                " WHERE id = :id"
            ),
            {"id": _ID_A, "other": _ID_B},
        )
    with pytest.raises(DecisionLogCorruptRowError):
        await repository.get_metadata(_ID_A)

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE komari_decision_logs"
                " SET details = (details - 'preview')"
                " || jsonb_build_object('decision_id', CAST(:id AS text)),"
                " status = 'failed'"
                " WHERE id = :id"
            ),
            {"id": _ID_A},
        )
    with pytest.raises(DecisionLogCorruptRowError):
        await repository.get_metadata(_ID_A)


async def test_dispatch_identity_is_optional_and_never_derived_body(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC：dispatch_row_id 只按 decision_id 反查；未登记时为 NULL 且正文不派生。"""
    repository = _repository(session_factory)
    await repository.record(_metadata(), body=None)

    raw = await _raw_log_row(engine, _ID_A)
    assert raw is not None
    assert raw["dispatch_row_id"] is None
    assert raw["body"] is None
    assert await _raw_count(engine, "komari_decision_dispatches") == 0
    captured = await repository.get_body(_ID_A)
    assert captured == DecisionLogBody(decision_id=_ID_A, body=None)

    dispatch_row_id = await _insert_dispatch(engine, _ID_B)
    await repository.record(
        _metadata(
            decision_id=_ID_B,
            identity=_identity(message_id="msg-b"),
        ),
        body={"state": "外部正文"},
    )
    linked = await _raw_log_row(engine, _ID_B)
    assert linked is not None
    assert linked["dispatch_row_id"] == dispatch_row_id
