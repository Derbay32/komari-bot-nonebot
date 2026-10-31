"""TSK-203 automatic knowledge lookup can bypass legacy query rewriting."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

engine_module = import_module("komari_bot.plugins.komari_knowledge.engine")
plugin_module = import_module("komari_bot.plugins.komari_knowledge")


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        total_limit=5,
        layer1_limit=3,
        layer2_limit=3,
        query_rewrite_rules={"你": "小鞠"},
        knowledge_enabled=True,
    )


class _Engine(engine_module.KnowledgeEngine):
    def __init__(self) -> None:
        super().__init__()
        self._pool = object()
        self.keyword_queries: list[str] = []
        self.vector_queries: list[tuple[str, Any]] = []

    async def _layer1_keyword_search(self, query: str, limit: int) -> list[Any]:
        del limit
        self.keyword_queries.append(query)
        return []

    async def _layer2_vector_search(
        self,
        query: str,
        limit: int,
        seen_ids: set[int],
        *,
        query_vec: list[float] | None = None,
        embedding_guard: Any = None,
    ) -> list[Any]:
        del limit, seen_ids, query_vec
        self.vector_queries.append((query, embedding_guard))
        return []


def test_engine_default_still_applies_rewrite_and_raw_query_bypasses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "get_config", _config)

    rewritten = _Engine()
    asyncio.run(rewritten.search("你喜欢什么"))
    assert rewritten.keyword_queries == ["小鞠喜欢什么"]
    assert rewritten.vector_queries[0][0] == "小鞠喜欢什么"

    raw = _Engine()
    guard = lambda: True  # noqa: E731
    asyncio.run(raw.search("你喜欢什么", raw_query=True, embedding_guard=guard))
    assert raw.keyword_queries == ["你喜欢什么"]
    assert raw.vector_queries == [("你喜欢什么", guard)]


def _load_public_wrapper(monkeypatch: pytest.MonkeyPatch) -> Any:
    import nonebot

    class _DriverStub:
        def on_startup(self, callback: Any) -> Any:
            return callback

        def on_shutdown(self, callback: Any) -> Any:
            return callback

    monkeypatch.setattr(nonebot, "get_driver", lambda: _DriverStub())
    package_dir = (
        Path(__file__).resolve().parents[2]
        / "komari_bot"
        / "plugins"
        / "komari_knowledge"
    )
    package_name = "komari_bot.plugins.komari_knowledge"
    previous_package = sys.modules.get(package_name)
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_package is None:
            sys.modules.pop(package_name, None)
        else:
            sys.modules[package_name] = previous_package
    return module


def test_public_search_wrapper_passes_raw_query_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    wrapper = _load_public_wrapper(monkeypatch)

    class _WrapperEngine:
        async def search(self, query: str, limit: int | None = None, **kwargs: object) -> list[Any]:
            calls.append({"query": query, "limit": limit, **kwargs})
            return []

    monkeypatch.setattr(wrapper, "get_engine", lambda: _WrapperEngine())
    monkeypatch.setattr(
        wrapper,
        "config_manager",
        SimpleNamespace(get=lambda: _config()),
    )

    guard = lambda: True  # noqa: E731
    asyncio.run(wrapper.search_knowledge("原文", 3, embedding_guard=guard))
    assert calls[-1] == {
        "query": "原文",
        "limit": 3,
        "query_vec": None,
        "embedding_guard": guard,
        "raw_query": False,
    }

    asyncio.run(
        wrapper.search_knowledge(
            "原文",
            3,
            embedding_guard=guard,
            raw_query=True,
        )
    )
    assert calls[-1]["raw_query"] is True


def test_raw_query_is_keyword_only_public_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = _load_public_wrapper(monkeypatch)
    signature = inspect.signature(wrapper.search_knowledge)
    parameter = signature.parameters["raw_query"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False

    engine_signature = inspect.signature(engine_module.KnowledgeEngine.search)
    engine_parameter = engine_signature.parameters["raw_query"]
    assert engine_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert engine_parameter.default is False
