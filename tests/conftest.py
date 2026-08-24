"""测试公共初始化。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

import nonebot.plugin
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.core.nonebot_compat import (
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
        "fastapi_docs_url": "/api/docs",
        "fastapi_openapi_url": "/api/openapi.json",
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
    def get_config_manager(
        name: str,
        schema: object,
        *,
        env_config_schema: object | None = None,
    ) -> _DummyConfigManager:
        del name, schema, env_config_schema
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
    class ReplyFulfillmentOpsConflictError(Exception):
        pass

    class ReplyFulfillmentOpsNotFoundError(Exception):
        pass

    class ReplyFulfillmentOpsValidationError(Exception):
        pass

    @staticmethod
    def get_reply_fulfillment_ops_service() -> object | None:
        return None

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
    def is_fetch_available(**_kwargs: object) -> bool:
        return True

    @staticmethod
    async def search_web(_query: str, **_kwargs: object) -> str:
        return "[测试搜索未启用]"

    @staticmethod
    async def fetch_page(_urls: list[str], **_kwargs: object) -> str:
        return "[测试抓取结果]"


class _DummyEmbeddingPlugin:
    @staticmethod
    async def embed(_text: str, instruction: str = "") -> list[float]:
        del instruction
        return [0.1, 0.2, 0.3]


class _DummyDecisionPlugin:
    pass


class _DummyGroupAdmissionPlugin:
    """require("group_admission") 只作加载声明，返回值不承载业务符号。

    业务裁决/状态符号一律由真实顶层包暴露面提供（ADR-0006），本票不对
    该包注入 shim，保证测试能验证真实顶层 ``__all__``。
    """


class _DummyApschedulerPlugin:
    """require("nonebot_plugin_apscheduler") 只作加载声明。

    TSK-248 起 group_admission 作为首个未 shim 的真实插件在包入口声明该硬
    依赖；实际 scheduler 对象由本文件顶部 ``sys.modules`` 的
    ``nonebot_plugin_apscheduler`` shim（``_DummyScheduler``）提供，
    ``lifecycle_context`` 会把它替换为可记录 fake。
    """


_REQUIRE_REGISTRY: dict[str, object] = {
    "config_manager": _DummyConfigManagerPlugin(),
    "group_admission": _DummyGroupAdmissionPlugin(),
    "nonebot_plugin_apscheduler": _DummyApschedulerPlugin(),
    "llm_provider": _DummyLLMProvider(),
    "agent_run_logger": _DummyAgentRunLoggerPlugin(),
    "embedding_provider": _DummyEmbeddingPlugin(),
    "user_data": _DummyUserDataPlugin(),
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


# 为 komari_debug 测试注入包级导出到 shim；group_admission 按 ADR-0006 从
# config_manager 顶层 import 版本化快照类型，因此注入真实类型身份（来自 .manager
# 子模块），dummy get_config_manager 保留且不获得新业务行为。
from komari_bot.plugins.config_manager.manager import (
    ConfigManager as _RealConfigManager,
)
from komari_bot.plugins.config_manager.manager import (
    ConfigSnapshot as _RealConfigSnapshot,
)

_inject_package_exports(
    "config_manager",
    {"get_config_manager": _DummyConfigManagerPlugin.get_config_manager},
)
_inject_package_exports(
    "config_manager",
    {
        "ConfigManager": _RealConfigManager,
        "ConfigSnapshot": _RealConfigSnapshot,
    },
)
_inject_package_exports(
    "character_binding",
    {
        "get_binding_manager": _DummyCharacterBindingPlugin.get_binding_manager,
        "get_character_name": _DummyCharacterBindingPlugin.get_character_name,
    },
)
_inject_package_exports(
    "user_data",
    {
        "get_user_favorability": _DummyUserDataPlugin.get_user_favorability,
        "set_user_favorability": _DummyUserDataPlugin.set_user_favorability,
        "get_user_count": _DummyUserDataPlugin.get_user_count,
        "get_config": _DummyUserDataPlugin.get_config,
        "adjust_user_favorability": _DummyUserDataPlugin.adjust_user_favorability,
    },
)
# 业务插件改为普通 import 后，保持 require 桩语义注入 shim 导出
# KOMARIBOT-12：komari_memory 顶层暴露面符号（ADR-0006 边界）同步注入
# shim；符号取真实实现（shim 下子模块仍按真实路径加载），保证
# komari_chat 经顶层包导入后测试中的构造与调用语义不变
from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.core.retry import retry_async
from komari_bot.plugins.komari_memory.services.memory_service import MemoryService
from komari_bot.plugins.komari_memory.services.redis_manager import (
    MessageSchema,
    RedisManager,
)

_inject_package_exports(
    "komari_memory",
    {
        "get_plugin_manager": _DummyMemoryPlugin.get_plugin_manager,
        "KomariMemoryConfigSchema": KomariMemoryConfigSchema,
        "MemoryService": MemoryService,
        "MessageSchema": MessageSchema,
        "RedisManager": RedisManager,
        "retry_async": retry_async,
    },
)
_inject_package_exports(
    "agent_run_logger",
    {
        "create_collector": _DummyAgentRunLoggerPlugin.create_collector,
        "finalize_collector": _DummyAgentRunLoggerPlugin.finalize_collector,
    },
)
_inject_package_exports(
    "komari_knowledge",
    {
        "search_knowledge": _DummyKnowledgePlugin.search_knowledge,
        "search_by_keyword": _DummyKnowledgePlugin.search_by_keyword,
    },
)
_inject_package_exports(
    "komari_chat",
    {
        "generate_debug_reply": _DummyChatPlugin.generate_debug_reply,
        "get_reply_fulfillment_ops_service": (
            _DummyChatPlugin.get_reply_fulfillment_ops_service
        ),
        "ReplyFulfillmentOpsConflictError": (
            _DummyChatPlugin.ReplyFulfillmentOpsConflictError
        ),
        "ReplyFulfillmentOpsNotFoundError": (
            _DummyChatPlugin.ReplyFulfillmentOpsNotFoundError
        ),
        "ReplyFulfillmentOpsValidationError": (
            _DummyChatPlugin.ReplyFulfillmentOpsValidationError
        ),
    },
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
_inject_package_exports(
    "llm_provider",
    {
        "generate_text": _DummyLLMProvider.generate_text,
        "generate_text_with_messages": _DummyLLMProvider.generate_text_with_messages,
        "generate_messages_completion": _DummyLLMProvider.generate_messages_completion,
        "generate_completion": _DummyLLMProvider.generate_completion,
    },
)


@pytest.fixture(autouse=True)
def _isolate_event_gate_preprocessor(request: pytest.FixtureRequest) -> Iterator[None]:
    """隔离 ``group_admission`` 全局事件门禁前置处理器。

    默认（无 ``group_admission_acceptance`` 标记）移除
    ``_admission_event_gate`` 前置处理器，使现有 matcher 单元测试保持
    隔离运行。标记为 ``group_admission_acceptance`` 的测试保留真实
    全局门禁，由 ``event_gate_context`` 管理生命周期。

    TSK-224 引入的全局事件门禁在 ``event_gate`` 模块 import 时自动注册
    到 ``nonebot.message._event_preprocessors``。单跑特定测试文件时不受
    影响，但全量测试套件中该门禁会拦截 komari_debug / komari_help / sr /
    user_ban 等插件的 matcher 测试事件，导致 63 个假阳性失败。
    """
    # 延迟 import 避免模块加载副作用
    from nonebot.message import _event_preprocessors

    # 收集门禁条目：call.__module__ 匹配 event_gate 模块的 Dependent 对象
    gate_entries = {
        dep
        for dep in _event_preprocessors
        if getattr(dep, "call", None) is not None
        and getattr(dep.call, "__module__", None)
        == "komari_bot.plugins.group_admission.event_gate"
    }

    # 判断当前测试是否属于门禁验收套件
    has_marker = (
        request.node.get_closest_marker("group_admission_acceptance") is not None
    )

    if not has_marker and gate_entries:
        _event_preprocessors.difference_update(gate_entries)

    try:
        yield
    finally:
        if not has_marker and gate_entries:
            _event_preprocessors.update(gate_entries)
