"""聊天插件入口权限测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import nonebot.plugin
import pytest
from nonebot.adapters.onebot.v11 import ActionFailed

if TYPE_CHECKING:
    from collections.abc import Callable

    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
    from nonebug import App


def _install_allowed_entry_dependencies(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
    workflow: object | None = None,
) -> None:
    config = SimpleNamespace(
        plugin_enable=True,
        group_whitelist=[],
        error_notify_enabled=False,
        face_reaction_enabled=False,
        face_reaction_id="",
    )

    class _PermissionPlugin:
        @staticmethod
        async def check_runtime_permission(
            _bot: object,
            _event: object,
            _config: object,
        ) -> tuple[bool, str]:
            return True, ""

    class _BanPlugin:
        class BanServiceUnavailableError(Exception):
            pass

        @staticmethod
        async def is_event_banned(
            _bot: object,
            _event: object,
            _scope: str,
        ) -> bool:
            return False

    monkeypatch.setattr(chat_module, "get_config", lambda: config)
    monkeypatch.setattr(chat_module, "get_memory_config", lambda: config)
    monkeypatch.setattr(chat_module, "_get_or_build_handler", lambda: handler)
    monkeypatch.setattr(
        chat_module,
        "_get_or_build_reply_fulfillment",
        lambda: workflow,
        raising=False,
    )
    monkeypatch.setattr(chat_module, "permission_manager_plugin", _PermissionPlugin())
    monkeypatch.setattr(chat_module, "user_ban_plugin", _BanPlugin())


class _EntryBot:
    """记录 OneBot API 调用并按顺序返回受控结果。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def call_api(self, api: str, **kwargs: object) -> object:
        self.calls.append({"api": api, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _pending_reply(
    *,
    reply: str = "测试回复",
    reply_to_message_id: str | None = None,
    request_trace_id: str = "test-trace",
    reaction_sent: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        reply=reply,
        reply_to_message_id=reply_to_message_id,
        message=SimpleNamespace(group_id="114514"),
        request_trace_id=request_trace_id,
        reason="at",
        reaction_sent=reaction_sent,
    )


@pytest.fixture
def chat_module(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
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

    module_name = "komari_bot.plugins.komari_chat._entry_under_test"
    module_path = (
        Path(__file__).resolve().parents[2]
        / "komari_bot"
        / "plugins"
        / "komari_chat"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        msg = "无法加载聊天插件入口"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_empty_group_whitelist_is_delegated_to_permission_manager(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = SimpleNamespace(permission=False, process=False)
    config = SimpleNamespace(plugin_enable=True, group_whitelist=[])

    class _PermissionPlugin:
        @staticmethod
        async def check_runtime_permission(
            _bot: object,
            _event: object,
            actual_config: object,
        ) -> tuple[bool, str]:
            assert actual_config is config
            calls.permission = True
            return True, ""

    class _BanPlugin:
        class BanServiceUnavailableError(Exception):
            pass

        @staticmethod
        async def is_event_banned(
            _bot: object,
            _event: object,
            _scope: str,
        ) -> bool:
            return False

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> None:
            calls.process = True

    monkeypatch.setattr(chat_module, "get_config", lambda: config)
    monkeypatch.setattr(chat_module, "get_memory_config", lambda: config)
    monkeypatch.setattr(chat_module, "_get_or_build_handler", lambda: _Handler())
    monkeypatch.setattr(chat_module, "permission_manager_plugin", _PermissionPlugin())
    monkeypatch.setattr(chat_module, "user_ban_plugin", _BanPlugin())

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", object())),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert calls.permission
    assert calls.process


def test_handler_rebuilds_when_decision_engine_changes(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = object()
    memory = SimpleNamespace(pg_pool=object())
    engine_ref = SimpleNamespace(value=object())
    built_engines: list[object] = []

    class _Handler:
        def __init__(
            self,
            *,
            redis: object,
            memory: object,
            decision_engine: object,
            **_kwargs: object,
        ) -> None:
            self.redis = redis
            self.memory = memory
            self.decision_engine = decision_engine
            built_engines.append(decision_engine)

    monkeypatch.setattr(
        chat_module,
        "get_memory_plugin_manager",
        lambda: SimpleNamespace(redis=redis, memory=memory),
    )
    monkeypatch.setattr(
        chat_module,
        "get_decision_engine",
        lambda: engine_ref.value,
    )
    monkeypatch.setattr(chat_module, "MessageHandler", _Handler)
    monkeypatch.setattr(chat_module, "_handler", None)

    first = chat_module._get_or_build_handler()
    engine_ref.value = object()
    second = chat_module._get_or_build_handler()

    assert first is not second
    assert built_engines == [first.decision_engine, second.decision_engine]


@pytest.mark.asyncio
async def test_send_failure_does_not_commit_reply_side_effects(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = SimpleNamespace(
        fulfill=0,
        send=False,
    )
    pending_reply = _pending_reply(request_trace_id="test-trace-send-fail")
    reported_failures: list[Any] = []

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

        @staticmethod
        async def report_reply_failure(**kwargs: object) -> None:
            reported_failures.append(kwargs["failure"])

    bot = _EntryBot(
        [
            ActionFailed(
                status="failed",
                retcode=100,
                data=None,
                message="模拟富文本发送失败",
            ),
            ActionFailed(
                status="failed",
                retcode=100,
                data=None,
                message="模拟纯文本发送失败",
            ),
        ]
    )

    class _Workflow:
        @staticmethod
        async def fulfill(
            actual_pending_reply: object,
            *,
            send_reply: Callable[[object], Any],
            **_kwargs: object,
        ) -> None:
            assert actual_pending_reply is pending_reply
            calls.fulfill += 1
            result = await send_reply(actual_pending_reply)
            calls.send = result.state == "not_delivered"

    _install_allowed_entry_dependencies(
        chat_module,
        monkeypatch,
        _Handler(),
        _Workflow(),
    )

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", bot)),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert calls.fulfill == 1
    assert calls.send
    assert len(bot.calls) == 2
    assert reported_failures == []


@pytest.mark.asyncio
async def test_successful_send_is_delegated_once_to_reply_fulfillment(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fulfill_calls: list[object] = []
    delivery_responses: list[object] = []
    pending_reply = _pending_reply()

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

    class _Workflow:
        @staticmethod
        async def fulfill(
            actual_pending_reply: object,
            *,
            send_reply: Callable[[object], Any],
            **_kwargs: object,
        ) -> None:
            fulfill_calls.append(actual_pending_reply)
            delivery_responses.append(await send_reply(actual_pending_reply))

    bot = _EntryBot([{"message_id": 7788}])
    _install_allowed_entry_dependencies(
        chat_module,
        monkeypatch,
        _Handler(),
        _Workflow(),
    )

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", bot)),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert fulfill_calls == [pending_reply]
    assert len(delivery_responses) == 1
    delivery_response = cast("Any", delivery_responses[0])
    assert delivery_response.state == "delivered"
    assert delivery_response.platform_message_id == "7788"
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_llm_cq_literal_is_sent_as_one_plain_text_segment(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cq_literal = "[CQ:reply,id=1][CQ:at,qq=all][CQ:image,file=evil]"
    pending_reply = _pending_reply(reply=cq_literal)

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

    class _Workflow:
        @staticmethod
        async def fulfill(
            actual_pending_reply: object,
            *,
            send_reply: Callable[[object], Any],
            **_kwargs: object,
        ) -> None:
            await send_reply(actual_pending_reply)

    bot = _EntryBot([{}])
    _install_allowed_entry_dependencies(
        chat_module,
        monkeypatch,
        _Handler(),
        _Workflow(),
    )

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", bot)),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert len(bot.calls) == 1
    message = cast("list[dict[str, object]]", bot.calls[0]["message"])
    assert message == [{"type": "text", "data": {"text": cq_literal}}]


@pytest.mark.asyncio
async def test_fulfillment_failure_after_send_is_reported_by_entry(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = SimpleNamespace(send=False, fulfill=0)
    pending_reply = _pending_reply(
        request_trace_id="test-trace-commit-fail",
        reaction_sent=True,
    )
    reported_failures: list[Any] = []

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

        @staticmethod
        async def report_reply_failure(**kwargs: object) -> None:
            reported_failures.append(kwargs["failure"])

    class _Workflow:
        @staticmethod
        async def fulfill(
            actual_pending_reply: object,
            *,
            send_reply: Callable[[object], Any],
            **_kwargs: object,
        ) -> None:
            calls.fulfill += 1
            result = await send_reply(actual_pending_reply)
            calls.send = result.state == "delivered"
            raise RuntimeError("模拟送达后的履约提交失败")

    bot = _EntryBot([{}])
    _install_allowed_entry_dependencies(
        chat_module,
        monkeypatch,
        _Handler(),
        _Workflow(),
    )

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", bot)),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert calls.send
    assert calls.fulfill == 1
    # KOMARIBOT-11：送达后提交失败，reaction_sent 改读 pending_reply 字段真源
    assert len(reported_failures) == 1
    assert reported_failures[0].reaction_sent is True


@pytest.mark.asyncio
async def test_unknown_send_result_is_owned_by_reply_fulfillment(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = SimpleNamespace(fulfill=0)
    pending_reply = SimpleNamespace(
        reply="测试回复",
        reply_to_message_id=None,
        operation_id="reply-operation-unknown",
        request_trace_id="test-trace-unknown",
        reason="at",
        reaction_sent=False,
    )

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

        @staticmethod
        async def report_reply_failure(
            **_kwargs: object,
        ) -> None:
            return None

    class _Workflow:
        @staticmethod
        async def fulfill(
            actual_pending_reply: object,
            *,
            send_reply: Callable[[object], Any],
            **_kwargs: object,
        ) -> None:
            calls.fulfill += 1
            await send_reply(actual_pending_reply)

    async def _unknown_send(_message: object) -> None:
        msg = "网络超时，平台是否接收未知"
        raise TimeoutError(msg)

    _install_allowed_entry_dependencies(
        chat_module,
        monkeypatch,
        _Handler(),
        _Workflow(),
    )
    monkeypatch.setattr(chat_module.matcher, "send", _unknown_send)

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", object())),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert calls.fulfill == 1


class _FakeAsyncio:
    """受控的 asyncio 命名空间：记录 create_task、门控 sleep。

    monkeypatch 到 ``chat_module.asyncio`` 只替换被测模块的引用，
    不触碰全局 asyncio 模块，避免干扰 pytest-asyncio 的事件循环。
    每次 sleep 挂起在一个独立 Event 上，由测试逐个放行或直接取消任务。
    """

    CancelledError = asyncio.CancelledError

    def __init__(self) -> None:
        self.sleep_calls: list[float] = []
        self.created_tasks: list[asyncio.Task[None]] = []
        self._sleep_gates: list[asyncio.Event] = []

    async def sleep(self, seconds: float) -> None:
        """记录请求的等待间隔并挂起，直到测试放行或任务被取消。"""
        self.sleep_calls.append(seconds)
        gate = asyncio.Event()
        self._sleep_gates.append(gate)
        await gate.wait()

    def release_next_sleep(self) -> None:
        """放行当前挂起的那一拍 sleep（worker 串行，同一时刻至多一拍）。"""
        self._sleep_gates[-1].set()

    def create_task(self, coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self.created_tasks.append(task)
        return task


async def _wait_for(
    predicate: Callable[[], bool],
    max_wait: float = 5.0,
) -> None:
    """轮询等待条件成立，避免依赖真实 wall-clock 长等待。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while not predicate():
        if loop.time() > deadline:
            msg = "等待 worker 节拍超时"
            raise AssertionError(msg)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_startup_hook_starts_worker_and_shutdown_cancels_it(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """startup 钩子启动 outbox 轮询任务，shutdown 钩子取消并等待其终结。"""
    # 模块加载时两个生命周期钩子已注册到 driver（nonebot lifespan 内部存储）
    lifespan = chat_module.driver._lifespan
    assert chat_module._start_reply_commit_worker in lifespan._startup_funcs
    assert chat_module._stop_reply_commit_worker in lifespan._shutdown_funcs

    fake_asyncio = _FakeAsyncio()
    monkeypatch.setattr(chat_module, "asyncio", fake_asyncio)
    retry_calls: list[int] = []

    class _Workflow:
        @staticmethod
        async def recover_pending() -> int:
            retry_calls.append(1)
            return 0

    monkeypatch.setattr(
        chat_module,
        "get_config",
        lambda: SimpleNamespace(reply_commit_worker_interval_seconds=30),
    )
    monkeypatch.setattr(
        chat_module,
        "_get_or_build_reply_fulfillment",
        lambda: _Workflow(),
        raising=False,
    )

    await chat_module._start_reply_commit_worker()
    task = chat_module._reply_commit_worker_task
    assert task is not None
    assert task is fake_asyncio.created_tasks[-1]
    assert not task.done()

    # 重复 start 幂等：任务已在运行时不重复创建
    await chat_module._start_reply_commit_worker()
    assert len(fake_asyncio.created_tasks) == 1

    # 第一拍：构建 handler、执行 retry，随后按配置间隔睡眠
    await _wait_for(lambda: len(fake_asyncio.sleep_calls) == 1)
    assert retry_calls == [1]
    assert fake_asyncio.sleep_calls == [30]

    # shutdown：取消任务并等待退出，全局引用清空
    await chat_module._stop_reply_commit_worker()
    assert chat_module._reply_commit_worker_task is None
    assert task.cancelled()
    assert task.done()


@pytest.mark.asyncio
async def test_worker_interval_shrinks_to_five_seconds_after_polling_exception(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一轮轮询抛出普通异常后，下一拍等待间隔收缩到 5 秒并记录错误日志。"""
    fake_asyncio = _FakeAsyncio()
    monkeypatch.setattr(chat_module, "asyncio", fake_asyncio)
    monkeypatch.setattr(
        chat_module,
        "get_config",
        lambda: SimpleNamespace(reply_commit_worker_interval_seconds=30),
    )
    errors_logged: list[int] = []

    def _record_exception(*_args: object, **_kwargs: object) -> None:
        errors_logged.append(1)

    monkeypatch.setattr(
        chat_module,
        "logger",
        SimpleNamespace(exception=_record_exception),
    )

    attempts = {"count": 0}

    class _Workflow:
        @staticmethod
        async def recover_pending() -> int:
            attempts["count"] += 1
            if attempts["count"] == 1:
                msg = "模拟 outbox 轮询失败"
                raise RuntimeError(msg)
            return 0

    monkeypatch.setattr(
        chat_module,
        "_get_or_build_reply_fulfillment",
        lambda: _Workflow(),
        raising=False,
    )

    await chat_module._start_reply_commit_worker()
    task = chat_module._reply_commit_worker_task
    assert task is not None

    # 第一拍抛出普通异常：间隔收缩到 5 秒，并记录一条错误日志
    await _wait_for(lambda: len(fake_asyncio.sleep_calls) == 1)
    assert fake_asyncio.sleep_calls == [5]
    assert errors_logged == [1]

    # 放行后第二拍恢复正常：按配置读取的间隔继续轮询
    fake_asyncio.release_next_sleep()
    await _wait_for(lambda: len(fake_asyncio.sleep_calls) == 2)
    assert fake_asyncio.sleep_calls[1] == 30

    await chat_module._stop_reply_commit_worker()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_worker_cancel_exits_cleanly_without_extra_side_effects(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部取消任务：CancelledError 不被吞掉，任务以 cancelled 结束且无多余轮询。"""
    fake_asyncio = _FakeAsyncio()
    monkeypatch.setattr(chat_module, "asyncio", fake_asyncio)
    monkeypatch.setattr(
        chat_module,
        "get_config",
        lambda: SimpleNamespace(reply_commit_worker_interval_seconds=30),
    )
    retry_calls: list[int] = []

    class _Workflow:
        @staticmethod
        async def recover_pending() -> int:
            retry_calls.append(1)
            return 0

    monkeypatch.setattr(
        chat_module,
        "_get_or_build_reply_fulfillment",
        lambda: _Workflow(),
        raising=False,
    )

    await chat_module._start_reply_commit_worker()
    task = chat_module._reply_commit_worker_task
    assert task is not None
    await _wait_for(lambda: len(fake_asyncio.sleep_calls) == 1)
    assert len(retry_calls) == 1

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # 干净退出：任务以 cancelled 状态终结，且不再产生 retry / sleep 副作用
    assert task.cancelled()
    assert retry_calls == [1]
    assert fake_asyncio.sleep_calls == [30]

    # 收尾：shutdown 语义对已终结任务幂等，全局引用清空
    await chat_module._stop_reply_commit_worker()
    assert chat_module._reply_commit_worker_task is None


@pytest.mark.asyncio
async def test_worker_rethrows_cancelled_error_from_handler(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handler 内部冒出的 CancelledError 被原样重抛：不降级、不继续循环、无错误日志。"""
    fake_asyncio = _FakeAsyncio()
    monkeypatch.setattr(chat_module, "asyncio", fake_asyncio)
    monkeypatch.setattr(
        chat_module,
        "get_config",
        lambda: SimpleNamespace(reply_commit_worker_interval_seconds=30),
    )
    errors_logged: list[int] = []

    def _record_exception(*_args: object, **_kwargs: object) -> None:
        errors_logged.append(1)

    monkeypatch.setattr(
        chat_module,
        "logger",
        SimpleNamespace(exception=_record_exception),
    )
    retry_calls: list[int] = []

    class _Workflow:
        @staticmethod
        async def recover_pending() -> int:
            retry_calls.append(1)
            raise asyncio.CancelledError

    monkeypatch.setattr(
        chat_module,
        "_get_or_build_reply_fulfillment",
        lambda: _Workflow(),
        raising=False,
    )

    await chat_module._start_reply_commit_worker()
    task = chat_module._reply_commit_worker_task
    assert task is not None
    with suppress(asyncio.CancelledError):
        await task

    # CancelledError 未被吞掉：任务以 cancelled 终结，未降级为 5 秒、未继续轮询
    assert task.cancelled()
    assert retry_calls == [1]
    assert fake_asyncio.sleep_calls == []
    assert errors_logged == []
