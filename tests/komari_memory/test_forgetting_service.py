"""ForgettingService tests."""

from __future__ import annotations

from komari_bot.plugins.komari_memory.services import (
    forgetting_service as forgetting_service_module,
)


def test_forgetting_context_is_untrusted_and_explicitly_bounded() -> None:
    original = "&" * 2_000

    rendered = forgetting_service_module._render_bounded_memory_context(
        content=original,
        source_id="forgetting-conversation:1",
    )

    assert 'source_type="memory"' in rendered
    assert '"original_characters":2000' in rendered
    assert '"truncated":true' in rendered
    assert original not in rendered
    assert len(rendered) <= (
        forgetting_service_module._FORGETTING_RENDERED_CONTEXT_BUDGET.max_characters
    )

