"""TSK-280 管理装配绑定修复生命周期行为测试。

管理 composition root 必须在启动时构造真实 ``BindingRepairService``（注入
真实绑定管理器，即 ``character_binding`` 顶层 ``get_binding_manager``），关闭
时移除全局引用并真正关闭旧服务。这里用真实生命周期函数与可观测的 sentinel
管理器验证行为，不对生产源码做字符串实现断言。
"""

from __future__ import annotations

from typing import Any

import nonebot
import nonebot.plugin
import pytest

from komari_bot.plugins.character_binding import repair as repair_module
from komari_bot.plugins.komari_management import binding_repair_lifecycle


@pytest.fixture
def repair_lifecycle_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """暴露 character_binding 顶层修复服务导出（镜像真实 ``__init__`` 暴露面）。

    顶层 conftest 把 ``komari_bot.plugins.character_binding`` shim 化以避开
    启动副作用；生产装配经顶层包取 ``BindingRepairService`` /
    ``set_binding_repair_service`` / ``get_binding_repair_service``，这里按真实
    暴露面注入同一实现。
    """
    import komari_bot.plugins.character_binding as binding_package

    for name in (
        "BindingRepairService",
        "get_binding_repair_service",
        "set_binding_repair_service",
    ):
        monkeypatch.setattr(
            binding_package, name, getattr(repair_module, name), raising=False
        )
    repair_module.set_binding_repair_service(None)
    return repair_module


@pytest.mark.asyncio
async def test_management_lifecycle_starts_with_real_manager_and_closes_service(
    repair_lifecycle_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动后全局服务非 None 且持有真实管理器；关闭后引用为 None 且服务已关闭。"""
    import komari_bot.plugins.character_binding as binding_package

    sentinel_manager = object()
    monkeypatch.setattr(
        binding_package, "get_binding_manager", lambda: sentinel_manager
    )

    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name in {"komari_roulette", "nonebot_plugin_orm"}:
            return None
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot, "require", _require)
    monkeypatch.setattr(nonebot.plugin, "require", _require)

    try:
        binding_repair_lifecycle.start_binding_repair_service()
        service = repair_lifecycle_module.get_binding_repair_service()
        assert service is not None
        # 生产必须注入真实绑定管理器（非 None 占位）。
        assert service._manager is sentinel_manager

        await binding_repair_lifecycle.stop_binding_repair_service()
        assert repair_lifecycle_module.get_binding_repair_service() is None
        # 旧引用必须已关闭：拒绝一切操作。
        with pytest.raises(RuntimeError):
            await service.preview(
                app_id="closed-app",
                group_openid="closed-group",
                operator_id="closed-operator",
                reason="关闭后引用验证",
            )
    finally:
        repair_lifecycle_module.set_binding_repair_service(None)
