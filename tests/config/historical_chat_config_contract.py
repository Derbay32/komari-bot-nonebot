"""0032 聊天配置的已退役字段默认值，仅供历史迁移测试。"""

HISTORICAL_RETIRED_CHAT_DEFAULTS: dict[str, object] = {
    "agent_tool_call_mode": "required",
    "assistant_prefill_enabled": False,
    "dsv4_roleplay_instruct_mode": "auto",
    "image_understanding_mode": "delegated",
    "llm_max_tokens_chat": 4000,
    "llm_model_chat": "gemini-3-flash-preview",
    "llm_reasoning_effort_chat": "",
    "llm_request_api_chat": "chat_completions",
    "llm_stream_enabled_chat": False,
    "llm_temperature_chat": 1.0,
    "llm_thinking_mode_chat": False,
    "query_rewrite_model": "deepseek-v4-flash",
    "query_rewrite_reasoning_effort": "",
    "query_rewrite_request_api": "chat_completions",
    "query_rewrite_stream_enabled": False,
    "query_rewrite_thinking_mode": False,
}
