"""komari_chat 对判定插件的引用边界与引擎接线验收测试（ticket #31）。

验收目标：
- 聊天插件内不再存在指向 komari_decision 内部子模块的 import；
- MessageHandler 构造函数只接收引擎实例（DecisionEngineProtocol 注解），
  不再接收场景运行时与状态提供器；
- 插件入口经顶层 import 的引擎工厂接线：引擎未就绪时跳过消息处理，
  引擎身份变化时重建处理器。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import nonebot.plugin
import pytest

from komari_bot.plugins.komari_chat.handlers.message_handler import MessageHandler

if TYPE_CHECKING:
    from typing import Any

    from nonebug import App

CHAT_PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "komari_bot" / "plugins" / "komari_chat"
)

FORBIDDEN_IMPORT_MARKERS = (
    "komari_decision.services",
    "komari_decision.repositories",
    "komari_decision.handlers",
)


def test_chat_package_has_no_decision_internal_imports() -> None:
    offenders = [
        f"{module_file.name}: {marker}"
        for module_file in sorted(CHAT_PACKAGE_DIR.rglob("*.py"))
        for marker in FORBIDDEN_IMPORT_MARKERS
        if marker in module_file.read_text(encoding="utf-8")
    ]
    assert not offenders, f"聊天插件存在判定插件深 import: {offenders}"


def test_message_handler_constructor_requires_repository_and_engine() -> None:
    """构造契约：outbox 仓库为必填硬依赖（KOMARIBOT-10），引擎注入保持。"""
    signature = inspect.signature(MessageHandler.__init__)
    assert list(signature.parameters) == [
        "self",
        "redis",
        "memory",
        "reply_commit_repository",
        "decision_engine",
    ]
    repository_param = signature.parameters["reply_commit_repository"]
    assert repository_param.default is inspect.Parameter.empty
    assert "ReplyCommitRepository" in str(repository_param.annotation)
    annotation = signature.parameters["decision_engine"].annotation
    assert "DecisionEngineProtocol" in str(annotation)


@pytest.fixture
def chat_entry_module(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
    """以受控依赖加载聊天插件入口模块（参照 test_plugin_entry.py 既有手法）。"""
    del app
    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name in {"komari_memory", "komari_decision"}:
            return SimpleNamespace(get_plugin_manager=lambda: None)
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot.plugin, "require", _require)
    decision_package_name = "komari_bot.plugins.komari_decision"
    if decision_package_name not in sys.modules:
        decision_package = types.ModuleType(decision_package_name)
        decision_package.__path__ = [  # type: ignore[attr-defined]
            str(
                Path(__file__).resolve().parents[2]
                / "komari_bot"
                / "plugins"
                / "komari_decision"
            )
        ]
        decision_package.get_decision_engine = lambda: None  # type: ignore[attr-defined]
        sys.modules[decision_package_name] = decision_package

    module_name = "komari_bot.plugins.komari_chat._boundary_entry_under_test"
    module_path = CHAT_PACKAGE_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        msg = "无法加载聊天插件入口"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_handler_is_none_when_engine_unavailable(
    chat_entry_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_entry_module,
        "get_memory_plugin_manager",
        lambda: SimpleNamespace(redis=object(), memory=object()),
    )
    monkeypatch.setattr(chat_entry_module, "get_decision_engine", lambda: None)
    monkeypatch.setattr(chat_entry_module, "_handler", None)

    assert chat_entry_module._get_or_build_handler() is None


def test_handler_built_with_engine_and_rebuilt_on_identity_change(
    chat_entry_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_ref = SimpleNamespace(value=object())
    built_engines: list[object] = []

    class _Handler:
        def __init__(
            self,
            *,
            redis: object,
            memory: object,
            reply_commit_repository: object,
            decision_engine: object,
        ) -> None:
            self.redis = redis
            self.memory = memory
            self.reply_commit_repository = reply_commit_repository
            self.decision_engine = decision_engine
            built_engines.append(decision_engine)

    monkeypatch.setattr(
        chat_entry_module,
        "get_memory_plugin_manager",
        lambda: SimpleNamespace(
            redis=object(), memory=SimpleNamespace(pg_pool=object())
        ),
    )
    monkeypatch.setattr(
        chat_entry_module,
        "get_decision_engine",
        lambda: engine_ref.value,
    )
    monkeypatch.setattr(
        chat_entry_module,
        "ReplyCommitRepository",
        lambda pg_pool: SimpleNamespace(pg_pool=pg_pool),
    )
    monkeypatch.setattr(chat_entry_module, "MessageHandler", _Handler)
    monkeypatch.setattr(chat_entry_module, "_handler", None)

    first = chat_entry_module._get_or_build_handler()
    assert first is not None
    assert first.decision_engine is engine_ref.value

    assert chat_entry_module._get_or_build_handler() is first

    engine_ref.value = object()
    second = chat_entry_module._get_or_build_handler()
    assert second is not first
    assert second.decision_engine is engine_ref.value
    assert built_engines == [first.decision_engine, second.decision_engine]
