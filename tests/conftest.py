"""测试公共初始化。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

import nonebot.plugin

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.nonebot_compat import (
    install_nonebot_forwardref_compatibility,
)

install_nonebot_forwardref_compatibility()

from nonebug import NONEBOT_INIT_KWARGS


class _PytestConfigWithStash(Protocol):
    stash: dict[object, object]


def pytest_configure(config: object) -> None:
    """在 NoneBug 初始化前写入 NoneBot 启动参数。"""
    pytest_config = cast("_PytestConfigWithStash", config)
    pytest_config.stash[NONEBOT_INIT_KWARGS] = {
        "driver": "~fastapi",
        "command_start": ["。", "."],
        "command_sep": [" "],
        "superusers": {"42", "669293859"},
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
_ensure_package_shim("agent_run_logger")
_ensure_package_shim("komari_management")
_ensure_package_shim("character_binding")
_ensure_package_shim("komari_chat")
_ensure_package_shim("user_data")
_ensure_package_shim("user_ban")
_ensure_package_shim("komari_custom")
_ensure_package_shim("config_manager")


def _inject_package_exports(plugin_name: str, exports: dict[str, object]) -> None:
    """向已 shim 化的包模块注入导出以供测试使用。"""
    package_name = f"komari_bot.plugins.{plugin_name}"
    mod = sys.modules.get(package_name)
    if mod is not None:
        for name, val in exports.items():
            setattr(mod, name, val)


class _DummyConfigManager:
    def get(self) -> object:
        return SimpleNamespace(
            plugin_enable=True,
            llm_model="deepseek-chat",
            llm_temperature=1.0,
            llm_max_tokens=8192,
        )

    async def get_async(self) -> object:
        return self.get()


class _DummyConfigManagerPlugin:
    @staticmethod
    def get_config_manager(name: str, schema: object) -> _DummyConfigManager:
        del name, schema
        return _DummyConfigManager()


class _DummyLLMProvider:
    @staticmethod
    async def generate_text(**_kwargs: object) -> str:
        return "<content>有效的模糊化测试内容</content>"

    @staticmethod
    async def generate_text_with_messages(**_kwargs: object) -> str:
        return "<content>有效的模糊化测试内容</content>"

    @staticmethod
    async def generate_messages_completion(**_kwargs: object) -> object:
        return SimpleNamespace(
            content="规划完成",
            tool_calls=[],
            finish_reason="stop",
            duration_ms=100.0,
            usage=None,
            reasoning_content=None,
        )

    @staticmethod
    async def generate_completion(**_kwargs: object) -> object:
        return SimpleNamespace(
            content="重写后查询",
            tool_calls=[],
            finish_reason="stop",
            duration_ms=50.0,
            usage=None,
            reasoning_content=None,
        )


class _DummyAgentRunLoggerPlugin:
    @staticmethod
    def create_collector(**kwargs: object) -> object | None:
        if (
            not kwargs.get("force_collect")
            and kwargs.get("origin", "normal") != "debug"
        ):
            return None
        from komari_bot.plugins.agent_run_logger.diagnostic import AgentRunCollector

        return AgentRunCollector(
            request_id=cast("str | None", kwargs.get("trace_id")),
            run_type=cast("Any", kwargs.get("run_type", "chat_reply")),
            task_kind=str(kwargs.get("task_kind", "test")),
            origin=cast("Any", kwargs.get("origin", "normal")),
            input_data=kwargs.get("input_data"),
            persist=False,
        )

    @staticmethod
    async def finalize_collector(
        collector: object,
        **kwargs: object,
    ) -> bool:
        if collector is None:
            return False
        mark_finished = cast("Any", collector).mark_finished
        return bool(
            mark_finished(
                status=kwargs.get("status", "success"),
                output=kwargs.get("output"),
                error=kwargs.get("error"),
            )
        )

    @staticmethod
    def get_agent_run_log_reader() -> None:
        return None

    @staticmethod
    def register_agent_run_log_api(*_args: object, **_kwargs: object) -> None:
        return None

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
            favorability=0,
            stage_index=1,
            stage_name="疏离戒备",
            stage_prompt="当前关系偏疏离和戒备，回复应克制、保持距离，不主动表现亲昵。",
            updated_at="2026-06-07T00:00:00+00:00",
        )

    @staticmethod
    async def adjust_user_favorability(user_id: str, delta: int) -> object:
        return SimpleNamespace(
            user_id=user_id,
            before=0,
            delta=delta,
            after=max(0, min(400, delta)),
            stage_index=1,
            stage_name="疏离戒备",
            updated_at="2026-06-07T00:00:00+00:00",
        )

    @staticmethod
    async def set_user_favorability(user_id: str, value: int) -> object:
        return SimpleNamespace(
            user_id=user_id,
            before=0,
            after=value,
            stage_index=1 if value < 100 else 2,
            stage_name="疏离戒备" if value < 100 else "普通熟人",
            updated_at="2026-06-07T00:00:00+00:00",
        )

    @staticmethod
    async def get_user_count() -> int:
        return 0


class _DummyPermissionManagerPlugin:
    @staticmethod
    def check_context_permission(
        config: object,
        *,
        user_id: str,
        group_id: str | None,
        is_superuser: bool = False,
    ) -> tuple[bool, str]:
        if not bool(getattr(config, "plugin_enable", True)):
            return False, "插件当前已禁用"
        if is_superuser:
            return True, ""
        user_whitelist = getattr(config, "user_whitelist", [])
        group_whitelist = getattr(config, "group_whitelist", [])
        if user_whitelist and user_id not in user_whitelist:
            return False, "用户不在白名单"
        if group_id is not None and group_whitelist and group_id not in group_whitelist:
            return False, "群组不在白名单"
        return True, ""

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


class _DummyUserBanPlugin:
    class BanServiceUnavailableError(RuntimeError):
        pass

    @staticmethod
    async def is_event_banned(
        _bot: object,
        _event: object,
        _scope: object,
    ) -> bool:
        return False


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

    @staticmethod
    def get_binding_manager() -> object:
        return _DummyBindingManager()


class _DummyBindingManager:
    def __init__(self) -> None:
        self._bindings: dict[str, str] = {}

    def has_binding(self, user_id: str) -> bool:
        return user_id in self._bindings

    def get_character_name(
        self, user_id: str, fallback_nickname: str | None = None
    ) -> str:
        if user_id in self._bindings:
            return self._bindings[user_id]
        if fallback_nickname:
            return fallback_nickname
        return user_id

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        self._bindings[user_id] = character_name

    async def remove_character_name(self, user_id: str) -> bool:
        if user_id not in self._bindings:
            return False
        del self._bindings[user_id]
        return True

    def list_bindings(self) -> dict[str, str]:
        return self._bindings.copy()


class _DummyChatPlugin:
    @staticmethod
    async def generate_debug_reply(**kwargs: object) -> object:
        from komari_bot.plugins.agent_run_logger.diagnostic import (
            LLMDiagnosticCollector,
        )

        collector = kwargs.get("collector")
        if collector is None:
            collector = LLMDiagnosticCollector(request_id="test-debug")
        return SimpleNamespace(
            reply="测试回复内容",
            reply_to_message_id=None,
            favorability_delta=5,
            favorability_reason="测试好感度变化",
            interaction_history=None,
            collector=collector,
        )


class _DummyGroupHistorySummaryPlugin:
    class SummaryBusyError(Exception):
        pass

    class CapabilityNotSupportedError(Exception):
        pass


class _DummySearchPlugin:
    @staticmethod
    def is_search_available(**_kwargs: object) -> bool:
        return False

    @staticmethod
    async def search_web(_query: str, **_kwargs: object) -> str:
        return "[测试搜索未启用]"


class _DummyEmbeddingPlugin:
    @staticmethod
    async def embed(_text: str, instruction: str = "") -> list[float]:
        del instruction
        return [0.1, 0.2, 0.3]


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
    "agent_run_logger": _DummyAgentRunLoggerPlugin(),
    "embedding_provider": _DummyEmbeddingPlugin(),
    "user_data": _DummyUserDataPlugin(),
    "permission_manager": _DummyPermissionManagerPlugin(),
    "user_ban": _DummyUserBanPlugin(),
    "komari_memory": _DummyMemoryPlugin(),
    "komari_knowledge": _DummyKnowledgePlugin(),
    "character_binding": _DummyCharacterBindingPlugin(),
    "komari_search": _DummySearchPlugin(),
    "komari_decision": _DummyDecisionPlugin(),
    "komari_chat": _DummyChatPlugin(),
    "group_history_summary": _DummyGroupHistorySummaryPlugin(),
}


def _fake_require(name: str) -> object:
    """测试阶段替换 nonebot.require，避免真实插件加载。"""
    plugin = _REQUIRE_REGISTRY.get(name)
    if plugin is not None:
        return plugin
    msg = f"Unsupported plugin require in tests: {name}"
    raise RuntimeError(msg)


nonebot.plugin.require = _fake_require


# 为 komari_debug 测试注入包级导出到 shim
_inject_package_exports(
    "config_manager",
    {"get_config_manager": _DummyConfigManagerPlugin.get_config_manager},
)
_inject_package_exports(
    "character_binding",
    {"get_binding_manager": _DummyCharacterBindingPlugin.get_binding_manager},
)
_inject_package_exports(
    "user_data",
    {
        "get_user_favorability": _DummyUserDataPlugin.get_user_favorability,
        "set_user_favorability": _DummyUserDataPlugin.set_user_favorability,
        "get_user_count": _DummyUserDataPlugin.get_user_count,
    },
)
_inject_package_exports(
    "komari_chat",
    {"generate_debug_reply": _DummyChatPlugin.generate_debug_reply},
)
_inject_package_exports(
    "agent_run_logger",
    {
        "create_collector": _DummyAgentRunLoggerPlugin.create_collector,
        "finalize_collector": _DummyAgentRunLoggerPlugin.finalize_collector,
        "get_agent_run_log_reader": _DummyAgentRunLoggerPlugin.get_agent_run_log_reader,
        "register_agent_run_log_api": _DummyAgentRunLoggerPlugin.register_agent_run_log_api,
    },
)
