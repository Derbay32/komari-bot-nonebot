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
    import komari_bot.plugins.group_admission as admission_pkg  # noqa: PLC0415

    result = admission_pkg.adjudicate(_normalized_group_id(group_id))
    return result.qualification.value == "business"


__all__ = ["effect_business_admitted"]