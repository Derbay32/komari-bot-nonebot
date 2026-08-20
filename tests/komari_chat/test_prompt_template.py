"""TSK-190 chat Prompt 公开 loader seam 冷启动完整性测试。

AC5/AC7：冷启动门槛是“完整 Prompt”，不是“有行即可”。PostgreSQL 返回
stored row 但缺至少一个强类型 Schema 字段、或某字段仅空白，且无旧缓存
时，``komari_chat.services.prompt_template.get_template()`` 必须抛出
RuntimeError 并点名 Prompt 与缺失/空字段。

TSK-191 起测试缝统一替换 ``prompt_storage.get_prompt_storage``（存储
对象），loader 与 ``prompt_storage`` 的加载/合并逻辑走真实生产代码。
字段集合从 ``KomariChatPromptSchema`` 派生，输入使用测试自有 marker，
不复制 seed 正文。chat 侧完整 gate 已随 TSK-190 实现（本文件用例当前为
绿色 spec）；memory/group 的同类冷启动缺口由
``tests/config/test_prompt_loader_contract.py`` 以 RED 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from komari_bot.config import prompt_storage
from komari_bot.config.prompt_storage import StoredPrompt
from komari_bot.plugins.komari_chat.prompt_schema import KomariChatPromptSchema
from komari_bot.plugins.komari_chat.services import (
    prompt_template as chat_prompt_template,
)

PROMPT_STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})


def _chat_prompt_field_names() -> set[str]:
    """强类型 Schema 正文字段集（不含存储专用字段）。"""
    return set(KomariChatPromptSchema.model_fields) - PROMPT_STORAGE_FIELDS


def _complete_marker_row() -> dict[str, str]:
    """测试自有 marker 的完整 Prompt 行（不复制 seed 正文）。"""
    return {field: f"marker-{field}" for field in sorted(_chat_prompt_field_names())}


class _FakePromptStorage:
    def __init__(self, row: dict[str, str]) -> None:
        self._row = dict(row)

    def register_invalidator(self, _resource_id: str, _callback: object) -> None:
        return

    async def fetch_async(self, _resource_id: str) -> StoredPrompt:
        return StoredPrompt(
            resource_id="komari_chat",
            prompt_data=dict(self._row),
            revision=1,
            updated_at=datetime.now(UTC),
        )

    async def update_if_unchanged_async(self, **_kwargs: object) -> None:
        # 自动同步视为冲突：不写库，由调用方重读
        return None


def _install_partial_stored_row(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, str],
) -> None:
    """让真实 loader 读到字段不完整/为空的 stored row（fresh loader，无缓存）。

    只替换存储层取值，loader 走真实 ``get_template_async`` /
    ``_accept_loaded`` 路径，不锁死 loader 内部实现。
    """
    monkeypatch.setattr(
        prompt_storage,
        "get_prompt_storage",
        lambda: _FakePromptStorage(row),
    )
    loader = chat_prompt_template._loader
    # 清空模块级 loader 缓存，保证本用例从无缓存冷启动
    loader._cache = {}
    loader._cache_updated_at = None
    loader._cache_revision = 0
    loader._cache_checked_at = 0.0
    loader._invalidated = True


def _assert_cold_start_error_names_prompt_and_field(
    error: BaseException,
    field: str,
) -> None:
    message = str(error)
    assert "Prompt" in message, "错误消息必须点名 Prompt"
    assert field in message, f"错误消息必须点名缺失/空字段: {field}"
    assert "komari_chat" in message or "Komari Chat Prompt" in message, (
        "错误消息必须点名 Prompt 资源"
    )


@pytest.mark.asyncio
async def test_get_template_rejects_stored_row_missing_schema_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7：stored row 缺 Schema 字段且无旧缓存时 get_template 必须抛错。"""
    row = _complete_marker_row()
    missing_field = sorted(row)[0]
    del row[missing_field]

    _install_partial_stored_row(monkeypatch, row)

    with pytest.raises(RuntimeError) as exc_info:
        await chat_prompt_template.get_template()
    _assert_cold_start_error_names_prompt_and_field(exc_info.value, missing_field)


@pytest.mark.asyncio
async def test_get_template_rejects_stored_row_with_blank_only_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7：stored row 某字段仅空白且无旧缓存时 get_template 必须抛错。"""
    row = _complete_marker_row()
    blank_field = sorted(row)[1]
    row[blank_field] = "   "

    _install_partial_stored_row(monkeypatch, row)

    with pytest.raises(RuntimeError) as exc_info:
        await chat_prompt_template.get_template()
    _assert_cold_start_error_names_prompt_and_field(exc_info.value, blank_field)
