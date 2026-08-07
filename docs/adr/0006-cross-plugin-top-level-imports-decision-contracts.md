# 跨插件引用只走顶层暴露面，判定契约类型下沉共享层

插件间曾直接深 import 彼此的内部子模块（如 `komari_chat` import `komari_decision/services/decision_engine.py`），以换取 pyright 完整类型信息。我们确立边界规则：**跨插件引用遵循 NoneBot2 官方姿势——`require()` 仅作依赖加载声明，实际引用走普通 `import`，且 import 只允许指向依赖插件的顶层包（`__init__.py` 暴露面），禁止指向其内部子模块；被多方以类型身份引用的纯契约符号不属于任一插件的实现，统一下沉到顶层共享包 `komari_bot/decision/`**。decision 插件的 `DecisionEngine` 构造权收归本插件（惰性 `get_decision_engine()` 顶层导出），`komari_chat` 不再越权组装引擎。

## Considered Options

- **保持深 import 换类型**：被拒绝——插件 `services/` 内部实现对消费方暴露，重构任一插件的内部结构都会波及跨插件消费者，边界形同虚设。
- **经 `require()` 返回的模块属性访问**（`decision_plugin.get_decision_engine`）：被拒绝——`require()` 返回值对 pyright 是松散类型，官方文档（nonebot.dev/docs/advanced/requiring）推荐的也是 require 声明 + 顶层 import 的组合。
- **DecisionEngine 类整体下沉共享层**：被拒绝——引擎构造签名引用 `komari_memory` 的 `RedisManager` 与本插件的 `SceneRuntimeService`，下沉会把插件类型网拖进共享层，破坏共享层「无 NoneBot 依赖」的成色。
- **场景 CRUD 也下沉或经 `SceneRepository` 顶层直通**：被拒绝——Repository 是 PG 仓储实现，不是契约；管理 API 需要的 `list_scenes` / `get_scene_by_key` / `upsert_scene` 由 `SceneAdminService` 补齐后经顶层 `get_scene_admin_service()` 暴露。

## Consequences

- 新增第 8 个顶层共享包 `komari_bot/decision/`（延续 ADR-0005「按边界选桶」规则），承载 `DecisionOutcome` 及 Literal 别名、`DecisionRuntimeState/Status`、`UnifiedRerankResult`、`CandidateSchema`、`SceneRuntimeUnavailableError`、`TimingScoreBreakdown`、`FilterResult` 与 `DecisionEngineProtocol`；以上全部保持零依赖纯件成色，算法实现（rerank、timing 打分、preprocess、引擎本体）原样留在插件内。
- `komari_decision/__init__.py` 新增 `get_decision_engine()` 惰性导出（依赖身份变化时重建、未就绪返回 `None`，语义与原 `komari_chat._get_or_build_handler` 逐字节等价）；`get_runtime_state` 导出随唯一消费方（komari_chat）迁移完成而退役。
- `komari_management/scene_api.py` 的 `_fallback_repository` 旁路（decision 未就绪时自建 Repository 直连 PG）删除；decision 未就绪时场景管理 API 统一报服务未就绪，与既有 scene sync 报错风格一致。
- 例外：管理插件 import 各插件 `config_schema.py` 注册管理资源是 config 体系既定模式（14 个插件统一如此），不视为违规，不在本规则约束范围。
- 测试不搬家（引擎与仓储测试仍在 `tests/komari_decision/`），仅做 import 机械改写；`tests/conftest.py` 的 decision 插件桩补 `get_decision_engine`。
