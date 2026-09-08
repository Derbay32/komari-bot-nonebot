"""TSK-224 事件门控测试共享基础设施（测试专用，不承载生产语义）。

提供 ``event_gate_context`` 异步上下文管理器：registry 与 lifespan 容器的
快照、清空与恢复统一委托 ``registry_isolation_context()``（TSK-253 唯一真
源），本上下文只保留领域编排——

1. 弹出 ``event_gate`` 模块引用（``sys.modules`` 与包属性）；
2. 重新加载 ``group_admission`` 包，触发底层 import 自动注册一个
   生产事件前置处理器（``event_preprocessor``）；
3. 退出时（正常 / 异常）经唯一真源精确恢复全部被管理容器。

不提供 ``install_event_gate`` 测试接缝。

还提供：

- ``ProbeBot``：真实 OneBot V11 Bot 测试身份，记录 ``call_api`` 调用；
- ``dispatch(bot, event)``：调用 ``nonebot.message.handle_event``
  分发事件。
- ``make_v11_event(event_cls, *, group_id=100, **overrides)``：构造
  一个 OneBot V11 事件实例，使用完整公共字段池经 ``model_construct``
  构建；
- ``register_phase_probe(trace, event_family)``：按事件族注册响应器，
  在 rule/run_pre/handler/run_post/event_post 各阶段追加 trace；
- ``read_status(app)``：读取群聊准入 status 端点并返回 JSON 体。
"""

from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

import nonebot
import nonebot.adapters
import nonebot.matcher as _matcher_mod
import nonebot.message as _msg_mod
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.event import Event, Sender

from tests.group_admission.management_support import (
    READER_TOKEN,
    STATUS_PATH,
    asgi_client,
    auth_headers,
)
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)

_GATE_MODULE = "komari_bot.plugins.group_admission.event_gate"
_PACKAGE = "komari_bot.plugins.group_admission"

# ---------------------------------------------------------------------------
# 公共 OneBot V11 字段池 — 覆盖全部事件类可能出现的字段
# ---------------------------------------------------------------------------
_COMMON_FIELD_POOL: dict[str, object] = {
    "time": 1000000,
    "self_id": 12345,
    "post_type": "message",
    "sub_type": "normal",
    "user_id": 67890,
    "message_type": "group",
    "message_id": 1,
    "message": Message("test"),
    "original_message": Message("test"),
    "raw_message": "test",
    "font": 0,
    "sender": Sender.model_construct(
        user_id=67890, nickname="tester", card=""
    ),
    "to_me": False,
    "reply": None,
    "group_id": 100,
    "anonymous": None,
    "notice_type": "",
    "request_type": "",
    "meta_event_type": "",
    "target_id": 0,
    "flag": "",
    "comment": "",
    "status": None,
    "interval": 0,
}


def make_v11_event(
    event_cls: type[Event],
    *,
    group_id: object = 100,
    **overrides: object,
) -> Event:
    """构造一个真实的 OneBot V11 事件实例。

    使用 ``cast(Any, event_cls).model_construct`` 构建，字段来自完整公共
    池（``_COMMON_FIELD_POOL``）过滤到目标类的 ``model_fields``，再应用
    ``overrides`` 覆盖。

    ``group_id`` 在过滤前先写入池中（默认 100），``overrides`` 在过滤后
    覆盖。

    ``Message`` / ``original_message`` 使用真实 OneBot V11 ``Message``，
    ``Sender`` 使用真实 ``Sender.model_construct``。

    不含 Pydantic 校验 hack 以外的非法边缘。
    """
    pool = dict(_COMMON_FIELD_POOL)
    pool["group_id"] = group_id

    # 过滤到目标事件类实际声明的字段
    filtered = {
        key: value
        for key, value in pool.items()
        if key in event_cls.model_fields
    }

    # 应用调用方覆盖
    filtered.update(overrides)

    # 通过 cast 绕过类型检查，调用 model_construct
    return cast("Any", event_cls).model_construct(**filtered)


def register_phase_probe(trace: list[str], event_family: str) -> object | None:
    """按事件族注册响应器，将各阶段名追加到 trace。

    ``event_family`` 映射：
    - ``"message"`` → ``nonebot.on_message``
    - ``"notice"`` → ``nonebot.on_notice``
    - ``"request"`` → ``nonebot.on_request``
    - ``"meta_event"`` → ``nonebot.on_metaevent``
    - 其他值 → 返回 ``None``（不注册响应器）

    注册的 matcher 在 rule 追加 ``"rule"`` 并返回 ``True``；
    handler 追加 ``"handler"``；全局 run_pre / run_post / event_post
    处理器在 matcher 注册后挂载。

    返回注册的 matcher 类型（或 ``None``）。
    """
    family_map: dict[str, Any] = {
        "message": nonebot.on_message,
        "notice": nonebot.on_notice,
        "request": nonebot.on_request,
        "meta_event": nonebot.on_metaevent,
    }
    on_func = family_map.get(event_family)
    if on_func is None:
        return None

    def _rule() -> bool:
        trace.append("rule")
        return True

    matcher = on_func(rule=_rule, priority=1, block=False)

    @matcher.handle()
    async def _handler() -> None:
        trace.append("handler")

    @_msg_mod.run_preprocessor
    async def _run_pre() -> None:
        trace.append("run_pre")

    @_msg_mod.run_postprocessor
    async def _run_post() -> None:
        trace.append("run_post")

    @_msg_mod.event_postprocessor
    async def _event_post() -> None:
        trace.append("event_post")

    return matcher


async def read_status(app: FastAPI) -> dict[str, Any]:
    """读取群聊准入 ``/status`` 端点并返回 JSON 体。

    使用已有的 ``asgi_client`` / ``STATUS_PATH`` / ``auth_headers`` /
    ``READER_TOKEN``，不创建新运行时。
    """
    async with asgi_client(app) as client:
        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
        assert response.status_code == 200, response.text
        return response.json()


class ProbeBot(OneBotBot):
    """真实 OneBot V11 Bot 身份，记录 ``call_api`` 调用并返回 ``None``。"""

    def __init__(self, self_id: str = "12345") -> None:
        adapter = cast("OneBotAdapter", OneBotAdapter.__new__(OneBotAdapter))
        super().__init__(adapter, self_id)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, api: str, **data: object) -> None:
        self.calls.append((api, data))





async def dispatch(bot: object, event: object) -> None:
    """调用真实的 ``nonebot.message.handle_event`` 分发事件。

    ``bot`` 会被 ``cast`` 为 ``nonebot.adapters.Bot`` 以满足类型签名。
    """
    cast_bot = cast("nonebot.adapters.Bot", bot)  # type: ignore[valid-type]
    cast_event = cast("nonebot.adapters.Event", event)  # type: ignore[valid-type]
    await _msg_mod.handle_event(cast_bot, cast_event)


def snapshot_event_registries() -> dict[str, Any]:
    """返回五组注册表的浅拷贝快照字典。"""
    return {
        "matchers": dict(_matcher_mod.matchers.items()),
        "run_pre": set(_msg_mod._run_preprocessors),
        "run_post": set(_msg_mod._run_postprocessors),
        "event_post": set(_msg_mod._event_postprocessors),
        "event_pre": set(_msg_mod._event_preprocessors),
    }


@asynccontextmanager
async def event_gate_context() -> AsyncIterator[None]:
    """异步上下文管理器：加载事件门控，注册表与 lifespan 由唯一真源隔离。

    TSK-253：registry/lifespan 的保存、清空与恢复统一委托
    ``registry_isolation_context()``（快照 + 清空进入、精确恢复退出）；本函
    数只保留领域编排——弹出 ``event_gate`` 模块引用并重载包，触发底层
    import 自动注册一个生产事件前置处理器（``event_preprocessor``）。

    测试失败：门禁未注册时消息不被拦截，流经完整五阶段而非零阶段。context
    退出时（正常 / 异常）仍精确恢复全部被管理容器，避免跨用例累积残留
    （AC6「无 listener/task/reference 残留」的测试环境侧保证）。
    """
    with registry_isolation_context():
        # 从 sys.modules 与包属性中弹出 event_gate 模块引用
        sys.modules.pop(_GATE_MODULE, None)
        pkg = sys.modules.get(_PACKAGE)
        if pkg is not None:
            with suppress(AttributeError):
                delattr(pkg, "event_gate")

        # 重新加载 group_admission 包，触发底层 import
        # 自动注册一个生产事件前置处理器（event_preprocessor）
        importlib.reload(sys.modules[_PACKAGE])

        yield
