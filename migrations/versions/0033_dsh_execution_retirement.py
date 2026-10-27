"""Retire dedicated Python model execution storage (TSK-207).

Revision 0033 follows 0032. Preserve all surviving values, revisions,
management permissions and formal business/source-memory data unchanged.
Operators must export legacy prompts before this separately authorized upgrade.
No prompt text is imported into DSH by this migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    # Reviewed autogenerate: exactly 16 chat and 15 summary columns.
    with op.batch_alter_table("komari_chat_config", schema=None) as batch_op:
        batch_op.drop_column("image_understanding_mode")
        batch_op.drop_column("llm_reasoning_effort_chat")
        batch_op.drop_column("query_rewrite_model")
        batch_op.drop_column("assistant_prefill_enabled")
        batch_op.drop_column("llm_max_tokens_chat")
        batch_op.drop_column("llm_thinking_mode_chat")
        batch_op.drop_column("dsv4_roleplay_instruct_mode")
        batch_op.drop_column("llm_temperature_chat")
        batch_op.drop_column("llm_request_api_chat")
        batch_op.drop_column("query_rewrite_thinking_mode")
        batch_op.drop_column("query_rewrite_request_api")
        batch_op.drop_column("agent_tool_call_mode")
        batch_op.drop_column("llm_model_chat")
        batch_op.drop_column("query_rewrite_reasoning_effort")
        batch_op.drop_column("llm_stream_enabled_chat")
        batch_op.drop_column("query_rewrite_stream_enabled")

    with op.batch_alter_table(
        "komari_group_history_summary_config", schema=None
    ) as batch_op:
        batch_op.drop_column("summary_planning_model")
        batch_op.drop_column("assistant_prefill_enabled")
        batch_op.drop_column("summary_temperature")
        batch_op.drop_column("summary_thinking_mode")
        batch_op.drop_column("summary_planning_thinking_mode")
        batch_op.drop_column("summary_planning_stream_enabled")
        batch_op.drop_column("summary_planning_reasoning_effort")
        batch_op.drop_column("summary_planning_request_api")
        batch_op.drop_column("summary_max_tokens")
        batch_op.drop_column("summary_stream_enabled")
        batch_op.drop_column("summary_planning_max_tokens")
        batch_op.drop_column("summary_reasoning_effort")
        batch_op.drop_column("summary_request_api")
        batch_op.drop_column("dsv4_roleplay_instruct_mode")
        batch_op.drop_column("summary_model")

    # Metadata-external tables are protected by autogenerate: handwritten drops.
    op.execute('DROP TABLE "komari_llm_provider_config"')
    op.execute('DROP TABLE "komari_prompt_komari_chat"')
    op.execute('DROP TABLE "komari_prompt_group_history_summary"')


def downgrade(name: str = "") -> None:
    if name:
        return
    msg = (
        "0033_TSK207_IS_IRREVERSIBLE: retired Python model configuration and "
        "prompt stores cannot be recreated; repair forward on the current version."
    )
    raise RuntimeError(msg)
