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


def _normalize_group_id(group_id: object) -> int | None:
    """把调用方持有的群号归一为正整数；不可归一时返回 ``None``。

    记忆链路的群号多为 Redis/JSON 里的字符串，而 ``adjudicate`` 的归属校验
    （``_validate_attribution``）只接受正整数集合，标量或字符串元素都会被判
    归属不可用而故障关闭。归一失败时调用方以空集合传递，与 ``None`` 走同一
    ``group_attribution_unavailable`` 拒绝路径，保留遥测与持有态语义（AC8）。
    """
    if type(group_id) is int:
        return group_id if group_id > 0 else None
    if isinstance(group_id, str):
        try:
            value = int(group_id.strip())
        except ValueError:
            return None
        return value if value > 0 else None
    return None


def memory_conversation_business_admitted(*, group_id: Any) -> bool:
    """对紧接其后的单个对话 processing 效果执行一次 BUSINESS 裁决，返回是否获准。

    同步、无 I/O，裁决是效果前最后一个同步步骤；只认 ``business`` 资格。群号
    归一为正整数后以单元素集合传递；不可归一时仍以空集合调用裁决，由裁决层
    故障关闭并记录归属失败（AC8），绝不跳过裁决直接放行。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    normalized = _normalize_group_id(group_id)
    result = admission_pkg.adjudicate([normalized] if normalized is not None else [])
    return result.qualification.value == "business"


def memory_associated_groups_business_admitted(*, group_ids: Any) -> bool:
    """对一批互动记录的关联群集合在最前做 BUSINESS 全量裁决。

    按 ``(group_id, user_id)`` 分区：逐个关联群独立裁决，任一受限则该分区视为
    受限（休眠），获准分区不被同批次受限群拖累（AC6/补项1）。只认 ``business``
    资格。群号逐个归一为正整数单元素集合裁决；不可归因的群以空集合传递，走
    归属失败路径并视为受限。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    for group_id in dict.fromkeys(group_ids):
        normalized = _normalize_group_id(group_id)
        result = admission_pkg.adjudicate(
            [normalized] if normalized is not None else []
        )
        if result.qualification.value != "business":
            return False
    return True


def memory_admitted_partition_groups(*, group_ids: Any) -> list[Any]:
    """按 (group_id, user_id) 分区逐群独立裁决，返回获准的关联群清单。

    混批互不阻塞（补项1）：受限群的贡献休眠，获准群的贡献仍进入不可逆全局
    commit；返回的获准群保留调用方原始群号（供按原始记录过滤），携带 lineage
    直到 global commit。同步、无 I/O。群号归一只发生在裁决传参边界，不改变
    返回值与存储层的原始表示。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    admitted: list[Any] = []
    for group_id in dict.fromkeys(group_ids):
        normalized = _normalize_group_id(group_id)
        result = admission_pkg.adjudicate(
            [normalized] if normalized is not None else []
        )
        if result.qualification.value == "business":
            admitted.append(group_id)
    return admitted


def memory_batch_business_admitted(*, group_ids: Any) -> bool:
    """对全局每日衰减的关联群集合做一次全量 BUSINESS 裁决。

    forgetting 是跨全库的批量持久群工作：作用前按最小归属投影对关联群集合裁决，
    任一受限（或归属缺失）则当日衰减跳过（AC7），恢复后不补算休眠天数。即便投影
    为空也保留裁决接缝（归属失败进入持有态）。元素逐个归一，任一不可归因即以
    空集合记归属失败并整批受限。
    """
    import komari_bot.plugins.group_admission as admission_pkg

    normalized_groups: list[int] = []
    for group_id in dict.fromkeys(group_ids):
        normalized = _normalize_group_id(group_id)
        if normalized is None:
            result = admission_pkg.adjudicate([])
            return result.qualification.value == "business"
        normalized_groups.append(normalized)
    result = admission_pkg.adjudicate(normalized_groups)
    return result.qualification.value == "business"


__all__ = [
    "memory_admitted_partition_groups",
    "memory_associated_groups_business_admitted",
    "memory_batch_business_admitted",
    "memory_conversation_business_admitted",
]
