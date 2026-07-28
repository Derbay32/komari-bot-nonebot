"""KomariMemory 总结 worker 画像压缩测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import nonebot.plugin

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services.profile_compaction import (
    count_profile_traits,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    PROJECT_ROOT / "komari_bot/plugins/komari_memory/handlers/summary_worker.py"
)
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot/plugins/komari_memory"


def _make_profile(trait_count: int) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": "10001",
        "display_name": "阿明",
        "traits": {
            f"特征{i:02d}": {
                "value": f"长期描述{i}",
                "category": "general",
                "importance": 4,
                "updated_at": f"2026-03-21T00:00:{i % 60:02d}+08:00",
            }
            for i in range(trait_count)
        },
    }


def _load_summary_worker_module(monkeypatch: Any) -> Any:
    def _fake_require(name: str) -> object:
        if name == "character_binding":
            return types.SimpleNamespace(
                get_character_name=lambda user_id, fallback_nickname="": (
                    fallback_nickname or user_id
                )
            )
        if name == "llm_provider":
            return types.SimpleNamespace(generate_text=lambda **_kwargs: None)
        return object()

    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    memory_package = types.ModuleType("komari_bot.plugins.komari_memory")
    memory_package.__path__ = [str(PACKAGE_ROOT)]  # type: ignore[attr-defined]
    handlers_package = types.ModuleType("komari_bot.plugins.komari_memory.handlers")
    handlers_package.__path__ = [str(PACKAGE_ROOT / "handlers")]  # type: ignore[attr-defined]

    original_memory_package = sys.modules.get("komari_bot.plugins.komari_memory")
    original_handlers_package = sys.modules.get(
        "komari_bot.plugins.komari_memory.handlers"
    )
    original_module = sys.modules.get(
        "komari_bot.plugins.komari_memory.handlers.summary_worker"
    )

    sys.modules["komari_bot.plugins.komari_memory"] = memory_package
    sys.modules["komari_bot.plugins.komari_memory.handlers"] = handlers_package
    try:
        spec = importlib.util.spec_from_file_location(
            "komari_bot.plugins.komari_memory.handlers.summary_worker",
            MODULE_PATH,
        )
        if spec is None or spec.loader is None:
            raise AssertionError

        module = importlib.util.module_from_spec(spec)
        sys.modules["komari_bot.plugins.komari_memory.handlers.summary_worker"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if original_module is not None:
            sys.modules["komari_bot.plugins.komari_memory.handlers.summary_worker"] = (
                original_module
            )
        else:
            sys.modules.pop(
                "komari_bot.plugins.komari_memory.handlers.summary_worker",
                None,
            )

        if original_handlers_package is not None:
            sys.modules["komari_bot.plugins.komari_memory.handlers"] = (
                original_handlers_package
            )
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory.handlers", None)

        if original_memory_package is not None:
            sys.modules["komari_bot.plugins.komari_memory"] = original_memory_package
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory", None)


def test_enforce_profile_trait_limit_uses_compacted_profile(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)

    async def _fake_compact_profile_with_llm(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return _make_profile(20)

    monkeypatch.setattr(module, "compact_profile_with_llm", _fake_compact_profile_with_llm)

    result = asyncio.run(
        module._enforce_profile_trait_limit(
            group_id="114514",
            user_id="10001",
            base_profile=_make_profile(6),
            merged_profile=_make_profile(26),
            config=KomariMemoryConfigSchema(profile_trait_limit=20),
        )
    )

    assert count_profile_traits(result) == 20


def test_enforce_profile_trait_limit_falls_back_to_base_profile(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)

    async def _boom(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise RuntimeError("boom")

    base_profile = _make_profile(4)
    monkeypatch.setattr(module, "compact_profile_with_llm", _boom)

    result = asyncio.run(
        module._enforce_profile_trait_limit(
            group_id="114514",
            user_id="10001",
            base_profile=base_profile,
            merged_profile=_make_profile(25),
            config=KomariMemoryConfigSchema(profile_trait_limit=20),
        )
    )

    assert result == base_profile
