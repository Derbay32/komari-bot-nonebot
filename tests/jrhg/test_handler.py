"""JRHG 主流程测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import nonebot.plugin

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "komari_bot/plugins/jrhg/__init__.py"
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot/plugins/jrhg"


def _load_jrhg_module(monkeypatch: Any) -> Any:
    class _DummyMatcher:
        @staticmethod
        def handle() -> Any:
            def _decorator(func: Any) -> Any:
                return func

            return _decorator

        async def finish(self, _message: str) -> None:
            return None

    class _DummyConfigManager:
        def get(self) -> object:
            return SimpleNamespace(plugin_enable=True)

        def update_field(self, _field: str, _value: object) -> None:
            return None

    class _DummyConfigManagerPlugin:
        @staticmethod
        def get_config_manager(_name: str, _schema: object) -> _DummyConfigManager:
            return _DummyConfigManager()

    async def _generate_or_update_favorability(_user_id: str) -> object:
        return SimpleNamespace(
            daily_favor=73,
            cumulative_favor=114,
            is_new_day=False,
            favor_level="友好",
        )

    async def _format_favor_response(
        ai_response: str,
        user_nickname: str,
        daily_favor: int,
    ) -> str:
        del user_nickname, daily_favor
        return ai_response

    async def _check_runtime_permission(
        _bot: object,
        _event: object,
        _config: object,
    ) -> tuple[bool, str]:
        return True, ""

    def _fake_require(name: str) -> object:
        if name == "config_manager":
            return _DummyConfigManagerPlugin()
        if name == "user_data":
            return SimpleNamespace(
                generate_or_update_favorability=_generate_or_update_favorability,
                format_favor_response=_format_favor_response,
            )
        if name == "permission_manager":
            return SimpleNamespace(
                check_runtime_permission=_check_runtime_permission,
                check_plugin_status=lambda _config: (True, "🟢 正常"),
                format_permission_info=lambda _config: "权限正常",
            )
        if name == "komari_memory":
            return SimpleNamespace(get_plugin_manager=lambda: None)
        if name == "llm_provider":
            return SimpleNamespace()
        if name == "character_binding":
            return SimpleNamespace(
                get_character_name=lambda user_id, fallback_nickname="": (
                    fallback_nickname or user_id
                )
            )
        msg = f"Unsupported plugin require in tests: {name}"
        raise RuntimeError(msg)

    monkeypatch.setattr(nonebot, "on_command", lambda *_args, **_kwargs: _DummyMatcher())
    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    original_package = sys.modules.get("komari_bot.plugins.jrhg")
    try:
        spec = importlib.util.spec_from_file_location(
            "komari_bot.plugins.jrhg",
            MODULE_PATH,
            submodule_search_locations=[str(PACKAGE_ROOT)],
        )
        if spec is None or spec.loader is None:
            raise AssertionError

        module = importlib.util.module_from_spec(spec)
        sys.modules["komari_bot.plugins.jrhg"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if original_package is not None:
            sys.modules["komari_bot.plugins.jrhg"] = original_package
        else:
            sys.modules.pop("komari_bot.plugins.jrhg", None)


def _make_event(
    *,
    message_id: int,
    plain_text: str,
    group_id: int | None,
) -> Any:
    event = SimpleNamespace(
        message_id=message_id,
        sender=SimpleNamespace(nickname="阿虚", card=""),
        get_user_id=lambda: "10001",
        get_plaintext=lambda: plain_text,
    )
    if group_id is not None:
        event.group_id = group_id
    return event


def test_jrhg_function_reads_group_interaction_history_and_ignores_tail_text(
    monkeypatch: Any,
) -> None:
    module = _load_jrhg_module(monkeypatch)
    interaction_history = {
        "summary": "最近常找小鞠聊天",
        "records": [{"event": "投喂", "result": "开心"}],
    }
    captured_prompts: list[dict[str, Any]] = []
    finished_messages: list[str] = []

    async def _get_interaction_history(**_kwargs: object) -> dict[str, Any]:
        return interaction_history

    def _build_prompt(**kwargs: Any) -> list[dict[str, str]]:
        captured_prompts.append(kwargs)
        return [{"role": "user", "content": "prompt"}]

    async def _generate_reply(**_kwargs: object) -> str:
        return "生成回复"

    async def _finish(message: str) -> None:
        finished_messages.append(message)

    monkeypatch.setattr(
        module,
        "komari_memory_plugin",
        SimpleNamespace(
            get_plugin_manager=lambda: SimpleNamespace(
                memory=SimpleNamespace(
                    get_interaction_history=_get_interaction_history,
                )
            )
        ),
    )
    monkeypatch.setattr(module, "build_prompt", _build_prompt)
    monkeypatch.setattr(module, "generate_reply", _generate_reply)
    monkeypatch.setattr(module.jrhg, "finish", _finish)

    asyncio.run(
        module.jrhg_function(
            SimpleNamespace(),
            _make_event(message_id=1, plain_text=".jrhg", group_id=114514),
        )
    )
    asyncio.run(
        module.jrhg_function(
            SimpleNamespace(),
            _make_event(
                message_id=2,
                plain_text=".jrhg 任意尾随文本",
                group_id=114514,
            ),
        )
    )

    assert captured_prompts == [
        {"daily_favor": 73, "interaction_history": interaction_history},
        {"daily_favor": 73, "interaction_history": interaction_history},
    ]
    assert finished_messages == ["生成回复", "生成回复"]


def test_jrhg_function_private_chat_uses_fallback_when_llm_fails(
    monkeypatch: Any,
) -> None:
    module = _load_jrhg_module(monkeypatch)
    captured_prompts: list[dict[str, Any]] = []
    finished_messages: list[str] = []

    def _build_prompt(**kwargs: Any) -> list[dict[str, str]]:
        captured_prompts.append(kwargs)
        return [{"role": "user", "content": "prompt"}]

    async def _generate_reply(**_kwargs: object) -> str:
        raise RuntimeError("boom")

    async def _finish(message: str) -> None:
        finished_messages.append(message)

    monkeypatch.setattr(
        module,
        "komari_memory_plugin",
        SimpleNamespace(get_plugin_manager=lambda: None),
    )
    monkeypatch.setattr(module, "build_prompt", _build_prompt)
    monkeypatch.setattr(module, "generate_reply", _generate_reply)
    monkeypatch.setattr(module.jrhg, "finish", _finish)

    asyncio.run(
        module.jrhg_function(
            SimpleNamespace(),
            _make_event(
                message_id=3,
                plain_text=".jrhg 任意尾随文本",
                group_id=None,
            ),
        )
    )

    assert captured_prompts == [{"daily_favor": 73, "interaction_history": None}]
    assert finished_messages == [module._get_response(73, "阿虚")]


def test_load_interaction_history_returns_none_when_memory_has_no_record(
    monkeypatch: Any,
) -> None:
    module = _load_jrhg_module(monkeypatch)

    async def _get_interaction_history(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        module,
        "komari_memory_plugin",
        SimpleNamespace(
            get_plugin_manager=lambda: SimpleNamespace(
                memory=SimpleNamespace(
                    get_interaction_history=_get_interaction_history,
                )
            )
        ),
    )

    result = asyncio.run(
        module._load_interaction_history(user_id="10001", group_id="114514")
    )

    assert result is None
