---
status: accepted
---

# 场景归类由判定插件的深重排 module 统一拥有

群总结曾在插件外无参构造宽重排 module，自行持有 runtime、场景键、阈值与异常降级语义，导致无数字总结请求稳定退化为静默未命中。我们决定让 `komari_decision` 内部的一个深重排 module 统一拥有场景 runtime、embedding、召回、rerank 与用途策略：现有聊天引擎保持专用 interface 和既有行为，群总结经判定插件顶层的专用异步场景归类 operation 获得命中、未命中或不可用结果；两者共享 implementation，但不共享万能用途参数或宽结果契约。

## Considered Options

- **扩展 `DecisionEngine` 或公开通用 `classify(purpose=...)`**：被拒绝——聊天判定还包含过滤、时机、回复与记忆语义，通用用途参数会让两个调用方共同承担不属于自己的 interface 成本。
- **由群总结获取 runtime 后自行组装重排**：被拒绝——构造权、场景身份、阈值、候选集合与失败语义会再次泄漏到判定插件之外，并违反 ADR-0006 的顶层 seam 纪律。
- **以能力端口集合替换现有聊天组合根**：被拒绝——会为修复一个坏 seam 重写稳定的聊天 interface，并引入不成比例的 adapter 与迁移成本。
- **保留旧重排 module，再增加总结 facade**：被拒绝——facade 只能转译宽结果，无法通过 deletion test，旧 module 仍然 shallow。

## Consequences

- `UnifiedCandidateRerankService` 及其宽重排契约已退出跨插件暴露面并物理删除（KOMARIBOT-27），调用方与测试只穿过新的窄 interface。
- 群总结专用 operation 内化启用门控、数字快速识别、一般场景归类、固定目标场景身份、用途配置和预期不可用语义；未命中放行普通聊天，不可用由群总结接入共享失败通知 module。
- 聊天用途严格保持现有候选、分数与判定行为；总结用途共用场景 embedding，但拥有独立的 query、rerank、top-k 与阈值配置。
- 实施前置依赖链已完成：群总结不可用经共享失败通知 module 接入，场景归类配置经通用配置分区元数据落地，本 seam 实施不再受依赖阻塞。PostgreSQL 配置数据初始化是独立事项，另行解决，决定全新部署何时自动具备总结场景与校准配置，不构成对本 seam 的阻塞。
