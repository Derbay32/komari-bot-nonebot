"""Prompt 存储辅助逻辑测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from komari_bot.common import prompt_storage
from komari_bot.common.prompt_storage import (
    PromptTemplateLoader,
    StoredPrompt,
    load_prompt_values,
    merge_prompt_values,
    save_prompt_values,
    validate_prompt_values,
)


@dataclass
class _Resource:
    resource_id: str = "test_prompt"
    display_name: str = "测试 Prompt"
    defaults: dict[str, str] = field(
        default_factory=lambda: {"system_prompt": "默认", "memory_ack": "收到"}
    )


class _FakePromptStorage:
    def __init__(self, stored: StoredPrompt | None = None) -> None:
        self.stored = stored
        self.saved: dict[str, str] | None = None

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        assert resource_id == "test_prompt"
        return self.stored

    def upsert(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str = "1.0",
    ) -> StoredPrompt:
        assert resource_id == "test_prompt"
        assert display_name == "测试 Prompt"
        self.saved = prompt_data
        self.stored = StoredPrompt(
            resource_id=resource_id,
            display_name=display_name,
            prompt_data=dict(prompt_data),
            version=version,
            updated_at=datetime.now(UTC),
        )
        return self.stored


class _ClosablePromptStorage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_merge_prompt_values_only_accepts_known_string_fields() -> None:
    merged = merge_prompt_values(
        {"system_prompt": "默认", "memory_ack": "收到"},
        {"system_prompt": "覆盖\n", "unknown": "忽略", "memory_ack": 123},
    )

    assert merged == {"system_prompt": "覆盖", "memory_ack": "收到"}


def test_validate_prompt_values_rejects_unknown_and_blank_fields() -> None:
    defaults = {"system_prompt": "默认"}

    assert validate_prompt_values(defaults, {"system_prompt": "新值\n"}) == {
        "system_prompt": "新值"
    }
    with pytest.raises(ValueError, match="未知提示词字段"):
        validate_prompt_values(defaults, {"unknown": "新值"})
    with pytest.raises(ValueError, match="非空字符串"):
        validate_prompt_values(defaults, {"system_prompt": "   "})


def test_load_and_save_prompt_values_use_prompt_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _Resource()
    stored = StoredPrompt(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        prompt_data={"system_prompt": "PG 值"},
        version="1.0",
        updated_at=datetime.now(UTC),
    )
    fake_storage = _FakePromptStorage(stored)
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    loaded = load_prompt_values(resource)
    saved = save_prompt_values(resource, {"memory_ack": "保存值"})

    assert loaded.values == {"system_prompt": "PG 值", "memory_ack": "收到"}
    assert fake_storage.saved == {"system_prompt": "默认", "memory_ack": "保存值"}
    assert saved.prompt_data == fake_storage.saved


def test_prompt_template_loader_falls_back_to_cache_on_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_load_prompt_values(_resource: object) -> prompt_storage.PromptValues:
        nonlocal calls
        calls += 1
        if calls == 1:
            stored = StoredPrompt(
                resource_id="test_prompt",
                display_name="测试 Prompt",
                prompt_data={"system_prompt": "PG 值"},
                version="1.0",
                updated_at=datetime.now(UTC),
            )
            return prompt_storage.PromptValues(
                values={"system_prompt": "PG 值"},
                stored=stored,
            )
        msg = "PG 故障"
        raise RuntimeError(msg)

    monkeypatch.setattr(prompt_storage, "load_prompt_values", fake_load_prompt_values)
    loader = PromptTemplateLoader(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        defaults={"system_prompt": "默认"},
        log_prefix="[Test]",
    )

    assert loader.get_template() == {"system_prompt": "PG 值"}
    assert loader.get_template() == {"system_prompt": "PG 值"}


def test_close_prompt_storage_if_created_does_not_create_storage() -> None:
    prompt_storage._StorageState.storage = None

    prompt_storage.close_prompt_storage_if_created()

    assert prompt_storage._StorageState.storage is None


def test_close_prompt_storage_if_created_closes_and_clears_storage() -> None:
    storage = _ClosablePromptStorage()
    prompt_storage._StorageState.storage = storage

    prompt_storage.close_prompt_storage_if_created()

    assert storage.closed is True
    assert prompt_storage._StorageState.storage is None
