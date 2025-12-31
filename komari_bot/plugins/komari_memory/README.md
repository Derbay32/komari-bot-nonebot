# Komari Memory - 小鞠常识库插件

基于 Hybrid-RAG（混合检索增强生成）的 Bot 人物知识库管理插件，提供高效的知识检索能力。

## 特性

- **混合检索**：Layer 1 关键词精确匹配（微秒级）+ Layer 2 向量语义检索（毫秒级）
- **WebUI 管理界面**：Streamlit 提供的可视化知识库管理
- **双模式运行**：支持 NoneBot 插件模式和独立运行模式
- **灵活配置**：通过 config_manager 插件动态配置，支持热重载

## 架构

```
komari_memory/
├── __init__.py          # NoneBot 插件入口
├── engine.py            # 核心引擎（支持双模式）
├── config_schema.py     # 配置 Schema
├── webui.py             # Streamlit 管理界面
├── README.md            # 本文档
└── init_db.sql          # 数据库初始化脚本
```

### 核心组件

| 组件 | 职责 |
|------|------|
| `engine.py` | 核心检索引擎，支持 NoneBot 和独立两种模式 |
| `__init__.py` | NoneBot 插件入口，注册驱动钩子 |
| `webui.py` | Streamlit 管理界面，使用 importlib 直接加载引擎 |
| `config_schema.py` | Pydantic 配置模型定义 |

### 双模式支持

- **NoneBot 模式**：通过 Bot 加载，使用 config_manager 插件管理配置
- **独立模式**：WebUI 直接加载引擎，从 JSON 文件读取配置

---

## 快速开始

### 1. 数据库初始化

#### 1.1 启用 pgvector 扩展

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

验证：
```sql
SELECT * FROM pg_extension WHERE extname = 'vector';
```

#### 1.2 执行初始化脚本

```bash
psql -U your_username -d your_database -f init_db.sql
```

#### 1.3 验证表创建

```sql
\d komari_knowledge
```

应看到以下索引：
- `idx_komari_knowledge_embedding` (HNSW 向量索引)
- `idx_komari_knowledge_keywords` (GIN 关键词索引)
- `idx_komari_knowledge_category`
- `idx_komari_knowledge_created_at`

### 2. 配置插件

在 `config/config_manager/komari_memory_config.json` 中配置：

```json
{
  "plugin_enable": true,
  "pg_host": "localhost",
  "pg_port": 5432,
  "pg_database": "komari_bot",
  "pg_user": "your_username",
  "pg_password": "your_password",
  "webui_enabled": true,
  "webui_port": 8502
}
```

### 3. 启动使用

**通过 Bot 启动（推荐）**：Bot 启动时会自动初始化引擎并启动 WebUI

**独立启动 WebUI**：
```bash
streamlit run komari_bot/plugins/komari_memory/webui.py
```

---

## 知识分类

插件支持以下知识分类，用于组织和管理不同类型的知识：

| 分类 | 说明 | 示例 |
|------|------|------|
| `general` | 通用知识 | 日常常识、通用信息 |
| `character` | 人物设定 | 角色性格、喜好、背景 |
| `setting` | 世界设定 | 世界观、规则、地理 |
| `plot` | 情节记录 | 重要事件、剧情节点 |
| `other` | 其他 | 无法归类的知识 |

**分类用途**：
- WebUI 中按分类筛选知识
- 为未来按类别检索预留扩展
- 帮助组织和管理大型知识库

---

## WebUI 使用

访问 `http://localhost:8502`（默认端口）进入管理界面。

### 功能

1. **录入知识**：添加新知识，设置关键词和分类
2. **检索测试**：测试混合检索效果
3. **知识列表**：查看、筛选、删除知识

### 关键词设置建议

- 使用核心名词作为关键词（如：`小鞠`、`布丁`、`姐姐`）
- 3-5 个关键词为宜
- 避免过于常见的词（如：`的`、`了`）

---

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `plugin_enable` | bool | false | 插件总开关 |
| `pg_host` | str | localhost | 数据库主机 |
| `pg_port` | int | 5432 | 数据库端口 |
| `pg_database` | str | komari_bot | 数据库名称 |
| `pg_user` | str | - | 数据库用户名 |
| `pg_password` | str | - | 数据库密码 |
| `embedding_model` | str | BAAI/bge-small-zh-v1.5 | 向量嵌入模型 |
| `similarity_threshold` | float | 0.65 | 向量相似度阈值（0-1） |
| `layer1_limit` | int | 3 | Layer 1 关键词匹配返回数 |
| `layer2_limit` | int | 2 | Layer 2 向量检索返回数 |
| `total_limit` | int | 5 | 总返回结果数上限 |
| `webui_enabled` | bool | false | 是否启动 WebUI |
| `webui_port` | int | 8502 | WebUI 端口 |

---

## 在其他插件中使用

### 直接调用（手动管理上下文）

```python
from nonebot.plugin import require

memory_plugin = require("komari_memory")

# 检索知识
results = await memory_plugin.search_memory("小鞠喜欢吃什么？")
for result in results:
    print(f"[{result.source}] {result.content}")

# 添加知识
kid = await memory_plugin.add_knowledge(
    content="小鞠非常喜欢布丁",
    keywords=["小鞠", "布丁", "喜欢"],
    category="character"
)

# 获取所有知识
all_knowledge = await memory_plugin.get_all_knowledge()

# 删除知识
await memory_plugin.delete_knowledge(kid)
```

### 与 LLM Provider 集成（自动上下文注入）

`llm_provider` 已内置对 `komari_memory` 的支持，启用后会自动检索相关知识并注入到系统提示词中：

```python
from nonebot.plugin import require

llm_provider = require("llm_provider")

# 启用知识库检索的 LLM 调用
response = await llm_provider.generate_text(
    prompt="小鞠喜欢做什么？",
    provider="gemini",
    system_instruction="你是小鞠，请用第一人称回答",
    enable_knowledge=True,      # 启用知识库检索
    knowledge_limit=3,          # 检索最多 3 条相关知识
)
```

**参数说明**：
- `enable_knowledge`: 是否启用知识库检索（默认 `False`）
- `knowledge_query`: 自定义检索查询，`None` 则使用 `prompt`
- `knowledge_limit`: 检索返回的知识数量上限（默认 `3`）

**工作原理**：
1. 使用 `prompt` 或 `knowledge_query` 从知识库检索相关知识
2. 将检索结果格式化为 `【相关人物设定和知识】` 添加到系统指令最前面
3. LLM 基于检索到的上下文生成响应

**使用建议**：
- 适合需要人物设定、世界观的对话场景
- 建议 `knowledge_limit` 设置为 3-5 条，避免上下文过长
- 在 `system_instruction` 中明确角色身份，配合知识库使用效果更佳

**注意**：本模块提供**静态知识库**功能（人物设定、世界观等），未来将添加**动态记忆**功能（对话历史、用户偏好等），两者将区分管理。

---

## 检索原理

### Layer 1: 关键词匹配

- 在内存中构建关键词 → 知识 ID 的倒排索引
- 查询时检查是否包含已知关键词
- 时间复杂度：O(n×m)，n 为关键词数量，m 为查询词数
- **微秒级响应**

### Layer 2: 向量检索

- 使用 bge-small-zh-v1.5 生成 512 维向量
- PostgreSQL pgvector HNSW 索引加速
- 余弦相似度计算
- **毫秒级响应**

### 混合策略

1. 先执行 Layer 1，获取精确匹配结果
2. 如果结果不足 total_limit，用 Layer 2 补充
3. 按相似度排序返回

---

## 检查清单

初始化前：
- [ ] PostgreSQL 已安装 pgvector 扩展
- [ ] komari_knowledge 表已创建
- [ ] 索引已创建（embedding HNSW + keywords GIN）
- [ ] 数据库连接信息已配置

使用前：
- [ ] 配置文件已正确设置
- [ ] Bot 已启动（或 WebUI 已独立启动）
- [ ] 已录入基础知识

---

## 常见问题

### Q: WebUI 启动报错 "Cannot load plugin config_manager"

A: 这是正常的警告日志，WebUI 使用独立模式，不需要 NoneBot 的 config_manager。只要 WebUI 界面能正常访问即可。

### Q: 检索结果为空？

A: 检查以下几点：
1. 知识库是否有数据
2. 关键词是否正确设置
3. `similarity_threshold` 是否设置过高

### Q: 如何调整检索结果数量？

A: 修改配置中的 `layer1_limit`、`layer2_limit` 和 `total_limit`。

---

## 技术栈

- **PostgreSQL** + **pgvector**：向量存储和检索
- **fastembed**：轻量级向量嵌入（无需 GPU）
- **asyncpg**：异步数据库连接池
- **Streamlit**：WebUI 界面
- **Pydantic**：配置验证
