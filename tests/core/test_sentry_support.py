"""Sentry 黑名单脱敏测试。

验证三层净化机制：
1. 字段名黑名单（归一化匹配）
2. 值模式正则（五种形状）
3. 精确值替换（register_sensitive_value + collector 注入）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

import pytest
from nonebot.exception import (
    FinishedException,
    PausedException,
    RejectedException,
    StopPropagation,
    TypeMisMatch,
)

from komari_bot.core import sentry_support
from komari_bot.core.sentry_support import (
    build_sentry_init_options,
    ensure_sentry_privacy_hooks,
    get_ignored_sentry_exceptions,
    register_sensitive_value,
    sentry_before_breadcrumb,
    sentry_before_send,
    sentry_before_send_log,
    sentry_before_send_transaction,
    set_sensitive_value_collector,
)

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _build_type_mismatch() -> TypeMisMatch:
    param = cast(
        "Any",
        SimpleNamespace(
            name="event",
            _type_display=lambda: "GroupMessageEvent",
        ),
    )
    return TypeMisMatch(param, "private_event")


@dataclass(slots=True)
class _DummySentryConfig:
    environment: str = ""
    release: str = ""
    debug: bool = False
    error_sample_rate: float = 1.0
    traces_sample_rate: float = 0.2
    profiles_sample_rate: float = 0.0
    attach_stacktrace: bool = True
    send_default_pii: bool = False
    max_breadcrumbs: int = 100
    breadcrumb_level: str = "INFO"
    sentry_logs_level: str = "INFO"
    event_level: str = "ERROR"


@pytest.fixture(autouse=True)
def _reset_sensitive_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前后重置模块级敏感值状态，防止测试间污染。"""
    monkeypatch.setattr(sentry_support, "_registered_sensitive_values", set())
    monkeypatch.setattr(sentry_support, "_sensitive_value_collector", None)
    # 重置 TTL 缓存状态，防止测试间 collector 调用计数污染
    monkeypatch.setattr(sentry_support, "_cached_collector", None)
    monkeypatch.setattr(sentry_support, "_collector_refresh_at", 0.0)


# ---------------------------------------------------------------------------
# 控制流异常
# ---------------------------------------------------------------------------


def test_sentry_before_send_drops_nonebot_control_flow_exceptions() -> None:
    """StopPropagation / PausedException / RejectedException / FinishedException
    返回 None，通知 Sentry 丢弃该事件。"""
    for error in (
        StopPropagation(),
        PausedException(),
        RejectedException(),
        FinishedException(),
    ):
        result = sentry_before_send({}, {"exc_info": (type(error), error, None)})
        assert result is None, f"{type(error).__name__} 应被丢弃"


def test_sentry_before_send_keeps_business_and_type_mismatch_errors() -> None:
    """业务异常（如 RuntimeError）和 TypeMisMatch 不被丢弃。"""
    type_mismatch = _build_type_mismatch()

    assert sentry_before_send(
        {"id": "1"}, {"exc_info": (TypeMisMatch, type_mismatch, None)}
    ) == {"id": "1"}
    assert sentry_before_send(
        {"id": "2"}, {"exc_info": (RuntimeError, RuntimeError("boom"), None)}
    ) == {"id": "2"}


# ---------------------------------------------------------------------------
# 字段名黑名单：dict 的 key 归一化后命中 _SENSITIVE_KEY_NAMES 则整值替换
# ---------------------------------------------------------------------------


def test_field_name_blacklist_basic() -> None:
    """字段名精确命中黑名单时值替换为 [Filtered]。"""
    event = {
        "request": {
            "headers": {
                "authorization": "Bearer abc123def456ghi789",
                "content-type": "application/json",
                "accept": "text/html",
            },
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    headers = sanitized["request"]["headers"]
    assert headers["authorization"] == "[Filtered]"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "text/html"


def test_field_name_blacklist_normalization_variants() -> None:
    """大小写 / -_ 变体归一化后命中黑名单。"""
    event = {
        "request": {
            "headers": {
                    "Authorization": "Bearer token1",
                    "X-Api-Key": "key-x-api-key",
                    "x_api_key": "key-x-api-key-underscore",
                    "access-token": "token-access-token-kebab",
                    "access_token": "token-access-token",
                "ClientSecret": "secret-value",
                "Session": "session-id",
                "PrivateKey": "-----BEGIN RSA PRIVATE KEY-----",
                "Content-Type": "application/json",  # 非敏感字段
            },
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    headers = sanitized["request"]["headers"]

    # 命中黑名单的 key：值被替换为 [Filtered]
    for key in (
        "Authorization",
        "X-Api-Key",
        "x_api_key",
        "access-token",
        "access_token",
        "ClientSecret",
        "Session",
        "PrivateKey",
    ):
        assert headers[key] == "[Filtered]", f"key={key} 应打码"

    # 非敏感 key：值保持原样
    assert headers["Content-Type"] == "application/json"


def test_field_name_blacklist_recursive_in_nested_structures() -> None:
    """黑名单匹配在嵌套 dict / list 内递归生效。"""
    event = {
        "breadcrumbs": {
            "values": [
                {"data": {"token": "secret-in-breadcrumb"}},
                {"data": {"message": "safe message"}},
            ],
        },
        "contexts": {
            "database": {
                "dsn": "postgres://user:pass@localhost/db",
                "db_name": "komari",
            },
        },
        "extra": {"password": "supersecret"},
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None

    # breadcrumb 中嵌套的 token 被替换
    assert sanitized["breadcrumbs"]["values"][0]["data"]["token"] == "[Filtered]"
    assert sanitized["breadcrumbs"]["values"][1]["data"]["message"] == "safe message"

    # contexts 中的 dsn 被替换
    assert sanitized["contexts"]["database"]["dsn"] == "[Filtered]"
    assert sanitized["contexts"]["database"]["db_name"] == "komari"

    # extra 中的 password 被替换
    assert sanitized["extra"]["password"] == "[Filtered]"


# ---------------------------------------------------------------------------
# 点号键名分段匹配（修复：_is_sensitive_key 按 . 分段后逐段归一化匹配）
# ---------------------------------------------------------------------------


def test_dot_separated_key_authorization_hit() -> None:
    """点号键命中：http.request.header.authorization 按 . 分段后 authorization 段命中。"""
    event = {
        "request": {
            "headers": {
                "http.request.header.authorization": "Bearer secret-token-value",
                "http.request.header.content-type": "application/json",
            },
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    headers = sanitized["request"]["headers"]
    assert headers["http.request.header.authorization"] == "[Filtered]"
    assert headers["http.request.header.content-type"] == "application/json"


def test_dot_separated_key_variants() -> None:
    """点号键变体命中：x-api-key、password、secret 等含敏感段的点号键均被脱敏。"""
    event = {
        "data": {
            "http.request.header.x-api-key": "key-abc123xyz",
            "db.password.primary": "my-db-password",
            "service.client.secret": "client-secret-value",
            "auth.oauth.access.token": "oauth-token-123456",
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    data = sanitized["data"]
    assert data["http.request.header.x-api-key"] == "[Filtered]"
    assert data["db.password.primary"] == "[Filtered]"
    assert data["service.client.secret"] == "[Filtered]"
    assert data["auth.oauth.access.token"] == "[Filtered]"


def test_dot_separated_key_no_false_positive() -> None:
    """不含敏感段的点号键保留原值，如 http.response.status_code、server.timestamp。"""
    event = {
        "data": {
            "http.response.status_code": 200,
            "request.content.length": 1024,
            "server.timestamp": "2024-01-01T00:00:00Z",
            "trace.span.id": "abc123def456",
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    data = sanitized["data"]
    assert data["http.response.status_code"] == 200
    assert data["request.content.length"] == 1024
    assert data["server.timestamp"] == "2024-01-01T00:00:00Z"
    assert data["trace.span.id"] == "abc123def456"


def test_dot_separated_key_with_hyphens_and_underscores_in_segments() -> None:
    """点号键各段中 - 和 _ 在归一化时被去除后仍能匹配。"""
    event = {
        "data": {
            "x.api_key.value": "secret-key-123",
            "auth.access_token.inner": "inner-token-456",
            "db.primary.x-api-key": "x-api-key-789",
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    data = sanitized["data"]
    assert data["x.api_key.value"] == "[Filtered]"
    assert data["auth.access_token.inner"] == "[Filtered]"
    assert data["db.primary.x-api-key"] == "[Filtered]"


# ---------------------------------------------------------------------------
# 五种值模式正则
# ---------------------------------------------------------------------------


def test_value_pattern_openai_sk_key() -> None:
    """sk- 开头 + 16 位以上字母数字 → [Filtered]。"""
    event = {"message": "使用 key: sk-abcdefghijklmnop12345 连接 API"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "sk-abcdefghijklmnop12345" not in sanitized["message"]
    assert "[Filtered]" in sanitized["message"]


def test_value_pattern_bearer_token() -> None:
    """Bearer + 16 位以上 token 字符 → [Filtered]（大小写不敏感）。"""
    event = {
        "request": {
            "headers": {
                "authorization": "Bearer abcdef1234567890_token_value",
            },
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    # authorization 作为 key 已被字段名黑名单处理，值为 [Filtered]
    # 但我们要测试值模式：把 Bearer token 放在一个非黑名单 key 的值中
    event2 = {"message": "Header: bearer AbCdEf1234567890+extra+/data=="}
    sanitized2 = sentry_before_send(event2, {})
    assert sanitized2 is not None
    assert "bearer AbCdEf1234567890+extra+/data==" not in sanitized2["message"]
    assert "[Filtered]" in sanitized2["message"]


def test_value_pattern_connection_string_userinfo() -> None:
    """连接串中的 user:pass 部分被替换为 [Filtered]。
    覆盖 postgres://user:pass@host / redis://user:pass@host 等。"""
    event = {
        "extra": {
            "connection_string": "postgres://myuser:mypassword@localhost:5432/db",
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    conn = sanitized["extra"]["connection_string"]
    assert "myuser:mypassword" not in conn
    assert "[Filtered]" in conn
    assert "localhost:5432/db" in conn


def test_value_pattern_sentry_dsn() -> None:
    """Sentry DSN 形状（https://key@host/...）→ key 部分被替换为 [Filtered]。"""
    event = {"dsn": "https://abc123def4567890@sentry.example.com/123"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    # dsn 作为 key 已被字段名黑名单处理，值为 [Filtered]
    # 放到不匹配黑名单的字段中测试正则
    event2 = {"message": "DSN: https://abc123def4567890@sentry.example.com/123"}
    sanitized2 = sentry_before_send(event2, {})
    assert sanitized2 is not None
    assert "[Filtered]" in sanitized2["message"]


def test_value_pattern_api_shape_url() -> None:
    """API 形状 URL（/v1、/v1beta 等）→ 整段 URL 被替换为 [Filtered]。"""
    event = {"message": "API: https://api.example.com/v1/chat/completions"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "https://api.example.com/v1/chat/completions" not in sanitized["message"]
    assert "[Filtered]" in sanitized["message"]

    # /v1beta 形状
    event2 = {"message": "API: https://api.openai.com/v1beta/assistants"}
    sanitized2 = sentry_before_send(event2, {})
    assert sanitized2 is not None
    assert "https://api.openai.com/v1beta/assistants" not in sanitized2["message"]
    assert "[Filtered]" in sanitized2["message"]


# ---------------------------------------------------------------------------
# 反误伤：不应触发脱敏的合法输入
# ---------------------------------------------------------------------------


def test_md5_hex_not_scrubbed() -> None:
    """MD5（32 位 hex 字符串）不应被任何正则误伤。"""
    event = {"message": "hash: d41d8cd98f00b204e9800998ecf8427e"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert sanitized["message"] == "hash: d41d8cd98f00b204e9800998ecf8427e"


def test_normal_long_string_not_scrubbed() -> None:
    """普通长字符串（UUID、base64、长文本）不应触发正则。"""
    event = {
        "message": "Trace id: 550e8400-e29b-41d4-a716-446655440000, "
        "span: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        "extra": {
            "some_base64": "dGhpcyBpcyBhIG5vcm1hbCBiYXNlNjQgc3RyaW5nIHRoYXQgc2hvdWxkIG5vdCBiZSBzY3J1YmJlZA=="
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "550e8400-e29b-41d4-a716-446655440000" in sanitized["message"]
    assert "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6" in sanitized["message"]
    assert (
        "dGhpcyBpcyBhIG5vcm1hbCBiYXNlNjQgc3RyaW5nIHRoYXQgc2hvdWxkIG5vdCBiZSBzY3J1YmJlZA=="
        in sanitized["extra"]["some_base64"]
    )


def test_short_sk_like_string_not_scrubbed() -> None:
    """sk- 后不足 16 位的字符串不触发正则。"""
    event = {"message": "key: sk-short"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert sanitized["message"] == "key: sk-short"


def test_short_bearer_token_not_scrubbed() -> None:
    """Bearer 后不足 16 位的 token 不触发正则。"""
    event = {"message": "Bearer short"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert sanitized["message"] == "Bearer short"


# ---------------------------------------------------------------------------
# 精确值替换：register_sensitive_value / collector 注入
# ---------------------------------------------------------------------------


def test_register_sensitive_value_exact_replacement() -> None:
    """register_sensitive_value 登记的值在 payload 中被子串替换为 [Filtered]。"""
    register_sensitive_value("my-secret-api-key-12345")

    event = {"message": "Using key: my-secret-api-key-12345 for API"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "my-secret-api-key-12345" not in sanitized["message"]
    assert "[Filtered]" in sanitized["message"]


def test_register_sensitive_value_embedded_in_longer_string() -> None:
    """秘密嵌在长字符串中间时也被精确替换。"""
    register_sensitive_value("SECRET_MIDDLE_123456")

    event = {"message": "prefix-SECRET_MIDDLE_123456-suffix"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "SECRET_MIDDLE_123456" not in sanitized["message"]
    assert "[Filtered]" in sanitized["message"]
    assert "prefix-" in sanitized["message"]
    assert "-suffix" in sanitized["message"]


def test_register_sensitive_value_ignores_short_values() -> None:
    """strip 后长度 < 8 的值被忽略，不进入累计集合。"""
    register_sensitive_value("short")  # 5 字符
    register_sensitive_value("  1234567  ")  # strip 后 7 字符

    event = {"message": "key: short, code: 1234567"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    # 短值不应被替换
    assert sanitized["message"] == "key: short, code: 1234567"


def test_register_sensitive_value_none_ignored() -> None:
    """传入 None 时静默忽略，不抛异常。"""
    register_sensitive_value(None)  # 不抛异常

    event = {"message": "nothing to scrub"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert sanitized["message"] == "nothing to scrub"


def test_set_sensitive_value_collector_injection() -> None:
    """collector 返回的值参与精确替换。"""
    def _collector() -> list[str]:
        return ["collector-secret-value-123", "another-secret"]

    set_sensitive_value_collector(_collector)

    event = {"message": "secrets: collector-secret-value-123 and another-secret"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "collector-secret-value-123" not in sanitized["message"]
    assert "another-secret" not in sanitized["message"]
    assert sanitized["message"].count("[Filtered]") == 2


def test_set_sensitive_value_collector_strips_and_filters_short() -> None:
    """collector 返回的值中，strip 后 < 8 字符的被忽略。"""
    def _collector() -> list[str]:
        return ["  short  ", "   long-enough-value-123  "]

    set_sensitive_value_collector(_collector)

    event = {"message": "values: short and long-enough-value-123"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "short" in sanitized["message"]  # 不被替换
    assert "long-enough-value-123" not in sanitized["message"]
    assert "[Filtered]" in sanitized["message"]


def test_collector_exception_degradation() -> None:
    """collector 抛异常时不阻断事件，已登记的累计值仍生效。"""
    register_sensitive_value("registered-value-123456")

    def _crashing_collector() -> list[str]:
        raise RuntimeError("collector unavailable")  # noqa: TRY003

    set_sensitive_value_collector(_crashing_collector)

    # collector 抛异常，但事件仍应正常处理
    event = {"message": "Using registered-value-123456 and normal content"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    # 已登记的累计值仍然生效
    assert "registered-value-123456" not in sanitized["message"]
    assert "[Filtered]" in sanitized["message"]
    assert "normal content" in sanitized["message"]


# ---------------------------------------------------------------------------
# collector TTL 缓存（修复：高频钩子避免反复遍历配置管理器）
# ---------------------------------------------------------------------------


def test_collector_ttl_cache_avoids_duplicate_calls() -> None:
    """TTL 缓存生效：连续两次调用钩子，collector 只被调用 1 次。"""
    call_count = 0

    def _counting_collector() -> list[str]:
        nonlocal call_count
        call_count += 1
        return ["ttl-secret-12345678"]

    set_sensitive_value_collector(_counting_collector)

    # 第一次调用：collector 被调用
    sentry_before_send({"message": "using ttl-secret-12345678"}, {})
    assert call_count == 1

    # 第二次调用：TTL 未过期，collector 不再被调用
    sentry_before_send({"message": "using ttl-secret-12345678"}, {})
    assert call_count == 1


def test_collector_change_triggers_rerun() -> None:
    """collector 更换后立即重跑新 collector，旧 collector 不再被调用。"""
    call_count_1 = 0
    call_count_2 = 0

    def _collector1() -> list[str]:
        nonlocal call_count_1
        call_count_1 += 1
        return ["collector1-secret-12345678"]

    def _collector2() -> list[str]:
        nonlocal call_count_2
        call_count_2 += 1
        return ["collector2-secret-12345678"]

    set_sensitive_value_collector(_collector1)
    sentry_before_send({}, {})
    assert call_count_1 == 1
    assert call_count_2 == 0

    # 更换 collector（缓存键是 collector 对象身份）
    set_sensitive_value_collector(_collector2)
    sentry_before_send({}, {})
    assert call_count_1 == 1  # 旧 collector 不再被调用
    assert call_count_2 == 1  # 新 collector 被调用


def test_collector_ttl_expiry_triggers_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 过期后 collector 再次被调用。"""
    call_count = 0

    def _counting_collector() -> list[str]:
        nonlocal call_count
        call_count += 1
        return ["ttl-expire-secret-12345678"]

    set_sensitive_value_collector(_counting_collector)

    # 第一次调用：激活 TTL 缓存
    sentry_before_send({}, {})
    assert call_count == 1

    # 将 _collector_refresh_at 设为 0.0，模拟 TTL 已过期
    monkeypatch.setattr(sentry_support, "_collector_refresh_at", 0.0)

    # 第二次调用：TTL 已过期，collector 再次被调用
    sentry_before_send({}, {})
    assert call_count == 2


def test_registered_value_bypasses_ttl_cache() -> None:
    """TTL 未过期时 register_sensitive_value 登记的值仍参与替换。

    register_sensitive_value 直接写入累计集合，不受 TTL 门控影响；
    缓存窗口内轮换的秘密由 collector 旧缓存 + 累计集合共同保护。
    """
    call_count = 0

    def _counting_collector() -> list[str]:
        nonlocal call_count
        call_count += 1
        return ["collector-secret-12345678"]

    set_sensitive_value_collector(_counting_collector)

    # 第一次调用：激活 TTL 缓存
    sentry_before_send({}, {})
    assert call_count == 1

    # TTL 未过期时登记新值
    register_sensitive_value("registered-after-ttl-start")

    # 第二次调用：collector 不再被调用（TTL 缓存生效）
    event = {
        "message": "using registered-after-ttl-start and collector-secret-12345678"
    }
    sanitized = sentry_before_send(event, {})
    assert call_count == 1  # collector 未被重复调用

    assert sanitized is not None
    # 两种来源的秘密均被脱敏
    assert "registered-after-ttl-start" not in sanitized["message"]
    assert "collector-secret-12345678" not in sanitized["message"]
    assert sanitized["message"].count("[Filtered]") == 2


# ---------------------------------------------------------------------------
# user 上下文门控
# ---------------------------------------------------------------------------


def test_allow_user_context_false_removes_user() -> None:
    """allow_user_context=False 时移除 user，其余内容保留。"""
    event = {
        "user": {"id": "10001", "username": "testuser"},
        "message": "test message",
        "tags": {"component": "chat"},
    }
    sanitized = sentry_before_send(event, {}, allow_user_context=False)
    assert sanitized is not None
    assert "user" not in sanitized
    assert sanitized["message"] == "test message"
    assert sanitized["tags"] == {"component": "chat"}


def test_allow_user_context_true_keeps_user() -> None:
    """allow_user_context=True 时保留 user。"""
    event = {
        "user": {"id": "10001"},
        "message": "test",
    }
    sanitized = sentry_before_send(event, {}, allow_user_context=True)
    assert sanitized is not None
    assert sanitized["user"] == {"id": "10001"}
    assert sanitized["message"] == "test"


def test_sentry_before_send_default_user_removed() -> None:
    """默认不传 allow_user_context 时移除 user。"""
    event = {"user": {"id": "10001"}, "message": "hello"}
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert "user" not in sanitized


def test_transaction_removes_user_when_disallowed() -> None:
    """事务钩子也遵循 allow_user_context 门控。"""
    event = {
        "user": {"id": "10001"},
        "transaction": "GET /api/test",
    }
    sanitized = sentry_before_send_transaction(event, {}, allow_user_context=False)
    assert sanitized is not None
    assert "user" not in sanitized
    assert sanitized["transaction"] == "GET /api/test"


def test_transaction_keeps_user_when_allowed() -> None:
    event = {
        "user": {"id": "10001"},
        "transaction": "GET /api/test",
    }
    sanitized = sentry_before_send_transaction(event, {}, allow_user_context=True)
    assert sanitized is not None
    assert sanitized["user"] == {"id": "10001"}


# ---------------------------------------------------------------------------
# 诊断正文无条件保留（黑名单式脱敏的核心行为）
# ---------------------------------------------------------------------------


def test_log_body_preserved_unconditionally() -> None:
    """日志正文（body）无条件保留，不做摘要化处理。"""
    log = {
        "severity_text": "WARN",
        "body": "用户 10001 请求 /api/chat 失败: timeout after 30s",
        "attributes": {
            "logger.name": "komari.chat",
            "code.line.number": 42,
        },
    }
    sanitized = sentry_before_send_log(log, {})
    assert sanitized is not None
    # 正文完整保留
    assert sanitized["body"] == "用户 10001 请求 /api/chat 失败: timeout after 30s"
    # 属性中的非敏感字段保留
    assert sanitized["attributes"]["logger.name"] == "komari.chat"
    assert sanitized["attributes"]["code.line.number"] == 42


def test_log_body_scrubbed_only_for_sensitive_values() -> None:
    """日志正文保留，但其中包含的凭据仍被脱敏。"""
    register_sensitive_value("sk-sensitive-in-log-body")
    log = {
        "body": "API key: sk-sensitive-in-log-body used for request",
        "attributes": {},
    }
    sanitized = sentry_before_send_log(log, {})
    assert sanitized is not None
    assert "sk-sensitive-in-log-body" not in sanitized["body"]
    assert "[Filtered]" in sanitized["body"]
    assert "used for request" in sanitized["body"]


def test_breadcrumb_message_preserved_unconditionally() -> None:
    """breadcrumb 正文（message）无条件保留。"""
    breadcrumb = {
        "type": "default",
        "category": "komari.chat",
        "message": "用户 10001 发送消息: 你好世界",
        "data": {
            "code.function.name": "process_message",
            "status_code": 200,
        },
    }
    sanitized = sentry_before_breadcrumb(breadcrumb, {})
    assert sanitized is not None
    assert sanitized["message"] == "用户 10001 发送消息: 你好世界"
    assert sanitized["data"]["code.function.name"] == "process_message"
    assert sanitized["data"]["status_code"] == 200


def test_exception_value_preserved() -> None:
    """异常 value 正文保留（但凭据仍脱敏）。"""
    event = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "Request failed with token: my-secret-token-123",
                },
            ],
        },
    }
    register_sensitive_value("my-secret-token-123")
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    exception_value = sanitized["exception"]["values"][0]["value"]
    assert "Request failed with token:" in exception_value
    assert "my-secret-token-123" not in exception_value
    assert "[Filtered]" in exception_value


def test_stack_frame_vars_preserved() -> None:
    """堆栈帧 vars 保留（但凭据脱敏）。"""
    event = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "error",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "process_message",
                                "vars": {
                                    "content": "normal variable content",
                                    "api_key": "sk-real-api-key-abcdef",
                                },
                            },
                        ],
                    },
                },
            ],
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    frame = sanitized["exception"]["values"][0]["stacktrace"]["frames"][0]

    # vars 字典存在（未被移除）
    assert "vars" in frame

    # api_key 作为字段名命中黑名单 → [Filtered]
    assert frame["vars"]["api_key"] == "[Filtered]"

    # 普通变量内容保留
    assert frame["vars"]["content"] == "normal variable content"


def test_tags_and_extra_preserved() -> None:
    """tags 和 extra 全量保留（仅凭据脱敏）。"""
    event = {
        "tags": {
            "component": "chat",
            "user_id": "10001",
            "topic": "general",
        },
        "extra": {
            "prompt": "This is a long prompt about anime discussion",
            "model": "deepseek-chat",
        },
        "contexts": {
            "trace": {"trace_id": "trace-abc123"},
            "response": {"status_code": 200, "body": "response body content"},
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None

    # tags 全部保留
    assert sanitized["tags"]["component"] == "chat"
    assert sanitized["tags"]["user_id"] == "10001"
    assert sanitized["tags"]["topic"] == "general"

    # extra 全部保留
    assert sanitized["extra"]["prompt"] == "This is a long prompt about anime discussion"
    assert sanitized["extra"]["model"] == "deepseek-chat"

    # contexts 全部保留
    assert sanitized["contexts"]["trace"]["trace_id"] == "trace-abc123"
    assert sanitized["contexts"]["response"]["status_code"] == 200
    assert sanitized["contexts"]["response"]["body"] == "response body content"


def test_request_url_and_data_preserved() -> None:
    """request 中的 URL 和 data 全量保留（仅凭据脱敏）。"""
    event = {
        "request": {
            "method": "POST",
            "url": "https://example.invalid/user/12345/profile",
            "query_string": "page=1&size=10",
            "data": '{"message": "hello world"}',
        },
    }
    sanitized = sentry_before_send(event, {})
    assert sanitized is not None
    assert sanitized["request"]["method"] == "POST"
    assert sanitized["request"]["url"] == "https://example.invalid/user/12345/profile"
    assert sanitized["request"]["query_string"] == "page=1&size=10"
    assert sanitized["request"]["data"] == '{"message": "hello world"}'


# ---------------------------------------------------------------------------
# ensure_sentry_privacy_hooks：组合、幂等与故障关闭
# ---------------------------------------------------------------------------


def test_ensure_sentry_privacy_hooks_composes_external_and_is_idempotent() -> None:
    """外部钩子与隐私钩子组合，第二次调用幂等。"""
    external_calls: list[str] = []

    def _external_hook(
        payload: dict[str, Any],
        _hint: dict[str, Any],
    ) -> dict[str, Any]:
        external_calls.append("called")
        payload["external_marker"] = True
        return payload

    client = SimpleNamespace(
        options={
            "before_send": _external_hook,
            "before_breadcrumb": _external_hook,
            "before_send_transaction": _external_hook,
            "before_send_log": _external_hook,
            "ignore_errors": [ValueError],
        }
    )

    # 第一次安装
    ensure_sentry_privacy_hooks(client, allow_user_context=False)
    installed_hooks = {
        name: client.options[name]
        for name in (
            "before_send",
            "before_breadcrumb",
            "before_send_transaction",
            "before_send_log",
        )
    }
    # 第二次安装：幂等，钩子引用不变
    ensure_sentry_privacy_hooks(client, allow_user_context=False)
    for name, hook in installed_hooks.items():
        assert client.options[name] is hook, (
            f"{name} 应保持不变（幂等）"
        )

    # ignore_errors 合并
    assert ValueError in client.options["ignore_errors"]
    assert set(get_ignored_sentry_exceptions()).issubset(
        client.options["ignore_errors"]
    )

    # 外部钩子被调用，且后续脱敏生效
    sanitized_event = client.options["before_send"](
        {"message": "event-canary", "password": "secret123"}, {}
    )
    sanitized_breadcrumb = client.options["before_breadcrumb"](
        {"message": "breadcrumb-canary", "token": "secret456"}, {}
    )
    sanitized_transaction = client.options["before_send_transaction"](
        {"transaction": "txn-canary", "api_key": "secret789"}, {}
    )
    sanitized_log = client.options["before_send_log"](
        {"body": "log-canary", "secret": "secret000"}, {}
    )

    assert external_calls == ["called"] * 4
    # 外部钩子的标记存在
    assert sanitized_event["external_marker"] is True
    # 正文保留
    assert sanitized_event["message"] == "event-canary"
    assert sanitized_breadcrumb["message"] == "breadcrumb-canary"
    assert sanitized_transaction["transaction"] == "txn-canary"
    assert sanitized_log["body"] == "log-canary"
    # 凭据脱敏
    assert sanitized_event["password"] == "[Filtered]"
    assert sanitized_breadcrumb["token"] == "[Filtered]"
    assert sanitized_transaction["api_key"] == "[Filtered]"
    assert sanitized_log["secret"] == "[Filtered]"


def test_ensure_sentry_privacy_hooks_fail_closed_on_external_error() -> None:
    """外部钩子抛异常时故障关闭，防止未净化的 payload 被发送。"""
    def _crashing_hook(
        _payload: dict[str, Any],
        _hint: dict[str, Any],
    ) -> dict[str, Any]:
        raise RuntimeError("simulated hook crash")  # noqa: TRY003

    client = SimpleNamespace(
        options={
            "before_send": _crashing_hook,
            "ignore_errors": [],
        }
    )

    ensure_sentry_privacy_hooks(client, allow_user_context=False)

    # 外部钩子崩溃 → 组合钩子返回 None，丢弃事件
    result = client.options["before_send"](
        {"message": "sensitive data", "password": "should-not-leak"},
        {},
    )
    assert result is None


def test_ensure_sentry_privacy_hooks_no_existing_hooks() -> None:
    """初始没有任何钩子时，直接安装隐私钩子。"""
    client = SimpleNamespace(
        options={
            "ignore_errors": [],
        }
    )

    ensure_sentry_privacy_hooks(client, allow_user_context=True)

    # 隐私钩子被安装
    assert client.options["before_send"] is not None
    assert client.options["before_breadcrumb"] is not None
    assert client.options["before_send_transaction"] is not None
    assert client.options["before_send_log"] is not None

    # 能正常执行
    result = client.options["before_send"]({"user": {"id": "1"}, "message": "ok"}, {})
    assert result is not None
    assert result["user"] == {"id": "1"}  # allow_user_context=True 保留 user
    assert result["message"] == "ok"


def test_ensure_sentry_privacy_hooks_preserves_ignore_errors() -> None:
    """已有 ignore_errors 时合并而非覆盖。"""
    client = SimpleNamespace(
        options={
            "ignore_errors": [ValueError, TypeError],
            "before_send": lambda p, _hint: p,
        }
    )

    ensure_sentry_privacy_hooks(client, allow_user_context=False)

    assert ValueError in client.options["ignore_errors"]
    assert TypeError in client.options["ignore_errors"]
    assert StopPropagation in client.options["ignore_errors"]


# ---------------------------------------------------------------------------
# build_sentry_init_options：钩子参数断言
# ---------------------------------------------------------------------------


def test_build_sentry_init_options_hook_keywords() -> None:
    """before_send keywords 仅含 allow_user_context，before_send_log 是直接函数引用。"""
    config = _DummySentryConfig()

    options = build_sentry_init_options(
        config=config,
        dsn="https://example@sentry.invalid/1",
        resolve_level=lambda name, default: getattr(logging, name, default),
        logging_integration_factory=lambda **kwargs: kwargs,
        loguru_integration_factory=lambda **kwargs: kwargs,
        asyncio_integration_factory=lambda: "asyncio",
        fastapi_integration_factory=lambda: "fastapi",
        starlette_integration_factory=lambda: "starlette",
        environ={"ENVIRONMENT": "prod"},
    )

    # before_send 是 partial，keywords 仅 allow_user_context
    before_send = options["before_send"]
    assert isinstance(before_send, partial)
    assert before_send.func is sentry_before_send
    assert before_send.keywords == {"allow_user_context": False}

    # before_breadcrumb 是直接函数引用（非 partial）
    assert options["before_breadcrumb"] is sentry_before_breadcrumb

    # before_send_transaction 是 partial
    before_send_transaction = options["before_send_transaction"]
    assert isinstance(before_send_transaction, partial)
    assert before_send_transaction.func is sentry_before_send_transaction
    assert before_send_transaction.keywords == {"allow_user_context": False}

    # before_send_log 是直接函数引用（非 partial）
    assert options["before_send_log"] is sentry_before_send_log

    # ignore_errors 包含 NoneBot 控制流异常
    assert options["ignore_errors"] == list(get_ignored_sentry_exceptions())

    # 其他字段
    assert options["environment"] == "prod"
    assert options["release"] is None
    assert options["enable_logs"] is True


def test_build_sentry_init_options_pii_enables_user_context() -> None:
    """send_default_pii=True 时 before_send 传入 allow_user_context=True。"""
    config = _DummySentryConfig(send_default_pii=True)

    options = build_sentry_init_options(
        config=config,
        dsn="https://example@sentry.invalid/1",
        resolve_level=lambda name, default: getattr(logging, name, default),
        logging_integration_factory=lambda **kwargs: kwargs,
        loguru_integration_factory=lambda **kwargs: kwargs,
        asyncio_integration_factory=lambda: "asyncio",
        fastapi_integration_factory=lambda: "fastapi",
        starlette_integration_factory=lambda: "starlette",
        environ={"ENVIRONMENT": "prod"},
    )

    before_send = options["before_send"]
    assert before_send.keywords == {"allow_user_context": True}

    # before_send_log 仍是直接函数引用
    assert options["before_send_log"] is sentry_before_send_log


def test_build_sentry_init_options_resolves_log_levels() -> None:
    """日志等级字符串正确转换为 int。"""
    config = _DummySentryConfig(
        breadcrumb_level="DEBUG",
        sentry_logs_level="WARNING",
        event_level="ERROR",
    )

    captured: dict[str, dict[str, int]] = {}

    def _logging_integration_factory(**kwargs: int) -> dict[str, int]:
        captured["logging"] = dict(kwargs)
        return dict(kwargs)

    def _loguru_integration_factory(**kwargs: int) -> dict[str, int]:
        captured["loguru"] = dict(kwargs)
        return dict(kwargs)

    build_sentry_init_options(
        config=config,
        dsn="https://example@sentry.invalid/1",
        resolve_level=lambda name, default: getattr(logging, name, default),
        logging_integration_factory=_logging_integration_factory,
        loguru_integration_factory=_loguru_integration_factory,
        asyncio_integration_factory=lambda: "asyncio",
        fastapi_integration_factory=lambda: "fastapi",
        starlette_integration_factory=lambda: "starlette",
        environ={"ENVIRONMENT": "prod"},
    )

    assert captured["logging"] == {
        "sentry_logs_level": logging.WARNING,
        "level": logging.DEBUG,
        "event_level": logging.ERROR,
    }
    assert captured["loguru"] == {
        "sentry_logs_level": logging.WARNING,
        "level": logging.DEBUG,
        "event_level": logging.ERROR,
    }
