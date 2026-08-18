"""TSK-191 三个 Prompt 资源公开 loader 冷启动/缓存契约测试。

在公开 loader seam（``<插件>.get_template``）上验证 AC4/AC5/AC9：

- 冷启动没有完整数据库值时明确失败：缺行、stored row 缺 Schema 字段、
  字段纯空白三种形态，成功路径绝不回退到 Python 默认正文（AC4）；
- 成功加载后的最后有效缓存支持数据库短故障，恢复后按 revision 刷新
  （AC5）；
- 模板值完全来自 PostgreSQL 快照，不并入任何默认正文（AC2/AC3）。

存储缝只替换 ``prompt_storage.get_prompt_storage``，loader 走生产加载路径
（当前实现会拿 memory/group 的 ``DEFAULTS`` 合并补全不完整快照，因此
memory/group 的冷启动用例是 TSK-191 的可解释 RED）。字段集合从强类型
Schema 派生，输入用测试自有 marker，不复制生产/seed 正文。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Any

import pytest

from komari_bot.config import prompt_storage
from komari_bot.config.prompt_storage import StoredPrompt
from tests.config.prompt_field_contract import (
    PROMPT_RESOURCE_IDS,
    prompt_display_name,
    prompt_marker_values,
    prompt_resource_field_names,
)

_LOADER_MODULE_NAMES: dict[str, str] = {
    "komari_chat": "komari_bot.plugins.komari_chat.services.prompt_template",
    "komari_memory_summary": (
        "komari_bot.plugins.komari_memory.services.summary_prompt_template"
    ),
    "group_history_summary": "komari_bot.plugins.group_history_summary.prompt_template",
}


def _loader_module(resource_id: str) -> Any:
    return importlib.import_module(_LOADER_MODULE_NAMES[resource_id])


def _reset_loader_cache(resource_id: str) -> None:
    """清空模块级 loader 单例缓存，保证用例彼此隔离。"""
    loader = _loader_module(resource_id)._loader
    loader._cache = {}
    loader._cache_updated_at = None
    loader._cache_revision = 0
    loader._cache_checked_at = 0.0
    loader._invalidated = True


def _reset_all_loaders() -> None:
    for resource_id in PROMPT_RESOURCE_IDS:
        _reset_loader_cache(resource_id)


def _stored(resource_id: str, prompt_data: dict[str, str], *, revision: int) -> StoredPrompt:
    return StoredPrompt(
        resource_id=resource_id,
        prompt_data=dict(prompt_data),
        revision=revision,
        updated_at=datetime.now(UTC),
    )


class _ScriptedStorage:
    """顺序消费脚本的存储替身；元素为 StoredPrompt | None | 异常。"""

    def __init__(self, steps: list[StoredPrompt | None | BaseException]) -> None:
        self._steps = list(steps)
        self.calls = 0

    def register_invalidator(self, _resource_id: str, _callback: object) -> None:
        return

    async def fetch_async(self, _resource_id: str) -> StoredPrompt | None:
        step = self._steps[min(self.calls, len(self._steps) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return step

    async def update_if_unchanged_async(self, **_kwargs: object) -> None:
        # 自动同步视为冲突：不写库，由调用方重读
        return None


def _install_storage(
    monkeypatch: pytest.MonkeyPatch,
    steps: list[StoredPrompt | None | BaseException],
) -> _ScriptedStorage:
    storage = _ScriptedStorage(steps)
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    return storage


def _assert_error_names_resource_and_field(
    error: BaseException,
    resource_id: str,
    field: str | None = None,
) -> None:
    message = str(error)
    assert "Prompt" in message, "错误消息必须点名 Prompt"
    if field is not None:
        assert field in message, f"错误消息必须点名缺失/空字段: {field}"
    assert resource_id in message or prompt_display_name(resource_id) in message, (
        f"错误消息必须点名 Prompt 资源: {resource_id}"
    )
    assert "komari" in message or "Komari" in message, (
        "错误消息必须包含 Prompt 资源标识"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", PROMPT_RESOURCE_IDS)
async def test_get_template_fails_cold_start_without_stored_row(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
) -> None:
    """AC4：无 DB 行且无缓存时冷启动必须失败（绝不回退到默认正文）。"""
    _reset_all_loaders()
    _install_storage(monkeypatch, [None])

    with pytest.raises(RuntimeError) as exc_info:
        await _loader_module(resource_id).get_template()
    _assert_error_names_resource_and_field(exc_info.value, resource_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", PROMPT_RESOURCE_IDS)
async def test_get_template_fails_cold_start_with_partial_stored_row(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
) -> None:
    """AC4：stored row 缺 Schema 字段且无旧缓存时必须点名缺失字段并抛错。"""
    _reset_all_loaders()
    fields = sorted(prompt_resource_field_names(resource_id))
    missing_field = fields[-1]
    row = prompt_marker_values(resource_id)
    del row[missing_field]
    _install_storage(monkeypatch, [_stored(resource_id, row, revision=1)])

    with pytest.raises(RuntimeError) as exc_info:
        await _loader_module(resource_id).get_template()
    _assert_error_names_resource_and_field(exc_info.value, resource_id, missing_field)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", PROMPT_RESOURCE_IDS)
async def test_get_template_fails_cold_start_with_blank_only_field(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
) -> None:
    """AC4：stored row 某字段纯空白且无旧缓存时必须点名空白字段并抛错。"""
    _reset_all_loaders()
    fields = sorted(prompt_resource_field_names(resource_id))
    blank_field = fields[-1]
    row = prompt_marker_values(resource_id)
    row[blank_field] = "   "
    _install_storage(monkeypatch, [_stored(resource_id, row, revision=1)])

    with pytest.raises(RuntimeError) as exc_info:
        await _loader_module(resource_id).get_template()
    _assert_error_names_resource_and_field(exc_info.value, resource_id, blank_field)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", PROMPT_RESOURCE_IDS)
async def test_get_template_returns_stored_values_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
) -> None:
    """AC2/AC3：模板值完全来自 PostgreSQL 快照（marker），不并入默认正文。"""
    _reset_all_loaders()
    row = prompt_marker_values(resource_id)
    _install_storage(monkeypatch, [_stored(resource_id, row, revision=1)])

    template = await _loader_module(resource_id).get_template()

    assert template == row


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", PROMPT_RESOURCE_IDS)
async def test_get_template_keeps_last_valid_cache_on_db_failure_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
) -> None:
    """AC5：成功加载后 DB 短故障用最后有效缓存，恢复后按 revision 刷新。"""
    _reset_all_loaders()
    clock = {"now": 1000.0}
    monkeypatch.setattr(prompt_storage, "monotonic", lambda: clock["now"])

    v1 = {field: f"v1-{field}" for field in sorted(prompt_resource_field_names(resource_id))}
    v2 = {field: f"v2-{field}" for field in sorted(prompt_resource_field_names(resource_id))}
    storage = _install_storage(
        monkeypatch,
        [
            _stored(resource_id, v1, revision=1),
            RuntimeError("PG 暂时故障（测试模拟）"),
            _stored(resource_id, v2, revision=2),
        ],
    )
    getter = _loader_module(resource_id).get_template

    assert await getter() == v1
    assert storage.calls == 1

    # 缓存新鲜：不读取
    assert await getter() == v1
    assert storage.calls == 1

    # 越过陈限读取失败：保留最后有效缓存
    clock["now"] += 2.0
    assert await getter() == v1
    assert storage.calls == 2

    # 恢复后读取新 revision 并刷新缓存
    clock["now"] += 2.0
    assert await getter() == v2
    assert storage.calls == 3
