"""PaperPilot - 面向课题攻关的可解释智能文献工作流系统。
Phase 1: 课题输入 → 关键词提取 → 论文抓取 → 排序展示
"""

import asyncio

import flet as ft

from paperpilot.keywords import extract_all_keywords, merge_keywords
from paperpilot.mt_translator import translate_terms
from paperpilot.fetcher import fetch_arxiv, fetch_openalex, deduplicate
from paperpilot.indexer import build_index, search_similar


def _border(color):
    """Flet 0.85 兼容的边框辅助函数。"""
    side = ft.BorderSide(1, color)
    return ft.Border(side, side, side, side)


# ── 全局状态 ──
class AppState:
    def __init__(self):
        self.topic_name: str = ""
        self.topic_desc: str = ""
        self.keywords: list[str] = []
        self.papers: list[dict] = []
        self.scores: list[tuple[dict, float]] = []
        self.is_searching: bool = False
        self.status_text: str = ""
        self.selected_paper: dict | None = None


state = AppState()
_page: ft.Page | None = None
results_summary: ft.Text | None = None
detail_sidebar: ft.Container | None = None  # 右侧文献详情侧边栏


def _has_cjk(text: str) -> bool:
    """检测文本是否包含中日韩字符，用于区分中英文关键词。"""
    return any('一' <= c <= '鿿' for c in text)


def _run_pipeline():
    """在后台线程中运行完整的搜索流水线。"""
    papers = []
    max_per = int(max_results_slider.value)

    # 翻译中文关键词为英文
    search_terms = list(state.keywords)
    en_terms = translate_terms(state.keywords)
    valid_en = [t for t in en_terms if t]
    if valid_en:
        seen = set(search_terms)
        for t in valid_en:
            if t.lower() not in seen:
                seen.add(t.lower())
                search_terms.append(t)
        print(f"[PaperPilot] 翻译: {len(valid_en)} 个英文术语")
    else:
        print("[PaperPilot] 警告: 翻译失败，所有英文术语为空（检查 API Key 或网络）")

    # 为英文数据源过滤掉中文关键词
    en_search_terms = [t for t in search_terms if t and not _has_cjk(t)]
    if not en_search_terms:
        # 翻译完全失败，没有可用的英文搜索词 → 直接跳过数据源检索
        print("[PaperPilot] 错误: 没有可用的英文搜索词，跳过 arXiv 和 OpenAlex 检索")
        print("[PaperPilot] 提示: 请在设置页配置有效的 DeepSeek API Key")
        return [], []

    print(f"\n[PaperPilot] 开始检索，关键词: {state.keywords}")
    print(f"[PaperPilot] 英文搜索词: {en_search_terms[:10]}...")
    print(f"[PaperPilot] 课题描述: {state.topic_desc[:80]}...")

    if arxiv_switch.value:
        state.status_text = "arXiv 抓取中..."
        print(f"[PaperPilot] arXiv 查询: {' AND '.join(en_search_terms)}")
        try:
            results = fetch_arxiv(en_search_terms, max_results=max_per)
            print(f"[PaperPilot] arXiv 返回: {len(results)} 篇")
            papers += results
        except Exception as e:
            print(f"[PaperPilot] arXiv 失败: {e}")

    if openalex_switch.value:
        state.status_text = "OpenAlex 抓取中..."
        # OpenAlex: 过滤掉单字泛词（Battery/Ion/Aqueous），保留词组
        oa_terms = [t for t in en_search_terms if " " in t or "-" in t]
        if not oa_terms:
            oa_terms = en_search_terms
        oa_query = " OR ".join(f'"{kw}"' for kw in oa_terms)
        print(f"[PaperPilot] OpenAlex 查询: {oa_query[:200]}")
        try:
            results = fetch_openalex([oa_query], max_results=max_per)
            print(f"[PaperPilot] OpenAlex 返回: {len(results)} 篇")
            papers += results
        except Exception as e:
            print(f"[PaperPilot] OpenAlex 失败: {e}")

    state.status_text = "去重中..."
    papers = deduplicate(papers)
    print(f"[PaperPilot] 去重后: {len(papers)} 篇")

    if not papers:
        print("[PaperPilot] 未找到论文，尝试用课题描述直接搜索...")
        # 翻译课题描述用于兜底搜索
        desc = state.topic_desc.strip()
        desc_en_terms = translate_terms([desc])
        desc_en = [t for t in desc_en_terms if t and not _has_cjk(t)]
        if desc_en:
            desc_kw = desc_en
            print(f"[PaperPilot] 描述兜底搜索（英文）: {desc_kw[0][:80]}...")
        else:
            # 翻译也失败了，课题描述中若纯英文可用，否则无计可施
            if not _has_cjk(desc):
                desc_kw = [desc[:200]]
                print(f"[PaperPilot] 描述兜底搜索（原文）: {desc_kw[0][:80]}...")
            else:
                print("[PaperPilot] 无法生成英文兜底查询，放弃搜索")
                return [], []

        if arxiv_switch.value:
            try:
                results = fetch_arxiv(desc_kw, max_results=max_per)
                print(f"[PaperPilot] 描述搜索 arXiv: {len(results)} 篇")
                papers += results
            except Exception as e:
                print(f"[PaperPilot] 描述搜索 arXiv 失败: {e}")
        if openalex_switch.value:
            try:
                results = fetch_openalex(desc_kw, max_results=max_per)
                print(f"[PaperPilot] 描述搜索 OpenAlex: {len(results)} 篇")
                papers += results
            except Exception as e:
                print(f"[PaperPilot] 描述搜索 OpenAlex 失败: {e}")
        papers = deduplicate(papers)
        print(f"[PaperPilot] 描述搜索去重后: {len(papers)} 篇")

    if not papers:
        return [], []

    state.status_text = f"构建索引中（{len(papers)} 篇）..."
    idx, indexed_papers = build_index(papers)

    state.status_text = "排序中..."
    scores = search_similar(state.topic_desc, idx, indexed_papers, top_k=20)

    return papers, scores


# ── 左侧导航栏 ──
NAV_ITEMS = [
    ("课题", ft.Icons.EDIT_NOTE, 0),
    ("文献", ft.Icons.FORMAT_LIST_NUMBERED, 1),
    ("设置", ft.Icons.SETTINGS, 2),
]


def build_left_nav(active_idx: int) -> ft.Column:
    """生成左侧导航栏内容列，页面切换时替换此列即可。"""

    def on_nav_click(e):
        idx = e.control.data
        page_switcher(idx)

    nav_buttons = []
    for label, icon, idx in NAV_ITEMS:
        is_active = idx == active_idx
        nav_buttons.append(
            ft.TextButton(
                content=ft.Row([
                    ft.Icon(icon, size=20,
                            color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                    ft.Text(label, size=14,
                           weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL,
                           color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                ], spacing=10),
                data=idx,
                on_click=on_nav_click,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.PRIMARY_CONTAINER if is_active else None,
                    padding=ft.padding.Padding(left=16, top=12, right=16, bottom=12),
                ),
            )
        )

    return ft.Column([
        ft.Text("PaperPilot", size=18, weight=ft.FontWeight.BOLD),
        ft.Divider(height=20),
        *nav_buttons,
    ], spacing=4)


# ── 页面切换 ──
def page_switcher(idx: int):
    """切换页面容器。"""
    container_project.visible = idx == 0
    container_results.visible = idx == 1
    container_settings.visible = idx == 2

    nav_content_ref.content = build_left_nav(idx)
    nav_content_ref.update()
    container_project.update()
    container_results.update()
    container_settings.update()


# ── 课题页 ──
def build_project_page():
    global results_summary, detail_sidebar

    topic_name_field = ft.TextField(
        label="课题名称", hint_text="例如：钙钛矿太阳能电池稳定性",
        prefix_icon=ft.Icons.TITLE, expand=True,
    )
    topic_desc_field = ft.TextField(
        label="课题描述", hint_text="输入 1-3 句描述研究方向，用于论文匹配",
        prefix_icon=ft.Icons.DESCRIPTION, multiline=True, min_lines=3, max_lines=5,
        expand=True,
    )

    keywords_row = ft.Row(wrap=True, spacing=6)
    manual_kw_field = ft.TextField(
        label="手动添加关键词", hint_text="输入后回车添加",
        prefix_icon=ft.Icons.ADD, expand=True,
    )
    progress_bar = ft.ProgressBar(visible=False, expand=True)
    status_text = ft.Text("", size=13, italic=True)

    # ── 文献详情侧边栏 ──
    sb_title = ft.Text("", size=18, weight=ft.FontWeight.BOLD)
    sb_meta = ft.Text("", size=13)
    sb_abstract = ft.Text("", size=13)
    sb_url = ft.Text("", size=12, color=ft.Colors.PRIMARY)

    def on_close_sidebar(e):
        detail_sidebar.visible = False
        detail_sidebar.update()

    sidebar = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Text("文献详情", size=16, weight=ft.FontWeight.W_600),
                ft.IconButton(icon=ft.Icons.CLOSE, on_click=on_close_sidebar),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(height=8),
            sb_title,
            sb_meta,
            ft.Divider(height=8),
            ft.Text("摘要", size=14, weight=ft.FontWeight.W_500),
            ft.Container(content=sb_abstract, expand=True),
            ft.Divider(height=8),
            sb_url,
        ], spacing=6),
        width=400,
        padding=ft.padding.Padding(left=16, top=12, right=16, bottom=12),
        border=_border(ft.Colors.OUTLINE_VARIANT),
        border_radius=8,
        visible=False,
    )
    sidebar._title = sb_title
    sidebar._meta = sb_meta
    sidebar._abstract = sb_abstract
    sidebar._url = sb_url
    detail_sidebar = sidebar

    # ── 检索结果区域 ──
    def _summary_text():
        return (
            f"课题：{state.topic_name}  |  检索到 {len(state.scores)} 篇论文  |  "
            f"关键词：{', '.join(state.keywords[:5])}"
        )

    summary = ft.Text(_summary_text(), size=14)
    results_summary = summary

    results_area = ft.Column([
        ft.Divider(height=16),
        ft.Text("检索结果", size=22, weight=ft.FontWeight.BOLD),
        summary,
        ft.Divider(height=8),
        ft.Text("点击某行查看详情", size=12, italic=True),
        ft.Row([
            ft.Column([results_table], expand=True, scroll=ft.ScrollMode.AUTO),
            sidebar,
        ], spacing=8, expand=True),
    ], spacing=6, expand=True, visible=False)

    def on_extract(e):
        desc = topic_desc_field.value.strip()
        if not desc:
            status_text.value = "请先输入课题描述"
            status_text.update()
            return
        status_text.value = "正在提取关键词..."
        status_text.update()
        try:
            weighted = extract_all_keywords(desc, top_n=8)
            state.keywords = [kw for kw, _ in weighted]
            refresh_keyword_chips(keywords_row, manual_kw_field)
            status_text.value = f"已提取 {len(state.keywords)} 个关键词"
        except Exception as ex:
            status_text.value = f"提取失败: {ex}"
        status_text.update()

    def on_add_keyword(e):
        kw = (e.control.value or "").strip()
        if kw:
            state.keywords = merge_keywords(state.keywords, [kw])
            manual_kw_field.value = ""
            manual_kw_field.update()
            refresh_keyword_chips(keywords_row, manual_kw_field)

    manual_kw_field.on_submit = on_add_keyword

    def on_start_search(e):
        if not topic_desc_field.value.strip():
            status_text.value = "请先输入课题描述"
            status_text.update()
            return
        if not state.keywords:
            status_text.value = "请先提取关键词"
            status_text.update()
            return

        state.topic_name = topic_name_field.value.strip() or "未命名课题"
        state.topic_desc = topic_desc_field.value.strip()
        state.is_searching = True
        state.papers = []
        state.scores = []

        progress_bar.visible = True
        search_btn.disabled = True
        status_text.value = "正在抓取论文..."
        progress_bar.update()
        search_btn.update()
        status_text.update()

        async def _do_search():
            loop = asyncio.get_running_loop()
            try:
                papers, scores = await loop.run_in_executor(None, _run_pipeline)

                if not papers:
                    state.status_text = "未找到相关论文"
                    state.papers = []
                    state.scores = []
                    results_table.rows.clear()
                    results_table.update()
                    summary.value = _summary_text()
                    results_area.visible = True
                    results_area.update()
                else:
                    state.papers = papers
                    state.scores = scores
                    state.status_text = f"完成！共 {len(scores)} 篇"
                    refresh_results_table()
                    summary.value = _summary_text()
                    results_area.visible = True
                    results_area.update()
            except Exception as ex:
                state.status_text = f"检索失败: {ex}"
            finally:
                state.is_searching = False
                progress_bar.visible = False
                search_btn.disabled = False
                status_text.value = state.status_text
                progress_bar.update()
                search_btn.update()
                status_text.update()

        _page.run_task(_do_search)

    search_btn = ft.FilledButton(
        content=ft.Text("开始检索"), icon=ft.Icons.SEARCH, on_click=on_start_search,
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=32, top=16, right=32, bottom=16)),
    )

    return ft.Column([
        ft.Text("PaperPilot", size=28, weight=ft.FontWeight.BOLD),
        ft.Text("课题驱动的智能文献筛选", size=14, italic=True),
        ft.Divider(height=20),
        ft.Text("课题信息", size=16, weight=ft.FontWeight.W_500),
        topic_name_field,
        topic_desc_field,
        ft.Row([
            ft.FilledTonalButton(
                content=ft.Text("提取关键词"), icon=ft.Icons.AUTO_AWESOME,
                on_click=on_extract,
            ),
            manual_kw_field,
        ], spacing=8),
        keywords_row,
        ft.Divider(height=12),
        ft.Row([search_btn, progress_bar], spacing=16),
        status_text,
        results_area,
    ], spacing=8, scroll=ft.ScrollMode.AUTO)


def refresh_keyword_chips(row: ft.Row, kw_field: ft.TextField):
    """刷新关键词标签列表。"""
    row.controls.clear()
    for kw in state.keywords:

        def make_delete(k=kw):
            def on_delete(e):
                state.keywords = [k2 for k2 in state.keywords if k2 != k]
                refresh_keyword_chips(row, kw_field)
                row.update()
            return on_delete

        row.controls.append(
            ft.Chip(label=ft.Text(kw), on_delete=make_delete())
        )
    if state.keywords:
        row.controls.append(ft.Text(f"（{len(state.keywords)} 个）", size=12, italic=True))
    row.update()


# ── 文献详情 ──
def show_paper_detail(paper: dict):
    """在右侧侧边栏展示论文详情。"""
    sb = detail_sidebar
    if sb is None:
        return
    sb._title.value = paper.get("title", "")
    source = {"arxiv": "arXiv", "openalex": "OpenAlex", "local_pdf": "本地"}.get(
        paper.get("source", ""), paper.get("source", "")
    )
    sb._meta.value = (
        f"作者: {paper.get('authors', '未知')}  |  "
        f"年份: {paper.get('year', '—')}  |  来源: {source}"
    )
    sb._abstract.value = paper.get("abstract", "") or "（无摘要）"
    sb._url.value = f"链接: {paper['url']}" if paper.get("url") else ""
    sb.visible = True
    sb.update()


# ── 推荐页 ──
results_table = ft.DataTable(
    columns=[
        ft.DataColumn(ft.Text("#"), numeric=True),
        ft.DataColumn(ft.Text("标题"), on_sort=lambda e: sort_table("title")),
        ft.DataColumn(ft.Text("作者"), on_sort=lambda e: sort_table("authors")),
        ft.DataColumn(ft.Text("年份"), numeric=True, on_sort=lambda e: sort_table("year")),
        ft.DataColumn(ft.Text("来源")),
        ft.DataColumn(ft.Text("得分"), numeric=True, on_sort=lambda e: sort_table("score")),
    ],
    rows=[],
    sort_column_index=5,
    sort_ascending=False,
    border=_border(ft.Colors.OUTLINE_VARIANT),
    expand=True,
)
_sort_ascending = False
_sort_column = "score"


def sort_table(column: str):
    global _sort_ascending, _sort_column
    if _sort_column == column:
        _sort_ascending = not _sort_ascending
    else:
        _sort_column = column
        _sort_ascending = False

    key_map = {"title": "title", "authors": "authors", "year": "year", "score": "score"}
    key = key_map.get(column, "score")
    reverse = _sort_ascending

    scored = sorted(state.scores, key=lambda x: (
        x[0].get(key, "") or "" if key != "score" else x[1]
    ), reverse=not reverse if key != "score" else reverse)

    refresh_results_table(scored)


def refresh_results_table(scored=None):
    if scored is None:
        scored = state.scores
    results_table.rows.clear()
    for i, (paper, score) in enumerate(scored):
        year_str = str(paper.get("year") or "—")
        source_label = {"arxiv": "arXiv", "openalex": "OpenAlex", "local_pdf": "本地"}
        src = source_label.get(paper.get("source", ""), paper.get("source", ""))

        color = (
            ft.Colors.GREEN if score >= 0.4
            else ft.Colors.ORANGE if score >= 0.2
            else ft.Colors.OUTLINE
        )
        score_widget = ft.Text(f"{score:.3f}", color=color, weight=ft.FontWeight.BOLD)

        results_table.rows.append(
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(str(i + 1))),
                    ft.DataCell(ft.Container(
                        ft.Text(paper.get("title", "")[:80]),
                        on_click=lambda e, p=paper: show_paper_detail(p),
                    )),
                    ft.DataCell(ft.Text((paper.get("authors") or "")[:40])),
                    ft.DataCell(ft.Text(year_str)),
                    ft.DataCell(ft.Text(src)),
                    ft.DataCell(score_widget),
                ],
            )
        )
    results_table.update()


def build_results_page():
    """文献管理页面（预留，后续开发）。"""
    return ft.Column([
        ft.Text("文献管理", size=22, weight=ft.FontWeight.BOLD),
        ft.Divider(height=16),
        ft.Text("此功能正在开发中，敬请期待。", size=14, italic=True),
    ], spacing=8)


# ── 设置页 ──
arxiv_switch = ft.Switch(label="arXiv", value=True)
openalex_switch = ft.Switch(label="OpenAlex", value=True)
max_results_slider = ft.Slider(min=10, max=200, value=30, divisions=19,
                                label="{value} 篇")


def build_settings_page():
    from paperpilot.config import load_config, save_config as do_save

    current_config = load_config()
    current_key = current_config.get("deepseek", {}).get("api_key", "")

    def mask_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 8:
            return key[:3] + "****" + key[-1:]
        return key[:3] + "****" + key[-4:]

    key_status = ft.Text("", size=12, italic=True)
    if current_key:
        key_status.value = f"已配置: {mask_key(current_key)}"
        key_status.color = ft.Colors.GREEN
    else:
        key_status.value = "未配置 API Key，关键词提取和翻译功能不可用"
        key_status.color = ft.Colors.ORANGE

    api_key_field = ft.TextField(
        label="DeepSeek API Key",
        hint_text="sk-...",
        value=current_key,
        password=True,
        can_reveal_password=True,
        expand=True,
    )

    save_status = ft.Text("", size=13, italic=True)

    def on_save_key(e):
        new_key = api_key_field.value.strip()
        if not new_key:
            save_status.value = "API Key 不能为空"
            save_status.color = ft.Colors.ERROR
            save_status.update()
            return
        do_save(updates={"deepseek": {"api_key": new_key}})
        save_status.value = "API Key 已保存，下次搜索生效"
        save_status.color = ft.Colors.GREEN
        save_status.update()
        key_status.value = f"已配置: {mask_key(new_key)}"
        key_status.color = ft.Colors.GREEN
        key_status.update()

    return ft.Column([
        ft.Text("设置", size=22, weight=ft.FontWeight.BOLD),
        ft.Divider(height=16),
        ft.Text("DeepSeek API", size=16, weight=ft.FontWeight.W_500),
        ft.Text("用于关键词提取和中英翻译，密钥仅存储在本地 config.yaml", size=13, italic=True),
        key_status,
        ft.Row([
            api_key_field,
            ft.FilledTonalButton(
                content=ft.Text("保存"), icon=ft.Icons.SAVE, on_click=on_save_key,
            ),
        ], spacing=8),
        save_status,
        ft.Divider(height=16),
        ft.Text("数据源", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择从哪些来源获取论文", size=13, italic=True),
        arxiv_switch,
        openalex_switch,
        ft.Divider(height=16),
        ft.Text("检索数量", size=16, weight=ft.FontWeight.W_500),
        ft.Text("每个来源的最大检索结果数", size=13, italic=True),
        max_results_slider,
        ft.Divider(height=16),
        ft.Text("离线模式", size=16, weight=ft.FontWeight.W_500),
        ft.Text("Embedding 模型：paraphrase-multilingual-MiniLM-L12-v2", size=13),
        ft.Text("已下载至本地缓存，无需联网", size=13, color=ft.Colors.GREEN),
    ], spacing=8, scroll=ft.ScrollMode.AUTO)


# ── 应用入口 ──
def main(page: ft.Page):
    global _page, nav_content_ref
    global container_project, container_results, container_settings

    _page = page
    page.title = "PaperPilot"
    page.window.width = 1200
    page.window.height = 750
    page.window.min_width = 900
    page.window.min_height = 500
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0

    # 左侧导航容器（内容后续由 page_switcher 动态替换）
    nav_content_ref = ft.Container(
        content=build_left_nav(0),
        width=180,
        padding=ft.padding.Padding(top=16, right=12, bottom=16, left=8),
        border=ft.Border(right=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
    )

    container_project = ft.Container(
        content=build_project_page(), visible=True, expand=True,
        padding=ft.padding.Padding(left=24, top=16, right=16, bottom=16),
    )
    container_results = ft.Container(
        content=build_results_page(), visible=False, expand=True,
        padding=ft.padding.Padding(left=24, top=16, right=24, bottom=16),
    )
    container_settings = ft.Container(
        content=build_settings_page(), visible=False, expand=True,
        padding=ft.padding.Padding(left=24, top=16, right=24, bottom=16),
    )

    page.add(
        ft.Container(
            content=ft.Row([
                nav_content_ref,
                ft.Stack([
                    container_project,
                    container_results,
                    container_settings,
                ], expand=True),
            ], expand=True),
            bgcolor="#E5FFF7",
            expand=True,
        ),
    )


if __name__ == "__main__":
    ft.run(main)
