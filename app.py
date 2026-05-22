"""PaperPilot - 面向课题攻关的可解释智能文献工作流系统。
Phase 1: 课题输入 → 关键词提取 → 论文抓取 → 排序展示
"""

import asyncio

import flet as ft

from paperpilot.keywords import extract_keywords, merge_keywords
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


state = AppState()
_page: ft.Page | None = None
results_summary: ft.Text | None = None


def _run_pipeline():
    """在后台线程中运行完整的搜索流水线。"""
    papers = []
    if arxiv_switch.value:
        state.status_text = "arXiv 抓取中..."
        papers += fetch_arxiv(state.keywords, max_results=int(max_results_slider.value))
    if openalex_switch.value:
        state.status_text = "OpenAlex 抓取中..."
        papers += fetch_openalex(state.keywords, max_results=int(max_results_slider.value))

    state.status_text = "去重中..."
    papers = deduplicate(papers)

    if not papers:
        return [], []

    state.status_text = f"构建索引中（{len(papers)} 篇）..."
    idx, indexed_papers = build_index(papers)

    state.status_text = "排序中..."
    scores = search_similar(state.topic_desc, idx, indexed_papers, top_k=20)

    return papers, scores


# ── 导航栏 ──
def build_nav(page_index: int) -> ft.Container:
    """顶部导航栏，点击切换页面。"""
    tabs = [
        ("课题", ft.Icons.EDIT_NOTE),
        ("推荐", ft.Icons.FORMAT_LIST_NUMBERED),
        ("设置", ft.Icons.SETTINGS),
    ]

    def on_tab_click(e):
        idx = e.control.data
        page_switcher(idx)

    buttons = []
    for i, (label, icon) in enumerate(tabs):
        is_active = i == page_index
        buttons.append(
            ft.Button(
                content=ft.Text(label),
                icon=icon,
                data=i,
                on_click=on_tab_click,
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.PRIMARY_CONTAINER if is_active else None,
                    color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None,
                ),
            )
        )
    return ft.Container(
        content=ft.Row(buttons, spacing=8, alignment=ft.MainAxisAlignment.CENTER),
        padding=ft.padding.Padding(top=16, bottom=8),
    )


# ── 页面切换 ──
def page_switcher(idx: int):
    """切换三个页面容器。"""
    container_project.visible = idx == 0
    container_results.visible = idx == 1
    container_settings.visible = idx == 2
    # 同步更新导航栏
    nav_container.content = build_nav(idx)
    nav_container.update()
    container_project.update()
    container_results.update()
    container_settings.update()


# ── 课题页 ──
def build_project_page():
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

    def on_extract(e):
        desc = topic_desc_field.value.strip()
        if not desc:
            status_text.value = "请先输入课题描述"
            status_text.update()
            return
        status_text.value = "正在提取关键词..."
        status_text.update()
        try:
            kw = extract_keywords(desc, top_n=8)
            state.keywords = list(kw)
            refresh_keyword_chips(keywords_row, manual_kw_field)
            status_text.value = f"已提取 {len(kw)} 个关键词"
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
                    if results_summary and results_summary.page:
                        results_summary.value = f"课题：{state.topic_name}  |  检索到 0 篇论文  |  关键词：{', '.join(state.keywords[:5])}"
                        results_summary.update()
                else:
                    state.papers = papers
                    state.scores = scores
                    state.status_text = f"完成！共 {len(scores)} 篇"
                    refresh_results_table()
                    if results_summary and results_summary.page:
                        results_summary.value = f"课题：{state.topic_name}  |  检索到 {len(state.scores)} 篇论文  |  关键词：{', '.join(state.keywords[:5])}"
                        results_summary.update()
                    page_switcher(1)
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

    def refresh_keyword_chips(row: ft.Row, kw_field: ft.TextField):
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

    return ft.Column([
        ft.Text("PaperPilot", size=28, weight=ft.FontWeight.BOLD),
        ft.Text("课题驱动的智能文献筛选", size=14, italic=True),
        ft.Divider(height=20),
        ft.Text("课题信息", size=16, weight=ft.FontWeight.W_500),
        topic_name_field,
        topic_desc_field,
        ft.Row([
            ft.FilledTonalButton(content=ft.Text("提取关键词"), icon=ft.Icons.AUTO_AWESOME, on_click=on_extract),
            manual_kw_field,
        ], spacing=8),
        keywords_row,
        ft.Divider(height=12),
        ft.Row([search_btn, progress_bar], spacing=16),
        status_text,
    ], spacing=8, scroll=ft.ScrollMode.AUTO)


def refresh_keyword_chips(row: ft.Row, kw_field: ft.TextField):
    """刷新关键词标签列表（从 build_project_page 引用）。"""
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
                    ft.DataCell(ft.Text(paper.get("title", "")[:80])),
                    ft.DataCell(ft.Text((paper.get("authors") or "")[:40])),
                    ft.DataCell(ft.Text(year_str)),
                    ft.DataCell(ft.Text(src)),
                    ft.DataCell(score_widget),
                ],
                on_select_changed=lambda e, p=paper: show_paper_detail(p),
            )
        )
    results_table.update()


paper_detail = ft.Container(
    content=ft.Column([
        ft.Text("", size=20, weight=ft.FontWeight.BOLD),
        ft.Text(""),
        ft.Text(""),
        ft.Text("", size=12, color=ft.Colors.PRIMARY),
    ], spacing=8),
    padding=16,
    border=_border(ft.Colors.OUTLINE_VARIANT),
    border_radius=8,
    visible=False,
)


def show_paper_detail(paper: dict):
    paper_detail.content.controls[0].value = paper.get("title", "")
    paper_detail.content.controls[1].value = f"作者: {paper.get('authors', '未知')}  |  年份: {paper.get('year', '—')}  |  来源: {paper.get('source', '')}"
    paper_detail.content.controls[2].value = paper.get("abstract", "") or "（无摘要）"
    paper_detail.content.controls[3].value = f"链接: {paper['url']}" if paper.get("url") else ""
    paper_detail.visible = True
    paper_detail.update()


def build_results_page():
    def _summary_text():
        return f"课题：{state.topic_name}  |  检索到 {len(state.scores)} 篇论文  |  关键词：{', '.join(state.keywords[:5])}"
    global results_summary
    summary = ft.Text(_summary_text(), size=14)
    results_summary = summary

    def refresh_summary():
        summary.value = _summary_text()
        if summary.page:
            summary.update()

    return ft.Column([
        ft.Text("检索结果", size=22, weight=ft.FontWeight.BOLD),
        summary,
        ft.Divider(height=12),
        ft.Text("点击某行查看完整摘要", size=12, italic=True),
        results_table,
        paper_detail,
    ], spacing=8, expand=True, scroll=ft.ScrollMode.AUTO)


# ── 设置页 ──
arxiv_switch = ft.Switch(label="arXiv", value=True)
openalex_switch = ft.Switch(label="OpenAlex", value=True)
max_results_slider = ft.Slider(min=10, max=50, value=30, divisions=8,
                                label="{value} 篇")


def build_settings_page():
    return ft.Column([
        ft.Text("设置", size=22, weight=ft.FontWeight.BOLD),
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
    global _page
    _page = page
    page.title = "PaperPilot"
    page.window.width = 1100
    page.window.height = 750
    page.window.min_width = 800
    page.window.min_height = 500
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 24

    global nav_container, container_project, container_results, container_settings

    nav_container = ft.Container(content=build_nav(0))

    container_project = ft.Container(
        content=build_project_page(), visible=True, expand=True,
    )
    container_results = ft.Container(
        content=build_results_page(), visible=False, expand=True,
    )
    container_settings = ft.Container(
        content=build_settings_page(), visible=False, expand=True,
    )

    page.add(
        nav_container,
        ft.Divider(height=4),
        ft.Stack([
            container_project,
            container_results,
            container_settings,
        ], expand=True),
    )


if __name__ == "__main__":
    ft.run(main)
