"""群历史总结编排层准入辅助（ADR-0012 / TSK-229）。

统一群聊准入只由持有归属的编排 module 放置，低层 LLM / 平台读取 Adapter
策略无感。本模块是 ``group_history_summary`` 编排层消费顶层 ``adjudicate``
的单一同步 helper：

- 每个不可分总结瞬时效果（history 平台读取 / planning LLM / summary LLM /
  图片渲染 / 群输出）在效果前最后一个同步步骤调用它；裁决与效果提交点之间
  不插入无关 ``await``，一次裁决只授权紧随其后的一个不可分效果。
- 归属来自调用方持有的 ``group_id``（总结命令所在群），intent 恒为
  BUSINESS；缺少归属时裁决层故障关闭（拒绝）。
- 调用面读取顶层 ``adjudicate``（包级运行时属性解析，可被测试替身替换拦截，
  也可被未来可观测替换）；只在被调用时惰性 import，避免测试 / 插件装载期
  触发 ``require``。跨插件只消费顶层暴露面，不 deep import 内部（ADR-0006）。
"""

from __future__ import annotations


class SummaryAdmissionDeniedError(Exception):
    """单个总结效果被群准入拒绝。

    属正常控制流：瞬时总结任务立即终止，不作失败记录、不触发失败通知 /
    retry、恢复准入后不复活。执行入口专门捕获并转为无声终止。
    """


def _normalized_group_id(group_id: str | int | None) -> tuple[int, ...]:
    """把调用方持有的群号规范为裁决关联群；缺值/非法时为空（故障关闭）。"""
    if group_id is None:
        return ()
    try:
        return (int(group_id),)
    except (TypeError, ValueError):
        return ()


def effect_business_admitted(*, group_id: str | int | None) -> bool:
    """对紧接其后的单个总结效果执行一次 BUSINESS 裁决，返回是否获准。

    同步、无 I/O。裁决使用顶层 ``adjudicate`` 的默认 BUSINESS 意图；只认
    ``business`` 资格——既成事实收尾或技术清理资格不能扩张解释为可开展普通
    业务。受限时返回 ``False``，调用方按正常控制流终止该阶段。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    result = admission_pkg.adjudicate(_normalized_group_id(group_id))
    return result.qualification.value == "business"


def ensure_effect_admitted(*, group_id: str | int | None) -> None:
    """效果前同步裁决；受限时抛 ``SummaryAdmissionDeniedError`` 立即终止当前阶段。

    供编排层在需要中止整个总结流水线时使用：裁决通过则继续，拒绝则抛出
    框架内可识别的正常控制流异常，由 ``execute_group_summary`` 统一收口为
    无声终止（不留错误、不发通知、不复活）。
    """
    if not effect_business_admitted(group_id=group_id):
        raise SummaryAdmissionDeniedError


__all__ = [
    "SummaryAdmissionDeniedError",
    "effect_business_admitted",
    "ensure_effect_admitted",
]
