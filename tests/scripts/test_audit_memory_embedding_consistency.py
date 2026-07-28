"""记忆向量一致性只读检查脚本测试。"""

import hashlib

from scripts.audit_memory_embedding_consistency import audit_rows


def test_audit_rows_reports_ids_without_exposing_content() -> None:
    valid_content = "已同步正文"
    mismatched_content = "正文已经变化"
    rows = [
        {
            "record_id": 1,
            "content": valid_content,
            "content_hash": hashlib.sha256(valid_content.encode()).hexdigest(),
            "stored_dimension": 3,
            "actual_dimension": 3,
        },
        {
            "record_id": 2,
            "content": mismatched_content,
            "content_hash": hashlib.sha256("旧正文".encode()).hexdigest(),
            "stored_dimension": 3,
            "actual_dimension": 3,
        },
        {
            "record_id": 3,
            "content": "隐私优先降级后没有向量",
            "content_hash": None,
            "stored_dimension": None,
            "actual_dimension": None,
        },
        {
            "record_id": 4,
            "content": "维度元数据损坏",
            "content_hash": hashlib.sha256("维度元数据损坏".encode()).hexdigest(),
            "stored_dimension": 4,
            "actual_dimension": 3,
        },
    ]

    result = audit_rows(rows, sample_limit=10)

    assert result.scanned == 4
    assert result.hash_mismatches == 1
    assert result.hash_mismatch_ids == [2]
    assert result.dimension_mismatches == 1
    assert result.dimension_mismatch_ids == [4]
    assert result.unindexed == 1
    assert result.unindexed_ids == [3]
    assert result.inconsistent == 2
    assert mismatched_content not in repr(result)


def test_audit_rows_caps_each_id_sample() -> None:
    rows = [
        {
            "record_id": record_id,
            "content": f"正文{record_id}",
            "content_hash": "错误哈希",
            "stored_dimension": 3,
            "actual_dimension": 3,
        }
        for record_id in range(10)
    ]

    result = audit_rows(rows, sample_limit=2)

    assert result.hash_mismatches == 10
    assert result.hash_mismatch_ids == [0, 1]
