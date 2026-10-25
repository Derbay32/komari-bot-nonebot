"""Neutral Redis key definitions for recent-context and fulfillment evidence.

The strings are live fulfillment identities and must remain byte-for-byte
stable.  Memory-only staging/processing/global-interaction keys and the dead
session/last-message metadata no longer have a runtime reader or writer and are
not defined here.
"""


class RedisKeys:
    """Shared Redis key namespace for active recent-context records."""

    PREFIX = "komari_memory"

    # Recent message buffer
    BUFFER = f"{PREFIX}:buffer:%s"

    # Chat fulfillment idempotency evidence
    CHAT_COMMIT_STEP = f"{PREFIX}:chat_commit:%s:%s"

    @classmethod
    def buffer(cls, group_id: str) -> str:
        """Return the recent-message buffer key for a group."""
        return cls.BUFFER % group_id

    @classmethod
    def chat_commit_step(cls, operation_id: str, step: str) -> str:
        """Return the once-only chat fulfillment evidence key."""
        return cls.CHAT_COMMIT_STEP % (operation_id, step)


__all__ = ["RedisKeys"]
