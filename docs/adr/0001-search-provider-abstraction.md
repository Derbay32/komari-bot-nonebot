# 联网搜索双提供者抽象与共享密钥

komari_search 从 Tavily 硬绑定升级为 Tavily / EXA 双提供者架构，由配置层 `search_provider` 字段选择引擎，LLM 工具定义不感知底层提供者。两个提供者共用一个 `search_api_key` 配置项，由部署方自行保证 key 与 provider 对应。

选择配置层选择而非 LLM 侧选择或自动路由，是因为搜索引擎选择是基础设施决策而非语义决策：LLM 不应消耗推理预算在引擎选择上，自动 fallback 会使熔断器和计费复杂度翻倍。共享密钥而非分字段存储，是因为同一时刻只有一个 provider 活跃，分字段只增加配置噪音。

## Considered Options

- **LLM 侧选择**：给工具参数加 `provider` 字段。拒绝原因：浪费推理预算，模型大概率选不好，工具定义随 provider 变化。
- **自动路由 + 降级**：默认 provider 失败后 fallback 到另一个。拒绝原因：两个 provider 的 API key、计费、速率限制不同，熔断器复杂度翻倍，第一版不值得。
- **分字段 API Key**：`tavily_api_key` + `exa_api_key` 并存。拒绝原因：同一时刻只有一个 provider 活跃，多余字段是配置噪音。
