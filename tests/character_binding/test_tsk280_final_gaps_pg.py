"""TSK-280 最终缺口 RED：成员范围确认审计哈希与提交后快照安全失效。

本文件固定独立双轴 review 找到的两个最后确定缺陷，全部经真实 FastAPI
路由 + 真实 ``BindingRepairService`` + 真实 PostgreSQL 与既有绑定管理器
观察，不注入虚构服务接口：

1. ``confirm`` 路由只按请求计算群哈希，仅在成功后用响应成员覆盖；合法
   成员令牌的 ``started`` 与 ``failed``（依赖变动 409）事件因此携带群哈希，
   且 ``started`` 丢失 version/expected_count。修复要求业务前做纯只读令牌
   审计上下文验证（不消耗令牌）：``started``/``succeeded`` 与
   ``started``/``failed`` 都必须携带真实成员哈希及 version/expected_count；
   无效令牌（含他人操作者令牌）只能退化为请求群安全哈希，绝不泄露他人
   目标；``started`` 审计失败不消耗令牌、不删除，可再次确认。
2. 正式删除已提交后 ``manager.refresh_snapshot`` 失败仍按失败上报 503 并
   保留已删成员缓存。修复要求确定提交后的缓存安全失效：确认成功、正式行
   删除、双协议快照移除受影响关系且未受影响成员/群保留、令牌重试不重复
   删除；不新增持久化后台重试或 Schema。
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from komari_bot.plugins.character_binding import management_api
from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.character_binding.repair import (
    BindingRepairService,
    get_binding_repair_service,
    set_binding_repair_service,
)
from tests.character_binding.tsk280_support import (
    PG_REQUIRED,
    WILDCARD_CREDENTIALS,
    bind_member,
    create_engine_and_factory,
    group_binding_rows,
    group_mapping_rows,
    make_game_state_reader,
    member_rows,
    reset_shared_orm_engine,
    safe_target_hash,
    write_headers,
)
from tests.character_binding.tsk280_support import (
    scope as make_scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from nonebug import App
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from komari_bot.management.management_audit import ManagementAuditEvent

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

TWO_MANAGE_CREDENTIALS = [
    {
        "credential_id": "binding-operator-a",
        "token": "operator-a-token-0001",
        "permissions": ["character_binding:manage"],
    },
    {
        "credential_id": "binding-operator-b",
        "token": "operator-b-token-0001",
        "permissions": ["character_binding:manage"],
    },
]


@dataclass(frozen=True, slots=True)
class Harness:
    """与 ``test_tsk280_pg`` 同款：独立真实引擎 + 会话工厂 + 绑定管理器。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    binding_manager: CharacterBindingManager


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for engine, session_factory in create_engine_and_factory():
        manager = CharacterBindingManager()
        await manager.initialize()
        try:
            yield Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()


def _make_service(
    harness: Harness,
) -> BindingRepairService:
    return BindingRepairService(
        session_factory=harness.session_factory,
        clock=lambda: datetime.now(UTC),
        game_state_reader=make_game_state_reader(),
        manager=harness.binding_manager,
    )


def _record_into(
    events: list[ManagementAuditEvent],
) -> Callable[[ManagementAuditEvent], Awaitable[None]]:
    async def _record(event: ManagementAuditEvent) -> None:
        events.append(event)

    return _record


def _register(
    repair_app: FastAPI,
    recorder: Callable[[ManagementAuditEvent], Awaitable[None]],
    *,
    api_token: list[dict[str, object]] = WILDCARD_CREDENTIALS,
) -> None:
    management_api.register_character_binding_repair_api(
        repair_app,
        api_token=api_token,
        allowed_origins=[],
        service_getter=get_binding_repair_service,
        audit_recorder=recorder,
    )


def _confirm_events(
    events: list[ManagementAuditEvent],
) -> list[ManagementAuditEvent]:
    return [
        event for event in events if event.action == "character_binding.repair.confirm"
    ]


def _only(events: list[ManagementAuditEvent], outcome: str) -> ManagementAuditEvent:
    matches = [event for event in events if event.outcome == outcome]
    assert len(matches) == 1, [event.outcome for event in events]
    return matches[0]


def _assert_no_raw_ids(
    events: list[ManagementAuditEvent],
    raw_values: tuple[str, ...],
) -> None:
    rendered = json.dumps(
        [event.to_dict() for event in events],
        ensure_ascii=False,
        sort_keys=True,
    )
    for value in raw_values:
        assert value not in rendered


# ─────────────────────────── 缺陷 1：成员哈希审计 ───────────────────────────


async def test_member_confirm_audit_has_member_hash_on_started_and_succeeded(
    harness: Harness,
    app: App,
) -> None:
    """合法成员令牌：started 与 succeeded 都必须携带真实成员哈希与 version。"""
    current = make_scope("final-audit-ok")
    member = current.with_member(1)
    await bind_member(harness.binding_manager, current, 1, name="甲")

    audit_events: list[ManagementAuditEvent] = []
    service = _make_service(harness)
    set_binding_repair_service(service)
    try:
        repair_app = FastAPI()
        _register(repair_app, _record_into(audit_events))
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(request_id="final-audit-ok-preview"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "member_openid": member.member_openid,
                },
            )
            version = preview.json()["version"]
            confirm = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-audit-ok-confirm"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
    finally:
        set_binding_repair_service(None)

    assert preview.status_code == 200
    assert confirm.status_code == 200
    confirm_events = _confirm_events(audit_events)
    started = _only(confirm_events, "started")
    succeeded = _only(confirm_events, "succeeded")
    member_hash = safe_target_hash(current, member_openid=member.member_openid)
    assert started.target_hash == member_hash
    assert started.metadata["scope"] == "member"
    assert started.metadata["version"] == version
    assert started.metadata["expected_count"] == 1
    assert succeeded.target_hash == member_hash
    assert succeeded.metadata["version"] == version
    assert succeeded.metadata["expected_count"] == 1
    assert succeeded.metadata["cleared_count"] == 1
    _assert_no_raw_ids(
        audit_events,
        (current.app_id, current.group_openid, member.member_openid, member.member_qq),
    )


async def test_confirm_dependency_change_audit_has_member_hash_on_started_and_failed(
    harness: Harness,
    app: App,
) -> None:
    """合法成员令牌但依赖变动 409：started 与 failed 都必须携带成员哈希。"""
    current = make_scope("final-audit-drift")
    member = current.with_member(1)
    await bind_member(harness.binding_manager, current, 1, name="甲")

    audit_events: list[ManagementAuditEvent] = []
    service = _make_service(harness)
    set_binding_repair_service(service)
    try:
        repair_app = FastAPI()
        _register(repair_app, _record_into(audit_events))
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(request_id="final-audit-drift-preview"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "member_openid": member.member_openid,
                },
            )
            version = preview.json()["version"]
            async with harness.engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE komari_character_binding_members
                           SET character_name = '改名后'
                         WHERE app_id = :app_id
                           AND group_openid = :group_openid
                           AND member_openid = :member_openid
                        """
                    ),
                    {
                        "app_id": current.app_id,
                        "group_openid": current.group_openid,
                        "member_openid": member.member_openid,
                    },
                )
            confirm = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-audit-drift-confirm"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
    finally:
        set_binding_repair_service(None)

    assert preview.status_code == 200
    assert confirm.status_code == 409
    confirm_events = _confirm_events(audit_events)
    started = _only(confirm_events, "started")
    failed = _only(confirm_events, "failed")
    member_hash = safe_target_hash(current, member_openid=member.member_openid)
    assert started.target_hash == member_hash
    assert started.metadata["scope"] == "member"
    assert started.metadata["version"] == version
    assert started.metadata["expected_count"] == 1
    assert failed.target_hash == member_hash
    assert failed.metadata["version"] == version
    assert failed.metadata["expected_count"] == 1
    _assert_no_raw_ids(
        audit_events,
        (current.app_id, current.group_openid, member.member_openid, member.member_qq),
    )
    # 依赖变动后正式行必须保留，只有审计被记录。
    async with harness.session_factory() as session:
        rows = await member_rows(session, current)
    assert [row[0] for row in rows] == [member.member_openid]


async def test_other_operator_token_audit_falls_back_to_request_group_hash(
    harness: Harness,
    app: App,
) -> None:
    """无效令牌（他人操作者）不得泄露目标：started/failed 只用请求群哈希。"""
    current = make_scope("final-audit-foreign")
    member = current.with_member(1)
    await bind_member(harness.binding_manager, current, 1, name="甲")

    audit_events: list[ManagementAuditEvent] = []
    service = _make_service(harness)
    set_binding_repair_service(service)
    try:
        repair_app = FastAPI()
        _register(
            repair_app,
            _record_into(audit_events),
            api_token=TWO_MANAGE_CREDENTIALS,
        )
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(
                    request_id="final-audit-foreign-preview",
                    token="operator-a-token-0001",
                ),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "member_openid": member.member_openid,
                },
            )
            confirm = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(
                    request_id="final-audit-foreign-confirm",
                    token="operator-b-token-0001",
                ),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
    finally:
        set_binding_repair_service(None)

    assert preview.status_code == 200
    assert confirm.status_code == 422
    confirm_events = _confirm_events(audit_events)
    started = _only(confirm_events, "started")
    failed = _only(confirm_events, "failed")
    group_hash = safe_target_hash(current)
    member_hash = safe_target_hash(current, member_openid=member.member_openid)
    assert started.target_hash == group_hash
    assert failed.target_hash == group_hash
    assert started.target_hash != member_hash
    assert failed.target_hash != member_hash
    _assert_no_raw_ids(
        audit_events,
        (current.app_id, current.group_openid, member.member_openid, member.member_qq),
    )
    async with harness.session_factory() as session:
        rows = await member_rows(session, current)
    assert [row[0] for row in rows] == [member.member_openid]


class _FailConfirmStartedRecorder:
    """仅在 ``confirm.started`` 上抛错的审计 recorder（可开关）。"""

    def __init__(self) -> None:
        self.fail_next_confirm_started = False
        self.events: list[ManagementAuditEvent] = []

    async def __call__(self, event: ManagementAuditEvent) -> None:
        is_confirm_started = (
            event.action == "character_binding.repair.confirm"
            and event.outcome == "started"
        )
        if is_confirm_started and self.fail_next_confirm_started:
            self.fail_next_confirm_started = False
            msg = "audit CANARY-不可用"
            raise RuntimeError(msg)
        self.events.append(event)


async def test_confirm_started_audit_failure_keeps_token_and_rows_for_retry(
    harness: Harness,
    app: App,
) -> None:
    """started 审计失败不消耗令牌、不删除；同一令牌再次确认仍可成功。"""
    current = make_scope("final-audit-start-fail")
    member = current.with_member(1)
    await bind_member(harness.binding_manager, current, 1, name="甲")

    recorder = _FailConfirmStartedRecorder()
    service = _make_service(harness)
    set_binding_repair_service(service)
    try:
        repair_app = FastAPI()
        _register(repair_app, recorder)
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(request_id="final-audit-start-preview"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "member_openid": member.member_openid,
                },
            )
            token = preview.json()["token"]
            recorder.fail_next_confirm_started = True
            blocked = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-audit-start-confirm-1"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": token,
                },
            )
            async with harness.session_factory() as session:
                rows_after_block = await member_rows(session, current)
            retried = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-audit-start-confirm-2"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": token,
                },
            )
    finally:
        set_binding_repair_service(None)

    assert preview.status_code == 200
    assert blocked.status_code == 500
    assert [row[0] for row in rows_after_block] == [member.member_openid]
    assert retried.status_code == 200
    assert retried.json()["cleared_count"] == 1
    confirm_events = _confirm_events(recorder.events)
    assert _only(confirm_events, "succeeded").metadata["cleared_count"] == 1
    async with harness.session_factory() as session:
        rows_after_retry = await member_rows(session, current)
    assert rows_after_retry == []


# ──────────────── 缺陷 2：提交后快照安全失效（refresh 失败） ────────────────


def _install_load_all_failure(
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
) -> None:
    async def _fail_load_all() -> list[dict[str, object]]:
        msg = "injected load_all failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(harness.binding_manager._database, "load_all", _fail_load_all)


async def test_committed_member_delete_survives_refresh_failure(
    harness: Harness,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成员级删除提交后 refresh 失败：确认成功、双协议快照移除受影响关系。"""
    current = make_scope("final-cache-member")
    member1 = current.with_member(1)
    member2 = current.with_member(2)
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    other = make_scope("final-cache-other")
    other_member = other.with_member(1)
    await bind_member(harness.binding_manager, other, 1, name="丙")
    _install_load_all_failure(monkeypatch, harness)

    audit_events: list[ManagementAuditEvent] = []
    service = _make_service(harness)
    set_binding_repair_service(service)
    try:
        repair_app = FastAPI()
        _register(repair_app, _record_into(audit_events))
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(request_id="final-cache-member-preview"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "member_openid": member1.member_openid,
                },
            )
            confirm = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-cache-member-confirm"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
            retry = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-cache-member-retry"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
    finally:
        set_binding_repair_service(None)

    # 预览不依赖 load_all（提交前纯读路径）。
    assert preview.status_code == 200
    # 提交已确定：不得因 refresh 失败回报 503/failed。
    assert confirm.status_code == 200
    assert confirm.json()["cleared_count"] == 1
    outcomes = [event.outcome for event in _confirm_events(audit_events)]
    assert outcomes == ["started", "succeeded"]
    # 令牌重试不重复删除也不报存储失败。
    assert retry.status_code == 422

    # 正式库：仅目标成员行被删，群映射与另一成员保留。
    assert await group_binding_rows(harness.engine, current) == 1
    assert await group_mapping_rows(harness.engine, current) == 1
    async with harness.session_factory() as session:
        remaining = await member_rows(session, current)
    assert remaining == [(member2.member_openid, "乙")]

    # 两种协议的进程内快照都必须移除受影响关系，未受影响成员/群保留。
    manager = harness.binding_manager
    canonical = manager.list_group_bindings(
        app_id=current.app_id,
        group_openid=current.group_openid,
    )
    assert {record.member_openid for record in canonical} == {member2.member_openid}
    assert manager.list_onebot_group_bindings(group_id=current.group_id) == {
        member2.member_qq: "乙"
    }
    assert (
        manager.get_qq_character_name(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=member1.member_openid,
        )
        is None
    )
    assert (
        manager.get_character_name(
            group_id=current.group_id,
            user_id=member1.member_qq,
        )
        == member1.member_qq
    )
    other_records = manager.list_group_bindings(
        app_id=other.app_id,
        group_openid=other.group_openid,
    )
    assert {record.member_openid for record in other_records} == {
        other_member.member_openid
    }
    assert manager.list_onebot_group_bindings(group_id=other.group_id) == {
        other_member.member_qq: "丙"
    }


async def test_committed_group_delete_survives_refresh_failure(
    harness: Harness,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """群级删除提交后 refresh 失败：确认成功、双协议快照移除整组关系。"""
    current = make_scope("final-cache-group")
    member1 = current.with_member(1)
    await bind_member(harness.binding_manager, current, 1, name="甲")
    await bind_member(harness.binding_manager, current, 2, name="乙")
    other = make_scope("final-cache-group-other")
    other_member = other.with_member(1)
    await bind_member(harness.binding_manager, other, 1, name="丙")
    _install_load_all_failure(monkeypatch, harness)

    audit_events: list[ManagementAuditEvent] = []
    service = _make_service(harness)
    set_binding_repair_service(service)
    try:
        repair_app = FastAPI()
        _register(repair_app, _record_into(audit_events))
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(request_id="final-cache-group-preview"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            confirm = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-cache-group-confirm"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
            retry = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="final-cache-group-retry"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )
    finally:
        set_binding_repair_service(None)

    assert preview.status_code == 200
    assert confirm.status_code == 200
    assert confirm.json()["cleared_count"] == 2
    outcomes = [event.outcome for event in _confirm_events(audit_events)]
    assert outcomes == ["started", "succeeded"]
    assert retry.status_code == 422

    assert await group_binding_rows(harness.engine, current) == 0
    assert await group_mapping_rows(harness.engine, current) == 0
    async with harness.session_factory() as session:
        remaining = await member_rows(session, current)
    assert remaining == []

    manager = harness.binding_manager
    assert (
        manager.list_group_bindings(
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
        == ()
    )
    assert manager.list_onebot_group_bindings(group_id=current.group_id) == {}
    assert (
        manager.get_qq_character_name(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=member1.member_openid,
        )
        is None
    )
    assert (
        manager.get_character_name(group_id=current.group_id, user_id=member1.member_qq)
        == member1.member_qq
    )
    other_records = manager.list_group_bindings(
        app_id=other.app_id,
        group_openid=other.group_openid,
    )
    assert {record.member_openid for record in other_records} == {
        other_member.member_openid
    }
    assert manager.list_onebot_group_bindings(group_id=other.group_id) == {
        other_member.member_qq: "丙"
    }
