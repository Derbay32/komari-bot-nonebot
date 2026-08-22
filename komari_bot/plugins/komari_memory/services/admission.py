"""komari_memory 编排层群准入辅助（ADR-0012 / TSK-230）。

统一群聊准入只由持有归属的编排 module 放置，低层 Redis/PG Repository、
Embedding/LLM Adapter 策略无感（见 ``test_dependency_boundary``）。本模块
是 komari_memory 编排层消费顶层 ``adjudicate`` 的同步 helper：

- 每个不可分持久群工作效果（候选发现后的正文处理、快照写入、互动全局
  commit、每日衰减）在效果前最后一个同步步骤调用它；裁决与效果提交点之间
  不插入无关 ``await``。
- 一次裁决只授权紧随其后的一个不可分效果，结果不跨效果缓存；受限时保存
  安全进度并休眠，不消耗 retry budget、不触发普通失败告警。
- 归属来自调用方持有的群号；缺值/非法时裁决层故障关闭（拒绝）并进入
  ``ADMISSION_ATTRIBUTION_FAILED`` 运维持有态（AC8）。
- 调用面读取 top-level ``adjudicate``（可被 ``chat_admission_support`` 的
  ``install_scripted_adjudicate`` 判定替换拦截），只在被调用时惰性 import，
  避免在测试 / 插件装载期触发 ``require``（ADR-0006 只消费顶层暴露面）。
"""

from __future__ import annotations

from typing import Any


def memory_conversation_business_admitted(*, group_id: Any) -> bool:
    """对紧接其后的单个对话 processing 效果执行一次 BUSINESS 裁决，返回是否获准。

    同步、无 I/O，裁决是效果前最后一个同步步骤；只认 ``business`` 资格。群号
    作为归属随调用一起传递（测试探测覆盖的呈现面），缺值/非法由裁决层故障关闭。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    result = admission_pkg.adjudicate(group_id)
    return result.qualification.value == "business"


def memory_associated_groups_business_admitted(*, group_ids: Any) -> bool:
    """对一批互动记录的关联群集合在最前做 BUSINESS 全量裁决。

    按 ``(group_id, user_id)`` 分区：逐个关联群独立裁决，任一受限则该分区视为
    受限（休眠），获准分区不被同批次受限群拖累（AC6/补项1）。只认 ``business``
    资格。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    for group_id in set(group_ids):
        result = admission_pkg.adjudicate(group_id)
        if result.qualification.value != "business":
            return False
    return True


def memory_admitted_partition_groups(*, group_ids: Any) -> list[Any]:
    """按 (group_id, user_id) 分区逐群独立裁决，返回获准的关联群清单。

    混批互不阻塞（补项1）：受限群的贡献休眠，获准群的贡献仍进入不可逆全局
    commit；返回的获准群可继续携带 lineage 直到 global commit。同步、无 I/O。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    admitted: list[Any] = []
    for group_id in set(group_ids):
        result = admission_pkg.adjudicate(group_id)
        if result.qualification.value == "business":
            admitted.append(group_id)
    return admitted


def memory_batch_business_admitted(*, group_ids: Any) -> bool:
    """对全局每日衰减的关联群集合做一次全量 BUSINESS 裁决。

    forgetting 是跨全库的批量持久群工作：作用前按最小归属投影对关联群集合裁决，
    任一受限（或归属缺失）则当日衰减跳过（AC7），恢复后不补算休眠天数。即便投影
    为空也保留裁决接缝（归属失败进入持有态）。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    result = admission_pkg.adjudicate(group_ids)
    return result.qualification.value == "business"


__all__ = [
    "memory_admitted_partition_groups",
    "memory_associated_groups_business_admitted",
    "memory_batch_business_admitted",
    "memory_conversation_business_admitted",
]
