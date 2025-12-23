from pathlib import Path
from pydantic import BaseModel


class Config(BaseModel):
    """用户数据插件配置"""

    # 数据库文件路径
    db_path: Path = Path("user_data.db")

    # 数据清理配置
    # 自动清理多少天前的用户属性数据，0表示不清理
    data_retention_days: int = 30