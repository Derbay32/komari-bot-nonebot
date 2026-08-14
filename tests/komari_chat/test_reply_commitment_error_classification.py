"""TSK-103：承诺错误分类只依赖结构化 error code 的验收测试。

``_classify_error`` 必须不再匹配异常类名字符串或异常正文子串；下游
异常携带的 ``error_code`` 字符串属性成为唯一分类依据（码→是否永久
的固定映射由 komari_chat 侧持有：``idempotency_conflict`` /
``protocol_violation`` 永久，其余瞬态）。本文件是实施阶段的验收基线：

- 携带 ``error_code`` 的异常按码分类（当前实现不读取该属性 → 红灯）；
- 真实 user_data 异常类暴露 ``error_code`` 类属性（当前缺失 → 红灯）；
- 两个字符串匹配辅助函数被删除（``hasattr`` 断言 → 红灯）；
- 旧形态无码异常不再按中文正文子串误分类（→ 红灯）；
- 既有 isinstance 类型映射与真实 user_data 异常分类逐项不变（→ 已绿，
  防实施阶段破坏既有分支）。
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

import asyncpg
import pytest
from redis.exceptions import RedisError

from komari_bot.plugins.komari_chat.reply_fulfillment_domain import (
    ReplyFulfillmentConflictError,
)

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def workflow_module(app: App) -> Any:
    del app
    return import_module(
        "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
    )


@pytest.fixture
def user_data_module(app: App) -> Any:
    """加载真实 user_data 插件入口，只消费其中的异常类定义。"""
    del app
    module_name = "komari_bot.plugins.user_data.__init__"
    sys.modules.pop(module_name, None)
    module = import_module(module_name)
    module_any = cast("Any", module)
    # 测试只读取异常类；重置生命周期状态，避免与其他 user_data 测试互相干扰。
    module_any._db = None
    module_any._db_init_lock = None
    module_any._db_init_lock_loop = None
    module_any._lifecycle_state = "new"
    return module_any


def _with_error_code(error: BaseException, code: str) -> BaseException:
    """模拟下游异常实例携带结构化 ``error_code``（接缝约定：类或实例属性）。"""
    cast("Any", error).error_code = code
    return error


def test_string_matching_helpers_are_removed(workflow_module: Any) -> None:
    """按类名/正文子串匹配的两个分类辅助函数必须被删除。"""
    assert not hasattr(workflow_module, "_is_service_unavailable")
    assert not hasattr(workflow_module, "_is_idempotency_conflict")


def test_runtime_error_with_error_code_classifies_service_unavailable(
    workflow_module: Any,
) -> None:
    """连接池场景：正文为任意无关文本，仅凭 error_code 分类为 service_unavailable。"""
    error = _with_error_code(RuntimeError("pool gone"), "service_unavailable")
    assert workflow_module._classify_error(error) == (False, "service_unavailable")


def test_value_error_with_error_code_classifies_idempotency_conflict(
    workflow_module: Any,
) -> None:
    """幂等冲突场景：正文为任意无关文本，仅凭 error_code 分类为幂等冲突。"""
    error = _with_error_code(ValueError("payload mismatch"), "idempotency_conflict")
    assert workflow_module._classify_error(error) == (True, "idempotency_conflict")


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("protocol_violation", (True, "protocol_violation")),
        ("transient_timeout", (False, "transient_timeout")),
        ("redis_unavailable", (False, "redis_unavailable")),
        ("connection_error", (False, "connection_error")),
        ("database_unavailable", (False, "database_unavailable")),
    ],
)
def test_error_code_drives_fixed_permanent_mapping(
    workflow_module: Any,
    error_code: str,
    expected: tuple[bool, str],
) -> None:
    """码→是否永久 固定映射：除幂等冲突/协议违例外均为瞬态，分类与异常类型无关。"""
    error = _with_error_code(Exception(f"无关正文 {error_code}"), error_code)
    assert workflow_module._classify_error(error) == expected


def test_real_user_data_error_classes_expose_error_code(
    user_data_module: Any,
) -> None:
    """真实 user_data 异常类暴露 error_code 类属性（与持久化列同一取值集合）。"""
    assert user_data_module.UserDataDisabledError.error_code == "service_unavailable"
    assert user_data_module.UserDataStoppingError.error_code == "service_unavailable"


def test_real_user_data_exceptions_still_classify_service_unavailable(
    workflow_module: Any,
    user_data_module: Any,
) -> None:
    """真实 user_data 异常无论正文措辞如何都分类为 service_unavailable（行为锚点）。"""
    assert workflow_module._classify_error(
        user_data_module.UserDataDisabledError("完全无关的文本")
    ) == (False, "service_unavailable")
    assert workflow_module._classify_error(
        user_data_module.UserDataStoppingError("完全无关的文本")
    ) == (False, "service_unavailable")


def test_plain_runtime_error_without_error_code_is_not_service_unavailable(
    workflow_module: Any,
) -> None:
    """旧形态无码 RuntimeError 不再按中文正文子串误分类为 service_unavailable。"""
    assert workflow_module._classify_error(
        RuntimeError("UserDataDB 连接池未初始化")
    ) == (False, "unexpected_error")


def test_plain_value_error_without_error_code_is_not_idempotency_conflict(
    workflow_module: Any,
) -> None:
    """旧形态无码 ValueError 不再按中文正文子串误分类为 idempotency_conflict。"""
    assert workflow_module._classify_error(
        ValueError("好感度 operation_id 与既有请求载荷冲突")
    ) == (False, "unexpected_error")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ReplyFulfillmentConflictError("冲突正文不可泄漏"), (True, "idempotency_conflict")),
        (TypeError("下游协议正文不可泄漏"), (True, "protocol_violation")),
        (TimeoutError("下游超时"), (False, "transient_timeout")),
        (RedisError("Redis 暂不可用"), (False, "redis_unavailable")),
        (ConnectionError("连接失败"), (False, "connection_error")),
        (OSError("系统调用失败"), (False, "connection_error")),
        (asyncpg.PostgresConnectionError("pg 连接失败"), (False, "database_unavailable")),
        (asyncpg.InterfaceError("pg 接口错误"), (False, "database_unavailable")),
        (KeyError("未知键"), (False, "unexpected_error")),
    ],
)
def test_existing_type_based_mapping_unchanged(
    workflow_module: Any,
    error: BaseException,
    expected: tuple[bool, str],
) -> None:
    """既有 isinstance 类型映射逐项不变（防实施阶段破坏既有分支）。"""
    assert workflow_module._classify_error(error) == expected
