"""测试公共初始化。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import nonebot.plugin

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _DummyScheduler:
    def add_job(self, *_args: object, **_kwargs: object) -> None:
        return None

    def remove_job(self, *_args: object, **_kwargs: object) -> None:
        return None


apscheduler_module = cast("Any", types.ModuleType("nonebot_plugin_apscheduler"))
apscheduler_module.scheduler = _DummyScheduler()
sys.modules.setdefault("nonebot_plugin_apscheduler", apscheduler_module)


def _ensure_package_shim(plugin_name: str) -> None:
    """为插件包注入 shim，避免测试导入触发插件入口副作用。"""
    package_name = f"komari_bot.plugins.{plugin_name}"
    if package_name in sys.modules:
        return

    package_path = PROJECT_ROOT / "komari_bot" / "plugins" / plugin_name

    shim = types.ModuleType(package_name)
    shim.__path__ = [str(package_path)]  # type: ignore[attr-defined]
    sys.modules[package_name] = shim


_ensure_package_shim("komari_memory")
_ensure_package_shim("komari_knowledge")
_ensure_package_shim("llm_provider")
_ensure_package_shim("character_binding")


class _DummyConfigManager:
    def get(self) -> object:
        return SimpleNamespace()


class _DummyConfigManagerPlugin:
    @staticmethod
    def get_config_manager(name: str, schema: object) -> _DummyConfigManager:
        del name, schema
        return _DummyConfigManager()


class _DummyLLMProvider:
    @staticmethod
    async def generate_text(**_kwargs: object) -> str:
        return "对话内容已模糊化处理"


def _fake_require(name: str) -> object:
    """测试阶段替换 nonebot.require，避免真实插件加载。"""
    if name == "config_manager":
        return _DummyConfigManagerPlugin()
    if name == "llm_provider":
        return _DummyLLMProvider()
    msg = f"Unsupported plugin require in tests: {name}"
    raise RuntimeError(msg)


nonebot.plugin.require = _fake_require
