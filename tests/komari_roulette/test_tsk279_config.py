# ruff: noqa: RUF003  # ｜ ＝ × 等定稿文案字符
"""TSK-279 Stage-A: typed config + frozen-copy validation (pure, no PG/Redis).

Green probes below exercise only *existing* canonical seams (``domain`` item
weights) so a red run cannot hide a broken fixture behind a missing-seam
import.  RED cases lazily load the proposed ``config_schema`` / ``copy_pool``
modules inside their own test; a missing module is reported as a missing seam,
never as a whole-file collection error.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from komari_bot.llm.content_budget import CONTENT_TEXT_BUDGET
from komari_bot.plugins.komari_roulette import RouletteCommandService
from komari_bot.plugins.komari_roulette.domain import (
    DEFAULT_ITEM_WEIGHTS,
    ITEM_TYPES,
    Action,
    ItemType,
)

from .support import (
    ScriptedRandomSource,
    assert_rejected,
    create_waiting,
    dispatch,
    join,
    player,
    start_active,
)
from .tsk279_support import (
    CONFIG_SCHEMA_MODULE,
    COPY_POOL_MODULE,
    load_symbol,
)

VALIDATION_ERRORS = (ValidationError, ValueError)
MAGNIFIER = ItemType.MAGNIFIER
BEER = ItemType.BEER
BURST = ItemType.BURST
LOCK = ItemType.LOCK


def _config_schema() -> Any:
    return load_symbol(CONFIG_SCHEMA_MODULE, "DynamicConfigSchema")


def _validate_template() -> Any:
    return load_symbol(COPY_POOL_MODULE, "validate_template")


def _copy_pool_symbol(name: str) -> Any:
    return load_symbol(COPY_POOL_MODULE, name)


# ---------------------------------------------------------------------------
# GREEN old-API probes: the domain already freezes item weights at start
# ---------------------------------------------------------------------------


def test_old_api_default_item_weights_are_each_one() -> None:
    assert set(DEFAULT_ITEM_WEIGHTS) == set(ITEM_TYPES)
    for item in ITEM_TYPES:
        assert DEFAULT_ITEM_WEIGHTS[item] == 1


def test_old_api_action_start_freezes_item_weights() -> None:
    weights = {MAGNIFIER: 2, BEER: 1, BURST: 1, LOCK: 1}
    state, _entropy = start_active(item_weights=weights)
    assert state.lifecycle == "active"
    assert dict(state.item_weights) == weights
    # ``Action.start`` snapshots the mapping: mutating the caller's dict after
    # the transition must not rewrite the frozen game.
    weights[LOCK] = 99
    assert state.item_weights[LOCK] == 1


def test_old_api_invalid_item_weights_are_rejected() -> None:
    bad_weights = (
        {MAGNIFIER: 1, BEER: 1, BURST: 1, LOCK: -1},
        {MAGNIFIER: 0, BEER: 0, BURST: 0, LOCK: 0},
        {MAGNIFIER: 1, BEER: 1, BURST: 1, LOCK: True},
    )
    for bad in bad_weights:
        state = create_waiting(host=1)
        joined = join(state, 2)
        assert joined.ok
        result = dispatch(
            joined.state,
            Action.start(player(1), item_weights=bad),
            random_source=ScriptedRandomSource(),
        )
        assert_rejected(result, "invalid_item_weights")


def test_old_api_weights_are_normalized_against_the_real_domain_contract() -> None:
    # The domain fills missing keys with 0 and rejects an all-zero sum; it does
    # not invent per-key upper bounds beyond non-negative integers.
    state, _entropy = start_active(item_weights={MAGNIFIER: 3})
    assert dict(state.item_weights) == {
        MAGNIFIER: 3,
        BEER: 0,
        BURST: 0,
        LOCK: 0,
    }


# ---------------------------------------------------------------------------
# RED S4: the service accepts a per-game item-weights provider
# ---------------------------------------------------------------------------


def test_service_ctor_accepts_item_weights_provider_seam() -> None:
    """The narrowest freeze seam: the service can be handed a weights provider.

    A strict construction here fails with ``TypeError`` (unknown keyword) while
    the real behaviour is verified against PostgreSQL in
    ``test_tsk279_configuration_pg.py``.
    """

    def provider() -> dict[ItemType, int]:
        return dict(DEFAULT_ITEM_WEIGHTS)

    service = RouletteCommandService(
        session_factory=lambda: None,
        reply_projector=lambda _context: None,
        item_weights_provider=provider,
    )
    assert service is not None


# ---------------------------------------------------------------------------
# RED: typed config resource (S1)
# ---------------------------------------------------------------------------


def test_config_schema_module_declares_single_row_resource() -> None:
    cls = _config_schema()
    assert cls.plugin_name == "komari_roulette"
    assert cls.__tablename__ == "komari_roulette_config"


def test_config_schema_plugin_enable_defaults_false_and_dynamic() -> None:
    cls = _config_schema()
    schema = cls()
    assert schema.plugin_enable is False
    field = cls.model_fields["plugin_enable"]
    extra = field.json_schema_extra or {}
    assert extra.get("apply_mode") == "immediate"


def test_config_schema_item_weights_default_each_one_and_mapping() -> None:
    cls = _config_schema()
    schema = cls()
    for attr in (
        "item_weight_magnifier",
        "item_weight_beer",
        "item_weight_burst",
        "item_weight_lock",
    ):
        assert getattr(schema, attr) == 1
    weights = schema.item_weights()
    assert set(weights) == set(ITEM_TYPES)
    assert all(value == 1 for value in weights.values())


@pytest.mark.parametrize(
    "field",
    [
        "item_weight_magnifier",
        "item_weight_beer",
        "item_weight_burst",
        "item_weight_lock",
    ],
)
def test_config_schema_rejects_negative_weight(field: str) -> None:
    cls = _config_schema()
    with pytest.raises(VALIDATION_ERRORS):
        cls(**{field: -1})


def test_config_schema_rejects_all_zero_weight_sum() -> None:
    cls = _config_schema()
    with pytest.raises(VALIDATION_ERRORS):
        cls(
            item_weight_magnifier=0,
            item_weight_beer=0,
            item_weight_burst=0,
            item_weight_lock=0,
        )


def test_config_schema_action_and_final_pools_are_closed_and_nonempty() -> None:
    cls = _config_schema()
    schema = cls()
    action_keys = _copy_pool_symbol("ACTION_COPY_KEYS")
    final_keys = _copy_pool_symbol("FINAL_COPY_KEYS")
    assert action_keys and final_keys
    assert set(schema.action_copy_pool) == set(action_keys)
    assert set(schema.final_copy_pool) == set(final_keys)
    for pool in (schema.action_copy_pool, schema.final_copy_pool):
        for templates in pool.values():
            assert templates
            assert all(template.strip() for template in templates)


def test_config_schema_rejects_unknown_and_empty_action_pool() -> None:
    cls = _config_schema()
    base = cls().action_copy_pool
    unknown = dict(base)
    unknown["not_a_success_action"] = ["x"]
    with pytest.raises(VALIDATION_ERRORS):
        cls(action_copy_pool=unknown)
    empty = dict(base)
    empty[next(iter(empty))] = []
    with pytest.raises(VALIDATION_ERRORS):
        cls(action_copy_pool=empty)


def test_config_schema_rejects_unknown_and_empty_final_pool() -> None:
    cls = _config_schema()
    base = cls().final_copy_pool
    unknown = dict(base)
    unknown["not_a_terminal_reason"] = ["x"]
    with pytest.raises(VALIDATION_ERRORS):
        cls(final_copy_pool=unknown)
    empty = dict(base)
    empty[next(iter(empty))] = []
    with pytest.raises(VALIDATION_ERRORS):
        cls(final_copy_pool=empty)


def test_config_schema_fixed_waiting_end_templates_are_not_configurable() -> None:
    cls = _config_schema()
    schema = cls()
    action_keys = set(_copy_pool_symbol("ACTION_COPY_KEYS"))
    final_keys = set(_copy_pool_symbol("FINAL_COPY_KEYS"))
    # TSK-266 1G fixes host-cancel / last-leave / waiting-timeout copy in code.
    for fixed in ("cancelled", "waiting_game_expired"):
        assert fixed not in action_keys
        assert fixed not in final_keys
        assert fixed not in schema.action_copy_pool
        assert fixed not in schema.final_copy_pool


def test_config_schema_delegates_action_placeholder_validation() -> None:
    cls = _config_schema()
    base = cls().action_copy_pool
    key = next(iter(base))
    bad = dict(base)
    bad[key] = ["{name.__class__}"]
    with pytest.raises(VALIDATION_ERRORS):
        cls(action_copy_pool=bad)


def test_config_schema_rejects_final_template_missing_required_placeholders() -> None:
    cls = _config_schema()
    base = cls().final_copy_pool
    key = next(iter(base))
    for missing in (
        "{winner} 获胜，累计胜场 {wins}。",  # no event
        "{event}，累计胜场 {wins}。",  # no winner
        "{event}，{winner} 获胜。",  # no wins
    ):
        bad = dict(base)
        bad[key] = [missing]
        with pytest.raises(VALIDATION_ERRORS):
            cls(final_copy_pool=bad)


def test_config_schema_rejects_native_mention_in_pools() -> None:
    cls = _config_schema()
    action = dict(cls().action_copy_pool)
    key = next(iter(action))
    action[key] = ['<qqbot-at-user id="member-2" />']
    with pytest.raises(VALIDATION_ERRORS):
        cls(action_copy_pool=action)


# ---------------------------------------------------------------------------
# RED: pure copy snapshot / compiler (S2)
# ---------------------------------------------------------------------------


def test_copy_pool_declares_closed_key_sets() -> None:
    action_keys = set(_copy_pool_symbol("ACTION_COPY_KEYS"))
    final_keys = set(_copy_pool_symbol("FINAL_COPY_KEYS"))
    assert action_keys
    assert final_keys == {"shot", "forfeit", "timeout"}
    assert action_keys.isdisjoint(final_keys)


def test_copy_pool_accepts_declared_placeholders() -> None:
    validate = _validate_template()
    result = validate(
        "{name}打出一发{kind}。",
        allowed_placeholders=frozenset({"name", "kind"}),
    )
    assert result == "{name}打出一发{kind}。"


@pytest.mark.parametrize(
    "template",
    [
        "{name.__class__}",  # attribute traversal
        "{name[0]}",  # index access
        "{0}",  # positional field
        "{}",  # automatic field
        "{name!r}",  # conversion bypass
        "{name:>5}",  # format spec
        "{name:{width}}",  # nested replacement
        "{other}",  # undeclared placeholder
    ],
)
def test_copy_pool_rejects_formatter_bypass_and_undeclared_fields(
    template: str,
) -> None:
    validate = _validate_template()
    with pytest.raises((ValueError, ValidationError)):
        validate(template, allowed_placeholders=frozenset({"name"}), final=False)


@pytest.mark.parametrize(
    "template",
    [
        '<qqbot-at-user id="member-2" />',
        "<@123456>",
        "<b>粗体</b>",
    ],
)
def test_copy_pool_rejects_configurable_native_mention(template: str) -> None:
    validate = _validate_template()
    with pytest.raises((ValueError, ValidationError)):
        validate(template, allowed_placeholders=frozenset(), final=False)


@pytest.mark.parametrize(
    "template",
    [
        "第一行\n第二行",
        "**粗体**",
        "***",
        "> 引用",
        "- 列表",
    ],
)
def test_copy_pool_rejects_multiline_and_markdown_terminal(template: str) -> None:
    validate = _validate_template()
    with pytest.raises((ValueError, ValidationError)):
        validate(template, allowed_placeholders=frozenset(), final=True)


def test_copy_pool_reuses_content_text_budget_without_new_length_ceilings() -> None:
    validate = _validate_template()
    allowed = frozenset()
    medium = "字" * 1000  # well inside the shared content budget
    assert validate(medium, allowed_placeholders=allowed, final=False) == medium
    too_long = "字" * (CONTENT_TEXT_BUDGET.max_characters + 1)
    with pytest.raises((ValueError, ValidationError)):
        validate(too_long, allowed_placeholders=allowed, final=False)


def test_copy_pool_default_snapshot_has_nonempty_templates() -> None:
    snapshot = _copy_pool_symbol("default_copy_snapshot")()
    action_keys = set(_copy_pool_symbol("ACTION_COPY_KEYS"))
    final_keys = set(_copy_pool_symbol("FINAL_COPY_KEYS"))
    assert set(snapshot.action_templates) == action_keys
    assert set(snapshot.final_templates) == final_keys
    for templates in snapshot.action_templates.values():
        assert templates
    for templates in snapshot.final_templates.values():
        assert templates


def test_copy_pool_compile_rejects_empty_and_unknown_pools() -> None:
    compile_ = _copy_pool_symbol("compile_copy_pool")
    action_defaults = dict(_copy_pool_symbol("DEFAULT_ACTION_COPY_POOL"))
    final_defaults = dict(_copy_pool_symbol("DEFAULT_FINAL_COPY_POOL"))
    with pytest.raises((ValueError, ValidationError)):
        compile_(action_copy_pool={}, final_copy_pool=final_defaults)
    bad_action = dict(action_defaults)
    bad_action["unknown"] = ["x"]
    with pytest.raises((ValueError, ValidationError)):
        compile_(action_copy_pool=bad_action, final_copy_pool=final_defaults)


def test_copy_pool_compile_returns_nonempty_tuples() -> None:
    compile_ = _copy_pool_symbol("compile_copy_pool")
    snapshot = compile_(
        action_copy_pool=dict(_copy_pool_symbol("DEFAULT_ACTION_COPY_POOL")),
        final_copy_pool=dict(_copy_pool_symbol("DEFAULT_FINAL_COPY_POOL")),
    )
    for templates in (*snapshot.action_templates.values(), *snapshot.final_templates.values()):
        assert templates
        assert all(isinstance(template, str) for template in templates)
