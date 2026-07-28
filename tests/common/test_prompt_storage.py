"""Prompt 存储辅助逻辑测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

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
    def __init__(
        self,
        stored: StoredPrompt | None = None,
        *,
        fail_fetch: bool = False,
        fail_upsert: bool = False,
        conflict_stored: StoredPrompt | None = None,
    ) -> None:
        self.stored = stored
        self.fail_fetch = fail_fetch
        self.fail_upsert = fail_upsert
        self.conflict_stored = conflict_stored
        self.saved: dict[str, str] | None = None
        self.saved_payloads: list[dict[str, str]] = []
        self.upsert_calls = 0

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        assert resource_id == "test_prompt"
        if self.fail_fetch:
            msg = "读取失败"
            raise RuntimeError(msg)
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
        self.upsert_calls += 1
        if self.fail_upsert:
            msg = "写入失败"
            raise RuntimeError(msg)
        self.saved = prompt_data
        self.saved_payloads.append(prompt_data)
        self.stored = StoredPrompt(
            resource_id=resource_id,
            display_name=display_name,
            prompt_data=dict(prompt_data),
            version=version,
            updated_at=datetime.now(UTC),
        )
        return self.stored

    def update_if_unchanged(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredPrompt | None:
        assert self.stored is not None
        assert expected_updated_at == self.stored.updated_at
        if self.conflict_stored is not None:
            self.stored = self.conflict_stored
            return None
        return self.upsert(
            resource_id=resource_id,
            display_name=display_name,
            prompt_data=prompt_data,
            version=version,
        )


class _ClosablePromptStorage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
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
    with pytest.raises(ValueError, match="字符上限"):
        validate_prompt_values(defaults, {"system_prompt": "字" * 12_001})


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


def test_load_prompt_values_syncs_added_and_removed_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _Resource()
    stored = StoredPrompt(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        prompt_data={"system_prompt": "PG 值\n", "legacy": "旧字段"},
        version="1.0",
        updated_at=datetime.now(UTC),
    )
    fake_storage = _FakePromptStorage(stored)
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    loaded = load_prompt_values(resource)

    assert loaded.values == {"system_prompt": "PG 值", "memory_ack": "收到"}
    assert fake_storage.saved_payloads == [
        {"system_prompt": "PG 值", "legacy": "旧字段", "memory_ack": "收到"}
    ]
    assert loaded.stored == fake_storage.stored


def test_load_prompt_values_returns_merged_values_when_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _Resource()
    stored = StoredPrompt(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        prompt_data={"legacy": "旧字段"},
        version="1.0",
        updated_at=datetime.now(UTC),
    )
    fake_storage = _FakePromptStorage(stored, fail_upsert=True)
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    loaded = load_prompt_values(resource)

    assert loaded.values == {"system_prompt": "默认", "memory_ack": "收到"}
    assert loaded.stored == stored
    assert fake_storage.upsert_calls == 1
    assert fake_storage.saved_payloads == []


def test_load_prompt_values_refetches_when_sync_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _Resource()
    stored = StoredPrompt(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        prompt_data={"legacy": "旧字段"},
        version="1.0",
        updated_at=datetime.now(UTC),
    )
    latest = StoredPrompt(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        prompt_data={"system_prompt": "管理员新值", "memory_ack": "收到"},
        version="1.0",
        updated_at=datetime.now(UTC),
    )
    fake_storage = _FakePromptStorage(stored, conflict_stored=latest)
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    loaded = load_prompt_values(resource)

    assert loaded.values == {"system_prompt": "管理员新值", "memory_ack": "收到"}
    assert loaded.stored == latest
    assert fake_storage.saved_payloads == []


def test_load_prompt_values_does_not_write_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _Resource()
    fake_storage = _FakePromptStorage(fail_fetch=True)
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    with pytest.raises(RuntimeError, match="读取失败"):
        load_prompt_values(resource)

    assert fake_storage.upsert_calls == 0


def test_prompt_template_loader_falls_back_to_cache_on_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Storage:
        def register_invalidator(self, _resource_id: str, _callback: object) -> None:
            return

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
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: _Storage())
    loader = PromptTemplateLoader(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        defaults={"system_prompt": "默认"},
        log_prefix="[Test]",
    )

    assert loader.get_template() == {"system_prompt": "PG 值"}
    assert loader.get_template() == {"system_prompt": "PG 值"}


@pytest.mark.asyncio
async def test_async_prompt_loader_uses_cache_and_notification_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InvalidationStorage:
        def __init__(self) -> None:
            self.callback: object | None = None

        def register_invalidator(self, _resource_id: str, callback: object) -> None:
            self.callback = callback

    storage = _InvalidationStorage()
    calls = 0

    async def fake_load_prompt_values(
        _resource: object,
    ) -> prompt_storage.PromptValues:
        nonlocal calls
        calls += 1
        stored = StoredPrompt(
            resource_id="test_prompt",
            display_name="测试 Prompt",
            prompt_data={"system_prompt": f"PG 值 {calls}"},
            version="1.0",
            updated_at=datetime.now(UTC),
            revision=calls,
        )
        return prompt_storage.PromptValues(
            values={"system_prompt": f"PG 值 {calls}"},
            stored=stored,
        )

    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    monkeypatch.setattr(
        prompt_storage,
        "load_prompt_values_async",
        fake_load_prompt_values,
    )
    loader = PromptTemplateLoader(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        defaults={"system_prompt": "默认"},
        log_prefix="[Test]",
    )

    results = [await loader.get_template_async() for _ in range(100)]
    assert calls == 1
    assert all(result == {"system_prompt": "PG 值 1"} for result in results)

    callback = cast("Any", storage.callback)
    callback()
    assert await loader.get_template_async() == {"system_prompt": "PG 值 2"}
    assert calls == 2


@pytest.mark.asyncio
async def test_async_prompt_loader_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Storage:
        def register_invalidator(self, _resource_id: str, _callback: object) -> None:
            return

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_load(_resource: object) -> prompt_storage.PromptValues:
        started.set()
        await release.wait()
        return prompt_storage.PromptValues(values={"system_prompt": "完成"}, stored=None)

    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: _Storage())
    monkeypatch.setattr(prompt_storage, "load_prompt_values_async", slow_load)
    loader = PromptTemplateLoader(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        defaults={"system_prompt": "默认"},
        log_prefix="[Test]",
    )

    operation = asyncio.create_task(loader.get_template_async())
    await started.wait()
    ticker_ran = False

    async def _tick() -> None:
        nonlocal ticker_ran
        await asyncio.sleep(0)
        ticker_ran = True

    await _tick()
    assert ticker_ran is True
    assert operation.done() is False
    release.set()
    assert await operation == {"system_prompt": "完成"}


@pytest.mark.asyncio
async def test_sync_prompt_loader_rejects_running_event_loop() -> None:
    loader = PromptTemplateLoader(
        resource_id="test_prompt",
        display_name="测试 Prompt",
        defaults={"system_prompt": "默认"},
        log_prefix="[Test]",
    )

    with pytest.raises(RuntimeError, match="禁止同步读取 Prompt"):
        loader.get_template()


def test_close_prompt_storage_if_created_does_not_create_storage() -> None:
    prompt_storage._StorageState.storage = None

    prompt_storage.close_prompt_storage_if_created()

    assert prompt_storage._StorageState.storage is None


def test_close_prompt_storage_if_created_closes_and_clears_storage() -> None:
    storage = _ClosablePromptStorage()
    cast("Any", prompt_storage._StorageState).storage = storage

    prompt_storage.close_prompt_storage_if_created()

    assert storage.closed is True
    assert prompt_storage._StorageState.storage is None


def test_prompt_storage_close_reclaims_pool_thread_and_loop() -> None:
    storage = prompt_storage.PromptStorage()
    pool = _FakePool()
    storage._pool = cast("Any", pool)
    thread = storage._thread
    loop = storage._loop

    storage.close()
    storage.close()

    assert pool.closed is True
    assert thread.is_alive() is False
    assert loop.is_closed() is True
