"""仍受支持的手工 SQL 默认值一致性测试。"""

from __future__ import annotations

from pathlib import Path

from komari_bot.plugins.embedding_provider.config_schema import DynamicConfigSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_knowledge_manual_sql_matches_embedding_provider_default_dimension() -> None:
    default_dimension = DynamicConfigSchema().embedding_dimension
    knowledge_sql = (
        PROJECT_ROOT / "komari_bot/plugins/komari_knowledge/init_db.sql"
    ).read_text(encoding="utf-8")

    expected_line = f"\\set embedding_dimension {default_dimension}"
    assert expected_line in knowledge_sql
