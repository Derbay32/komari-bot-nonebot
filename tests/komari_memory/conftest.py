"""komari_memory 服务测试公共 fixture。

TSK-191 起 Prompt 无 Python 默认正文：非 loader-contract 的服务测试不得
隐式依赖 DEFAULTS（无 DB 时公开 loader 冷启动失败）。本 conftest 为只测试
其他业务的用例经公开 loader seam（``services.summary_prompt_template.
get_template`` 及其消费方直接引用）注入完整的 marker Prompt 快照，字段集合
来自强类型 Schema oracle（``tests.config.prompt_field_contract``），不触碰
模块级 loader 缓存，避免测试顺序导致的 cache 泄漏。

只作用于本目录：loader 冷启动/cache 契约测试（``tests/config/
test_prompt_loader_contract.py`` 等）保持真实加载路径，不受本 fixture 影响。
"""

from __future__ import annotations

import pytest

from tests.config.prompt_field_contract import prompt_marker_values

_RESOURCE_ID = "komari_memory_summary"


@pytest.fixture(autouse=True)
def _admit_all_memory_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """为既有 memory 服务测试装载统一准入替身：恒 admit。

    TSK-230 起生产在持久群工作各效果前接入统一群准入（ADR-0012）。既有服务
    测试（lifecycle / summary_worker / forgetting / interaction worker 等）不
    关心准入语义，统一注入 ``admitted`` 判定替身使真实准入 seam 不阻断既有
    行为断言；准入语义本身由 tests/group_admission/*admission.py 专项验收。
    """
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    install_scripted_adjudicate(monkeypatch, ScriptedAdjudicate("admitted"))


@pytest.fixture(autouse=True)
def _inject_marker_prompt_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """为依赖总结 Prompt 的服务测试注入完整 marker 快照。"""
    import komari_bot.plugins.komari_memory.agent.profile_agent_service as profile_module
    import komari_bot.plugins.komari_memory.services.llm_service as llm_module
    import komari_bot.plugins.komari_memory.services.summary_prompt_template as seam_module

    template = prompt_marker_values(_RESOURCE_ID)

    async def _marker_template() -> dict[str, str]:
        return dict(template)

    # 公开 loader seam：reload 后的 profile_agent_service 从该模块取引用
    monkeypatch.setattr(seam_module, "get_template", _marker_template)
    # 既有模块实例已持有直接引用，必须就地替换
    monkeypatch.setattr(llm_module, "get_summary_template", _marker_template)
    monkeypatch.setattr(profile_module, "get_summary_template", _marker_template)
