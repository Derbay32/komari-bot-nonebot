"""测试公共初始化。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import nonebot.plugin


def _ensure_package_shim() -> None:
    """为 komari_memory 包注入 shim，避免测试导入触发插件入口副作用。"""
    package_name = "komari_bot.plugins.komari_memory"
    if package_name in sys.modules:
        return

    project_root = Path(__file__).resolve().parents[1]
    package_path = project_root / "komari_bot" / "plugins" / "komari_memory"

    shim = types.ModuleType(package_name)
    shim.__path__ = [str(package_path)]  # type: ignore[attr-defined]
    sys.modules[package_name] = shim


_ensure_package_shim()


class _DummyConfigManager:
    def get(self) -> object:
        return SimpleNamespace()


class _DummyConfigManagerPlugin:
    @staticmethod
    def get_config_manager(name: str, schema: object) -> _DummyConfigManager:
        del name, schema
        return _DummyConfigManager()


def _fake_require(name: str) -> object:
    """测试阶段替换 nonebot.require，避免真实插件加载。"""
    if name == "config_manager":
        return _DummyConfigManagerPlugin()
    msg = f"Unsupported plugin require in tests: {name}"
    raise RuntimeError(msg)


nonebot.plugin.require = _fake_require
