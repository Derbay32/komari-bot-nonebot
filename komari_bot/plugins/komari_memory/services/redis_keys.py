"""Redis 键名管理。"""


class RedisKeys:
    """集中管理 Redis 键名。

    所有键名都集中定义，避免拼写错误和重复。
    """

    PREFIX = "komari_memory"

    # 消息缓冲区
    BUFFER = f"{PREFIX}:buffer:%s"
    BUFFER_PATTERN = f"{PREFIX}:buffer:*"

    # Token 计数
    TOKENS = f"{PREFIX}:tokens:%s"

    # 消息计数
    MESSAGES = f"{PREFIX}:messages:%s"

    # 最后总结时间
    LAST_SUMMARY = f"{PREFIX}:last_summary:%s"

    # 主动回复冷却
    PROACTIVE_COOLDOWN = f"{PREFIX}:proactive:cd:%s"

    # 每小时主动回复计数
    PROACTIVE_COUNT = f"{PREFIX}:proactive:count:%s:%s"

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
    def tokens(cls, group_id: str) -> str:
        """获取 token 计数键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.TOKENS % group_id

    @classmethod
    def messages(cls, group_id: str) -> str:
        """获取消息计数键。

        Args:
            group_id: 群组 ID

        Returns:
            Redis 键
        """
        return cls.MESSAGES % group_id

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
