"""TSK-232 轮 B —— ``audit`` 只读审计聚合投影红基线（门控 DB）。

锁定 audit 命令的聚合投影契约（无动态身份、只有 closed code 与聚合 count）：

- policy-file 校验投影：``policy.valid`` / ``policy.fingerprint``；非法文件
  报 POLICY_FILE_INVALID 且不回显输入值；
- legacy_resources：八资源闭集逐项投影 present/entry_count/
  valid_entry_count/fingerprint（读旧 ``komari_plugin_configs``，表不存在
  时全部 absent）；名单本身绝不出现；
- reply_outbox：按 delivery state 聚合计数（键名闭集），探测存在表；
- proposals 按 status 聚合、announcements processing_count、memory_jobs
  incomplete_count；
- audit 为只读命令：不传 --redis-url 也必须可用。

红基线纪律：全部断言驱动 ``komari_bot.cutover.cli.main(argv)`` 真实入口；
目标模块未实现时以 ModuleNotFoundError 红。隔离库 = 门控库名 +
``_tsk232b1audit``，用例结束 DROP。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tests.cutover.support import (
    CANARY_BODY_TOKEN,
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    LEGACY_RESOURCES,
    OUTBOX_COUNT_KEYS,
    SKIP_NO_POSTGRES,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    collect_key_values,
    collect_resource_entries,
    cutover_scratch_database,
    find_key,
    oracle_list_fingerprint,
    run_cli,
    seed_announcements,
    seed_legacy_configs,
    seed_memory_jobs,
    seed_proposals,
    seed_reply_fulfillments,
    write_policy_file,
)

if TYPE_CHECKING:
    import pytest

pytestmark = [SKIP_NO_POSTGRES]


def _audit_argv(database_url: str, policy_file: str) -> list[str]:
    return ["audit", "--database-url", database_url, "--policy-file", policy_file]


def _resource_by_name(
    entries: list[dict[str, Any]],
    resource: str,
) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("resource") == resource:
            return entry
    return None


# ---------------------------------------------------------------------------
# 空 head 库：资源全部 absent + 零计数
# ---------------------------------------------------------------------------


def test_audit_on_empty_head_database_reports_absent_resources(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """空 head 库 audit：八资源全 absent、outbox/公告/memory job 全零。"""
    with cutover_scratch_database("audit") as (_params, database_url):
        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(capsys, *_audit_argv(database_url, policy_file))
        assert exit_code == 0
        assert payload is not None
        assert payload["command"] == "audit"
        assert payload["status"] == "ok"

        # 契约锁定 policy 投影为顶层键：{"valid": bool, "fingerprint": str|null}。
        policy_block = payload["policy"]
        assert policy_block["valid"] is True
        assert policy_block["fingerprint"] == VALID_POLICY_FINGERPRINT

        entries = collect_resource_entries(payload)
        reported = {entry["resource"] for entry in entries}
        assert reported == set(LEGACY_RESOURCES), "legacy 资源必须是八资源闭集"
        for entry in entries:
            assert entry["present"] is False
            assert entry["entry_count"] == 0
            assert entry["valid_entry_count"] == 0

        for key in OUTBOX_COUNT_KEYS:
            assert find_key(payload, key) == 0
        assert find_key(payload, "processing_count") == 0
        assert find_key(payload, "incomplete_count") == 0


def test_audit_rejects_invalid_policy_file_without_echo(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """非法 policy 文件 → POLICY_FILE_INVALID 错误面且不回显输入内容。"""
    with cutover_scratch_database("audit") as (_params, database_url):
        leak_marker = "canary-invalid-policy-marker-audit"
        invalid_payload = {
            "mode": "blacklist",
            "group_ids": [leak_marker],
            "unexpected": True,
        }
        policy_file = tmp_path / "invalid.json"
        policy_file.write_text(json.dumps(invalid_payload), encoding="utf-8")

        exit_code, payload = run_cli(
            capsys,
            *_audit_argv(database_url, str(policy_file)),
        )
        assert exit_code != 0
        assert payload is not None
        assert payload["status"] == "error"
        assert payload["error_code"] == "POLICY_FILE_INVALID"
        assert leak_marker not in json.dumps(payload)


# ---------------------------------------------------------------------------
# 模拟旧库：手工建 komari_plugin_configs canary 夹具
# ---------------------------------------------------------------------------


def _seed_mixed_validity_legacy_configs(params: dict[str, Any]) -> None:
    """三个资源三种形态：合法+非法混合、纯非法、空名单；其余五资源缺席。"""
    seed_legacy_configs(
        params,
        [
            (
                "komari_knowledge",
                {
                    "group_whitelist": [
                        CANARY_GROUP_B,
                        CANARY_GROUP_A,
                        -777001,
                        "not-a-group-id",
                    ],
                    "user_whitelist": [
                        "canary-user-alpha-01",
                        "canary-user-beta-02",
                        "canary-user-gamma-03",
                    ],
                    "note": CANARY_BODY_TOKEN,
                },
            ),
            (
                "sr",
                {
                    "group_whitelist": ["junk-entry"],
                    "user_whitelist": [],
                },
            ),
            (
                "komari_memory",
                {"group_whitelist": [], "user_whitelist": []},
            ),
        ],
    )


def test_audit_projects_aggregate_counts_and_fingerprints_only(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """旧库 audit：只报聚合 count 与 canonical fingerprint，不输出名单。"""
    with cutover_scratch_database("audit") as (params, database_url):
        _seed_mixed_validity_legacy_configs(params)
        seed_reply_fulfillments(params)
        seed_proposals(params)
        seed_announcements(params)
        seed_memory_jobs(params)

        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(capsys, *_audit_argv(database_url, policy_file))
        assert exit_code == 0
        assert payload is not None

        entries = collect_resource_entries(payload)
        knowledge = _resource_by_name(entries, "komari_knowledge")
        sr_entry = _resource_by_name(entries, "sr")
        memory = _resource_by_name(entries, "komari_memory")
        search = _resource_by_name(entries, "komari_search")

        # 合法+非法混合：条目数 4、合法 2、指纹为合法子集 canonical 形态。
        assert knowledge is not None and knowledge["present"] is True
        assert knowledge["entry_count"] == 4
        assert knowledge["valid_entry_count"] == 2
        assert knowledge["fingerprint"] == oracle_list_fingerprint(
            [CANARY_GROUP_A, CANARY_GROUP_B]
        )
        # 纯非法名单：合法数为 0，无 canonical 指纹可报。
        assert sr_entry is not None and sr_entry["present"] is True
        assert sr_entry["entry_count"] == 1
        assert sr_entry["valid_entry_count"] == 0
        assert sr_entry["fingerprint"] is None
        # 空名单：present 但零条目。
        assert memory is not None and memory["present"] is True
        assert memory["entry_count"] == 0
        assert memory["valid_entry_count"] == 0
        # 缺席资源：absent 且零计数。
        assert search is not None and search["present"] is False
        assert search["entry_count"] == 0
        assert search["valid_entry_count"] == 0

        # user_whitelist 只允许聚合 count 投影（夹具仅在 komari_knowledge 放置
        # 三个成员，per-resource 与全局聚合两种实现口径下每个投影值均为 3）。
        user_counts = collect_key_values(payload, "user_entry_count")
        assert user_counts, "audit 必须报告 user_whitelist 聚合条目数"
        assert all(count == 3 for count in user_counts), (
            f"user_entry_count 聚合值必须为 3: {user_counts}"
        )

        # outbox 聚合：与播种的 delivery state 行数一致。
        expected_outbox = {
            "not_started_count": 2,
            "pending_confirmation_count": 1,
            "delivered_count": 3,
            "not_delivered_count": 1,
        }
        for key, expected in expected_outbox.items():
            assert find_key(payload, key) == expected

        # proposals 按 status 聚合（votingx2、approvedx1）。
        assert find_key(payload, "voting") == 2
        assert find_key(payload, "approved") == 1
        assert find_key(payload, "processing_count") == 2
        assert find_key(payload, "incomplete_count") == 1


def test_audit_stdout_never_contains_roster_members(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """名单成员（群号/用户标记/正文标记）绝不原样出现在 stdout 序列化文本中。"""
    with cutover_scratch_database("audit") as (params, database_url):
        _seed_mixed_validity_legacy_configs(params)
        seed_reply_fulfillments(params)

        policy_file = write_policy_file(tmp_path, VALID_POLICY)
        exit_code, payload = run_cli(capsys, *_audit_argv(database_url, policy_file))
        assert exit_code == 0
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            str(CANARY_GROUP_A),
            str(CANARY_GROUP_B),
            "-777001",
            "not-a-group-id",
            "canary-user-alpha-01",
            CANARY_BODY_TOKEN,
        ):
            assert forbidden not in serialized, f"泄漏探针 {forbidden[:12]}… 出现"
