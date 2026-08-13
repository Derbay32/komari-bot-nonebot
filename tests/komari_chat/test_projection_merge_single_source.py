"""预占身份投影合并单源化验收测试 (TSK-114)。

``ReplyFulfillmentRepository.claim_fresh_not_started`` 与
``expire_stale_not_started`` 在拿到预占身份投影后各自逐字重复同一段
「行列表 + 投影 -> 合并」循环。验收要求该类新增私有实例方法
``_merge_proactive_reservation_projection(self, rows, projection)``
统一完成合并，两个公开方法都改为调用该辅助，合并语义不变
（``item = dict(row)`` 拷贝、缺失投影时两键为 None、行顺序保持）。

本文件直接 import 类所在模块再取类，避免 from-import 掩盖
「类没有该属性」的缺失；行为语义测试直接调用合并辅助，单源结构
测试用 ``inspect.getsource`` 断言两个公开方法都调用该辅助。
"""

from __future__ import annotations

import inspect

from komari_bot.plugins.komari_chat.repositories import (
    reply_fulfillment_repository,
)

Repository = reply_fulfillment_repository.ReplyFulfillmentRepository

ROW_A = {
    "fulfillment_id": "reply-1",
    "bot_self_id": "bot-1",
    "adapter_name": "OneBot V11",
    "prepared_at": "2026-08-13 10:00:00",
    "send_started_at": None,
}
ROW_B = {
    "fulfillment_id": "reply-2",
    "bot_self_id": "bot-2",
    "adapter_name": "OneBot V11",
    "prepared_at": "2026-08-13 10:01:00",
    "send_started_at": None,
}
ROW_C = {
    "fulfillment_id": "reply-3",
    "bot_self_id": "bot-3",
    "adapter_name": "OneBot V11",
    "prepared_at": "2026-08-13 10:02:00",
    "send_started_at": None,
}

# 投影形态与 ``_project_proactive_reservation_identities`` 返回一致：
# 键为 str(fulfillment_id)，值为含两投影键的 dict。
PROJECTION = {
    "reply-1": {
        "fulfillment_id": "reply-1",
        "proactive_group_id": "group-9",
        "proactive_reservation_id": "res-9",
    },
    "reply-3": {
        "fulfillment_id": "reply-3",
        "proactive_group_id": "group-7",
        "proactive_reservation_id": "res-7",
    },
}


def _repository() -> Repository:
    """``__init__`` 只存 pool，可传 ``None`` 或 ``object()``。"""
    return Repository(object())


def test_merge_projection_hit_carries_proactive_identity() -> None:
    """投影命中时输出行带出预占身份两键的实际值。"""
    merged = _repository()._merge_proactive_reservation_projection(
        [dict(ROW_A)],
        PROJECTION,
    )

    assert merged == [
        {
            **ROW_A,
            "proactive_group_id": "group-9",
            "proactive_reservation_id": "res-9",
        }
    ]


def test_merge_projection_missing_yields_none_keys() -> None:
    """投影缺失的行两键为 None（含投影存在但键缺失的边界）。"""
    repository = _repository()
    rows = [dict(ROW_A), dict(ROW_B), dict(ROW_C)]
    # 投影只有 reply-1 与 reply-3；reply-3 的投影值缺 proactive_reservation_id。
    projection = {
        "reply-1": dict(PROJECTION["reply-1"]),
        "reply-3": {"fulfillment_id": "reply-3"},
    }

    merged = repository._merge_proactive_reservation_projection(rows, projection)

    assert merged[0]["proactive_group_id"] == "group-9"
    assert merged[0]["proactive_reservation_id"] == "res-9"
    # 投影完全缺失：两键为 None。
    assert merged[1]["proactive_group_id"] is None
    assert merged[1]["proactive_reservation_id"] is None
    # 投影存在但键缺失：同样为 None，不得抛错。
    assert merged[2]["proactive_group_id"] is None
    assert merged[2]["proactive_reservation_id"] is None


def test_merge_projection_preserves_fields_copy_semantics_and_order() -> None:
    """既有字段原样保留、输入 rows 不被原地修改、行顺序保持输入顺序。"""
    repository = _repository()
    rows = [dict(ROW_B), dict(ROW_A), dict(ROW_C)]
    rows_before = [dict(row) for row in rows]

    merged = repository._merge_proactive_reservation_projection(rows, PROJECTION)

    # 行顺序保持输入顺序。
    assert [row["fulfillment_id"] for row in merged] == [
        "reply-2",
        "reply-1",
        "reply-3",
    ]
    # 输出行不是输入行的同一对象（拷贝语义）。
    assert all(merged[index] is not row for index, row in enumerate(rows))
    # 输入 rows 不被原地修改。
    assert rows == rows_before
    # 原行全部既有字段原样保留，且只新增两个投影键。
    for index, row in enumerate(rows_before):
        expected = dict(row)
        expected["proactive_group_id"] = (
            PROJECTION.get(str(row["fulfillment_id"]), {}).get(
                "proactive_group_id"
            )
        )
        expected["proactive_reservation_id"] = (
            PROJECTION.get(str(row["fulfillment_id"]), {}).get(
                "proactive_reservation_id"
            )
        )
        assert merged[index] == expected
        assert set(merged[index]) == set(row) | {
            "proactive_group_id",
            "proactive_reservation_id",
        }


def test_claim_and_expire_share_single_merge_helper_call() -> None:
    """两个公开方法都调用 ``_merge_proactive_reservation_projection``。

    断言逐字相同的合并循环只存一份实现、两个调用点共用：两个公开
    方法的源码都必须包含对该辅助的调用。
    """
    claim_source = inspect.getsource(Repository.claim_fresh_not_started)
    expire_source = inspect.getsource(Repository.expire_stale_not_started)

    assert "_merge_proactive_reservation_projection(" in claim_source
    assert "_merge_proactive_reservation_projection(" in expire_source
