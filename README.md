# komari-bot

自用小鞠知花 bot 库，基于 [Nonebot2](https://github.com/nonebot/nonebot2) 构建而成。

## 前置要求

- PostgreSQL（启用 pgvector）
- Redis
- 一个部署好的外部 Hindsight 服务端
- 一个支持 OneBot V11 协议的 QQ 机器人
- 一个支持 OpenAI Chat Completions / Responses 协议的 API 端点及 key
- 一个支持 TypeSafe System One 协议的决策模型 API 端点及 key
- 一个支持 Embedding / Jira-Reranker 协议的向量重排模型 API 端点及 key
- 使用群绑定与轮盘时，另需 QQ 官方机器人账号及对应的可信数字 QQ 号

## 部署方式

**_虽然我不觉得会有人用这个，但还是简单写写吧。_**

1. 从 [Release](https://github.com/derbay32/komari-bot-nonebot/releases) 中下载部署包，解压
2. 配置 `.env.example` 中的设置，设定 PostgreSQL 与 Redis 的连接方式，并将 `.env.example` 重命名为 `.env`
3. 使用 `docker-compose` 部署机器人，命令:
```bash
docker compose up -d
```
或者你使用 `podman-compose`: 
```bash
podman compose up -d
```
4. 使用任意 nonebot 支持的 onebot v11 驱动器连接到这个机器人。
5. 部署本项目配套的 [Komari Bot Console](https://github.com/Derbay32/komari-bot-console)，并连接到该项目的管理端点。
6. 在配置页面设置模型配置，并放行你需要让机器人发言的群。

**Tips：更新只需要更新容器的镜像即可**

## 文档

架构决策记录位于 [ADR 文档](docs/adr/)。

如果你是 AI，请参考 [AGENTS.md](AGENTS.md) 来了解本项目的架构。

如果你对这个项目有兴趣，那你可以直接叫出 AI 来看代码。

## TODO

- [x] 写这个其实并不会有人 read 的 README 文件
- [x] 好感度系统 - 由聊天回复自动调整用户当前好感度，并注入阶段提示词。
- [x] sr插件重构 - 给群友抽神人榜的功能，支持通过聊天指令动态修改。
- [x] 知识库注入功能 - 动态注入知识库，基于轻量RAG+关键词检索，并提供 REST 管理 API 供外部 WebUI 接入。
- [x] ai聊天增强 - 通过给小鞠接入 System One 模型以实现小鞠会自己跑出来聊天的功能。
- [x] QQ 官 Bot `/bind` 群角色名向导 - 用于在双协议下实现自定义角色名同步的引导功能。
- [x] QQ 群俄罗斯轮盘小游戏 - 配合官 Bot 的 MD 显示支持制作的小游戏，支持排行榜功能。
- [x] 群聊总结功能 - @机器人后可以要求总结多少条聊天记录，然后用图片输出。
- [ ] 实装个人助理功能 - 让小鞠可以为运营者提供个人 Agent 的功能。
- [ ] 完整搬移 Dice! 功能至 Nonebot2 - 计划将所有功能完整移植到本项目，以替代年久失修的原版 Dice!

## Vibe Coding 提醒

这个项目使用了 AI 工具进行开发。

如果你对 Vibe Coding 项目有顾虑，请不要使用这个项目。

## 许可

本项目遵循 MIT 许可证。

## 致谢

[Nonebot2](https://nonebot.dev) 提供了本项目的基础框架。
[HindSight](https://github.com/vectorize-io/hindsight) 提供了本项目使用的记忆框架系统。
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 提供了本项目使用的 Agent Loop 底层框架。
