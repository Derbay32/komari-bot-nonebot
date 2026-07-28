"""回复失败通知服务测试。

覆盖：
- send_group_reply_error_text 格式与失败降级
- notify_superusers_reply_failure 的配置开关、冷却去重、降级路径
- one_line_summary 截断
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest

error_notify_module = import_module(
    "komari_bot.plugins.komari_chat.services.error_notify"
)
GROUP_ERROR_TEXT = error_notify_module.GROUP_ERROR_TEXT


# ── fake bot / event / redis ───────────────────────────────────


class _FakeBot:
    """记录 call_api / send_private_msg 调用。"""

    def __init__(self) -> None:
        self.call_api_calls: list[dict[str, object]] = []
        self.send_private_msg_calls: list[dict[str, object]] = []

    async def call_api(self, api: str, **kwargs: object) -> dict[str, object]:
        self.call_api_calls.append({"api": api, **kwargs})
        return {"message_id": 1}

    async def send_private_msg(self, user_id: int, message: str) -> None:
        self.send_private_msg_calls.append({"user_id": user_id, "message": message})


class _FakeEvent:
    group_id = 12345
    message_id = 99999


class _FakeRedis:
    """模拟 redis.asyncio.Redis 的 set() 方法（nx=True/ex= 语义）。"""

    def __init__(self, *, nx_returns: bool = True, nx_raises: Exception | None = None) -> None:
        self.nx_returns = nx_returns
        self.nx_raises = nx_raises
        self.set_calls: list[dict[str, object]] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if self.nx_raises is not None:
            raise self.nx_raises
        return self.nx_returns


class _FakeRedisManager:
    """包装 _FakeRedis 以匹配 RedisManager.redis 接口。"""

    def __init__(self, redis_obj: _FakeRedis) -> None:
        self.redis = redis_obj


# ── send_group_reply_error_text ─────────────────────────────────


@pytest.mark.asyncio
async def test_send_group_reply_error_text_correct_format() -> None:
    """验证群内错误文本以 reply 段 + 固定文本发送。"""
    bot = _FakeBot()
    event = _FakeEvent()
    await error_notify_module.send_group_reply_error_text(bot, event)  # type: ignore[arg-type]

    assert len(bot.call_api_calls) == 1
    call = bot.call_api_calls[0]
    assert call["api"] == "send_group_msg"
    assert call["group_id"] == event.group_id
    message = call["message"]
    assert isinstance(message, list)
    assert len(message) == 2
    assert message[0] == {"type": "reply", "data": {"id": str(event.message_id)}}
    assert message[1] == {"type": "text", "data": {"text": GROUP_ERROR_TEXT}}


@pytest.mark.asyncio
async def test_send_group_reply_error_text_survives_failure() -> None:
    """bot.call_api 失败时 send_group_reply_error_text 仅记录日志，不抛出。"""
    bot = _FakeBot()
    event = _FakeEvent()

    async def _raise(*_args: object, **_kwargs: object) -> None:
        msg = "模拟 API 失败"
        raise RuntimeError(msg)

    bot.call_api = _raise  # type: ignore[method-assign]

    # 不应抛出异常
    await error_notify_module.send_group_reply_error_text(bot, event)  # type: ignore[arg-type]


# ── notify_superusers_reply_failure ─────────────────────────────


@pytest.mark.asyncio
async def test_notify_skipped_when_error_notify_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """error_notify_enabled=False 时通知整体静默，不发私聊。"""
    config_stub = SimpleNamespace(error_notify_enabled=False)
    redis = _FakeRedisManager(_FakeRedis())
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)

    bot = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )

    assert len(bot.send_private_msg_calls) == 0


@pytest.mark.asyncio
async def test_notify_sends_private_message_to_superusers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常通知路径：向所有SUPERUSER发送私聊并包含诊断信息。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    redis = _FakeRedisManager(_FakeRedis())
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)

    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"10001", "10002"})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
        summary="LLM 返回空回复",
        request_trace_id="trace-abc",
    )

    assert len(bot.send_private_msg_calls) == 2
    # 按ID排序发送
    assert bot.send_private_msg_calls[0]["user_id"] == 10001
    assert bot.send_private_msg_calls[1]["user_id"] == 10002
    # 消息包含诊断信息
    text = str(bot.send_private_msg_calls[0]["message"])
    assert "小鞠回复生成失败" in text
    assert "trace: trace-abc" in text
    assert "群: 12345" in text
    assert "触发: at" in text
    assert "阶段: generate" in text
    assert "异常: EmptyReplyError（LLM 返回空回复）" in text


@pytest.mark.asyncio
async def test_notify_skips_non_numeric_superuser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非数字 SUPERUSER 条目被跳过，不影响其他条目。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    redis = _FakeRedisManager(_FakeRedis())
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)

    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"invalid", "10003", ""})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot,  # type: ignore[arg-type]
        redis=redis,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="TestError",
    )

    # 仅 10003 收到通知
    assert len(bot.send_private_msg_calls) == 1
    assert bot.send_private_msg_calls[0]["user_id"] == 10003


@pytest.mark.asyncio
async def test_notify_cooldown_blocks_duplicate_within_5_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同群同error_type 5分钟内第二次通知被冷却阻止。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    # 第一次 set 返回 True（获准），第二次返回 None（冷却中）
    redis = _FakeRedis()
    call_count = 0

    async def _set(
        _key: str,
        _value: str,
        *,
        nx: bool = False,  # noqa: ARG001
        ex: int | None = None,  # noqa: ARG001
    ) -> bool | None:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return True  # 第一次：获准
        return None  # 第二次：冷却中

    # 不修改 _FakeRedis 实例的 set，直接重新赋给 manager
    redis.set = _set  # type: ignore[method-assign]
    redis_manager = _FakeRedisManager(redis)

    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)
    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"10001"})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot1 = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot1,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )
    # 第一次：应发送
    assert len(bot1.send_private_msg_calls) == 1

    # 第二次（同群同error_type）
    bot2 = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot2,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )
    # 第二次：冷却中，不发送
    assert len(bot2.send_private_msg_calls) == 0


@pytest.mark.asyncio
async def test_notify_cooldown_does_not_block_different_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同error_type不受冷却影响，各自独立冷却。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    redis = _FakeRedis(nx_returns=True)
    redis_manager = _FakeRedisManager(redis)
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)
    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"10001"})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot1 = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot1,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )
    assert len(bot1.send_private_msg_calls) == 1

    # 不同 error_type — 应照常发送
    bot2 = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot2,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="FavorabilityDeltaMissingError",
    )
    assert len(bot2.send_private_msg_calls) == 1


@pytest.mark.asyncio
async def test_notify_cooldown_does_not_block_different_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同群不受冷却影响。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    redis = _FakeRedis(nx_returns=True)
    redis_manager = _FakeRedisManager(redis)
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)
    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"10001"})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot1 = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot1,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )
    assert len(bot1.send_private_msg_calls) == 1

    # 不同群 — 照常发送
    bot2 = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot2,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="99999",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )
    assert len(bot2.send_private_msg_calls) == 1


@pytest.mark.asyncio
async def test_notify_cooldown_redis_error_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 冷却读写异常时降级为照常通知。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    redis = _FakeRedis(nx_raises=RuntimeError("Redis 不可用"))
    redis_manager = _FakeRedisManager(redis)
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)
    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"10001"})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot,  # type: ignore[arg-type]
        redis=redis_manager,  # type: ignore[arg-type]
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )

    # 降级照常通知
    assert len(bot.send_private_msg_calls) == 1


@pytest.mark.asyncio
async def test_notify_with_none_redis_still_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """redis=None 时跳过冷却检查，照常通知。"""
    config_stub = SimpleNamespace(error_notify_enabled=True)
    monkeypatch.setattr(error_notify_module, "get_config", lambda: config_stub)
    driver_stub = SimpleNamespace(
        config=SimpleNamespace(superusers={"10001"})
    )
    monkeypatch.setattr(error_notify_module, "get_driver", lambda: driver_stub)

    bot = _FakeBot()
    await error_notify_module.notify_superusers_reply_failure(
        bot=bot,  # type: ignore[arg-type]
        redis=None,
        group_id="12345",
        reason="at",
        stage="generate",
        error_type="EmptyReplyError",
    )

    assert len(bot.send_private_msg_calls) == 1


# ── one_line_summary ────────────────────────────────────────────


def test_one_line_summary_extracts_first_line_truncated() -> None:
    """提取异常的首行并截断超长部分。"""
    short = RuntimeError("短异常")
    assert error_notify_module.one_line_summary(short) == "短异常"

    long = RuntimeError("A" * 200)
    result = error_notify_module.one_line_summary(long)
    assert len(result) == 120
    assert result == "A" * 120

    multiline = RuntimeError("第一行\n第二行\n第三行")
    assert error_notify_module.one_line_summary(multiline) == "第一行"


def test_one_line_summary_empty_str() -> None:
    """空 str(exc) 返回空字符串。"""
    class EmptyError(Exception):
        def __str__(self) -> str:
            return ""

    assert error_notify_module.one_line_summary(EmptyError()) == ""
