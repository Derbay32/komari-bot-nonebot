"""Neutral Redis key definitions for recent-context and fulfillment evidence.

The strings are live fulfillment identities and must remain byte-for-byte
stable.  Memory-only staging/processing keys belong to the komari_memory
specialization, not to this module.
"""


class RedisKeys:
    """Shared Redis key namespace for active recent-context records."""

    PREFIX = "komari_memory"

    # Recent message buffer
    BUFFER = f"{PREFIX}:buffer:%s"
    BUFFER_PATTERN = f"{PREFIX}:buffer:*"

    # Last message / current session timestamps
    LAST_MESSAGE = f"{PREFIX}:last_message:%s"
    SESSION_START = f"{PREFIX}:session_start:%s"

    # Chat fulfillment idempotency evidence
    CHAT_COMMIT_STEP = f"{PREFIX}:chat_commit:%s:%s"

    # Cross-group interaction buffer used by chat fulfillment
    GLOBAL_INTERACTION = f"{PREFIX}:global_interaction:%s"
    GLOBAL_INTERACTION_PATTERN = f"{PREFIX}:global_interaction:*"
    GLOBAL_INTERACTION_PENDING = f"{PREFIX}:global_interaction:pending"

    @classmethod
    def buffer(cls, group_id: str) -> str:
        """Return the recent-message buffer key for a group."""
        return cls.BUFFER % group_id

    @classmethod
    def last_message(cls, group_id: str) -> str:
        """Return the last-message timestamp key for a group."""
        return cls.LAST_MESSAGE % group_id

    @classmethod
    def session_start(cls, group_id: str) -> str:
        """Return the current-session start timestamp key for a group."""
        return cls.SESSION_START % group_id

    @classmethod
    def chat_commit_step(cls, operation_id: str, step: str) -> str:
        """Return the once-only chat fulfillment evidence key."""
        return cls.CHAT_COMMIT_STEP % (operation_id, step)

    @classmethod
    def global_interaction(cls, user_id: str) -> str:
        """Return the cross-group interaction buffer key for a user."""
        return cls.GLOBAL_INTERACTION % user_id


__all__ = ["RedisKeys"]
