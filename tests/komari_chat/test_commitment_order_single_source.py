"""承诺写入顺序单一事实来源验收测试 (TSK-110)。

承诺写入顺序由领域模块 ``reply_fulfillment_domain`` 的公开常量
``COMMITMENT_ORDER`` 唯一权威定义；仓储模块与服务模块都必须
from-import 该常量，不得各自定义或引用下划线私有副本。本文件只
断言模块命名空间本身（直接 import 模块再访问属性），避免 from-import
掩盖「模块命名空间没有该名字」的缺失。
"""

from __future__ import annotations

from komari_bot.plugins.komari_chat import reply_fulfillment_domain
from komari_bot.plugins.komari_chat.repositories import (
    reply_fulfillment_repository,
)
from komari_bot.plugins.komari_chat.services import reply_commitment_workflow

KNOWN_COMMITMENT_TYPES = (
    "proactive_reply_confirmation",
    "favorability_adjustment",
    "assistant_reply_history",
    "interaction_history",
)


def test_domain_exports_commitment_order_derived_from_types() -> None:
    """验收标准 1：领域模块公开 COMMITMENT_ORDER 并加入 __all__。

    内容必须是 ``{t: i for i, t in enumerate(COMMITMENT_TYPES)}``，
    键集合恰为四个已知承诺类型。
    """
    assert "COMMITMENT_ORDER" in reply_fulfillment_domain.__all__

    order = reply_fulfillment_domain.COMMITMENT_ORDER
    expected = {
        commitment_type: index
        for index, commitment_type in enumerate(
            reply_fulfillment_domain.COMMITMENT_TYPES
        )
    }
    assert order == expected
    assert set(order) == set(KNOWN_COMMITMENT_TYPES)
    assert len(order) == len(KNOWN_COMMITMENT_TYPES)


def test_repository_reuses_domain_commitment_order() -> None:
    """验收标准 2：仓储模块 from-import 领域常量，不再保留私有副本。"""
    assert (
        reply_fulfillment_repository.COMMITMENT_ORDER
        is reply_fulfillment_domain.COMMITMENT_ORDER
    )
    assert not hasattr(reply_fulfillment_repository, "_COMMITMENT_ORDER")


def test_workflow_reuses_domain_commitment_order() -> None:
    """验收标准 3：服务模块 from-import 领域常量，不再引用仓储私有符号。"""
    assert (
        reply_commitment_workflow.COMMITMENT_ORDER
        is reply_fulfillment_domain.COMMITMENT_ORDER
    )
    assert not hasattr(reply_commitment_workflow, "_COMMITMENT_ORDER")


def test_commitment_order_get_sort_keeps_fixed_sequence() -> None:
    """验收标准 4（防漂移锚点）：按 .get 规则排序保持固定顺序。

    打乱顺序的四个承诺类型按
    ``COMMITMENT_ORDER.get(t, len(COMMITMENT_ORDER))`` 排序后必须
    等于 ``COMMITMENT_TYPES`` 的固定顺序；未知类型按兜底序号排到
    已知类型之后（仓储与服务模块的排序键共用同一规则）。本锚点用
    ``COMMITMENT_TYPES`` 本地推导参考序号（即实现落地后的推导规则），
    在单源化实现落地前即为绿，防排序行为漂移。
    """
    reference_order = {
        commitment_type: index
        for index, commitment_type in enumerate(
            reply_fulfillment_domain.COMMITMENT_TYPES
        )
    }
    shuffled = list(KNOWN_COMMITMENT_TYPES)
    shuffled.reverse()

    sorted_types = sorted(
        shuffled,
        key=lambda commitment_type: reference_order.get(
            commitment_type, len(reference_order)
        ),
    )
    assert sorted_types == list(reply_fulfillment_domain.COMMITMENT_TYPES)

    unknown_and_known = ["dynamic_callback", *KNOWN_COMMITMENT_TYPES]
    sorted_with_unknown = sorted(
        unknown_and_known,
        key=lambda commitment_type: reference_order.get(
            commitment_type, len(reference_order)
        ),
    )
    assert sorted_with_unknown == [
        *reply_fulfillment_domain.COMMITMENT_TYPES,
        "dynamic_callback",
    ]
