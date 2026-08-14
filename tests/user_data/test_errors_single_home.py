"""TSK-122：user_data 异常类型收编 errors.py 单一家园验收测试。

验收点（对应票据）：
a. 新建 ``komari_bot/plugins/user_data/errors.py`` 作为四个同族异常类型的
   唯一定义家园，四个类的 ``cls.__module__ == "komari_bot.plugins.user_data.errors"``。
b. 同一类对象身份：``database`` 子模块与插件顶层入口均以 from-import 引用
   ``errors`` 模块中的同一类对象（``is`` 同一），re-export 不复制定义。
c. 四个类的 ``__annotations__`` 均含 ``"error_code"`` 键（ClassVar 注解的
   可观测信号；旧两类当前为裸赋值，不产生注解条目 → 红线）。
d. 防漂移锚点（当前即绿、实现后仍绿）：基类、``error_code`` 值、包顶层
   ``__all__`` 仍含四个名字；两个旧类的消息正文经既有 raise 路径逐字保持。

import 方式：conftest 以 shim 覆盖 ``komari_bot.plugins.user_data`` 包名，
按既有测试约定（tests/user_data/test_error_code_exceptions.py）经
``komari_bot.plugins.user_data.__init__`` 加载真实插件入口，再以模块属性
访问断言；``errors`` 子模块当前尚不存在，一律在测试函数/辅助内经
``importlib.import_module`` 延迟加载（当前 ModuleNotFoundError 即红线），
避免模块级 import 导致整文件收集错误。
"""

from __future__ import annotations

import sys
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from nonebug import App

_ERROR_NAMES: tuple[str, ...] = (
    "UserDataDisabledError",
    "UserDataStoppingError",
    "UserDataUnavailableError",
    "FavorabilityIdempotencyConflictError",
)

_EXPECTED_BASES: dict[str, type[BaseException]] = {
    "UserDataDisabledError": RuntimeError,
    "UserDataStoppingError": RuntimeError,
    "UserDataUnavailableError": RuntimeError,
    "FavorabilityIdempotencyConflictError": ValueError,
}

_EXPECTED_ERROR_CODES: dict[str, str] = {
    "UserDataDisabledError": "service_unavailable",
    "UserDataStoppingError": "service_unavailable",
    "UserDataUnavailableError": "service_unavailable",
    "FavorabilityIdempotencyConflictError": "idempotency_conflict",
}

_ERRORS_MODULE_NAME = "komari_bot.plugins.user_data.errors"

_DISABLED_MESSAGE = "user_data 插件已禁用"
_STOPPED_MESSAGE = "user_data 已完成 shutdown，不能在同一生命周期内重启"


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


def _load_errors_module() -> Any:
    """延迟加载 errors 子模块；实现前不存在 → ModuleNotFoundError 即红线。"""
    return cast("Any", import_module(_ERRORS_MODULE_NAME))


def test_errors_module_defines_four_error_classes() -> None:
    """验收点 a：errors 模块定义四个异常类型。"""
    errors_module = _load_errors_module()
    for name in _ERROR_NAMES:
        assert hasattr(errors_module, name)


def test_all_four_error_classes_live_in_errors_module() -> None:
    """验收点 a：四个类的 __module__ 均为 errors 模块（唯一定义家园）。"""
    errors_module = _load_errors_module()
    for name in _ERROR_NAMES:
        cls = getattr(errors_module, name)
        defined_in = getattr(cls, "__module__", None)
        assert defined_in == _ERRORS_MODULE_NAME


def test_database_module_classes_are_single_home_in_errors() -> None:
    """验收点 b：database 的两个异常与 errors 模块同类对象 is 同一。"""
    import komari_bot.plugins.user_data.database as database_module

    errors_module = _load_errors_module()
    for name in (
        "UserDataUnavailableError",
        "FavorabilityIdempotencyConflictError",
    ):
        database_cls = getattr(database_module, name)
        errors_cls = getattr(errors_module, name)
        assert database_cls is errors_cls


def test_plugin_top_level_classes_are_single_home_in_errors(
    user_data_module: Any,
) -> None:
    """验收点 b：插件顶层四个异常属性与 errors 模块同类对象 is 同一。"""
    errors_module = _load_errors_module()
    for name in _ERROR_NAMES:
        top_cls = getattr(user_data_module, name)
        errors_cls = getattr(errors_module, name)
        assert top_cls is errors_cls


def test_all_four_classes_annotate_error_code() -> None:
    """验收点 c：四个类 __annotations__ 均含 "error_code" 键。

    旧两类当前为裸赋值（不产生注解条目），且 errors 模块尚不存在；
    任一未满足即红，实现补齐 ClassVar 注解后转绿。
    """
    errors_module = _load_errors_module()
    for name in _ERROR_NAMES:
        cls = getattr(errors_module, name)
        annotations = getattr(cls, "__annotations__", {})
        assert "error_code" in annotations


def test_error_base_classes_unchanged(user_data_module: Any) -> None:
    """验收点 d：四个类基类保持（当前即绿、实现后仍绿）。"""
    for name, expected_base in _EXPECTED_BASES.items():
        cls = getattr(user_data_module, name)
        bases = getattr(cls, "__bases__", ())
        assert bases == (expected_base,)


def test_error_code_values_unchanged(user_data_module: Any) -> None:
    """验收点 d：error_code 值保持（当前即绿、实现后仍绿）。"""
    for name, expected_code in _EXPECTED_ERROR_CODES.items():
        cls = getattr(user_data_module, name)
        error_code = getattr(cls, "error_code", None)
        assert error_code == expected_code


def test_package_top_level_all_exports_four_error_names(
    user_data_module: Any,
) -> None:
    """验收点 d：包顶层 __all__ 与可访问面仍含四个异常名字。"""
    for name in _ERROR_NAMES:
        assert name in user_data_module.__all__
        assert hasattr(user_data_module, name)


@pytest.mark.asyncio
async def test_user_data_disabled_error_message_preserved(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收点 d：UserDataDisabledError 消息正文经既有 raise 路径逐字保持。"""

    async def _disabled_config() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=False)

    monkeypatch.setattr(user_data_module, "_get_config_async", _disabled_config)

    with pytest.raises(user_data_module.UserDataDisabledError) as excinfo:
        await user_data_module.get_db()

    exc = excinfo.value
    error_code = getattr(exc, "error_code", None)
    assert str(exc) == _DISABLED_MESSAGE
    assert isinstance(exc, RuntimeError)
    assert error_code == "service_unavailable"


@pytest.mark.asyncio
async def test_user_data_stopping_error_message_preserved(
    user_data_module: Any,
) -> None:
    """验收点 d：UserDataStoppingError 消息正文经既有 raise 路径逐字保持。"""
    user_data_module._lifecycle_state = "stopped"

    with pytest.raises(user_data_module.UserDataStoppingError) as excinfo:
        await user_data_module.on_startup()

    exc = excinfo.value
    error_code = getattr(exc, "error_code", None)
    assert str(exc) == _STOPPED_MESSAGE
    assert isinstance(exc, RuntimeError)
    assert error_code == "service_unavailable"
