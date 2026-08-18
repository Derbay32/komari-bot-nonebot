"""group_history_summary 服务测试公共 fixture。

TSK-191 起 Prompt 无 Python 默认正文：非 loader-contract 的服务测试不得
隐式依赖 DEFAULTS（无 DB 时公开 loader 冷启动失败）。本 conftest 为只测试
其他业务的用例经公开 loader seam（``prompt_template.get_template`` 及其
消费方直接引用）注入完整的 marker Prompt 快照，字段集合来自强类型 Schema
oracle（``tests.config.prompt_field_contract``），不触碰模块级 loader 缓存，
避免测试顺序导致的 cache 泄漏。

只作用于本目录：loader 冷启动/cache 契约测试（``tests/config/
test_prompt_loader_contract.py`` 等）保持真实加载路径，不受本 fixture 影响。
"""

from __future__ import annotations

import pytest

from tests.config.prompt_field_contract import prompt_marker_values

_RESOURCE_ID = "group_history_summary"


@pytest.fixture(autouse=True)
def _inject_marker_prompt_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """为依赖总结/规划 Prompt 的服务测试注入完整 marker 快照。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module
    import komari_bot.plugins.group_history_summary.prompt_template as seam_module
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module

    template = prompt_marker_values(_RESOURCE_ID)

    async def _marker_template() -> dict[str, str]:
        return dict(template)

    # 公开 loader seam 与其消费方直接引用（后者为既有加载路径）
    monkeypatch.setattr(seam_module, "get_template", _marker_template)
    monkeypatch.setattr(planner_module, "get_template", _marker_template)
    monkeypatch.setattr(summarize_module, "get_template", _marker_template)
