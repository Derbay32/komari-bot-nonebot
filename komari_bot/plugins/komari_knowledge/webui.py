"""
Komari Knowledge 管理界面。

使用 Streamlit 提供可视化的知识库管理功能。

启动方式：
    streamlit run komari_bot/plugins/komari_knowledge/webui.py
"""

import asyncio
import atexit
import importlib.util
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

# 强制使用标准 asyncio，避免 uvloop 在 Streamlit 退出时的错误
sys.modules["uvloop"] = None  # type: ignore[reportGeneralTypeIssues]

import streamlit as st

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

# 首先加载 config_schema
config_spec = importlib.util.spec_from_file_location(
    "komari_bot.plugins.komari_knowledge.config_schema",
    Path(__file__).parent / "config_schema.py",
)
if config_spec is None:
    raise RuntimeError("无法加载 config_schema 模块")  # noqa:TRY003
if config_spec.loader is None:
    raise RuntimeError("config_schema 模块加载器为空")  # noqa:TRY003
config_module = importlib.util.module_from_spec(config_spec)
sys.modules["komari_bot.plugins.komari_knowledge.config_schema"] = config_module
config_spec.loader.exec_module(config_module)

# 然后加载 engine（设置正确的包上下文以支持相对导入）
engine_spec = importlib.util.spec_from_file_location(
    "komari_bot.plugins.komari_knowledge.engine",
    Path(__file__).parent / "engine.py",
)
if engine_spec is None:
    raise RuntimeError("无法加载 engine 模块")  # noqa:TRY003
if engine_spec.loader is None:
    raise RuntimeError("engine 模块加载器为空")  # noqa:TRY003
engine_module = importlib.util.module_from_spec(engine_spec)
engine_module.__package__ = "komari_bot.plugins.komari_knowledge"
sys.modules["komari_bot.plugins.komari_knowledge.engine"] = engine_module
engine_spec.loader.exec_module(engine_module)

KnowledgeEngine = engine_module.KnowledgeEngine
_active_context = None


class GlobalContext:
    def __init__(self) -> None:
        # 创建一个新的事件循环
        self.loop = asyncio.new_event_loop()
        self.engine: KnowledgeEngine | None = None
        self._closed = False

        # 定义一个在后台运行 Loop 的函数
        def start_background_loop(loop: asyncio.AbstractEventLoop) -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()  # 这个 loop 会一直运行，直到进程结束

        # 使用 threading 启动后台线程
        self.thread = threading.Thread(
            target=start_background_loop, args=(self.loop,), daemon=True
        )
        self.thread.start()

        # 在后台线程中初始化引擎（阻塞等待完成）
        future = asyncio.run_coroutine_threadsafe(self._init_engine(), self.loop)
        future.result()  # 等待初始化完成

    async def _init_engine(self) -> None:
        engine = KnowledgeEngine()
        await engine.initialize()
        self.engine = engine

    async def _close_engine(self) -> None:
        if self.engine is not None:
            await self.engine.close()
            self.engine = None

    def shutdown(self) -> None:
        """关闭后台引擎与事件循环。"""
        if self._closed:
            return

        self._closed = True
        if self.loop.is_closed():
            return

        try:
            future = asyncio.run_coroutine_threadsafe(self._close_engine(), self.loop)
            future.result(timeout=5)
        except FutureTimeoutError:
            pass
        except Exception:
            pass

        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)

        if not self.thread.is_alive() and not self.loop.is_closed():
            self.loop.close()


# 使用 st.cache_resource 确保全局只有一个后台线程在跑
@st.cache_resource(hash_funcs={GlobalContext: lambda _: None})
def get_global_context() -> GlobalContext:
    global _active_context  # noqa: PLW0603
    context = GlobalContext()
    _active_context = context
    return context


def _shutdown_active_context() -> None:
    global _active_context  # noqa: PLW0603
    if _active_context is None:
        return
    _active_context.shutdown()
    _active_context = None


atexit.register(_shutdown_active_context)


def run_async(coro):  # noqa: ANN001, ANN201
    """将协程提交给后台线程的 Loop 执行，并等待结果。"""
    ctx = get_global_context()

    # 将任务提交给后台线程
    future = asyncio.run_coroutine_threadsafe(coro, ctx.loop)

    # 在主线程等待结果
    try:
        return future.result()
    except Exception as e:
        st.error(f"异步任务执行出错: {e}")
        raise


def get_engine() -> KnowledgeEngine:
    ctx = get_global_context()
    # 告诉类型检查器 engine 一定初始化好了
    assert ctx.engine is not None
    return ctx.engine


# 页面配置
st.set_page_config(
    page_title="小鞠常识库管理",
    page_icon="🧠",
    layout="wide",
)

# 自定义 CSS
st.markdown(
    """
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #FF6B9D;
        text-align: center;
        margin-bottom: 1rem;
    }
    .result-card {
        background-color: #F0F2F6;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 0.5rem 0;
        border-left: 4px solid #FF6B9D;
    }
    .keyword-tag {
        display: inline-block;
        background-color: #E3F2FD;
        color: #1976D2;
        padding: 0.25rem 0.5rem;
        border-radius: 0.25rem;
        margin: 0.25rem;
        font-size: 0.875rem;
    }
    .layer1-badge {
        background-color: #C8E6C9;
        color: #2E7D32;
        padding: 0.25rem 0.5rem;
        border-radius: 0.25rem;
        font-size: 0.75rem;
        font-weight: bold;
    }
    .layer2-badge {
        background-color: #FFF9C4;
        color: #F57F17;
        padding: 0.25rem 0.5rem;
        border-radius: 0.25rem;
        font-size: 0.75rem;
        font-weight: bold;
    }
</style>
""",
    unsafe_allow_html=True,
)


def main() -> None:
    """主界面。"""
    # 初始化 session_state
    if "editing_kid" not in st.session_state:
        st.session_state.editing_kid = None
    if "reconnected" not in st.session_state:
        st.session_state.reconnected = False

    if st.session_state.reconnected:
        st.success("已重新连接！")
        st.session_state.reconnected = False

    st.markdown(
        '<h1 class="main-header">🧠 小鞠常识库管理</h1>', unsafe_allow_html=True
    )

    # 侧边栏配置
    with st.sidebar:
        st.header("⚙️ 配置")
        st.info(
            """
            常识库配置从 `config/config_manager/komari_knowledge_config.json` 读取。

            数据库连接优先读取 `config/config_manager/database_config.json`。
            若 `komari_knowledge_config.json` 中填写了 `pg_*` 字段，则会覆盖共享配置。

            关键配置项：
            - `pg_host`: 数据库主机
            - `pg_port`: 数据库端口
            - `pg_database`: 数据库名称
            - `pg_user`: 数据库用户名
            - `pg_password`: 数据库密码
            """
        )

        # 刷新按钮
        if st.button("🔄 重新连接"):
            get_global_context().shutdown()
            get_global_context.clear()
            st.session_state.reconnected = True
            st.rerun()

    # 初始化引擎
    try:
        engine = get_engine()
        st.success("✅ 数据库已连接")
    except Exception as e:
        st.error(f"❌ 数据库连接失败：{e}")
        st.stop()

    # 标签页
    tab1, tab2, tab3 = st.tabs(["📝 录入知识", "🔍 检索测试", "📚 知识列表"])

    # --- 标签页 1: 录入知识 ---
    with tab1:
        st.header("录入新知识")

        # 初始化表单状态
        if "form_submitted" not in st.session_state:
            st.session_state.form_submitted = False
        if "last_kid" not in st.session_state:
            st.session_state.last_kid = None

        # 显示上次的成功消息
        if st.session_state.form_submitted and st.session_state.last_kid:
            st.success(f"✅ 知识已添加！ID: {st.session_state.last_kid}")

        with st.form("add_knowledge_form", clear_on_submit=True):
            content = st.text_area(
                "知识内容",
                placeholder="例如：小鞠非常喜欢布丁，每次看到布丁都会很开心。",
                height=100,
                help="这将注入到 Bot 的 Prompt 中",
            )

            keywords_input = st.text_input(
                "关键词（用逗号分隔）",
                placeholder="小鞠,布丁,喜欢",
                help="用于快速匹配，多个关键词用逗号分隔",
            )

            col1, col2 = st.columns(2)
            with col1:
                category = st.selectbox(
                    "分类",
                    ["general", "character", "setting", "plot", "other"],
                    help="知识的分类",
                )
            with col2:
                notes = st.text_input("备注（可选）", placeholder="初始设定")

            submitted = st.form_submit_button("➕ 添加知识", type="primary")  # noqa: RUF001

            if submitted:
                if not content or not content.strip():
                    st.error("❌ 知识内容不能为空")
                elif not keywords_input or not keywords_input.strip():
                    st.error("❌ 关键词不能为空")
                else:
                    keywords = [
                        k.strip() for k in keywords_input.split(",") if k.strip()
                    ]

                    try:
                        kid = run_async(
                            engine.add_knowledge(
                                content=content,
                                keywords=keywords,
                                category=category,
                                notes=notes if notes else None,
                            )
                        )
                        # 记录成功状态
                        st.session_state.form_submitted = True
                        st.session_state.last_kid = kid
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ 添加失败：{e}")
                        st.session_state.form_submitted = False

    # --- 标签页 2: 检索测试 ---
    with tab2:
        st.header("检索测试")

        query = st.text_input(
            "输入查询文本",
            placeholder="例如：小鞠喜欢吃什么？",
            help="测试混合检索功能",
        )

        col1, col2 = st.columns(2)
        with col1:
            limit = st.slider("返回数量", 1, 10, 5)
        with col2:
            if st.button("🔍 检索"):
                st.rerun()

        if query and query.strip():
            results = run_async(engine.search(query, limit=limit))

            if results:
                st.success(f"✅ 找到 {len(results)} 条相关知识")

                for i, result in enumerate(results, 1):
                    badge_class = (
                        "layer1-badge" if result.source == "keyword" else "layer2-badge"
                    )
                    source_text = (
                        "关键词匹配" if result.source == "keyword" else "向量检索"
                    )

                    st.markdown(
                        f"""
                        <div class="result-card">
                            <strong>[{i}]</strong>
                            <span class="{badge_class}">{source_text}</span>
                            <span style="color: #666; font-size: 0.875rem;">
                                相似度: {result.similarity:.2f}
                            </span>
                            <p style="margin: 0.5rem 0;">{result.content}</p>
                            <small style="color: #999;">分类: {result.category} | ID: {result.id}</small>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
            else:
                st.warning("⚠️ 未找到相关知识")

    # --- 标签页 3: 知识列表 ---
    with tab3:
        st.header("知识列表")

        # 搜索和筛选
        col1, col2 = st.columns([2, 1])
        with col1:
            search_kw = st.text_input("搜索关键词", placeholder="输入关键词过滤...")
        with col2:
            filter_category = st.selectbox(
                "筛选分类",
                ["全部", "general", "character", "setting", "plot", "other"],
            )

        # 加载知识列表
        all_knowledge = run_async(engine.get_all_knowledge())

        # 应用筛选
        filtered = []
        for item in all_knowledge:
            # 关键词筛选
            if search_kw and search_kw.strip():
                kw_lower = search_kw.lower()
                keywords = item.get("keywords", []) or []
                content = item.get("content", "") or ""

                if (
                    not any(kw_lower in k.lower() for k in keywords)
                    and kw_lower not in content.lower()
                ):
                    continue

            # 分类筛选
            if filter_category != "全部" and item.get("category") != filter_category:
                continue

            filtered.append(item)

        st.info(f"📊 共 {len(filtered)} 条知识")

        # 编辑表单（当有编辑状态时显示）
        if st.session_state.editing_kid is not None:
            # 查找编辑的知识
            edit_item = next(
                (
                    item
                    for item in filtered
                    if item["id"] == st.session_state.editing_kid
                ),
                None,
            )

            if edit_item:
                st.markdown("---")
                st.subheader(f"✏️ 编辑知识 (ID: {edit_item['id']})")

                with st.form("edit_knowledge_form"):
                    edit_content = st.text_area(
                        "知识内容",
                        value=edit_item.get("content", ""),
                        height=100,
                        key="edit_content",
                    )

                    keywords_val = edit_item.get("keywords", []) or []
                    edit_keywords_input = st.text_input(
                        "关键词（用逗号分隔）",
                        value=",".join(keywords_val) if keywords_val else "",
                        key="edit_keywords",
                    )

                    col1, col2 = st.columns(2)
                    with col1:
                        edit_category = st.selectbox(
                            "分类",
                            ["general", "character", "setting", "plot", "other"],
                            index=[
                                "general",
                                "character",
                                "setting",
                                "plot",
                                "other",
                            ].index(edit_item.get("category", "general")),
                            key="edit_category",
                        )
                    with col2:
                        edit_notes = st.text_input(
                            "备注（可选）",
                            value=edit_item.get("notes", "") or "",
                            key="edit_notes",
                        )

                    col_a, col_b = st.columns(2)
                    with col_a:
                        if st.form_submit_button("💾 保存修改", type="primary"):
                            if not edit_content or not edit_content.strip():
                                st.error("❌ 知识内容不能为空")
                            elif (
                                not edit_keywords_input
                                or not edit_keywords_input.strip()
                            ):
                                st.error("❌ 关键词不能为空")
                            else:
                                edit_keywords = [
                                    k.strip()
                                    for k in edit_keywords_input.split(",")
                                    if k.strip()
                                ]

                                try:
                                    success = run_async(
                                        engine.update_knowledge(
                                            kid=edit_item["id"],
                                            content=edit_content,
                                            keywords=edit_keywords,
                                            category=edit_category,
                                            notes=edit_notes if edit_notes else None,
                                        )
                                    )
                                    if success:
                                        st.success("✅ 修改已保存")
                                        st.session_state.editing_kid = None
                                        st.rerun()
                                    else:
                                        st.error("❌ 保存失败")
                                except Exception as e:
                                    st.error(f"❌ 保存失败：{e}")

                    with col_b:
                        if st.form_submit_button("❌ 取消"):
                            st.session_state.editing_kid = None
                            st.rerun()

                st.markdown("---")

        # 显示列表
        for item in filtered:
            with st.expander(f"📌 {item['content'][:50]}... (ID: {item['id']})"):
                st.markdown(f"**内容：** {item['content']}")

                keywords = item.get("keywords", []) or []
                if keywords:
                    kw_tags = " ".join(
                        f'<span class="keyword-tag">{kw}</span>' for kw in keywords
                    )
                    st.markdown(f"**关键词：** {kw_tags}", unsafe_allow_html=True)

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.write(f"**分类：** {item.get('category', 'general')}")
                with col2:
                    created = item.get("created_at", "")
                    if created:
                        # created 可能是 datetime 对象或字符串
                        if hasattr(created, "strftime"):
                            created_str = created.strftime("%Y-%m-%d")
                        else:
                            created_str = str(created)[:10]
                        st.write(f"**创建：** {created_str}")
                with col3:
                    col_edit, col_del = st.columns(2)
                    with col_edit:
                        if st.button("✏️", key=f"edit_{item['id']}", help="编辑"):
                            st.session_state.editing_kid = item["id"]
                            st.rerun()
                    with col_del:
                        if st.button("🗑️", key=f"del_{item['id']}", help="删除"):
                            try:
                                success = run_async(engine.delete_knowledge(item["id"]))
                                if success:
                                    st.success("✅ 已删除")
                                    st.rerun()
                                else:
                                    st.error("❌ 删除失败")
                            except Exception as e:
                                st.error(f"❌ 删除失败：{e}")

                notes = item.get("notes")
                if notes:
                    st.caption(f"备注：{notes}")


if __name__ == "__main__":
    main()
