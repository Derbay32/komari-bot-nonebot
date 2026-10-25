"""TSK-192 phase A: the resource schemas have one owner each."""

from __future__ import annotations

import pytest

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
        "bot_nickname",
        "error_notify_enabled",
        "global_interaction_enabled",
        "global_interaction_trigger_size",
    }
    config = KomariContextConfigSchema()
    assert config.redis_db == 1
    assert config.bot_nickname == "小鞠知花"
    assert config.error_notify_enabled is True
    assert config.global_interaction_enabled is True
    assert config.global_interaction_trigger_size == 20
    field = KomariContextConfigSchema.model_fields["redis_db"]
    assert field.json_schema_extra == {"apply_mode": "rebuild"}
    notify_field = KomariContextConfigSchema.model_fields["error_notify_enabled"]
    assert notify_field.json_schema_extra == {"apply_mode": "immediate"}
    assert KomariContextConfigSchema.model_config.get("json_schema_extra") == {
        "default_apply_mode": "immediate"
    }


def test_memory_schema_no_longer_owns_redis_db() -> None:
    assert "redis_db" not in KomariMemoryConfigSchema.model_fields
    assert "redis_db" not in KomariMemoryConfigSchema().model_dump()


def test_context_schema_owns_shared_conversation_policy_fields() -> None:
    """TSK-192 phase B: shared chatter policy has one context owner."""
    fields = KomariContextConfigSchema.model_fields
    assert {
        "bot_nickname",
        "error_notify_enabled",
        "global_interaction_enabled",
        "global_interaction_trigger_size",
    } <= set(fields)
    with pytest.raises(ValueError):
        KomariContextConfigSchema(global_interaction_trigger_size=4)
    with pytest.raises(ValueError):
        KomariContextConfigSchema(global_interaction_trigger_size=201)
