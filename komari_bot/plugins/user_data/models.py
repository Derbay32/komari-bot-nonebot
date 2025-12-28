from datetime import date
from pydantic import BaseModel


class UserAttribute(BaseModel):
    """用户属性模型"""
    user_id: str
    attribute_name: str
    attribute_value: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class UserFavorability(BaseModel):
    """用户好感度模型"""
    user_id: str
    daily_favor: int = 0
    cumulative_favor: int = 0
    last_updated: date

    def __str__(self) -> str:
        return f"用户{self.user_id}的好感度: {self.daily_favor}/{self.cumulative_favor}"

    @property
    def favor_level(self) -> str:
        """根据好感度返回态度等级"""
        if self.daily_favor <= 20:
            return "非常冷淡"
        elif self.daily_favor <= 40:
            return "冷淡"
        elif self.daily_favor <= 60:
            return "中性"
        elif self.daily_favor <= 80:
            return "友好"
        else:
            return "非常友好"


class FavorGenerationResult(BaseModel):
    """好感度生成结果"""
    user_id: str
    daily_favor: int
    cumulative_favor: int
    is_new_day: bool
    favor_level: str