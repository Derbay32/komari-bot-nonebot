"""测试公共初始化。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

import nonebot.plugin
from nonebug import NONEBOT_INIT_KWARGS

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _PytestConfigWithStash(Protocol):
    stash: dict[object, object]


def pytest_configure(config: object) -> None:
    """在 NoneBug 初始化前写入 NoneBot 启动参数。"""
    pytest_config = cast("_PytestConfigWithStash", config)
    pytest_config.stash[NONEBOT_INIT_KWARGS] = {
        "driver": "~fastapi",
        "command_start": ["。", "."],
        "command_sep": [" "],
        "fastapi_docs_url": "/api/komari-management/docs",
        "fastapi_openapi_url": "/api/komari-management/openapi.json",
        "fastapi_redoc_url": None,
        "fastapi_include_adapter_schema": False,
    }


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
_ensure_package_shim("komari_management")
_ensure_package_shim("character_binding")
_ensure_package_shim("komari_chat")
_ensure_package_shim("user_data")


class _DummyConfigManager:
    def get(self) -> object:
        return SimpleNamespace(
            plugin_enable=True,
            llm_model="deepseek-chat",
            llm_temperature=1.0,
            llm_max_tokens=8192,
        )


class _DummyConfigManagerPlugin:
    @staticmethod
    def get_config_manager(name: str, schema: object) -> _DummyConfigManager:
        del name, schema
        return _DummyConfigManager()


class _DummyLLMProvider:
    @staticmethod
    async def generate_text(**_kwargs: object) -> str:
        return "对话内容已模糊化处理"

    @staticmethod
    async def generate_text_with_messages(**_kwargs: object) -> str:
        return "<content>对话内容已模糊化处理</content>"

    @staticmethod
    async def generate_messages_completion(**_kwargs: object) -> object:
        return SimpleNamespace(content="规划完成", tool_calls=[], finish_reason="stop")


class _DummyUserDataPlugin:
    @staticmethod
    def get_config() -> object:
        return SimpleNamespace(
            max_favorability_delta_per_reply=5,
        )

    @staticmethod
    async def get_user_favorability(user_id: str) -> object:
        return SimpleNamespace(
            user_id=user_id,
            favorability=100,
            stage_index=2,
            stage_name="普通熟人",
            stage_prompt="正常交流即可，不额外亲昵。",
            updated_at="2026-06-07T00:00:00+00:00",
        )

    @staticmethod
    async def adjust_user_favorability(user_id: str, delta: int) -> object:
        return SimpleNamespace(
            user_id=user_id,
            before=100,
            delta=delta,
            after=max(0, min(400, 100 + delta)),
            stage_index=2,
            stage_name="普通熟人",
            updated_at="2026-06-07T00:00:00+00:00",
        )


class _DummyPermissionManagerPlugin:
    @staticmethod
    async def check_runtime_permission(
        _bot: object,
        _event: object,
        _config: object,
    ) -> tuple[bool, str]:
        return True, ""

    @staticmethod
    async def check_plugin_status(_config: object) -> tuple[bool, str]:
        return True, "🟢 正常"

    @staticmethod
    def format_permission_info(_config: object) -> str:
        return "权限正常"


class _DummyMemoryPlugin:
    @staticmethod
    def get_plugin_manager() -> object | None:
        return None


class _DummyKnowledgePlugin:
    @staticmethod
    async def search_knowledge(**_kwargs: object) -> list[object]:
        return []

    @staticmethod
    async def search_by_keyword(*_args: object, **_kwargs: object) -> list[object]:
        return []


class _DummyCharacterBindingPlugin:
    @staticmethod
    def get_character_name(user_id: str, fallback_nickname: str = "") -> str:
        return fallback_nickname or user_id

    @staticmethod
    def refresh_if_file_updated() -> bool:
        return False


class _DummySearchPlugin:
    @staticmethod
    def is_search_available() -> bool:
        return False

    @staticmethod
    async def search_web(_query: str) -> str:
        return "[测试搜索未启用]"


class _DummyUnifiedCandidateRerankService:
    async def rank_message(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            best_scene_id="scene_group_history_summary",
            best_scene_score=1.0,
            meaningful_score=1.0,
            noise_score=0.0,
        )


class _DummyDecisionPlugin:
    UnifiedCandidateRerankService = _DummyUnifiedCandidateRerankService


_REQUIRE_REGISTRY: dict[str, object] = {
    "config_manager": _DummyConfigManagerPlugin(),
    "llm_provider": _DummyLLMProvider(),
    "user_data": _DummyUserDataPlugin(),
    "permission_manager": _DummyPermissionManagerPlugin(),
    "komari_memory": _DummyMemoryPlugin(),
    "komari_knowledge": _DummyKnowledgePlugin(),
    "character_binding": _DummyCharacterBindingPlugin(),
    "komari_search": _DummySearchPlugin(),
    "komari_decision": _DummyDecisionPlugin(),
}


def _fake_require(name: str) -> object:
    """测试阶段替换 nonebot.require，避免真实插件加载。"""
    plugin = _REQUIRE_REGISTRY.get(name)
    if plugin is not None:
        return plugin
    msg = f"Unsupported plugin require in tests: {name}"
    raise RuntimeError(msg)


nonebot.plugin.require = _fake_require
