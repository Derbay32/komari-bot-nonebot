"""komari_chat 编排层准入辅助（ADR-0012 / TSK-225）。

统一群聊准入只由持有归属的编排 module 放置，低层 LLM/Search/Embedding/
OneBot Adapter 策略无感（见 ``test_chat_admission_boundary``）。本模块是
komari_chat 编排层消费顶层 ``adjudicate`` 的单一同步 helper：

- 每个不可分聊天瞬时效果（LLM 轮 / 工具 dispatch / 视觉 / 图片下载 /
  Embedding / 平台读取 / 表情 / 群输出 / 失败文本 / debug 公开输出）在效果前
  最后一个同步步骤调用它；裁决与效果提交点之间不插入无关 ``await``。
- 一次裁决只授权紧随其后的一个不可分效果，结果不跨效果缓存。
- 归属来自调用方持有的 ``group_id``；缺少归属时裁决层故障关闭（拒绝）。
- 调用面读取 top-level ``adjudicate``（包级运行时属性解析，可被
  ``chat_admission_support.install_scripted_adjudicate`` 替换拦截，也可被
  未来可观测替换）；只在被调用时惰性 import，避免在测试/插件装载期触发
  ``require``。跨插件只消费顶层暴露面，不 deep import 内部（ADR-0006）。
"""

from __future__ import annotations

from typing import Any


def _normalized_group_id(group_id: str | int | None) -> tuple[int, ...]:
    """把调用方持有的群号规范为裁决关联群；缺值/非法时为空（故障关闭）。"""
    if group_id is None:
        return ()
    try:
        return (int(group_id),)
    except (TypeError, ValueError):
        return ()


def effect_business_admitted(*, group_id: str | int | None) -> bool:
    """对紧接其后的单个聊天瞬时效果执行一次 BUSINESS 裁决，返回是否获准。

    同步、无 I/O。裁决使用顶层 ``adjudicate`` 的默认 BUSINESS 意图；只认
    ``business`` 资格——既成事实收尾或技术清理资格不能扩张解释为可开展
    普通业务。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    result = admission_pkg.adjudicate(_normalized_group_id(group_id))
    return result.qualification.value == "business"


def _adjudicate_role_effect(
    *,
    group_id: str | int | None,
    intent: Any,
) -> Any:
    """按回复履约阶段声明的 intent 执行一次同步裁决，返回顶层结果。

    只在被调用时惰性 import ``komari_bot.plugins.group_admission`` 顶层
    包，避免在测试/插件装载期触发 ``require`` 副作用；调用面经顶层暴露
    面读取包级 ``adjudicate``（可被 ``fulfillment_admission_support`` 的
    替换管线拦截）。裁决是效果前最后一个同步步骤，一次只授权紧随其后的
    一个不可分效果。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    return admission_pkg.adjudicate(_normalized_group_id(group_id), intent=intent)


def reply_fulfillment_business_admitted(*, group_id: str | int | None) -> bool:
    """回复履约的 BUSINESS 阶梯：普通业务资格才允许准备与发送。

    只认 ``business`` 资格；既有事实收尾或技术清理不能扩张解释为可再次
    开展普通业务（ADR-0012）。
    """
    from komari_bot.plugins.group_admission import AdmissionIntent

    result = _adjudicate_role_effect(group_id=group_id, intent=AdmissionIntent.BUSINESS)
    return result.qualification.value == "business"


def reply_fulfillment_finalization_granted(*, group_id: str | int | None) -> bool:
    """回复履约的送达/承诺对账阶梯：按 FACT_FINALIZATION 重裁决。

    只认既成事实收尾资格；受限群（REJECTED）与查明缺失时不允许执行承诺。
    """
    from komari_bot.plugins.group_admission import AdmissionIntent

    result = _adjudicate_role_effect(
        group_id=group_id,
        intent=AdmissionIntent.FACT_FINALIZATION,
    )
    return result.qualification.value != "rejected"


def reply_fulfillment_cleanup_authorized(*, group_id: str | int | None) -> bool:
    """终态证据清理只用 TECHNICAL_CLEANUP 声明一次裁决，恒获准（ADR-0012）。

    仅用于调用面记录（记录 intent）与可观测；技术清理在受限
    群与空关联群均允许，因此本函数恒返回 True，不阻断清理本身。
    """
    from komari_bot.plugins.group_admission import AdmissionIntent

    _adjudicate_role_effect(
        group_id=group_id,
        intent=AdmissionIntent.TECHNICAL_CLEANUP,
    )
    return True


__all__ = [
    "effect_business_admitted",
    "reply_fulfillment_business_admitted",
    "reply_fulfillment_cleanup_authorized",
    "reply_fulfillment_finalization_granted",
]
