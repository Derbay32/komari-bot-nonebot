"""TSK-232 —— 旧机制物理删除的生成式守卫（结构面红基线）。

覆盖工单 D/E 区：permission_manager 插件、十六个旧 typed 字段、
legacy JSONB 名单键、旧 validators/defaults/管理 metadata、komari_search
兼容参数、活动 env/docs/tests 入口全部物理删除；retired artifacts 清单
逐项断言不存在；docs/ 仅允许在登记的豁免文件内保留历史提及。

当前实现（重排前）这些产物仍全部存在，因此本文件绝大多数用例处于红态；
这是"物理删除"的正确红基线：当前存在即红。实现代理的唯一合格路径是在
生产代码真正删除，不得改用例/放宽断言充数。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KOMARI_BOT = PROJECT_ROOT / "komari_bot"
TESTS = PROJECT_ROOT / "tests"
DOCS = PROJECT_ROOT / "docs"

#: 参与批量字段删除的八个 config_schema.py（与侦察结果一一对应）。
EIGHT_WHITELISTED_SCHEMAS = (
    "group_history_summary",
    "komari_custom",
    "komari_decision",
    "komari_help",
    "komari_knowledge",
    "komari_memory",
    "komari_search",
    "sr",
)

#: 三处引用旧 permission 插件（删除后不得再 require/import）的业务插件。
PERMISSION_REFERENCING_PLUGINS = ("komari_chat", "group_history_summary", "komari_custom")

RETIRED_PATHS: tuple[Path, ...] = (KOMARI_BOT / "plugins" / "permission_manager",)
RETIRED_TABLES = ("komari_plugin_configs",)
RETIRED_FIELDS = ("user_whitelist", "group_whitelist")
RETIRED_SEARCH_NAMES = ("_METADATA_FIELDS",)

#: 本文件及其兄弟基线文件允许在测试源码内保留旧标识（仅文档性提及，
#: 用于把"当前存在即红"表达清楚，绝不 import 旧插件）。
TEST_ALLOWLIST = {
    Path("db/test_tsk232_physical_cleanup.py"),
    Path("db/test_tsk232_chain_structure.py"),
}


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def test_permission_plugin_entirely_removed() -> None:
    """旧 permission 插件目录与生产/活动测试源码全部移除。"""
    for root in (KOMARI_BOT, TESTS):
        for file in _iter_py_files(root):
            if file.is_relative_to(TESTS) and file.relative_to(TESTS) in TEST_ALLOWLIST:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            if "permission_manager" in text or "check_runtime_permission" in text:
                raise AssertionError(f"旧 permission 引用残留: {file}")  # noqa: TRY003
    assets = list((KOMARI_BOT / "plugins").rglob("*permission_manager*"))
    assert assets == [], f"插件目录仍存在 permission_manager 文件: {assets}"


def test_retired_artifacts_paths_do_not_exist() -> None:
    """登记进 RETIRED_PATHS 的文件/目录必须物理不存在。"""
    for asset in RETIRED_PATHS:
        assert not asset.exists(), f"retired 路径仍存在: {asset}"


def test_eight_config_schema_removed_whitelist_fields() -> None:
    """八个 config_schema.py 不得再有 user_whitelist / group_whitelist 字段。"""
    pattern = re.compile(
        r"^\s*(?:" + "|".join(re.escape(f) for f in RETIRED_FIELDS) + r")\s*[:=]",
        re.IGNORECASE | re.MULTILINE,
    )
    for plugin in EIGHT_WHITELISTED_SCHEMAS:
        path = KOMARI_BOT / "plugins" / plugin / "config_schema.py"
        if not path.exists():
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), (
            f"{plugin}/config_schema.py 遗留旧名单字段"
        )


def test_no_whitelist_field_anywhere_in_production_config() -> None:
    """生产 plugin 目录整体不得再出现 user_whitelist / group_whitelist。"""
    for file in _iter_py_files(KOMARI_BOT / "plugins"):
        text = file.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(r"\buser_whitelist\b|\bgroup_whitelist\b", text), (
            f"插件生产代码遗留名单字段: {file}"
        )


def test_komari_search_compat_layer_removed() -> None:
    """komari_search/api.py 不得再有兼容剔除层/caller 兼容参数。"""
    text = (KOMARI_BOT / "plugins" / "komari_search" / "api.py").read_text("utf-8")
    for name in RETIRED_SEARCH_NAMES:
        assert name not in text, f"komari_search/api.py 遗留 {name}"


def test_startup_cleanup_no_longer_refers_legacy_table() -> None:
    """management/startup_cleanup 不得再读写旧 JSONB 配置表。"""
    rows = list(
        (KOMARI_BOT / "plugins" / "komari_management").rglob("startup_cleanup.py")
    )
    if rows:
        text = rows[0].read_text(encoding="utf-8")
        for table in RETIRED_TABLES:
            assert table not in text, f"startup_cleanup.py 遗留 {table}"


def test_three_plugins_no_old_permission_runtime_call() -> None:
    """三业务插件改自持 plugin_enable + 统一准入，不再调用 check_runtime_permission。"""
    for plugin in PERMISSION_REFERENCING_PLUGINS:
        init = KOMARI_BOT / "plugins" / plugin / "__init__.py"
        if not init.exists():
            continue
        text = init.read_text(encoding="utf-8")
        assert "check_runtime_permission" not in text, (
            f"{plugin}/__init__.py 仍调用 check_runtime_permission"
        )
        assert "permission_manager" not in text, (
            f"{plugin}/__init__.py 仍引用 permission_manager"
        )
        assert re.search(r"plugin_enable\s*[:=]", text) or re.search(
            r"require\(['\"]group_admission['\"]\)", text
        ), f"{plugin}/__init__.py 未自持开关/未接入统一准入"


def test_legacy_plugin_configs_table_not_in_migration_chain() -> None:
    """新迁移链不得再为 komari_plugin_configs 建表。"""
    for file in sorted((PROJECT_ROOT / "migrations" / "versions").glob("*.py")):
        text = file.read_text(encoding="utf-8")
        assert "CREATE TABLE komari_plugin_configs" not in text, (
            f"{file.name} 重建 legacy 表 komari_plugin_configs"
        )


def test_docs_mention_residual_within_allowlist() -> None:
    """docs/ 对旧产物的历史提及仅限登记的豁免文件。"""
    allowed_docs = {
        Path("adr/0012-unified-group-admission.md"),
    }
    for file in sorted(DOCS.rglob("*.md")):
        text = file.read_text(encoding="utf-8", errors="ignore")
        if not (
            "permission_manager" in text
            or "check_runtime_permission" in text
            or "group_whitelist" in text
            or "user_whitelist" in text
        ):
            continue
        rel = file.relative_to(DOCS)
        assert rel in allowed_docs, (
            f"活动文档 {file} 提及旧产物但未登记豁免（追加到 allowed_docs）"
        )
