import random
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import FavorGenerationResult, UserAttribute, UserFavorability


class UserDataDB:
    """用户数据数据库操作类"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """初始化数据库连接和表结构"""
        # 确保数据库目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建数据库连接
        self._connection = await aiosqlite.connect(
            str(self.db_path.absolute()),
            isolation_level=None,  # 自动提交模式
        )

        # 创建表结构
        await self._create_tables()

    async def _create_tables(self):
        """创建数据库表结构"""
        assert self._connection is not None
        # 用户属性表
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS user_attributes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                attribute_name TEXT NOT NULL,
                attribute_value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, attribute_name)
            )
        """)

        # 好感度表
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS user_favorability (
                user_id TEXT NOT NULL,
                daily_favor INTEGER DEFAULT 0,
                cumulative_favor INTEGER DEFAULT 0,
                last_updated DATE NOT NULL,
                PRIMARY KEY (user_id, last_updated)
            )
        """)

        # 创建索引以提高查询性能
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_attributes_composite
            ON user_attributes(user_id, attribute_name)
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_favorability_composite
            ON user_favorability(user_id, last_updated)
        """)

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._connection:
            await self._connection.close()
            self._connection = None

    # ===== 用户属性相关操作 =====

    async def get_user_attribute(self, user_id: str, attribute_name: str) -> str | None:
        """获取用户属性值"""
        assert self._connection is not None
        cursor = await self._connection.execute(
            """
            SELECT attribute_value
            FROM user_attributes
            WHERE user_id = ? AND attribute_name = ?
            """,
            (user_id, attribute_name),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_user_attribute(
        self, user_id: str, attribute_name: str, attribute_value: str
    ) -> bool:
        """设置用户属性值

        Returns:
            操作是否成功
        """
        assert self._connection is not None
        await self._connection.execute(
            """
            INSERT OR REPLACE INTO user_attributes
            (user_id, attribute_name, attribute_value, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (user_id, attribute_name, attribute_value),
        )
        return True

    async def get_user_attributes(self, user_id: str) -> list[UserAttribute]:
        """获取用户的所有属性"""
        assert self._connection is not None
        cursor = await self._connection.execute(
            """
            SELECT user_id, attribute_name, attribute_value, created_at, updated_at
            FROM user_attributes
            WHERE user_id = ?
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            UserAttribute(
                user_id=row[0],
                attribute_name=row[1],
                attribute_value=row[2],
                created_at=row[3],
                updated_at=row[4],
            )
            for row in rows
        ]

    # ===== 好感度相关操作 =====

    async def get_user_favorability(
        self, user_id: str, target_date: date | None = None
    ) -> UserFavorability | None:
        """获取用户好感度"""
        assert self._connection is not None
        if target_date is None:
            target_date = datetime.now().astimezone().today()

        cursor = await self._connection.execute(
            """
            SELECT user_id, daily_favor, cumulative_favor, last_updated
            FROM user_favorability
            WHERE user_id = ? AND last_updated = ?
            """,
            (user_id, target_date),
        )
        row = await cursor.fetchone()

        if row:
            return UserFavorability(
                user_id=row[0],
                daily_favor=row[1],
                cumulative_favor=row[2],
                last_updated=date.fromisoformat(row[3])
                if isinstance(row[3], str)
                else row[3],
            )
        return None

    async def generate_or_update_favorability(
        self, user_id: str
    ) -> FavorGenerationResult:
        """生成或更新用户好感度"""
        assert self._connection is not None
        today = datetime.now().astimezone().today()
        existing_favor = await self.get_user_favorability(user_id, today)

        is_new_day = False
        daily_favor = 0
        cumulative_favor = 0

        if existing_favor:
            # 今日已有好感度，直接返回
            daily_favor = existing_favor.daily_favor
            cumulative_favor = existing_favor.cumulative_favor
        else:
            # 新的一天或新用户，需要生成好感度
            is_new_day = True
            daily_favor = random.randint(1, 100)

            # 获取历史累计好感度
            cumulative_favor = await self._get_cumulative_favor(user_id)
            cumulative_favor += daily_favor

            # 插入新的好感度记录
            await self._connection.execute(
                """
                INSERT INTO user_favorability
                (user_id, daily_favor, cumulative_favor, last_updated)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, daily_favor, cumulative_favor, today),
            )

        # 创建好感度对象以获取态度等级
        favor_obj = UserFavorability(
            user_id=user_id,
            daily_favor=daily_favor,
            cumulative_favor=cumulative_favor,
            last_updated=today,
        )

        return FavorGenerationResult(
            user_id=user_id,
            daily_favor=daily_favor,
            cumulative_favor=cumulative_favor,
            is_new_day=is_new_day,
            favor_level=favor_obj.favor_level,
        )

    async def _get_cumulative_favor(self, user_id: str) -> int:
        """获取用户的累计好感度（不包括今天）"""
        assert self._connection is not None
        today = datetime.now().astimezone().today()
        cursor = await self._connection.execute(
            """
            SELECT MAX(cumulative_favor)
            FROM user_favorability
            WHERE user_id = ? AND last_updated < ?
            """,
            (user_id, today),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

    async def get_favor_history(
        self, user_id: str, days: int = 7
    ) -> list[UserFavorability]:
        """获取用户好感度历史记录"""
        assert self._connection is not None
        cursor = await self._connection.execute(
            """
            SELECT user_id, daily_favor, cumulative_favor, last_updated
            FROM user_favorability
            WHERE user_id = ?
            ORDER BY last_updated DESC
            LIMIT ?
            """,
            (user_id, days),
        )
        rows = await cursor.fetchall()
        return [
            UserFavorability(
                user_id=row[0],
                daily_favor=row[1],
                cumulative_favor=row[2],
                last_updated=date.fromisoformat(row[3])
                if isinstance(row[3], str)
                else row[3],
            )
            for row in rows
        ]

    # ===== 数据清理操作 =====

    async def cleanup_old_data(self, retention_days: int = 7) -> bool:
        """清理旧的用户数据

        Args:
            retention_days: 数据保留天数

        Returns:
            操作是否成功，retention_days <= 0 时返回 False
        """
        assert self._connection is not None
        if retention_days <= 0:
            return False

        cutoff_date = datetime.now().astimezone() - timedelta(days=retention_days)

        # 清理 user_attributes 表
        await self._connection.execute(
            """
            DELETE FROM user_attributes
            WHERE updated_at < ?
            """,
            (cutoff_date,),
        )

        # 清理 user_favorability 表
        await self._connection.execute(
            """
            DELETE FROM user_favorability
            WHERE last_updated < ?
            """,
            (cutoff_date.date(),),
        )
        return True

    # ===== 统计操作 =====

    async def get_user_count(self) -> int:
        """获取总用户数"""
        assert self._connection is not None
        cursor = await self._connection.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_attributes"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
