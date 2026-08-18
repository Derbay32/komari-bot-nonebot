"""TSK-190 chat Prompt 公开 loader seam 冷启动完整性测试。

AC5/AC7：冷启动门槛是“完整 Prompt”，不是“有行即可”。PostgreSQL 返回
stored row 但缺至少一个强类型 Schema 字段、或某字段仅空白，且无旧缓存
时，``komari_chat.services.prompt_template.get_template()`` 必须抛出
RuntimeError 并点名 Prompt 与缺失/空字段。

当前实现只把 ``stored=None`` 判为冷启动失败；字段不完整或仅空白的
stored row 会被当作有效快照缓存并原样返回，因此本文件用例是 TSK-190
的可解释 RED。字段集合从 ``KomariChatPromptSchema`` 派生，输入使用测试
自有 marker，不复制 seed 正文。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from komari_bot.config import prompt_storage
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
    def register_invalidator(self, _resource_id: str, _callback: object) -> None:
        return


def _install_partial_stored_row(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, str],
) -> None:
    """让真实 loader 读到字段不完整/为空的 stored row（fresh loader，无缓存）。

    只替换存储层取值，loader 走真实 ``get_template_async`` /
    ``_accept_loaded`` 路径，不锁死 loader 内部实现。
    """
    stored = prompt_storage.StoredPrompt(
        resource_id="komari_chat",
        prompt_data=dict(row),
        revision=1,
        updated_at=datetime.now(UTC),
    )
    loaded = prompt_storage.PromptValues(values=dict(row), stored=stored)

    async def fake_load_prompt_values_async(
        _resource: object,
    ) -> prompt_storage.PromptValues:
        return loaded

    monkeypatch.setattr(
        prompt_storage,
        "get_prompt_storage",
        lambda: _FakePromptStorage(),
    )
    monkeypatch.setattr(
        prompt_storage,
        "load_prompt_values_async",
        fake_load_prompt_values_async,
    )

    loader = prompt_storage.PromptTemplateLoader(
        resource_id="komari_chat",
        display_name="Komari Chat Prompt",
        defaults={},
        log_prefix="[PromptTemplate]",
    )
    monkeypatch.setattr(chat_prompt_template, "_loader", loader)


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
