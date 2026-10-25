"""Komari Memory 专属 Redis 键名（共享近期上下文键的中性子类）。"""

from komari_bot.context.redis_keys import RedisKeys as SharedRedisKeys


class RedisKeys(SharedRedisKeys):
    """记忆专用处理/暂存键；共享上下文键全部继承自 ``SharedRedisKeys``。"""

    # 对话 processing 快照
    BUFFER_PROCESSING = f"{SharedRedisKeys.PREFIX}:buffer:processing:%s:%s"
    BUFFER_PROCESSING_PATTERN = f"{SharedRedisKeys.PREFIX}:buffer:processing:*"
    BUFFER_PROCESSING_CURRENT = f"{SharedRedisKeys.PREFIX}:buffer:processing_current:%s"
    BUFFER_PROCESSING_LOCK = f"{SharedRedisKeys.PREFIX}:buffer:processing_lock:%s"
    BUFFER_PROCESSING_CHUNKS = f"{SharedRedisKeys.PREFIX}:buffer:processing_chunks:%s:%s"
    BUFFER_PROCESSING_DEAD = f"{SharedRedisKeys.PREFIX}:buffer:processing_dead:%s:%s"
    BUFFER_PROCESSING_DEAD_INDEX = f"{SharedRedisKeys.PREFIX}:buffer:processing_dead_index"
    BUFFER_PROCESSING_META_LAST_MESSAGE = (
        f"{SharedRedisKeys.PREFIX}:buffer:processing_meta:%s:%s:last_message"
    )
    BUFFER_PROCESSING_META_SESSION_START = (
        f"{SharedRedisKeys.PREFIX}:buffer:processing_meta:%s:%s:session_start"
    )

    # 最后总结时间
    LAST_SUMMARY = f"{SharedRedisKeys.PREFIX}:last_summary:%s"

    # 画像 Agent 暂存区
    STAGING_PROFILE = f"{SharedRedisKeys.PREFIX}:staging:profile:%s"
    SNAPSHOT_PROFILE = f"{SharedRedisKeys.PREFIX}:snapshot:profile:%s:%s"
    SNAPSHOT_PROFILE_PATTERN = f"{SharedRedisKeys.PREFIX}:snapshot:profile:*"

    # 跨群用户互动事件处理 leases / snapshots
    GLOBAL_INTERACTION_LEASES = f"{SharedRedisKeys.PREFIX}:global_interaction:leases"
    GLOBAL_INTERACTION_LEASE_OWNERS = f"{SharedRedisKeys.PREFIX}:global_interaction:lease_owners"
    GLOBAL_INTERACTION_SNAPSHOTS = f"{SharedRedisKeys.PREFIX}:global_interaction:snapshots"
    GLOBAL_INTERACTION_PROCESSING = f"{SharedRedisKeys.PREFIX}:global_interaction:processing:%s:%s"

    @classmethod
    def buffer_processing(cls, group_id: str, token: str) -> str:
        """获取对话缓冲处理快照键。"""
        return cls.BUFFER_PROCESSING % (group_id, token)

    @classmethod
    def buffer_processing_lock(cls, group_id: str) -> str:
        """获取对话缓冲处理锁键。"""
        return cls.BUFFER_PROCESSING_LOCK % group_id

    @classmethod
    def buffer_processing_current(cls, group_id: str) -> str:
        """获取当前对话 processing 快照指针键。"""
        return cls.BUFFER_PROCESSING_CURRENT % group_id

    @classmethod
    def buffer_processing_chunks(cls, group_id: str, token: str) -> str:
        """获取对话 processing 分块覆盖账本键。"""
        return cls.BUFFER_PROCESSING_CHUNKS % (group_id, token)

    @classmethod
    def buffer_processing_dead(cls, group_id: str, token: str) -> str:
        """获取对话 processing dead-letter 元数据键。"""
        return cls.BUFFER_PROCESSING_DEAD % (group_id, token)

    @classmethod
    def buffer_processing_meta_last_message(cls, group_id: str, token: str) -> str:
        """获取对话缓冲处理快照的最后消息时间元数据键。"""
        return cls.BUFFER_PROCESSING_META_LAST_MESSAGE % (group_id, token)

    @classmethod
    def buffer_processing_meta_session_start(cls, group_id: str, token: str) -> str:
        """获取对话缓冲处理快照的会话开始时间元数据键。"""
        return cls.BUFFER_PROCESSING_META_SESSION_START % (group_id, token)

    @classmethod
    def last_summary(cls, group_id: str) -> str:
        """获取最后总结时间键。"""
        return cls.LAST_SUMMARY % group_id

    @classmethod
    def staging_profile(cls, session_id: str) -> str:
        """获取画像 Agent 暂存区键。"""
        return cls.STAGING_PROFILE % session_id

    @classmethod
    def snapshot_profile(cls, group_id: str, token: str) -> str:
        """获取画像 Agent 基线快照键。"""
        return cls.SNAPSHOT_PROFILE % (group_id, token)

    @classmethod
    def global_interaction_processing(cls, user_id: str, token: str) -> str:
        """获取跨群用户互动处理快照键。"""
        return cls.GLOBAL_INTERACTION_PROCESSING % (user_id, token)


__all__ = ["RedisKeys"]
