"""NoneBot 与当前 Python 运行时之间的临时兼容层。"""

from __future__ import annotations

import warnings
from threading import Lock
from typing import Any, ForwardRef

from typing_extensions import evaluate_forward_ref

_PATCH_LOCK = Lock()
_PATCH_INSTALLED = False
_WARNING_PREFIX = "Failing to pass a value to the 'type_params' parameter"


def _evaluate_forwardref(
    ref: ForwardRef,
    globalns: dict[str, Any],
    localns: dict[str, Any],
) -> Any:
    """通过公开兼容 API 解析 ForwardRef，并显式传入空类型参数域。"""
    return evaluate_forward_ref(
        ref,
        globals=globalns,
        locals=localns,
        type_params=(),
    )


def install_nonebot_forwardref_compatibility() -> bool:
    """仅在 NoneBot 2.5.0 仍触发 Python 3.13 弃用告警时安装补丁。

    Returns:
        ``True`` 表示当前进程需要且已经启用兼容实现；上游已修复时返回
        ``False``，避免永久覆盖框架的新实现。
    """
    import nonebot.dependencies.utils as dependency_utils
    import nonebot.typing as nonebot_typing

    global _PATCH_INSTALLED  # noqa: PLW0603
    with _PATCH_LOCK:
        if _PATCH_INSTALLED:
            return True

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", DeprecationWarning)
            try:
                nonebot_typing.evaluate_forwardref(ForwardRef("int"), {}, {})
            except TypeError:
                needs_compatibility = True
            else:
                needs_compatibility = any(
                    _WARNING_PREFIX in str(warning.message) for warning in captured
                )

        if not needs_compatibility:
            return False

        dependency_utils_any: Any = dependency_utils
        nonebot_typing.evaluate_forwardref = _evaluate_forwardref
        dependency_utils_any.evaluate_forwardref = _evaluate_forwardref
        _PATCH_INSTALLED = True
        return True


__all__ = ["install_nonebot_forwardref_compatibility"]
