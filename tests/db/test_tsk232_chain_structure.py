"""TSK-232 —— 迁移链重排的结构面静态守卫（无需真实数据库）。

本轮（结构面）红基线刷新 ``migrations/versions`` 版本链的验收契约。目标链
（单主干、head=0017）按 ADR-0012 「破坏性迁移后果」重排为：

- ``0001`` ``0001_fresh_marker``　fresh marker，不再是一次性全量基线
  （仅空库身份，不含任何 legacy 大表/扩展）；
- ``0002`` ``0002_typed_schema``　强类型配置表 + Prompt 表（原 0002/0003
  合并）；
- ``0003`` ``0003_komari_chat_config``　聊天单行配置表（原 0004）；
- ``0004`` ``0004_komari_decision_summary_config``　群总结归类列（原 0005）；
- ``0005-0008``　回复履约父子/送达/生命周期/告警四步（原 0006-0009）；
- ``0009`` ``0009_komari_chat_prompt_behavior``　聊天 Prompt 行为列
  （原 0012）；
- ``0010`` ``0010_group_admission_config``　admission expand（ADR 锚点 1）：
  建 ``komari_group_admission_config`` 强类型单行表；
- ``0011`` ``0011_reply_fulfillment_mirror``　回复履约镜像（ADR 锚点 2，
  即原 0010 停机回填重构）；
- ``0012`` ``0012_group_admission_backfill``　admission backfill
  forward-only barrier（ADR 锚点 3，标注 IRREVERSIBLE）；
- ``0013`` ``0013_group_admission_cutover``　coordinated contract（ADR
  锚点 4：单事务校验 policy/backfill/Redis → 删旧 outbox/改名/清 legacy
  名单，标注 IRREVERSIBLE；即原 0011 重构）；
- ``0014`` ``0014_agent_budget_and_tool_mode``　Agent 预算 + 工具模式
  （原 0013+0014 合并）；
- ``0015`` ``0015_image_understanding_mode``　图片理解模式（原 0015）；
- ``0016`` ``0016_custom_proposal_vote_epoch``　提案投票纪元（原 0016）；
- ``0017`` ``0017_custom_proposal_dormancy_rotation``　提案休眠轮换
  （原 0017，head）。

编号映射理由（每行附于映射常量）：先按 ADR 把四个准入/履约锚点固定到
0010-0013，再向上/向下保持原内容相对次序折叠填充，头保持 0017；禁止复用
旧 revision 字符串、禁止 branch/merge revision，故链内无 alias，也不为
数字命名做兼容别名。本文件只解析版本目录与迁移源文本，不要求真实数据库。
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"


#: TSK-232 重新编号链路。每项: (revision, 文件名必须包含的子串, 关键源文本子串)。
#: 实现代理必须按此编号创建迁移文件并包含对应关键文本；理由列在注释。
NEW_CHAIN = (
    # 0001 不再随手一次性全量基线（ADR「定期执行基线 0001 fresh marker」：
    # 只有空库自动初始化缺省 '' 即 default 全部获准；另见门禁用例）
    ("0001", "fresh_marker", None),
    # 0002 强类型配置表 + Prompt 表（原 0002/0003 合并，简化链条）
    ("0002", "typed_schema", "komari_prompt_komari_chat"),
    # 0003 骨架：komari_chat_config 单行表（原 0004）
    ("0003", "komari_chat_config", "CREATE TABLE komari_chat_config"),
    # 0004 群总结归类列（原 0005）
    ("0004", "decision_summary", "summary_embedding_instruction_query"),
    # 0005 回复履约父子表（原 0006）
    ("0005", "reply_fulfillment_parent_child", "KOMARI_CHAT_REPLY_FULFILLMENTS"),
    # 0006 回复送达恢复（原 0007）
    ("0006", "reply_delivery_recovery", "ALTER TABLE komari_chat_reply_commit_outbox"),
    # 0007 回复生命周期（原 0008）
    ("0007", "reply_fulfillment_lifecycle", "REPLY_CONTENT DROP NOT NULL"),
    # 0008 回复告警（原 0009）
    ("0008", "reply_fulfillment_alert", "PENDING_CONFIRMATION_ALERTED_AT"),
    # 0009 聊天 Prompt 行为列（原 0012；output_instruction 移除）
    ("0009", "chat_prompt_behavior", "output_instruction"),
    # 0010 admission expand（锚点①，ADR-0012）建强类型单行准入表
    ("0010", "group_admission_expand", "komari_group_admission_config"),
    # 0011 回复履约镜像（锚点②：原 0010 停机回填重构为镜像）
    ("0011", "reply_fulfillment_mirror", "FROM komari_chat_reply_commit_outbox"),
    # 0012 admission backfill（锚点③：forward-only barrier，IRREVERSIBLE）
    ("0012", "group_admission_backfill", None),
    # 0013 coordinated contract（锚点④：单事务校验后删旧/改名/清 legacy）
    ("0013", "group_admission_cutover", "komari_chat_reply_commit_outbox"),
    # 0014 Agent 预算 + 调用模式列（原 0013+0014 合并）
    ("0014", "agent_budget_and_tool_call", "agent_max_rounds"),
    # 0015 图片理解模式（原 0015）
    ("0015", "image_understanding_mode", "image_understanding_mode"),
    # 0016 提案投票纪元（原 0016）
    ("0016", "custom_proposal_vote_epoch", "vote_epoch"),
    # 0017 提案休眠轮换（原 0017，head）
    ("0017", "custom_proposal_dormancy_rotation", "dormancy"),
)

#: 标注 forward-only / IRREVERSIBLE 的 revision（实现源文本必须含该标记）。
FORWARD_ONLY_REVISIONS = ("0012", "0013")


def _load_script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    return ScriptDirectory.from_config(config)


def _migration_file(revision: str) -> Path:
    match = list(VERSIONS_DIR.glob(f"{revision}_*.py"))
    assert len(match) == 1, (
        f"新链 revision {revision} 的迁移文件必须唯一且以 {revision}_ 开头: {match}"
    )
    return match[0]


def test_chain_is_single_trunk_head_0017() -> None:
    """新链单主干、无 alias、head=0017。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    heads = script.get_heads()
    assert len(heads) == 1
    assert heads[0] == "0017"

    baselines = [rev for rev in revisions if rev.down_revision is None]
    assert len(baselines) == 1
    assert baselines[0].revision == "0001"
    # 不得保留旧号（不允许为兼容 alias/回退）
    legacy_files = [
        name
        for name in sorted(VERSIONS_DIR.glob("*.py"))
        if re.search(r"^(?:0002_typed_plugin_config|0010_reply_fulfillment|0011_reply_fulfillment_cutover)_", name.name)
    ]
    assert legacy_files == [], f"旧编号迁移文件必须被重排/移除，不得保留: {legacy_files}"


def test_fresh_marker_is_not_full_baseline() -> None:
    """0001 是 fresh marker：不得再创建 legacy 大表或向量扩展。"""
    source = _migration_file("0001").read_text(encoding="utf-8")
    for table in (
        "komari_plugin_configs",
        "komari_prompt_configs",
        "komari_user_bans",
        "komari_chat_reply_commit_outbox",
        "CREATE EXTENSION vector",
    ):
        assert table not in source, f"fresh marker 0001 不得再出现 {table}"


def test_semantic_anchors_present() -> None:
    """0010-0013 按 ADR-0012 语义锚点存在且链内两前序可追踪。"""
    script = _load_script_directory()
    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert set(FORWARD_ONLY_REVISIONS) <= set(revisions)
    assert revisions["0010"].down_revision == "0009"
    assert revisions["0011"].down_revision == "0010"
    assert revisions["0012"].down_revision == "0011"
    assert revisions["0013"].down_revision == "0012"

    # 0010 必须建别名准入表
    assert "komari_group_admission_config" in _migration_file("0010").read_text("utf-8")


def test_numbering_map_redistributes_original_content() -> None:
    """原 0002-0017 内容依映射归位到新 0002-0017，关键 SQL 文本特征逐项断言。"""
    for revision, _stem, marker in NEW_CHAIN:
        if marker is None:
            continue
        source = _migration_file(revision).read_text(encoding="utf-8")
        normalized = re.sub(r"\s+", " ", source).upper()
        assert marker.upper() in normalized, (
            f"新链 {revision} 必须含关键文本 {marker!r}"
        )


def test_forward_only_barrier_downgrades_are_irreversible() -> None:
    """0012/0013 的 downgrade() 真实执行必须抛 forward-only / IRREVERSIBLE。"""
    import importlib.util

    for revision in FORWARD_ONLY_REVISIONS:
        path = _migration_file(revision)
        spec = importlib.util.spec_from_file_location(f"mig_{revision}_guard", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.revision == revision
        marker = f"{revision.upper()}_TSK232_IS_IRREVERSIBLE"
        try:
            module.downgrade()
        except RuntimeError as exc:
            assert marker in str(exc), (
                f"{revision} downgrade 的 RuntimeError 必须含 forward-only 标记 {marker}"
            )
        else:
            raise AssertionError(f"{revision} downgrade 必须抛 RuntimeError")  # noqa: TRY003


def test_no_legacy_jsonb_list_loader_seam_in_chain() -> None:
    """迁移链不得出现旧 JSONB 名单合并/回退表明。

    仅最重要的两个文件包含任一旧名单读取视为整体违例的强特征（避免
    锁定实现细节）。ADR 禁止从八组旧名单做 union/intersection/优先级合并；
    若实现确有需要读取 released 迁移的 cutover 工具，须位于带
    ``legacy``/``cutover`` 命名且与上述 ANCHOR 在 0013 处收敛，不在此判断。
    """
    verdict_sources = {
        _migration_file(rev).read_text(encoding="utf-8")
        for rev in ("0010", "0012", "0013")
    }
    offending = []
    for index, source in enumerate(verdict_sources):
        lower = source.lower()
        if "user_whitelist" in lower and "group_whitelist" in lower and "merge" in lower:
            offending.append(index)
    assert offending == [], "迁移 0010/0012/0013 不得出现自动合并旧名单逻辑"
