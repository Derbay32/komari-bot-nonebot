"""Redis 键名管理。"""


class RedisKeys:
    """集中管理 Redis 键名。

    所有键名都集中定义，避免拼写错误和重复。
    """

    PREFIX = "komari_memory"

    # 消息缓冲区
    BUFFER = f"{PREFIX}:buffer:%s"
    BUFFER_PATTERN = f"{PREFIX}:buffer:*"
    BUFFER_PROCESSING = f"{PREFIX}:buffer:processing:%s:%s"
    BUFFER_PROCESSING_PATTERN = f"{PREFIX}:buffer:processing:*"
    BUFFER_PROCESSING_LOCK = f"{PREFIX}:buffer:processing_lock:%s"
    BUFFER_PROCESSING_META_LAST_MESSAGE = f"{PREFIX}:buffer:processing_meta:%s:%s:last_message"
    BUFFER_PROCESSING_META_SESSION_START = f"{PREFIX}:buffer:processing_meta:%s:%s:session_start"

    # 最后总结时间
    LAST_SUMMARY = f"{PREFIX}:last_summary:%s"

    # 最后一条消息时间
    LAST_MESSAGE = f"{PREFIX}:last_message:%s"

    # 当前会话开始时间
    SESSION_START = f"{PREFIX}:session_start:%s"

    # 主动回复冷却
    PROACTIVE_COOLDOWN = f"{PREFIX}:proactive:cd:%s"

    # 主动回复滑动窗口名额（含生成中的预占与已送达回复）
    PROACTIVE_SLOTS = f"{PREFIX}:proactive:slots:%s"

    # 画像 Agent 暂存区
    STAGING_PROFILE = f"{PREFIX}:staging:profile:%s"
    SNAPSHOT_PROFILE = f"{PREFIX}:snapshot:profile:%s:%s"
    SNAPSHOT_PROFILE_PATTERN = f"{PREFIX}:snapshot:profile:*"

    # 跨群用户互动事件缓冲
    GLOBAL_INTERACTION = f"{PREFIX}:global_interaction:%s"
    GLOBAL_INTERACTION_PATTERN = f"{PREFIX}:global_interaction:*"
    GLOBAL_INTERACTION_PENDING = f"{PREFIX}:global_interaction:pending"
    GLOBAL_INTERACTION_LEASES = f"{PREFIX}:global_interaction:leases"
    GLOBAL_INTERACTION_LEASE_OWNERS = f"{PREFIX}:global_interaction:lease_owners"
    GLOBAL_INTERACTION_SNAPSHOTS = f"{PREFIX}:global_interaction:snapshots"
    GLOBAL_INTERACTION_PROCESSING = f"{PREFIX}:global_interaction:processing:%s:%s"

    @classmethod
    def buffer(cls, group_id: str) -> str:
        """获取消息缓冲区键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.BUFFER % group_id

    @classmethod
    def buffer_processing(cls, group_id: str, token: str) -> str:
        """获取对话缓冲处理快照键。"""
        return cls.BUFFER_PROCESSING % (group_id, token)

    @classmethod
    def buffer_processing_lock(cls, group_id: str) -> str:
        """获取对话缓冲处理锁键。"""
        return cls.BUFFER_PROCESSING_LOCK % group_id

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
        """获取最后总结时间键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.LAST_SUMMARY % group_id

    @classmethod
    def last_message(cls, group_id: str) -> str:
        """获取最后一条消息时间键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.LAST_MESSAGE % group_id

    @classmethod
    def session_start(cls, group_id: str) -> str:
        """获取当前会话开始时间键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.SESSION_START % group_id

    @classmethod
    def proactive_cooldown(cls, group_id: str) -> str:
        """获取主动回复冷却键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.PROACTIVE_COOLDOWN % group_id

    @classmethod
    def proactive_slots(cls, group_id: str) -> str:
        """获取主动回复滑动窗口名额键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.PROACTIVE_SLOTS % group_id

    @classmethod
    def staging_profile(cls, session_id: str) -> str:
        """获取画像 Agent 暂存区键。"""
        return cls.STAGING_PROFILE % session_id

    @classmethod
    def snapshot_profile(cls, group_id: str, token: str) -> str:
        """获取画像 Agent 基线快照键。"""
        return cls.SNAPSHOT_PROFILE % (group_id, token)

    @classmethod
    def global_interaction(cls, user_id: str) -> str:
        """获取跨群用户互动缓冲键。"""
        return cls.GLOBAL_INTERACTION % user_id

    @classmethod
    def global_interaction_processing(cls, user_id: str, token: str) -> str:
        """获取跨群用户互动处理快照键。"""
        return cls.GLOBAL_INTERACTION_PROCESSING % (user_id, token)
