"""TSK-280 真实装配：register → service getter → 真实服务 → 真实 PostgreSQL。

这是 root 要求的「最小真实 REST 接缝」：不注入 FakeService，路由的
``service_getter`` 指向全局注册表 ``get_binding_repair_service``，服务用真实
会话工厂与真实 clock 构造，端到端完成 diagnose → preview → confirm 并验证
数据库不变量与真实审计事件。生产 ``repair`` / ``management_api`` 模块缺失
时本文件为预期 RED（ImportError）。

第二接缝补管理装配缺口：经生产
``komari_management.binding_repair_lifecycle.start_binding_repair_service``
创建服务并从公开 getter 取得，只在公开端口注入（shim 顶层包
``get_binding_manager`` 返回已有真实测试管理器、``nonebot require`` 依赖
加载端口旁路 ``komari_roulette`` 真实插件加载），不 mock 生产
``_read_game_state``、不替换生产存储 reader；waiting 对局由真实轮盘命令
服务落入真实 PG，验证 diagnose 读到 game_present/lifecycle=waiting、
preview 被 RepairBlockedByGameError 阻断且绑定未清除，以及真实
stop_binding_repair_service 后 getter 为 None、旧引用拒绝、二次关闭幂等。
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import nonebot
import nonebot.plugin
import pytest
from fastapi import FastAPI

from komari_bot.plugins.character_binding import management_api
from komari_bot.plugins.character_binding import repair as repair_module
from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.character_binding.repair import (
    BindingRepairService,
    RepairBlockedByGameError,
    get_binding_repair_service,
    set_binding_repair_service,
)
from komari_bot.plugins.komari_management import binding_repair_lifecycle
from tests.character_binding.tsk280_support import (
    PG_REQUIRED,
    WILDCARD_CREDENTIALS,
    clear_binding_scope,
    clear_roulette_scope,
    create_engine_and_factory,
    create_waiting,
    group_binding_rows,
    group_mapping_rows,
    make_game_state_reader,
    make_roulette,
    read_headers,
    reset_shared_orm_engine,
    seed_binding,
    write_headers,
)
from tests.character_binding.tsk280_support import (
    scope as make_scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nonebug import App
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from komari_bot.management.management_audit import ManagementAuditEvent

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]

CHANGE_REASON = "运营核对错误关联"


@dataclass(frozen=True, slots=True)
class Harness:
    """与 test_tsk280_pg 同款：独立真实引擎 + 会话工厂 + 绑定管理器。"""

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


async def test_register_to_service_get_real_assembly(
    harness: Harness,
    app: App,
) -> None:
    """真实装配链：set/get 全局服务 → register 路由 → 真实服务 → 真实 PG。"""
    current = make_scope("assembly")
    await seed_binding(harness.binding_manager, current, 1, name="甲")

    service = BindingRepairService(
        session_factory=harness.session_factory,
        clock=lambda: datetime.now(UTC),
        game_state_reader=make_game_state_reader(),
        manager=harness.binding_manager,
    )
    set_binding_repair_service(service)
    audit_events: list[ManagementAuditEvent] = []

    async def _record(event: ManagementAuditEvent) -> None:
        audit_events.append(event)

    try:
        repair_app = FastAPI()
        management_api.register_character_binding_repair_api(
            repair_app,
            api_token=WILDCARD_CREDENTIALS,
            allowed_origins=[],
            service_getter=get_binding_repair_service,
            audit_recorder=_record,
        )
        async with app.test_server(asgi=cast("Any", repair_app)) as ctx:
            client = ctx.get_client()
            diagnose = await client.get(
                f"{management_api.API_PREFIX}/diagnose"
                f"?app_id={current.app_id}&group_openid={current.group_openid}",
                headers=read_headers("wildcard-token-00000"),
            )
            preview = await client.post(
                f"{management_api.API_PREFIX}/preview",
                headers=write_headers(request_id="assembly-preview"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            confirm = await client.post(
                f"{management_api.API_PREFIX}/confirm",
                headers=write_headers(request_id="assembly-confirm"),
                json={
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "token": preview.json()["token"],
                },
            )

        assert diagnose.status_code == 200
        body = diagnose.json()
        assert body["app_id"] == current.app_id
        assert body["group_openid"] == current.group_openid
        assert len(body["members"]) == 1
        assert body["members"][0]["character_name"] == "甲"
        assert body["game_present"] is False

        assert preview.status_code == 200
        assert preview.json()["affected_count"] == 1
        assert preview.json()["cleared_names"] == ["甲"]

        assert confirm.status_code == 200
        assert confirm.json()["cleared_count"] == 1
        assert confirm.json()["cleared_names"] == ["甲"]

        # 数据库不变量：真实删除生效（群级语义：群映射一并清除）。
        assert await group_binding_rows(harness.engine, current) == 0
        assert await group_mapping_rows(harness.engine, current) == 0

        # 审计真实记录 preview/confirm 的 started/succeeded 事件。
        actions = [event.action for event in audit_events]
        assert actions.count("character_binding.repair.preview") == 2
        assert actions.count("character_binding.repair.confirm") == 2
        assert {event.outcome for event in audit_events} == {"started", "succeeded"}
        confirm_audit = [
            event
            for event in audit_events
            if event.action == "character_binding.repair.confirm"
            and event.outcome == "succeeded"
        ]
        assert len(confirm_audit) == 1
        # 真实确认审计必须携带预览 version 与预期/实删数量。
        assert confirm_audit[0].metadata["version"] == preview.json()["version"]
        assert confirm_audit[0].metadata["expected_count"] == 1
        assert confirm_audit[0].metadata["cleared_count"] == 1
        rendered = json.dumps(
            [event.to_dict() for event in audit_events],
            ensure_ascii=False,
            sort_keys=True,
        )
        assert current.app_id not in rendered
        assert current.group_openid not in rendered
        assert current.member_openid not in rendered
    finally:
        set_binding_repair_service(None)
        await reset_shared_orm_engine()


async def test_management_lifecycle_assembly_with_real_game_reader(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产管理装配创建的服务经真实 reader 直读 PG；真实关闭后旧引用拒绝。

    与手工构造的 ``test_register_to_service_get_real_assembly`` 互补：本用例的
    服务由生产 ``start_binding_repair_service`` 创建、经公开
    ``get_binding_repair_service`` 取得。注入只在公开端口：shim 顶层包的
    ``get_binding_manager`` 返回已有真实测试管理器；``nonebot require`` 依赖
    加载端口旁路 ``komari_roulette`` 真实插件加载（避免测试进程启动副作用），
    其余依赖（含 ``nonebot_plugin_orm``）走真实加载。生产 ``_read_game_state``
    与其真实 ``PostgresRouletteStorage.load_current`` reader 不做任何替换。
    """
    current = make_scope("assembly-lifecycle")
    await seed_binding(harness.binding_manager, current, 1, name="甲")
    roulette = make_roulette(harness.session_factory)
    await create_waiting(roulette, current, current.with_member(1).member_openid)

    import komari_bot.plugins.character_binding as binding_package

    # 镜像生产顶层暴露面：shim 包注入真实修复服务导出与真实测试管理器。
    for name in (
        "BindingRepairService",
        "get_binding_repair_service",
        "set_binding_repair_service",
    ):
        monkeypatch.setattr(
            binding_package, name, getattr(repair_module, name), raising=False
        )
    monkeypatch.setattr(
        binding_package, "get_binding_manager", lambda: harness.binding_manager
    )

    original_require = nonebot.require

    def _require(plugin_name: str) -> object:
        if plugin_name == "komari_roulette":
            return None
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot, "require", _require)
    monkeypatch.setattr(nonebot.plugin, "require", _require)

    try:
        # 真实创建：生产装配构造服务并装入全局注册表，公开 getter 可取。
        binding_repair_lifecycle.start_binding_repair_service()
        service = get_binding_repair_service()
        assert service is not None
        # 注入的是已有真实测试管理器（与种子写入同一实例）。
        assert service._manager is harness.binding_manager

        # 真实 reader 直读 PG：看到当前作用域的 waiting 对局与真实绑定。
        diagnosis = await service.diagnose(
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
        assert diagnosis.game_present is True
        assert diagnosis.game_lifecycle == "waiting"
        assert len(diagnosis.members) == 1
        assert diagnosis.members[0].character_name == "甲"

        # waiting 对局阻断预览，且绑定未被清除。
        with pytest.raises(RepairBlockedByGameError):
            await service.preview(
                app_id=current.app_id,
                group_openid=current.group_openid,
                operator_id="tsk280-operator",
                reason=CHANGE_REASON,
            )
        assert await group_binding_rows(harness.engine, current) == 1
        assert await group_mapping_rows(harness.engine, current) == 1

        # 真实关闭：getter 为 None，旧引用一切操作拒绝，二次关闭幂等。
        await binding_repair_lifecycle.stop_binding_repair_service()
        assert get_binding_repair_service() is None
        with pytest.raises(RuntimeError):
            await service.diagnose(
                app_id=current.app_id,
                group_openid=current.group_openid,
            )
        with pytest.raises(RuntimeError):
            await service.preview(
                app_id=current.app_id,
                group_openid=current.group_openid,
                operator_id="tsk280-operator",
                reason=CHANGE_REASON,
            )
        await binding_repair_lifecycle.stop_binding_repair_service()
        assert get_binding_repair_service() is None
    finally:
        set_binding_repair_service(None)
        await clear_roulette_scope(harness.engine, current)
        await clear_binding_scope(harness.engine, current)
