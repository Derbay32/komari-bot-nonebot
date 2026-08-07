"""NoneBot ForwardRef 兼容层测试。"""

from __future__ import annotations

import warnings
from typing import Any, ForwardRef, cast

import nonebot.dependencies.utils as dependency_utils
import nonebot.typing as nonebot_typing

from komari_bot.core.nonebot_compat import (
    install_nonebot_forwardref_compatibility,
)


def test_forwardref_compatibility_uses_one_warning_free_implementation() -> None:
    install_nonebot_forwardref_compatibility()
    dependency_utils_any = cast("Any", dependency_utils)

    assert dependency_utils_any.evaluate_forwardref is nonebot_typing.evaluate_forwardref
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", DeprecationWarning)
        result = dependency_utils_any.evaluate_forwardref(ForwardRef("int"), {}, {})

    assert result is int
    assert not [
        warning
        for warning in captured
        if "type_params" in str(warning.message)
    ]
