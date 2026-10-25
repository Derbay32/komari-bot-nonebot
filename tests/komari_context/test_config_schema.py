"""TSK-192 phase A: the resource schemas have one owner each."""

from __future__ import annotations

from komari_bot.plugins.komari_context.config_schema import (
    KomariContextConfigSchema,
)
from komari_bot.plugins.komari_memory.config_schema import (
    KomariMemoryConfigSchema,
)


def test_context_schema_owns_redis_db_with_rebuild_metadata() -> None:
    assert KomariContextConfigSchema.plugin_name == "komari_context"
    assert KomariContextConfigSchema.__tablename__ == "komari_context_config"
    assert set(KomariContextConfigSchema.model_fields) == {
        "id",
        "revision",
        "updated_at",
        "redis_db",
    }
    config = KomariContextConfigSchema()
    assert config.redis_db == 1
    field = KomariContextConfigSchema.model_fields["redis_db"]
    assert field.json_schema_extra == {"apply_mode": "rebuild"}
    assert KomariContextConfigSchema.model_config.get("json_schema_extra") == {
        "default_apply_mode": "immediate"
    }


def test_memory_schema_no_longer_owns_redis_db() -> None:
    assert "redis_db" not in KomariMemoryConfigSchema.model_fields
    assert "redis_db" not in KomariMemoryConfigSchema().model_dump()
