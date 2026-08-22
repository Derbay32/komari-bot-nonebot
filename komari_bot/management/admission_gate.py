"""管理面群目标准入裁决共享辅助（ADR-0012 / TSK-231）。

统一群聊准入只由持有归属的编排 module 在效果前最后一个同步步骤调用顶层
``adjudicate``。本模块为 komari_management / komari_memory 的受审计管理
端点（回复履约、维护公告、dead-letter、群记忆单目标）提供：

- 群号归一为正整数集合传递；不可/非法群号以空集合裁决，走归属失败拒绝，
  绝不裸标量或字符串元素直传（TSK-230 教训，零容忍）。
- 单目标效果的 HTTP 投影：有效策略/归属不可用 -> 503，策略拒绝 -> 403。
- 列表最小归属投影的逐项判定（受限项剔除）。

调用面只经顶层 ``group_admission`` 暴露面同步访问，且只在被调用时惰性
import，避免在插件装载 / 测试导入期触发 ``require``（本模块顶层不触碰
``group_admission``）。
"""

from __future__ import annotations

from typing import Any


def normalize_group_id(group_id: object) -> int | None:
    """把调用方持有的群号归一为正整数；不可归一时返回 ``None``。"""
    if type(group_id) is int:
        return group_id if group_id > 0 else None
    if isinstance(group_id, str):
        try:
            value = int(group_id.strip())
        except ValueError:
            return None
        return value if value > 0 else None
    return None


def resolve_admission_intent(name: str) -> object:
    """按名称惰性解析 ``AdmissionIntent``（避免模块加载期触碰 ``require``）。"""
    import komari_bot.plugins.group_admission as admission_pkg

    return admission_pkg.AdmissionIntent(name)


def gate_single(*, group_id: object, intent: Any = None) -> tuple[bool, int | None]:
    """对紧随其后的单个不可分群效果执行一次裁决。

    返回 ``(granted, http_status)``：``granted=True`` 时 ``http_status`` 为
    ``None``；被拒绝时 ``granted=False`` 且按原因给出 ``403``（策略拒绝）或
    ``503``（有效策略/归属不可用）。群号不可归一时仍以空集合调用裁决，走
    归属失败拒绝。绝不跳过裁决直接放行。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    normalized = normalize_group_id(group_id)
    result = admission_pkg.adjudicate(
        [normalized] if normalized is not None else [],
        intent=intent,
    )
    return _project_status(result)


def gate_item(*, group_id: object, intent: Any = None) -> bool:
    """列表最小归属投影的逐项判定：获准 ``True``、受限/归属失败 ``False``。"""
    granted, _ = gate_single(group_id=group_id, intent=intent)
    return granted


def _project_status(result: Any) -> tuple[bool, int | None]:
    qualification = result.qualification.value
    if qualification != "rejected":
        return True, None
    reason = getattr(result, "reason_code", None)
    if reason in ("group_attribution_unavailable", "effective_policy_unavailable"):
        return False, 503
    return False, 403


__all__ = [
    "gate_item",
    "gate_single",
    "normalize_group_id",
    "resolve_admission_intent",
]
