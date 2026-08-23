"""TSK-232 轮 B —— cutover CLI 输出面 canary 泄漏递归检查（门控 DB/Redis）。

锁定输出安全契约：CLI 所有输出（stdout JSON 与 stderr）只允许出现 closed
code 与聚合 count，绝不出现动态身份——群号、用户号、名单成员、正文、URL、
异常文本与类型。

方法：把明显非敏感的合成 canary（群号/用户号/正文标记/标准 URL/base64/
Bearer 探针）注入旧 ``komari_plugin_configs`` 夹具、reply outbox 行、提案
标题/正文、Redis 测试 namespace，然后驱动 audit/status 与各失败路径，对
stdout 原始文本做子串扫描 + 对解析后的 JSON 结构做递归等值扫描（复用
TSK-223 的 ``SensitiveCanaryBundle`` 扫描器）。

operator 自备的 backup_checkpoint 属于命令回显面而非动态身份，不在禁止集。
红基线纪律：目标模块未实现时以 ModuleNotFoundError 红；隔离库 = 门控库名
+ ``_tsk232b1safety``，用例结束 DROP；Redis 仅用 ``test:cutover:`` 前缀并
teardown 清理。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tests.cutover.support import (
    CANARY_BODY_TOKEN,
    CANARY_GROUP_A,
    CANARY_GROUP_B,
    CANARY_GROUP_C,
    CANARY_USER_ID,
    CANARY_USER_TOKEN_A,
    DEFAULT_SEEDED_POLICY_FINGERPRINT,
    REDIS_URL,
    SKIP_NO_POSTGRES,
    SKIP_NO_REDIS,
    VALID_POLICY,
    VALID_POLICY_FINGERPRINT,
    cutover_scratch_database,
    delete_gate_rows,
    redis_test_namespace,
    run_cli_raw,
    seed_announcements,
    seed_legacy_configs,
    seed_memory_jobs,
    seed_proposals,
    seed_reply_fulfillments,
    stage_gate_phase,
)
from tests.group_admission.sensitive_canary import (
    SensitiveCanaryBundle,
    SensitiveCanaryToken,
    build_standard_canary_bundle,
)

if TYPE_CHECKING:
    import pytest

pytestmark = [SKIP_NO_POSTGRES]


def _cutover_canary_bundle() -> SensitiveCanaryBundle:
    """cutover 输出面临时探针集：身份/名单/正文 + 标准泄漏探针。"""
    return SensitiveCanaryBundle(
        (
            *build_standard_canary_bundle().tokens,
            SensitiveCanaryToken(label="group-a", value=CANARY_GROUP_A),
            SensitiveCanaryToken(label="group-b", value=CANARY_GROUP_B),
            SensitiveCanaryToken(label="group-c", value=CANARY_GROUP_C),
            SensitiveCanaryToken(label="user-id", value=CANARY_USER_ID),
            SensitiveCanaryToken(label="user-token", value=CANARY_USER_TOKEN_A),
            SensitiveCanaryToken(label="body-token", value=CANARY_BODY_TOKEN),
        )
    )


def _assert_output_clean(
    stdout_text: str,
    payload: Any,
    bundle: SensitiveCanaryBundle,
    *,
    context: str,
) -> None:
    """原始文本子串扫描 + 解析结构递归扫描双通道断言无泄漏。"""
    for token in bundle.tokens:
        literal = token.value if isinstance(token.value, str) else str(token.value)
        assert literal not in stdout_text, f"{context}: stdout 泄漏探针 [{token.label}]"
    leaks = bundle.leak_report(payload)
    assert leaks == [], f"{context}: 结构化扫描检测到泄漏: {leaks}"


def _seed_canary_legacy_fixture(params: dict[str, Any]) -> None:
    """把全部探针塞进旧宽表夹具：名单成员、用户标记、URL/base64/Bearer 正文。"""
    seed_legacy_configs(
        params,
        [
            (
                "komari_knowledge",
                {
                    "group_whitelist": [CANARY_GROUP_A, CANARY_GROUP_B],
                    "user_whitelist": [CANARY_USER_TOKEN_A],
                    "webhook": "https://canary.example/exfil?key=9f3a7c",
                    "blob": "Q0FOQVJZLUI2NC1TRUNSRVQtOTZhMQ==",
                    "auth": "Bearer canary-token-0123456789abcdef",
                },
            ),
            (
                "sr",
                {"group_whitelist": [CANARY_GROUP_C]},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 只读命令输出面
# ---------------------------------------------------------------------------


def test_audit_and_status_stdout_are_canary_clean(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """audit/status 在满载 canary 旧库夹具上输出只含聚合投影。"""
    bundle = _cutover_canary_bundle()
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(VALID_POLICY), encoding="utf-8")

    with cutover_scratch_database("safety") as (params, database_url):
        _seed_canary_legacy_fixture(params)
        seed_reply_fulfillments(params)
        seed_proposals(params)
        seed_announcements(params)
        seed_memory_jobs(params)

        for command in ("audit", "status"):
            exit_code, stdout, stderr = run_cli_raw(
                capsys,
                command,
                "--database-url",
                database_url,
                "--policy-file",
                str(policy_file),
            )
            assert exit_code == 0
            payload = json.loads(stdout.strip())
            _assert_output_clean(stdout, payload, bundle, context=command)
            assert CANARY_BODY_TOKEN not in stderr, f"{command}: stderr 泄漏"


@SKIP_NO_REDIS
def test_capture_evidence_digest_output_is_canary_clean(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """capture-evidence 成功输出只有聚合 digest/checkpoint，不携带扫描内容。"""
    bundle = _cutover_canary_bundle()
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(VALID_POLICY), encoding="utf-8")

    with cutover_scratch_database("safety") as (_params, database_url):
        _seed_canary_legacy_fixture(_params)
        with redis_test_namespace(
            {
                "interaction-canary": f"body:{CANARY_BODY_TOKEN}",
                "proactive-canary": str(CANARY_USER_ID),
            }
        ):
            prepare_code, _prepare_out, _ = run_cli_raw(
                capsys,
                "prepare-policy",
                "--database-url",
                database_url,
                "--policy-file",
                str(policy_file),
                "--expected-fingerprint",
                VALID_POLICY_FINGERPRINT,
                "--apply",
            )
            assert prepare_code == 0

            exit_code, stdout, stderr = run_cli_raw(
                capsys,
                "capture-evidence",
                "--database-url",
                database_url,
                "--redis-url",
                REDIS_URL,
                "--expected-fingerprint",
                VALID_POLICY_FINGERPRINT,
                "--backup-checkpoint",
                "ckpt-canary-20260823-opaque",
                "--apply",
            )
            assert exit_code == 0
            payload = json.loads(stdout.strip())
            _assert_output_clean(stdout, payload, bundle, context="capture-evidence")
            assert str(CANARY_USER_ID) not in stderr


# ---------------------------------------------------------------------------
# 失败路径输出面
# ---------------------------------------------------------------------------


def test_failure_paths_never_leak_canaries(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    """各失败路径的 stdout/stderr 只含 closed code 与聚合 count。"""
    bundle = _cutover_canary_bundle()
    leak_policy_file = tmp_path / "leak.json"
    leak_policy_file.write_text(
        json.dumps({"mode": "blacklist", "group_ids": ["canary-leak-marker-x"]}),
        encoding="utf-8",
    )
    valid_policy_file = tmp_path / "valid.json"
    valid_policy_file.write_text(json.dumps(VALID_POLICY), encoding="utf-8")
    wrong_fingerprint = "0" * 64

    with cutover_scratch_database("safety") as (params, database_url):
        _seed_canary_legacy_fixture(params)
        stage_gate_phase(
            params,
            phase="REDIS_EVIDENCE_CAPTURED",
            policy_revision=4,
            policy_fingerprint=DEFAULT_SEEDED_POLICY_FINGERPRINT,
            backup_checkpoint="ckpt-canary-20260823-opaque",
            redis_evidence_digest="d" * 64,
        )

        scenarios: list[tuple[str, list[str]]] = [
            # audit 非法 policy 文件：错误面不得回显文件内容探针。
            (
                "audit-invalid-policy",
                [
                    "audit",
                    "--database-url",
                    database_url,
                    "--policy-file",
                    str(leak_policy_file),
                ],
            ),
            # prepare 指纹不符（合法文件 + 错误期望指纹）。
            (
                "prepare-mismatch",
                [
                    "prepare-policy",
                    "--database-url",
                    database_url,
                    "--policy-file",
                    str(valid_policy_file),
                    "--expected-fingerprint",
                    wrong_fingerprint,
                    "--apply",
                ],
            ),
            # capture 相位已越过 → PHASE_OUT_OF_ORDER。
            (
                "capture-out-of-order",
                [
                    "capture-evidence",
                    "--database-url",
                    database_url,
                    "--redis-url",
                    REDIS_URL or "unused://no-redis",
                    "--expected-fingerprint",
                    DEFAULT_SEEDED_POLICY_FINGERPRINT,
                    "--backup-checkpoint",
                    "ckpt-canary-20260823-opaque",
                    "--apply",
                ],
            ),
            # finalize 相位前置不满足。
            (
                "finalize-out-of-order",
                [
                    "finalize-redis",
                    "--database-url",
                    database_url,
                    "--expected-fingerprint",
                    DEFAULT_SEEDED_POLICY_FINGERPRINT,
                    "--apply",
                ],
            ),
            # abort 更早相位 → ABORT_FORBIDDEN。
            (
                "abort-forbidden",
                [
                    "abort-pre-backfill",
                    "--database-url",
                    database_url,
                    "--expected-fingerprint",
                    DEFAULT_SEEDED_POLICY_FINGERPRINT,
                    "--apply",
                ],
            ),
        ]

        invalid_policy_stdout = ""
        for label, argv in scenarios:
            exit_code, stdout, stderr = run_cli_raw(capsys, *argv)
            assert exit_code != 0, f"{label}: 预期失败路径意外成功"
            payload = json.loads(stdout.strip()) if stdout.strip() else {}
            _assert_output_clean(stdout, payload, bundle, context=label)
            for token in bundle.tokens:
                literal = (
                    token.value if isinstance(token.value, str) else str(token.value)
                )
                assert literal not in stderr, (
                    f"{label}: stderr 泄漏探针 [{token.label}]"
                )
            if label == "audit-invalid-policy":
                invalid_policy_stdout = stdout
        # POLICY_FILE_INVALID 错误面绝不回显输入文件内容探针。
        assert "canary-leak-marker-x" not in invalid_policy_stdout


def test_status_gate_missing_error_is_canary_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GATE_MISSING 错误面同样不得携带任何夹具身份。"""
    bundle = _cutover_canary_bundle()
    with cutover_scratch_database("safety") as (params, database_url):
        _seed_canary_legacy_fixture(params)
        delete_gate_rows(params)

        exit_code, stdout, stderr = run_cli_raw(
            capsys,
            "status",
            "--database-url",
            database_url,
        )
        assert exit_code != 0
        payload = json.loads(stdout.strip()) if stdout.strip() else {}
        _assert_output_clean(stdout, payload, bundle, context="status-gate-missing")
        assert "canary.example" not in stderr
