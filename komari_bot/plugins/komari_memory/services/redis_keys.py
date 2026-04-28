"""Redis 键名管理。"""


class RedisKeys:
    """集中管理 Redis 键名。

    所有键名都集中定义，避免拼写错误和重复。
    """

    PREFIX = "komari_memory"

    # 消息缓冲区
    BUFFER = f"{PREFIX}:buffer:%s"
    BUFFER_PATTERN = f"{PREFIX}:buffer:*"

    # 最后总结时间
    LAST_SUMMARY = f"{PREFIX}:last_summary:%s"

    # 最后一条消息时间
    LAST_MESSAGE = f"{PREFIX}:last_message:%s"

    # 当前会话开始时间
    SESSION_START = f"{PREFIX}:session_start:%s"

    # 主动回复冷却
    PROACTIVE_COOLDOWN = f"{PREFIX}:proactive:cd:%s"

    # 每小时主动回复计数
    PROACTIVE_COUNT = f"{PREFIX}:proactive:count:%s:%s"

    # 每日好感问候标记
    FAVOR_GREETED = f"{PREFIX}:favor:greeted:%s:%s"

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
    def proactive_count(cls, group_id: str, hour: int) -> str:
        """获取每小时主动回复计数键。

        Args:
            group_id: 群组 ID
            hour: 小时时间戳

        Returns:
            Redis 键
        """
        return cls.PROACTIVE_COUNT % (group_id, hour)

    @classmethod
    def favor_greeted(cls, group_id: str, user_id: str) -> str:
        """获取每日好感问候标记键。

        Args:
            group_id: 群组 ID
            user_id: 用户 ID

        Returns:
            Redis 键
        """
        return cls.FAVOR_GREETED % (group_id, user_id)
