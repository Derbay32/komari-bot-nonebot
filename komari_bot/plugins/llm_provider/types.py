"""结构化输出类型定义和工具函数。"""

from typing import Any

from pydantic import BaseModel

# 类型别名：结构化输出 Schema 可以是 Pydantic 模型类或 JSON Schema 字典
StructuredOutputSchema = type[BaseModel] | dict[str, Any]
