"""TSK-112：user_data error_code 类属性异常类型验收测试。

验收点：
1. 顶层导出与类型契约：``UserDataUnavailableError`` /
   ``FavorabilityIdempotencyConflictError`` 由 user_data 插件顶层导出并列入
   ``__all__``，且在 ``database`` 模块中定义（顶层 re-export 同一类对象）；
   isinstance 基类与类属性 ``error_code`` 符合约定（类上直接可取，不实例化）。
2. 未初始化路径行为：未初始化的 ``UserDataDB`` 经 ``_require_ready``（直接
   调用或任一公共入口）抛出 ``UserDataUnavailableError``，消息逐字为
   「UserDataDB 连接池未初始化」，兼容 ``RuntimeError``，实例
   ``error_code == "service_unavailable"``。
3. 幂等冲突类型契约（无需数据库）：``FavorabilityIdempotencyConflictError``
   实例是 ``ValueError``，``error_code == "idempotency_conflict"``，消息逐字
   保持。

import 方式：conftest 以 shim 覆盖 ``komari_bot.plugins.user_data`` 包名，
按既有测试约定（tests/user_data/test_plugin.py）经
``komari_bot.plugins.user_data.__init__`` 加载真实插件入口，再以模块属性
访问断言，避免 from-import 在导入期掩盖缺失。
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB

if TYPE_CHECKING:
    from nonebug import App

_UNINITIALIZED_MESSAGE = "UserDataDB 连接池未初始化"
_CONFLICT_MESSAGE = "好感度 operation_id 与既有请求载荷冲突"


@pytest.fixture
def user_data_module(app: App) -> Any:
    """加载真实 user_data 插件入口（只读异常类型与 __all__）。"""
    del app
    module_name = "komari_bot.plugins.user_data.__init__"
    sys.modules.pop(module_name, None)
    module = import_module(module_name)
    module_any = cast("Any", module)
    # 测试只读异常类型；重置生命周期状态，避免与其他 user_data 测试互相干扰。
    module_any._db = None
    module_any._db_init_lock = None
    module_any._db_init_lock_loop = None
    module_any._lifecycle_state = "new"
    return module_any


def _build_uninitialized_db() -> UserDataDB:
    """最小依赖构造未初始化的 UserDataDB（不触碰任何数据库连接）。"""
    return UserDataDB(DynamicConfigSchema(initial_favorability=0))


def test_error_types_exported_from_package_top_level(
    user_data_module: Any,
) -> None:
    """验收点 1：两个异常类型由插件顶层导出并列入 __all__。"""
    assert hasattr(user_data_module, "UserDataUnavailableError")
    assert hasattr(user_data_module, "FavorabilityIdempotencyConflictError")
    assert "UserDataUnavailableError" in user_data_module.__all__
    assert "FavorabilityIdempotencyConflictError" in user_data_module.__all__


def test_error_types_defined_in_database_module(
    user_data_module: Any,
) -> None:
    """验收点 1：异常类型在 database 模块中定义，顶层 re-export 同一类对象。"""
    import komari_bot.plugins.user_data.database as database_module

    assert hasattr(database_module, "UserDataUnavailableError")
    assert hasattr(database_module, "FavorabilityIdempotencyConflictError")
    assert (
        database_module.UserDataUnavailableError
        is user_data_module.UserDataUnavailableError
    )
    assert (
        database_module.FavorabilityIdempotencyConflictError
        is user_data_module.FavorabilityIdempotencyConflictError
    )


def test_user_data_unavailable_error_type_contract(
    user_data_module: Any,
) -> None:
    """验收点 1：基类与类属性 error_code（类上直接可取，不实例化）。"""
    cls = user_data_module.UserDataUnavailableError
    error_code = getattr(cls, "error_code", None)

    assert issubclass(cls, RuntimeError)
    assert error_code == "service_unavailable"


def test_favorability_idempotency_conflict_error_type_contract(
    user_data_module: Any,
) -> None:
    """验收点 1：基类与类属性 error_code（类上直接可取，不实例化）。"""
    cls = user_data_module.FavorabilityIdempotencyConflictError
    error_code = getattr(cls, "error_code", None)

    assert issubclass(cls, ValueError)
    assert error_code == "idempotency_conflict"


def test_require_ready_raises_user_data_unavailable_error(
    user_data_module: Any,
) -> None:
    """验收点 2：直接调用 _require_ready，抛出具名异常，消息逐字、error_code 正确。"""
    db = _build_uninitialized_db()

    with pytest.raises(user_data_module.UserDataUnavailableError) as excinfo:
        db._require_ready()

    exc = excinfo.value
    error_code = getattr(exc, "error_code", None)
    assert str(exc) == _UNINITIALIZED_MESSAGE
    assert isinstance(exc, RuntimeError)
    assert error_code == "service_unavailable"


@pytest.mark.asyncio
async def test_uninitialized_entry_point_raises_user_data_unavailable_error(
    user_data_module: Any,
) -> None:
    """验收点 2：公共入口（get_user_count）在未初始化时抛出同一具名异常。"""
    db = _build_uninitialized_db()

    with pytest.raises(user_data_module.UserDataUnavailableError) as excinfo:
        await db.get_user_count()

    exc = excinfo.value
    error_code = getattr(exc, "error_code", None)
    assert str(exc) == _UNINITIALIZED_MESSAGE
    assert isinstance(exc, RuntimeError)
    assert error_code == "service_unavailable"


def test_favorability_idempotency_conflict_error_instance_contract(
    user_data_module: Any,
) -> None:
    """验收点 3：以既有消息正文实例化，类型 / error_code / 消息逐字保持。"""
    exc = user_data_module.FavorabilityIdempotencyConflictError(_CONFLICT_MESSAGE)
    error_code = getattr(exc, "error_code", None)

    assert isinstance(exc, ValueError)
    assert error_code == "idempotency_conflict"
    assert str(exc) == _CONFLICT_MESSAGE
