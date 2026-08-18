"""Prompt 存储辅助逻辑测试。

TSK-191：共享 loader 不再把缺失数据库值与代码默认正文合并；旧默认机制
被删除（AC8），冷启动缺值/部分行/空白字段/跨资源字段由公开 loader 的
完整性契约测试覆盖（见 ``tests/config/test_prompt_loader_contract.py``）。
本文件只保留与合并语义无关的存储/传输/缓存机制测试，以及字段白名单来自
resource_id 对应强类型 Schema 的契约测试（AC3）。

TSK-191 新契约（本文件测试输入的唯一形态）：

- 测试资源对象（``_Resource``）只有 ``resource_id`` / ``display_name``，
  不携带 ``defaults``；
- ``PromptTemplateLoader`` 构造不再接受 ``defaults`` 参数；
- 直接测试 ``validate_prompt_values`` 时必须传资源对象（不能传空 dict 猜
  资源），字段集由 ``ensure_typed_prompt_model(resource.resource_id)``
  从强类型 Schema 派生；
- 白名单校验优先走资源感知的公开 seam（``save_prompt_values`` 等），并
  用 ``CROSS_RESOURCE_FOREIGN_FIELDS`` 证明 A 资源字段在 B 资源被拒绝
  （不是三资源字段的全局 union）。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from komari_bot.config import prompt_storage
from komari_bot.config.prompt_storage import (
    PromptStorage,
    PromptTemplateLoader,
    StoredPrompt,
    save_prompt_values,
    validate_prompt_values,
)
from tests.config.prompt_field_contract import (
    CROSS_RESOURCE_FOREIGN_FIELDS,
    PROMPT_RESOURCE_IDS,
    prompt_display_name,
    prompt_marker_values,
    prompt_resource_field_names,
)


@dataclass(frozen=True, slots=True)
class _Resource:
    """TSK-191 契约下的 Prompt 资源对象：只有 resource_id/display_name。

    不再携带 ``defaults``（AC8）。旧实现访问 ``resource.defaults`` 才
    能工作，因此把本资源传给 ``save_prompt_values`` /
    ``validate_prompt_values`` 的用例是 TSK-191 的可解释 RED。
    """

    resource_id: str
    display_name: str


def _resource(resource_id: str) -> Any:
    """按 TSK-191 契约构造资源对象。

    返回 ``Any``：生产签名按新契约实现前，避免 pyright 按旧
    ``PromptResourceProtocol``（含 defaults）报类型错误。
    """
    return _Resource(
        resource_id=resource_id,
        display_name=prompt_display_name(resource_id),
    )


class _FakePromptStorage:
    def __init__(self, stored: StoredPrompt | None = None) -> None:
        self.stored = stored
        self.saved: dict[str, str] | None = None

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        del resource_id
        return self.stored

    def upsert(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
    ) -> StoredPrompt:
        del resource_id
        self.saved = prompt_data
        self.stored = StoredPrompt(
            resource_id="test_prompt",
            prompt_data=dict(prompt_data),
            revision=1,
            updated_at=datetime.now(UTC),
        )
        return self.stored


class _ScriptedStorage:
    """以存储对象为缝的 loader 测试替身。

    每次 ``fetch`` 顺序消费脚本；脚本元素为 ``StoredPrompt | None | 异常``。
    """

    def __init__(self, steps: list[StoredPrompt | None | BaseException]) -> None:
        self._steps = list(steps)
        self.calls = 0
        self.callback: object | None = None

    def register_invalidator(self, _resource_id: str, callback: object) -> None:
        self.callback = callback

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        del resource_id
        step = self._steps[min(self.calls, len(self._steps) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return step

    async def fetch_async(self, resource_id: str) -> StoredPrompt | None:
        del resource_id
        step = self._steps[min(self.calls, len(self._steps) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return step

    async def update_if_unchanged_async(self, **_kwargs: object) -> None:
        # 自动同步在替身上视为冲突：不写库，由调用方重读
        return None

    def update_if_unchanged(self, **_kwargs: object) -> None:
        return None


class _ClosablePromptStorage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _stored(
    prompt_data: dict[str, str],
    *,
    revision: int = 1,
    resource_id: str = "komari_chat",
) -> StoredPrompt:
    return StoredPrompt(
        resource_id=resource_id,
        prompt_data=dict(prompt_data),
        revision=revision,
        updated_at=datetime.now(UTC),
    )


def _complete_row(
    resource_id: str = "komari_chat",
    **overrides: str,
) -> dict[str, str]:
    """该资源 Schema 完整的 marker 行（机制测试输入，不复制 seed/正文）。"""
    row = prompt_marker_values(resource_id)
    row.update(overrides)
    return row


def _loader(
    *,
    resource_id: str = "komari_chat",
    display_name: str = "Komari Chat Prompt",
) -> PromptTemplateLoader:
    """构造无 ``defaults`` 参数的 loader（TSK-191 后统一形态，AC8）。

    当前实现仍把 ``defaults`` 声明为必填参数；用 ``cast(Any, ...)`` 只按
    新契约调用，实现移除参数后即为完全合法的直接构造。
    """
    constructor = cast("Any", PromptTemplateLoader)
    return constructor(
        resource_id=resource_id,
        display_name=display_name,
        log_prefix="[Test]",
    )


def test_stored_prompt_has_no_version_field() -> None:
    """StoredPrompt 不再携带 version：Schema 版本唯一权威是 Alembic。"""
    stored = _stored({})

    with pytest.raises(AttributeError):
        # 刻意动态访问：断言该属性在运行时不存在，静态检查无法表达
        getattr(stored, "version")  # noqa: B009
    assert "version" not in stored.__dataclass_fields__


def test_prompt_template_loader_constructor_no_longer_accepts_defaults() -> None:
    """AC8(TSK-191)：PromptTemplateLoader 构造接口不再接受 defaults 参数。

    当前实现仍声明 ``defaults`` 参数，本用例是 TSK-191 的可解释 RED。
    """
    parameters = inspect.signature(PromptTemplateLoader.__init__).parameters

    assert "defaults" not in parameters


def test_validate_prompt_values_uses_schema_field_set_not_defaults() -> None:
    """AC3(TSK-191)：字段白名单来自 resource_id 对应强类型 Schema。

    新契约签名 ``validate_prompt_values(resource, values)``：传资源对象
    而不是空 dict 猜资源，字段集由 ``ensure_typed_prompt_model(
    resource.resource_id)`` 决定。旧实现以 defaults 键集作白名单且资源
    无 ``defaults`` 属性，因此本用例是 TSK-191 的可解释 RED。
    """
    resource = _resource("komari_chat")
    field_name = sorted(prompt_resource_field_names(resource.resource_id))[0]

    assert validate_prompt_values(resource, {field_name: "新值\n"}) == {
        field_name: "新值"
    }


def test_validate_prompt_values_rejects_unknown_and_blank_fields() -> None:
    """未知字段、空白字段、超预算字段一律拒绝（白名单来自资源 Schema）。

    当前实现读不到 ``resource.defaults``（契约要求资源不再携带
    defaults），因此本用例整体是 TSK-191 的可解释 RED；实现后空白/预算
    断言必须命中正文校验语义，而不是被当作「未知字段」。
    """
    resource = _resource("komari_chat")
    field_name = sorted(prompt_resource_field_names(resource.resource_id))[0]

    assert validate_prompt_values(resource, {field_name: "新值"}) == {
        field_name: "新值"
    }
    with pytest.raises(ValueError, match="未知提示词字段"):
        validate_prompt_values(resource, {"unknown": "新值"})
    with pytest.raises(ValueError, match="非空字符串"):
        validate_prompt_values(resource, {field_name: "   "})
    with pytest.raises(ValueError, match="字符上限"):
        validate_prompt_values(resource, {field_name: "字" * 12_001})


@pytest.mark.parametrize("resource_id", PROMPT_RESOURCE_IDS)
def test_save_prompt_values_accepts_complete_schema_payload_per_resource(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
) -> None:
    """AC3(TSK-191)：写入白名单来自资源 Schema；无 defaults 时完整载荷可写。

    三个资源一起验证；旧实现以 defaults 键集作白名单且资源无 defaults，
    因此本用例是 TSK-191 的可解释 RED。
    """
    resource = _resource(resource_id)
    fake_storage = _FakePromptStorage()
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    values = prompt_marker_values(resource_id)
    saved = save_prompt_values(resource, values)

    assert saved.prompt_data == values
    assert fake_storage.saved == values


@pytest.mark.parametrize(
    ("resource_id", "foreign_field"),
    sorted(CROSS_RESOURCE_FOREIGN_FIELDS.items()),
)
def test_save_prompt_values_rejects_cross_resource_field(
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
    foreign_field: str,
) -> None:
    """AC3(TSK-191)：A 资源字段注入 B 资源写入必须拒绝，且不落库。

    白名单是 resource_id 对应 Schema，不是三个资源字段的全局 union。
    旧实现资源无 ``defaults`` 属性，因此本用例是 TSK-191 的可解释 RED。
    """
    resource = _resource(resource_id)
    fake_storage = _FakePromptStorage()
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: fake_storage)

    values = prompt_marker_values(resource_id)
    values[foreign_field] = "跨资源字段值"

    with pytest.raises(ValueError, match="未知提示词字段"):
        save_prompt_values(resource, values)
    assert fake_storage.saved is None, "拒绝的载荷不得落入存储"


def test_prompt_template_loader_falls_back_to_cache_on_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：成功加载后存储读取失败时回退最后有效缓存（同步路径）。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(prompt_storage, "monotonic", lambda: clock["now"])

    v1 = _complete_row(system_prompt="PG 值")
    storage = _ScriptedStorage([_stored(v1), RuntimeError("PG 故障")])
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    loader = _loader()

    assert loader.get_template() == v1
    clock["now"] += 2.0
    assert loader.get_template() == v1
    assert storage.calls == 2


@pytest.mark.asyncio
async def test_async_prompt_loader_uses_cache_and_notification_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存命中零 SQL；本进程写入失效回调后重读。"""
    v1 = _complete_row(system_prompt="PG 值 1")
    v2 = _complete_row(system_prompt="PG 值 2")
    storage = _ScriptedStorage([_stored(v1, revision=1), _stored(v2, revision=2)])
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    loader = _loader()

    results = [await loader.get_template_async() for _ in range(100)]
    assert storage.calls == 1
    assert all(result == v1 for result in results)

    callback = cast("Any", storage.callback)
    callback()
    assert await loader.get_template_async() == v2
    assert storage.calls == 2


@pytest.mark.asyncio
async def test_async_prompt_loader_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowStorage:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def register_invalidator(
            self, _resource_id: str, _callback: object
        ) -> None:
            return

        async def fetch_async(
            self, resource_id: str
        ) -> StoredPrompt | None:
            del resource_id
            self.started.set()
            await self.release.wait()
            return _stored(_complete_row())

        async def update_if_unchanged_async(self, **_kwargs: object) -> None:
            return None

    storage = _SlowStorage()
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    loader = _loader()

    operation = asyncio.create_task(loader.get_template_async())
    await storage.started.wait()
    ticker_ran = False

    async def _tick() -> None:
        nonlocal ticker_ran
        await asyncio.sleep(0)
        ticker_ran = True

    await _tick()
    assert ticker_ran is True
    assert operation.done() is False
    storage.release.set()
    assert await operation == _complete_row()


@pytest.mark.asyncio
async def test_sync_prompt_loader_rejects_running_event_loop() -> None:
    loader = _loader()

    with pytest.raises(RuntimeError, match="禁止同步读取 Prompt"):
        loader.get_template()


def test_prompt_loader_refetches_after_staleness_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存 1 秒陈限：界内命中缓存，超界重新读取并接受新值。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(prompt_storage, "monotonic", lambda: clock["now"])

    v1 = _complete_row(system_prompt="第一版")
    v2 = _complete_row(system_prompt="第二版")
    storage = _ScriptedStorage([_stored(v1, revision=1), _stored(v2, revision=2)])
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    loader = _loader()

    assert loader.get_template() == v1
    assert storage.calls == 1
    assert loader.get_template() == v1
    assert storage.calls == 1

    clock["now"] += 2.0
    assert loader.get_template() == v2
    assert storage.calls == 2


def test_sync_operations_inside_event_loop_raise() -> None:
    storage = PromptStorage()

    async def _caller() -> None:
        with pytest.raises(RuntimeError, match="请改用对应的 _async"):
            storage.fetch("komari_chat")

    try:
        asyncio.run(_caller())
    finally:
        storage.close()


def test_closed_storage_rejects_new_operations() -> None:
    storage = PromptStorage()
    storage.close()

    with pytest.raises(RuntimeError, match="Prompt 存储已关闭"):
        storage.fetch("komari_chat")


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


def test_private_engine_uses_orm_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_urls: list[str] = []

    class _FakeEngine:
        def __init__(self, url: str) -> None:
            created_urls.append(url)
            self.sync_engine = object()

        async def dispose(self) -> None:
            return None

    async def _simple_op(session: object) -> str:
        del session
        return "ok"

    monkeypatch.setattr(
        prompt_storage,
        "create_async_engine",
        lambda url: _FakeEngine(url),
    )
    monkeypatch.setattr(
        prompt_storage,
        "get_orm_database_url",
        lambda: "postgresql+asyncpg://u:p@h:5432/db",
    )

    storage = PromptStorage()
    try:
        result = asyncio.run(storage._run_on_private_engine(_simple_op))
        assert result == "ok"
        assert created_urls == ["postgresql+asyncpg://u:p@h:5432/db"]
    finally:
        storage.close()


def test_prompt_storage_no_longer_builds_url_from_postgres_config() -> None:
    assert not hasattr(prompt_storage, "_build_database_url")
    assert not hasattr(prompt_storage, "get_shared_database_config")


def test_loader_cold_start_without_stored_prompt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4：无 DB 值且无缓存时，Loader 冷启动必须明确失败。"""
    storage = _ScriptedStorage([None])
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    loader = _loader()

    with pytest.raises(RuntimeError, match="Prompt"):
        asyncio.run(loader.get_template_async())


def test_async_loader_keeps_last_valid_cache_on_db_failure_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：成功加载后 DB 暂时故障继续使用最后有效缓存；恢复后刷新 revision。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(prompt_storage, "monotonic", lambda: clock["now"])

    v1 = _complete_row(system_prompt="PG 值 v1")
    v2 = _complete_row(system_prompt="PG 值 v2")
    storage = _ScriptedStorage(
        [_stored(v1, revision=1), RuntimeError("PG 暂时故障（测试模拟）"), _stored(v2, revision=2)]
    )
    monkeypatch.setattr(prompt_storage, "get_prompt_storage", lambda: storage)
    loader = _loader()

    assert asyncio.run(loader.get_template_async()) == v1
    assert storage.calls == 1

    # 缓存仍新鲜：不触发读取
    assert asyncio.run(loader.get_template_async()) == v1
    assert storage.calls == 1

    # 越过陈限后读取失败：保留最后有效缓存
    clock["now"] += 2.0
    assert asyncio.run(loader.get_template_async()) == v1
    assert storage.calls == 2

    # 恢复后读取新值并刷新缓存
    clock["now"] += 2.0
    assert asyncio.run(loader.get_template_async()) == v2
    assert storage.calls == 3
