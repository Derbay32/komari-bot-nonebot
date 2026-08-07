"""Memory Schema 生成脚本与旧 SQL 退役测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/render_memory_schema.py"
LEGACY_SQL_PATH = (
    PROJECT_ROOT
    / "komari_bot/plugins/komari_memory/database/init_orm.sql"
)
README_PATH = PROJECT_ROOT / "komari_bot/plugins/komari_memory/README.md"


def _load_script_module() -> Any:
    module_name = "tests.scripts.render_memory_schema_script"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_memory_schema_comes_from_runtime_ddl() -> None:
    module = _load_script_module()

    sql = module.build_memory_schema_sql(1536)

    assert "唯一 Schema 真源" in sql
    assert "embedding VECTOR(1536) NOT NULL" in sql
    assert "komari_memory_conversation_embeddings" in sql
    assert "komari_memory_interaction_embeddings" in sql
    assert "records JSONB" not in sql
    assert "table_schema = current_schema()" in sql


def test_legacy_sql_fails_closed_and_readme_only_recommends_generator() -> None:
    legacy_sql = LEGACY_SQL_PATH.read_text(encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")

    assert "\\quit" in legacy_sql
    assert "CREATE TABLE" not in legacy_sql
    assert "render_memory_schema.py" in readme
    assert "不要执行 `database/init_orm.sql`" in readme
