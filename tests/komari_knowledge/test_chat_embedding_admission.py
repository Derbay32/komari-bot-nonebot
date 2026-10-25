"""Admission checks at the knowledge embedding and chat-injection seams.

The permanent regression drives the real chat core, the real prompt builder,
the actual public ``komari_knowledge.search_knowledge`` wrapper and the actual
``KnowledgeEngine``. Only the external SDK/DB/embedding boundaries are
substituted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins import embedding_provider
from komari_bot.plugins.komari_knowledge import engine as engine_module
from komari_bot.plugins.komari_knowledge.engine import KnowledgeEngine
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from komari_bot.plugins.komari_knowledge.engine import SearchResult

_EMBEDDING = [0.1, 0.2, 0.3]
_KNOWLEDGE_CONTENT = "知识命中"
_KNOWLEDGE_ROW = {
    "id": 7,
    "category": "general",
    "content": _KNOWLEDGE_CONTENT,
    "similarity": 0.9,
}


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "query_rewrite_rules": {},
        "total_limit": 5,
        "layer1_limit": 0,
        "layer2_limit": 2,
        "similarity_threshold": 0.5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _RecordingPool:
    """Minimal shared-pool substitute that records query starts."""

    def __init__(
        self,
        *,
        keyword_rows: list[dict[str, Any]] | None = None,
        vector_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.queries: list[str] = []
        self.keyword_rows = list(keyword_rows or [])
        self.vector_rows = list(vector_rows or [])

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator["_RecordingConnection"]:
        yield _RecordingConnection(self)

    @property
    def vector_queries(self) -> list[str]:
        return [query for query in self.queries if "embedding <=>" in query]


class _RecordingConnection:
    def __init__(self, pool: _RecordingPool) -> None:
        self.pool = pool

    async def fetch(
        self,
        query: str,
        *_args: object,
    ) -> list[dict[str, Any]]:
        self.pool.queries.append(query)
        if "embedding <=>" in query:
            return list(self.pool.vector_rows)
        return list(self.pool.keyword_rows)


class _KeywordIndex:
    """Loaded in-memory keyword index with optional async refresh hook."""

    def __init__(
        self,
        entries: dict[str, set[int]] | None = None,
        *,
        on_ensure: Any = None,
    ) -> None:
        self.loaded = entries is not None
        self.entries = entries or {}
        self.on_ensure = on_ensure
        self.ensure_calls = 0

    async def ensure_fresh(self, _pool: Any, _loader: Any) -> bool:
        self.ensure_calls += 1
        if self.on_ensure is not None:
            await self.on_ensure()
        return False


class _DriverStub:
    """NoneBot driver substitute for loading the real public wrapper module."""

    def on_startup(self, callback: Any) -> Any:
        return callback

    def on_shutdown(self, callback: Any) -> Any:
        return callback


def _load_public_wrapper(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Execute the real package entry module without plugin-startup side effects."""
    import nonebot

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


@pytest.fixture
def actual_wrapper(monkeypatch: pytest.MonkeyPatch) -> Any:
    return _load_public_wrapper(monkeypatch)


def _build_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding_calls: list[str],
    config: SimpleNamespace | None = None,
    pool: _RecordingPool | None = None,
    index: _KeywordIndex | None = None,
) -> KnowledgeEngine:
    monkeypatch.setattr(engine_module, "get_config", lambda: config or _config())
    monkeypatch.setattr(engine_module.state, "nonebot_mode", True)

    async def _embed(text: str) -> list[float]:
        embedding_calls.append(text)
        return list(_EMBEDDING)

    monkeypatch.setattr(embedding_provider, "embed", _embed)
    engine = KnowledgeEngine()
    engine._pool = pool or _RecordingPool()
    engine._keyword_index = cast("Any", index or _KeywordIndex())
    return engine


async def test_engine_guard_false_skips_embedding_and_vector_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool()
    engine = _build_engine(monkeypatch, embedding_calls=embedding_calls, pool=pool)
    guard_calls: list[str] = []

    def _deny() -> bool:
        guard_calls.append("called")
        return False

    hits = await engine.search("你好", limit=3, embedding_guard=_deny)

    assert hits == []
    assert guard_calls == ["called"]
    assert embedding_calls == []
    assert pool.vector_queries == []


async def test_engine_guard_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool()
    engine = _build_engine(monkeypatch, embedding_calls=embedding_calls, pool=pool)

    def _raise() -> bool:
        raise RuntimeError("admission unavailable")  # noqa: TRY003

    hits = await engine.search("你好", limit=3, embedding_guard=_raise)

    assert hits == []
    assert embedding_calls == []
    assert pool.vector_queries == []


async def test_supplied_query_vector_needs_no_embedding_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool()
    engine = _build_engine(monkeypatch, embedding_calls=embedding_calls, pool=pool)
    guard_calls: list[str] = []

    def _guard() -> bool:
        guard_calls.append("called")
        return False

    hits = await engine.search(
        "你好",
        limit=3,
        query_vec=[1.0, 0.0, 0.0],
        embedding_guard=_guard,
    )

    assert hits == []
    assert guard_calls == []
    assert embedding_calls == []
    assert len(pool.vector_queries) == 1


async def test_rewrite_invalidated_vector_rechecks_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool()
    engine = _build_engine(
        monkeypatch,
        embedding_calls=embedding_calls,
        pool=pool,
        config=_config(query_rewrite_rules={"你好": "改写过"}),
    )
    guard_calls: list[str] = []

    def _deny() -> bool:
        guard_calls.append("called")
        return False

    hits = await engine.search(
        "你好",
        limit=3,
        query_vec=[1.0, 0.0, 0.0],
        embedding_guard=_deny,
    )

    assert hits == []
    assert guard_calls == ["called"]
    assert embedding_calls == []
    assert pool.vector_queries == []


async def test_keyword_only_fill_never_embeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool(keyword_rows=[_KNOWLEDGE_ROW])
    index = _KeywordIndex({"你好": {7}})
    engine = _build_engine(
        monkeypatch,
        embedding_calls=embedding_calls,
        pool=pool,
        index=index,
        config=_config(layer1_limit=1, layer2_limit=2, total_limit=1),
    )
    guard_calls: list[str] = []

    def _guard() -> bool:
        guard_calls.append("called")
        return False

    hits = await engine.search("你好", limit=1, embedding_guard=_guard)

    assert [hit.id for hit in hits] == [7]
    assert hits[0].source == "keyword"
    assert guard_calls == []
    assert embedding_calls == []
    assert pool.vector_queries == []
    assert index.ensure_calls == 1


async def test_denied_vector_keeps_keyword_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool(keyword_rows=[_KNOWLEDGE_ROW])
    index = _KeywordIndex({"你好": {7}})
    engine = _build_engine(
        monkeypatch,
        embedding_calls=embedding_calls,
        pool=pool,
        index=index,
        config=_config(layer1_limit=1, layer2_limit=1, total_limit=2),
    )
    guard_calls: list[str] = []

    def _deny() -> bool:
        guard_calls.append("called")
        return False

    hits = await engine.search("你好", limit=2, embedding_guard=_deny)

    assert [hit.id for hit in hits] == [7]
    assert guard_calls == ["called"]
    assert embedding_calls == []
    assert pool.vector_queries == []


async def test_public_wrapper_forwards_embedding_guard(
    monkeypatch: pytest.MonkeyPatch,
    actual_wrapper: Any,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool()
    engine = _build_engine(monkeypatch, embedding_calls=embedding_calls, pool=pool)
    monkeypatch.setattr(engine_module.state, "engine", engine)
    guard_calls: list[str] = []

    def _deny() -> bool:
        guard_calls.append("called")
        return False

    hits = await actual_wrapper.search_knowledge(
        "你好",
        limit=3,
        embedding_guard=_deny,
    )

    assert hits == []
    assert guard_calls == ["called"]
    assert embedding_calls == []
    assert pool.vector_queries == []


async def test_independent_knowledge_caller_without_guard_still_embeds(
    monkeypatch: pytest.MonkeyPatch,
    actual_wrapper: Any,
) -> None:
    embedding_calls: list[str] = []
    pool = _RecordingPool(vector_rows=[_KNOWLEDGE_ROW])
    engine = _build_engine(monkeypatch, embedding_calls=embedding_calls, pool=pool)
    monkeypatch.setattr(engine_module.state, "engine", engine)

    hits = await actual_wrapper.search_knowledge("你好", limit=3)

    assert [hit.id for hit in hits] == [7]
    assert embedding_calls == ["你好"]
    assert len(pool.vector_queries) == 1


async def _run_chat_knowledge_turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_after_recall: str,
) -> SimpleNamespace:
    from tests.group_admission import test_chat_seam_admission as seam
    from tests.komari_chat import test_prompt_builder as prompts
    from tests.komari_chat.hindsight_recall_support import (
        FakeHindsightMemory,
        FakeRecallSession,
    )

    admission = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, admission)
    actual_wrapper = _load_public_wrapper(monkeypatch)
    embedding_calls: list[str] = []
    pool = _RecordingPool(vector_rows=[_KNOWLEDGE_ROW])
    engine = _build_engine(monkeypatch, embedding_calls=embedding_calls, pool=pool)
    monkeypatch.setattr(engine_module.state, "engine", engine)

    seam._patch_recall_core(monkeypatch)
    config = seam.mh.get_config()
    config.knowledge_enabled = True
    config.knowledge_limit = 3

    async def _favorability(_user_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            user_id="u1",
            favorability=0,
            stage_index=1,
            stage_name="疏离戒备",
            stage_prompt="保持距离",
        )

    monkeypatch.setattr(
        seam.mh.user_data_plugin,
        "get_user_favorability",
        _favorability,
    )
    prompts._patch_dependencies(monkeypatch)
    monkeypatch.setattr(
        prompts.prompt_builder_module,
        "komari_knowledge",
        actual_wrapper,
    )
    monkeypatch.setattr(
        seam.mh,
        "build_prompt",
        prompts.prompt_builder_module.build_prompt,
    )
    generate_calls: list[dict[str, Any]] = []

    async def _generate(**kwargs: Any) -> Any:
        generate_calls.append(kwargs)
        return seam.llm.ReplyResult(
            content="ok",
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(seam.mh, "generate_reply_with_tools", _generate)
    original_recall = FakeRecallSession.recall

    async def _recall(self: Any, query: str) -> Any:
        response = await original_recall(self, query)
        admission.set_state(state_after_recall)
        return response

    monkeypatch.setattr(FakeRecallSession, "recall", _recall)
    memory = FakeHindsightMemory()
    result = await seam._run_recall_core(seam._recall_handler(memory))
    return SimpleNamespace(
        result=result,
        generate_calls=generate_calls,
        embedding_calls=embedding_calls,
        pool=pool,
        memory=memory,
    )


@pytest.mark.parametrize("state_after_recall", ["restricted", "failed"])
async def test_chat_knowledge_after_recall_loss_never_embeds(
    monkeypatch: pytest.MonkeyPatch,
    state_after_recall: str,
) -> None:
    run = await _run_chat_knowledge_turn(
        monkeypatch,
        state_after_recall=state_after_recall,
    )

    assert run.memory.recall_calls == ["你好"]
    assert run.embedding_calls == []
    assert run.pool.vector_queries == []
    assert run.result.content == "ok"
    rendered = json.dumps(run.generate_calls[0]["messages"], ensure_ascii=False)
    assert _KNOWLEDGE_CONTENT not in rendered


async def test_chat_knowledge_after_recall_admitted_embeds_and_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_chat_knowledge_turn(
        monkeypatch,
        state_after_recall="admitted",
    )

    assert run.memory.recall_calls == ["你好"]
    assert run.embedding_calls == ["你好"]
    assert len(run.pool.vector_queries) == 1
    rendered = json.dumps(run.generate_calls[0]["messages"], ensure_ascii=False)
    assert _KNOWLEDGE_CONTENT in rendered


async def test_revocation_during_async_keyword_refresh_blocks_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)

    async def _revoke_during_refresh() -> None:
        scripted.set_state("restricted")

    index = _KeywordIndex(
        {"你好": set()},
        on_ensure=_revoke_during_refresh,
    )
    embedding_calls: list[str] = []
    pool = _RecordingPool()
    engine = _build_engine(
        monkeypatch,
        embedding_calls=embedding_calls,
        pool=pool,
        index=index,
        config=_config(layer1_limit=1),
    )
    guard_calls: list[str] = []

    def _live_guard() -> bool:
        guard_calls.append("called")
        return scripted.state == "admitted"

    hits: list[SearchResult] = await engine.search(
        "你好",
        limit=3,
        embedding_guard=_live_guard,
    )

    assert hits == []
    assert index.ensure_calls == 1
    assert guard_calls == ["called"]
    assert embedding_calls == []
    assert pool.vector_queries == []
