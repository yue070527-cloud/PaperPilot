"""PaperPilot - 面向课题攻关的可解释智能文献工作流系统。
Phase 1: 课题输入 → 关键词提取 → 论文抓取 → 排序展示
"""

import asyncio

import flet as ft

from paperpilot.keywords import extract_all_keywords, merge_keywords
from paperpilot.mt_translator import translate_terms
from paperpilot.fetcher import fetch_arxiv, fetch_openalex, fetch_multi_primary, deduplicate
from paperpilot.indexer import rank_papers


# ── 主题定义 ──
THEMES = {
    "mint":  {"label": "薄荷绿", "seed": "#00A86B", "light_bg": "#E5FFF7", "dark_bg": "#0D1F17"},
    "ocean": {"label": "海蓝",   "seed": "#1565C0", "light_bg": "#E8F0FE", "dark_bg": "#0D1B2A"},
    "sand":  {"label": "暖沙",   "seed": "#E65100", "light_bg": "#FFF5F0", "dark_bg": "#1E1610"},
    "dusk":  {"label": "暮紫",   "seed": "#7B1FA2", "light_bg": "#F5F0FF", "dark_bg": "#1A1020"},
}
DEFAULT_THEME = "mint"


def apply_theme(page: ft.Page, theme_name: str, dark_mode: bool):
    """应用配色主题和夜间模式。"""
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    seed = theme["seed"]
    bg = theme["dark_bg"] if dark_mode else theme["light_bg"]
    page.theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=bg)
    page.dark_theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=theme["dark_bg"])
    page.theme_mode = ft.ThemeMode.DARK if dark_mode else ft.ThemeMode.LIGHT
    page.update()


def _border(color):
    """Flet 0.85 兼容的边框辅助函数。"""
    side = ft.BorderSide(1, color)
    return ft.Border(side, side, side, side)


# ── 全局状态 ──
class AppState:
    def __init__(self):
        self.topic_name: str = ""
        self.topic_desc: str = ""
        # 三层关键词结构（全部手工拖拽归类）
        self.keywords: list[str] = []               # 扁平列表，向后兼容
        self.primary_keywords: list[str] = []         # 主关键词（拖入）
        self.secondary_keywords: list[str] = []       # 副关键词（拖入）
        self.regular_keywords: list[str] = []         # 普通关键词（默认归属）
        self.papers: list[dict] = []
        self.scores: list[tuple[dict, float]] = []
        self.is_searching: bool = False
        self.status_text: str = ""
        self.selected_paper: dict | None = None
        self.theme_name: str = DEFAULT_THEME
        self.dark_mode: bool = False


state = AppState()
_page: ft.Page | None = None
results_summary: ft.Text | None = None
detail_sidebar: ft.Container | None = None  # 右侧文献详情侧边栏


def _has_cjk(text: str) -> bool:
    """检测文本是否包含中日韩字符，用于区分中英文关键词。"""
    return any('一' <= c <= '鿿' for c in text)


def _run_pipeline():
    """在后台线程中运行完整的搜索流水线。

    使用三级级联检索 + cross-encoder 精排 + API 分数融合 + 关键词匹配加分。
    """
    papers = []
    max_per = int(max_results_slider.value)

    # 1. 翻译课题描述（用于打分 + 并行描述检索）
    desc_en_query = None
    desc = state.topic_desc.strip()
    desc_en_terms = translate_terms([desc])
    desc_en = [t for t in desc_en_terms if t and not _has_cjk(t)]
    if desc_en:
        desc_en_query = desc_en[0]
        print(f"[PaperPilot] 课题描述翻译: {desc_en_query[:80]}...")
    elif not _has_cjk(desc):
        desc_en_query = desc
        print(f"[PaperPilot] 课题描述原文即英文: {desc_en_query[:80]}...")

    # 2. 翻译三层关键词
    primary_en_list = [t for t in translate_terms(state.primary_keywords)
                       if t and not _has_cjk(t)]
    secondary_en = [t for t in translate_terms(state.secondary_keywords)
                    if t and not _has_cjk(t)]
    regular_en = [t for t in translate_terms(state.regular_keywords)
                  if t and not _has_cjk(t)]

    primary_en = primary_en_list

    print(f"\n[PaperPilot] 开始检索")
    print(f"[PaperPilot] 主关键词: {primary_en}")
    print(f"[PaperPilot] 副关键词: {secondary_en}")
    print(f"[PaperPilot] 普通关键词: {regular_en}")

    # 3. 级联检索
    if arxiv_switch.value:
        state.status_text = "arXiv 抓取中..."
        try:
            arxiv_papers = fetch_multi_primary(
                primary_kw=primary_en,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="arxiv",
                max_results=max_per,
                min_results=3,
            )
            print(f"[PaperPilot] arXiv 返回: {len(arxiv_papers)} 篇 ({len(primary_en)}路主关键词)")
            papers += arxiv_papers
        except Exception as e:
            print(f"[PaperPilot] arXiv 失败: {e}")

        # 课题描述并行检索（补充召回）
        if desc_en_query:
            try:
                desc_papers = fetch_arxiv([desc_en_query], max_results=max_per, logic="OR")
                print(f"[PaperPilot] arXiv（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except Exception as e:
                print(f"[PaperPilot] arXiv（描述）失败: {e}")

    if openalex_switch.value:
        state.status_text = "OpenAlex 抓取中..."
        try:
            oa_papers = fetch_multi_primary(
                primary_kw=primary_en,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="openalex",
                max_results=max_per,
                min_results=3,
            )
            print(f"[PaperPilot] OpenAlex 返回: {len(oa_papers)} 篇 ({len(primary_en)}路主关键词)")
            papers += oa_papers
        except Exception as e:
            print(f"[PaperPilot] OpenAlex 失败: {e}")

        # 课题描述并行检索
        if desc_en_query:
            try:
                desc_papers = fetch_openalex([desc_en_query], max_results=max_per, logic="OR")
                print(f"[PaperPilot] OpenAlex（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except Exception as e:
                print(f"[PaperPilot] OpenAlex（描述）失败: {e}")

    # 4. 去重
    state.status_text = "去重中..."
    papers = deduplicate(papers)
    print(f"[PaperPilot] 去重后: {len(papers)} 篇")

    if not papers:
        print("[PaperPilot] 未找到论文")
        return [], []

    # 5. 排序打分（API 粗筛 -> cross-encoder 精排 -> 关键词加分）
    state.status_text = f"排序中（{len(papers)} 篇）..."
    query_for_scoring = desc_en_query if desc_en_query else state.topic_desc
    scores = rank_papers(
        query=query_for_scoring,
        papers=papers,
        top_k=20,
        primary_kw=primary_en,
        secondary_kw=secondary_en,
        regular_kw=regular_en,
    )

    return papers, scores


# ── 左侧导航栏 ──
NAV_ITEMS = [
    ("检索", ft.Icons.SEARCH, 0),
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

    # ── 三个拖拽区 ──
    primary_zone_row = ft.Row(wrap=True, spacing=6)
    secondary_zone_row = ft.Row(wrap=True, spacing=6)
    regular_zone_row = ft.Row(wrap=True, spacing=6)

    def _make_draggable_chip(kw: str, zone: str, icon, color):
        """创建可拖拽的关键词 Chip。"""
        chip = ft.Chip(
            label=ft.Text(kw, weight=ft.FontWeight.BOLD if zone == "primary" else None),
            leading=ft.Icon(icon, size=14, color=color) if icon else None,
            bgcolor=ft.Colors.PRIMARY_CONTAINER if zone == "primary" else None,
            on_delete=lambda e, k=kw: _on_delete_keyword(k),
        )
        return ft.Draggable(
            content=chip,
            data={"kw": kw, "from": zone},
            group="kw",
            content_feedback=ft.Chip(
                label=ft.Text(kw),
                bgcolor=ft.Colors.SURFACE,
            ),
        )

    def _on_delete_keyword(kw: str):
        """从所有区域中删除关键词。"""
        state.primary_keywords = [k for k in state.primary_keywords if k != kw]
        state.secondary_keywords = [k for k in state.secondary_keywords if k != kw]
        state.regular_keywords = [k for k in state.regular_keywords if k != kw]
        state.keywords = [k for k in state.keywords if k != kw]
        refresh_all_zones()

    def _make_on_accept(target_zone: str):
        """创建 DragTarget on_accept 回调。"""
        def on_accept(e: ft.DragTargetEvent):
            kw = e.src.data["kw"]
            from_zone = e.src.data["from"]
            if from_zone == target_zone:
                return
            # 从原区域移除
            for attr in ["primary_keywords", "secondary_keywords", "regular_keywords"]:
                lst = getattr(state, attr)
                if kw in lst:
                    lst.remove(kw)
                    break
            # 添加到目标区域
            if target_zone == "primary":
                state.primary_keywords.append(kw)
            elif target_zone == "secondary":
                state.secondary_keywords.append(kw)
            else:
                state.regular_keywords.append(kw)
            refresh_all_zones()
        return on_accept

    _drop_border = ft.BorderSide(2, ft.Colors.PRIMARY)

    def _make_zone(label: str, hint: str, chip_row: ft.Row,
                   zone_name: str, icon, color):
        """构建拖拽区：标题 + DragTarget。"""
        def on_will_accept(e: ft.DragTargetEvent):
            if e.src.data and e.src.data.get("from") != zone_name:
                zone_container.border = ft.Border(
                    _drop_border, _drop_border, _drop_border, _drop_border
                )
                zone_container.update()
                return True
            return False

        def on_leave(e: ft.DragTargetEvent):
            zone_container.border = _border(ft.Colors.OUTLINE_VARIANT)
            zone_container.update()

        zone_container = ft.Container(
            content=chip_row,
            border=_border(ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=8,
            bgcolor=ft.Colors.SURFACE if hasattr(ft.Colors, 'SURFACE') else None,
        )

        drag_target = ft.DragTarget(
            content=zone_container,
            group="kw",
            on_will_accept=on_will_accept,
            on_accept=_make_on_accept(zone_name),
            on_leave=on_leave,
        )

        return ft.Column([
            ft.Row([
                ft.Icon(icon, size=16, color=color) if icon else ft.Text(""),
                ft.Text(label, size=13, weight=ft.FontWeight.W_500),
            ], spacing=4),
            drag_target,
            ft.Text(hint, size=11, italic=True, color=ft.Colors.OUTLINE),
        ], spacing=4)

    def refresh_all_zones():
        """刷新三个拖拽区的 Chip 显示。"""
        zones = [
            ("primary",   state.primary_keywords,   primary_zone_row,
             ft.Icons.STAR, ft.Colors.AMBER, "拖拽关键词至此设为「主关键词」"),
            ("secondary", state.secondary_keywords, secondary_zone_row,
             ft.Icons.ARROW_FORWARD, ft.Colors.PRIMARY, "拖拽关键词至此设为「副关键词」"),
            ("regular",   state.regular_keywords,   regular_zone_row,
             None, None, "拖拽关键词至此设为「普通关键词」"),
        ]
        for zone_name, keywords, row, icon, color, hint in zones:
            row.controls.clear()
            for kw in keywords:
                row.controls.append(_make_draggable_chip(kw, zone_name, icon, color))
            if not keywords:
                row.controls.append(
                    ft.Text(hint, size=12, italic=True, color=ft.Colors.OUTLINE)
                )
            try:
                row.update()
            except RuntimeError:
                pass  # 控件尚未挂载到页面，跳过更新

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
            f"{state.topic_name}  |  检索到 {len(state.scores)} 篇论文  |  "
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
            status_text.value = "请先输入检索描述"
            status_text.update()
            return
        status_text.value = "正在提取关键词..."
        status_text.update()
        try:
            weighted = extract_all_keywords(desc, top_n=8)
            core_kw = [kw for kw, w in weighted if w >= 1.0]
            regular_kw = [kw for kw, w in weighted if 0 < w < 1.0]
            state.primary_keywords = []
            state.secondary_keywords = core_kw   # AI 认定的核心词初始放入副关键词
            state.regular_keywords = regular_kw  # 普通词放普通区
            state.keywords = [kw for kw, _ in weighted]
            refresh_all_zones()
            status_text.value = f"已提取 {len(state.keywords)} 个关键词"
        except Exception as ex:
            status_text.value = f"提取失败: {ex}"
        status_text.update()

    def on_add_keyword(e):
        kw = (e.control.value or "").strip()
        if kw:
            state.regular_keywords.append(kw)
            state.keywords = merge_keywords(state.keywords, [kw])
            manual_kw_field.value = ""
            manual_kw_field.update()
            refresh_all_zones()

    manual_kw_field.on_submit = on_add_keyword

    def on_start_search(e):
        if not topic_desc_field.value.strip():
            status_text.value = "请先输入检索描述"
            status_text.update()
            return
        if not state.keywords:
            status_text.value = "请先提取关键词"
            status_text.update()
            return

        state.topic_name = topic_name_field.value.strip() or "未命名检索"
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

    # 初始化分区（恢复已有状态）
    refresh_all_zones()

    return ft.Column([
        ft.Text("PaperPilot", size=28, weight=ft.FontWeight.BOLD),
        ft.Text("智能文献检索与筛选", size=14, italic=True),
        ft.Divider(height=20),
        ft.Text("检索信息", size=16, weight=ft.FontWeight.W_500),
        topic_name_field,
        topic_desc_field,
        ft.Row([
            ft.FilledTonalButton(
                content=ft.Text("提取关键词"), icon=ft.Icons.AUTO_AWESOME,
                on_click=on_extract,
            ),
            manual_kw_field,
        ], spacing=8),
        _make_zone("主关键词", "拖拽关键词至此设为「主关键词」",
                   primary_zone_row, "primary",
                   ft.Icons.STAR, ft.Colors.AMBER),
        _make_zone("副关键词", "拖拽关键词至此设为「副关键词」",
                   secondary_zone_row, "secondary",
                   ft.Icons.ARROW_FORWARD, ft.Colors.PRIMARY),
        _make_zone("普通关键词", "拖拽关键词至此设为「普通关键词」",
                   regular_zone_row, "regular",
                   None, None),
        ft.Divider(height=12),
        ft.Row([search_btn, progress_bar], spacing=16),
        status_text,
        results_area,
    ], spacing=8, scroll=ft.ScrollMode.AUTO)


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
max_results_slider = ft.Slider(min=10, max=500, value=30, divisions=49,
                                label="{value} 篇")


def _make_model_selector():
    """创建模型选择下拉框，读取/写入 config.yaml。"""
    from paperpilot.config import load_config as _lc, save_config as _sc

    current = _lc().get("deepseek", {}).get("model", "deepseek-v4-flash")

    model_options = [
        ft.dropdown.Option("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ft.dropdown.Option("deepseek-chat", "DeepSeek V3 (chat) - 7月下线"),
    ]

    model_dd = ft.Dropdown(
        options=model_options,
        value=current if current in ("deepseek-v4-flash", "deepseek-chat") else "deepseek-v4-flash",
        expand=True,
    )

    model_status = ft.Text("", size=12, italic=True)

    def on_change_model(e):
        _sc(updates={"deepseek": {"model": e.control.value}})
        model_status.value = f"已切换至 {e.control.value}，下次搜索生效"
        model_status.color = ft.Colors.GREEN
        model_status.update()

    model_dd.on_change = on_change_model

    return ft.Column([
        ft.Row([model_dd], spacing=8),
        model_status,
    ], spacing=4)


def build_settings_page():
    global container_settings
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

    # ── 主题切换 ──
    theme_buttons: dict[str, ft.Container] = {}

    def on_select_theme(name):
        state.theme_name = name
        apply_theme(_page, name, state.dark_mode)
        do_save(updates={"ui": {"theme": name, "dark_mode": state.dark_mode}})
        # 就地更新主题按钮边框（避免重建页面导致滚回顶部）
        for n, btn in theme_buttons.items():
            btn.border = _border(
                ft.Colors.ON_SURFACE if n == name else ft.Colors.OUTLINE_VARIANT
            )
            btn.update()
        # 导航栏重建（无滚动，不影响体验）
        nav_content_ref.content = build_left_nav(2)
        nav_content_ref.update()

    def on_toggle_dark(e):
        state.dark_mode = e.control.value
        apply_theme(_page, state.theme_name, state.dark_mode)
        do_save(updates={"ui": {"theme": state.theme_name, "dark_mode": state.dark_mode}})
        # 按钮边框不随夜间模式变化，只重建导航栏
        nav_content_ref.content = build_left_nav(2)
        nav_content_ref.update()

    theme_selector = ft.Row([
        ft.Column([
            ft.Container(
                width=44, height=44, border_radius=22,
                bgcolor=t["seed"],
                border=_border(
                    ft.Colors.ON_SURFACE if state.theme_name == name else ft.Colors.OUTLINE_VARIANT
                ),
                ink=True,
                on_click=lambda e, n=name: on_select_theme(n),
            ),
            ft.Text(t["label"], size=11, text_align=ft.TextAlign.CENTER),
        ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        for name, t in THEMES.items()
    ], spacing=20, alignment=ft.MainAxisAlignment.CENTER)

    # 收集按钮引用用于就地更新（避免重建页面导致滚回顶部）
    for i, name in enumerate(THEMES):
        col = theme_selector.controls[i]
        btn = col.controls[0]  # Container 是 Column 的第一个子控件
        theme_buttons[name] = btn

    dark_switch = ft.Switch(
        label="夜间模式",
        value=state.dark_mode,
        on_change=on_toggle_dark,
    )

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
        ft.Divider(height=12),
        ft.Text("模型", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择 DeepSeek API 模型，7月后 V3 将下线", size=13, italic=True),
        _make_model_selector(),
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
        ft.Text("外观", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择配色主题和夜间模式", size=13, italic=True),
        theme_selector,
        ft.Container(height=8),
        dark_switch,
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
    page.padding = 0

    # 加载主题偏好
    from paperpilot.config import load_config
    ui_config = load_config().get("ui", {})
    state.theme_name = ui_config.get("theme", DEFAULT_THEME)
    state.dark_mode = ui_config.get("dark_mode", False)
    apply_theme(page, state.theme_name, state.dark_mode)

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
            expand=True,
        ),
    )


if __name__ == "__main__":
    ft.run(main)
