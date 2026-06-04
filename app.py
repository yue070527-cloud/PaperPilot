"""PaperPilot - 面向课题攻关的可解释智能文献工作流系统。
Phase 1: 课题输入 → 关键词提取 → 论文抓取 → 排序展示
"""

import asyncio
import threading

import flet as ft

from paperpilot.keywords import extract_all_keywords, merge_keywords
from paperpilot.mt_translator import translate_terms
from paperpilot.fetcher import fetch_arxiv, fetch_openalex, fetch_with_cascade, fetch_multi_primary, deduplicate
from paperpilot.indexer import rank_papers
from paperpilot import library
from paperpilot.local_import import scan_folder, extract_pdfs
from paperpilot.pdf_viewer import open_full_reader, render_preview, is_full_reader_available


# ── 主题定义 ──
THEMES = {
    "mint":  {"label": "薄荷绿", "seed": "#00A86B", "light_bg": "#E5FFF7", "dark_bg": "#0D1F17"},
    "ocean": {"label": "海蓝",   "seed": "#1565C0", "light_bg": "#E8F0FE", "dark_bg": "#0D1B2A"},
    "sand":  {"label": "暖沙",   "seed": "#E65100", "light_bg": "#FFF5F0", "dark_bg": "#1E1610"},
    "dusk":  {"label": "暮紫",   "seed": "#7B1FA2", "light_bg": "#F5F0FF", "dark_bg": "#1A1020"},
    "rose":  {"label": "玫瑰",   "seed": "#D81B60", "light_bg": "#FFF0F4", "dark_bg": "#1F0E15"},
    "cyan":  {"label": "青碧",   "seed": "#0097A7", "light_bg": "#E5F7F9", "dark_bg": "#0D1A1C"},
}
DEFAULT_THEME = "mint"


def apply_theme(page: ft.Page, theme_name: str, dark_mode: bool):
    """应用配色主题和夜间模式。"""
    theme = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
    seed = theme["seed"]
    bg = theme["dark_bg"] if dark_mode else theme["light_bg"]
    page.theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=bg, font_family="Microsoft YaHei")
    page.dark_theme = ft.Theme(color_scheme_seed=seed, scaffold_bgcolor=theme["dark_bg"], font_family="Microsoft YaHei")
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
_refresh_library = None  # 文献页刷新函数引用，page_switcher 触发
_search_multi = False  # 检索结果多选模式
_search_selected_ids = set()  # 检索结果多选已选索引
_search_select_count_ref = None  # 多选计数 UI
_search_check_handler = None  # _on_search_check_one 引用
results_summary: ft.Text | None = None
detail_sidebar: ft.Container | None = None  # 右侧文献详情侧边栏


def _has_cjk(text: str) -> bool:
    """检测文本是否包含中日韩字符，用于区分中英文关键词。"""
    return any('一' <= c <= '鿿' for c in text)


def _run_pipeline(max_per: int, year_min: str, year_max: str,
                  use_arxiv: bool, use_openalex: bool,
                  top_k: int, ce_candidates: int):
    """在后台线程中运行完整的搜索流水线。

    所有 Flet 控件值由主线程读取后传入，避免跨线程访问控件。
    """
    import time as _time
    _t0 = _time.time()
    print(f"[PaperPilot] === 流水线启动 === max_per={max_per}, arxiv={use_arxiv}, "
          f"openalex={use_openalex}, top_k={top_k}, ce_candidates={ce_candidates}", flush=True)

    papers = []

    # 0. 年份筛选
    if year_min or year_max:
        print(f"[PaperPilot] 年份筛选: {year_min or '—'} ~ {year_max or '—'}", flush=True)

    # 1. 翻译课题描述
    state.status_text = "翻译课题描述..."
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
    state.status_text = "翻译关键词..."
    primary_en_list = [t for t in translate_terms(state.primary_keywords)
                       if t and not _has_cjk(t)]
    secondary_en = [t for t in translate_terms(state.secondary_keywords)
                    if t and not _has_cjk(t)]
    regular_en = [t for t in translate_terms(state.regular_keywords)
                  if t and not _has_cjk(t)]

    primary_kw_list = primary_en_list  # 所有主关键词作为 AND 核心
    # secondary_en 保持独立，不混入主关键词

    print(f"\n[PaperPilot] 开始检索")
    print(f"[PaperPilot] 主关键词: {primary_kw_list}")
    print(f"[PaperPilot] 副关键词: {secondary_en}")
    print(f"[PaperPilot] 普通关键词: {regular_en}")

    # 3. 多主关键词独立检索
    if use_arxiv:
        state.status_text = "arXiv 抓取中..."
        try:
            arxiv_papers = fetch_multi_primary(
                primary_kw=primary_kw_list,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="arxiv",
                max_results=max_per,
                min_results=3,
                year_min=year_min,
                year_max=year_max,
            )
            print(f"[PaperPilot] arXiv 返回: {len(arxiv_papers)} 篇 ({len(primary_kw_list)}路主关键词)")
            papers += arxiv_papers
        except Exception as e:
            print(f"[PaperPilot] arXiv 失败: {e}")

        if desc_en_query:
            try:
                desc_papers = fetch_arxiv([desc_en_query], max_results=max_per, logic="OR",
                                          year_min=year_min, year_max=year_max)
                print(f"[PaperPilot] arXiv（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except Exception as e:
                print(f"[PaperPilot] arXiv（描述）失败: {e}")

    if use_openalex:
        state.status_text = "OpenAlex 抓取中..."
        en_search_terms = [t for t in primary_kw_list + secondary_en + regular_en if t]
        print(f"[PaperPilot] OpenAlex 查询: {' AND '.join(en_search_terms)}")
        try:
            results = fetch_openalex(en_search_terms, max_results=max_per)
            print(f"[PaperPilot] OpenAlex 返回: {len(results)} 篇")
            papers += results
        except Exception as e:
            print(f"[PaperPilot] OpenAlex 失败: {e}")

        if desc_en_query:
            try:
                desc_papers = fetch_openalex([desc_en_query], max_results=max_per, logic="OR",
                                             year_min=year_min, year_max=year_max)
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

    # 5. 排序打分（首次会加载 942MB 语义模型，约需 10-30 秒）
    state.status_text = f"语义精排中（{len(papers)} 篇）..."
    query_for_scoring = desc_en_query if desc_en_query else state.topic_desc
    scores = rank_papers(
        query=query_for_scoring,
        papers=papers,
        top_k=top_k,
        ce_candidates=ce_candidates,
        primary_kw=primary_en_list,
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
        ft.Text("PaperPilot", size=18),
        ft.Divider(height=20),
        *nav_buttons,
    ], spacing=4)


# ── 页面切换 ──
def page_switcher(idx: int):
    """切换页面容器。"""
    container_project.visible = idx == 0
    container_results.visible = idx == 1
    container_settings.visible = idx == 2

    if idx == 1 and _refresh_library is not None:
        _refresh_library()

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
            label=ft.Text(kw),
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
            ft.Text(hint, size=11, color=ft.Colors.OUTLINE),
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
                    ft.Text(hint, size=12, color=ft.Colors.OUTLINE)
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
    status_text = ft.Text("", size=13)

    # ── 文献详情侧边栏 ──
    sb_title = ft.Text("", size=18, selectable=True)
    sb_meta = ft.Text("", size=13, selectable=True)
    sb_abstract = ft.Text("", size=13, selectable=True)
    sb_links = ft.Row([], spacing=8)

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
            sb_links,
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
    sidebar._links = sb_links
    detail_sidebar = sidebar

    # ── 检索结果区域 ──
    def _summary_text():
        return (
            f"{state.topic_name}  |  检索到 {len(state.scores)} 篇论文  |  "
            f"关键词：{', '.join(state.keywords[:5])}"
        )

    summary = ft.Text(_summary_text(), size=14)
    results_summary = summary

    # ── 检索结果多选 ──
    global _search_multi, _search_selected_ids
    _search_multi = False
    _search_selected_ids = set()

    search_multi_toggle = ft.TextButton(
        content=ft.Text("多选", size=13),
        icon=ft.Icons.CHECKLIST,
        visible=False,
    )
    search_select_count = ft.Text("", size=13)
    search_select_all_cb = ft.Checkbox(label="全选", visible=False)

    def _update_search_count():
        global _search_select_count_ref
        if _search_select_count_ref:
            _search_select_count_ref.value = f"已选 {len(_search_selected_ids)} 篇"
            _search_select_count_ref.update()

    def _on_search_select_all(e):
        if e.control.value:
            _search_selected_ids.update(range(len(state.scores)))
        else:
            _search_selected_ids.clear()
        refresh_results_table()

    def _on_search_check_one(e, idx: int):
        if e.control.value:
            _search_selected_ids.add(idx)
        else:
            _search_selected_ids.discard(idx)
        if _search_select_count_ref:
            _search_select_count_ref.value = f"已选 {len(_search_selected_ids)} 篇"
            _search_select_count_ref.update()

    global _search_select_count_ref, _search_check_handler
    _search_select_count_ref = search_select_count
    _search_check_handler = _on_search_check_one

    search_select_all_cb.on_change = _on_search_select_all

    def _on_toggle_search_multi(e):
        global _search_multi
        _search_multi = not _search_multi
        _search_selected_ids.clear()
        if _search_multi:
            search_multi_toggle.text = "退出多选"
            search_multi_toggle.icon = ft.Icons.CLOSE
            save_to_library_btn.text = "保存选中"
        else:
            search_multi_toggle.text = "多选"
            search_multi_toggle.icon = ft.Icons.CHECKLIST
            save_to_library_btn.text = "保存到文献库"
        search_select_all_cb.visible = _search_multi
        search_select_count.visible = _search_multi
        refresh_results_table()
        search_multi_toggle.update()
        save_to_library_btn.update()

    search_multi_toggle.on_click = _on_toggle_search_multi

    save_to_library_btn = ft.OutlinedButton(
        content=ft.Text("保存到文献库"),
        icon=ft.Icons.SAVE,
        visible=False,
    )

    def on_save_to_library(e):
        """弹出对话框，选择课题保存检索结果（多选模式下仅保存选中论文）。"""
        if _search_multi and not _search_selected_ids:
            return  # 不弹窗，静默忽略

        projects = library.get_all_projects()
        project_options = [ft.dropdown.Option(str(p.id), p.name) for p in projects]
        project_dd = ft.Dropdown(
            options=project_options,
            hint_text="选择已有课题",
            expand=True,
        )
        new_name_field = ft.TextField(
            label="或新建课题",
            hint_text="输入新课题名称",
            visible=False,
        )
        new_desc_field = ft.TextField(
            label="课题描述",
            hint_text=state.topic_desc[:200],
            visible=False,
        )

        def on_mode_change(e):
            is_new = e.control.value == "new"
            project_dd.visible = not is_new
            new_name_field.visible = is_new
            new_desc_field.visible = is_new
            project_dd.update()
            new_name_field.update()
            new_desc_field.update()

        save_mode = ft.RadioGroup(
            content=ft.Row([
                ft.Radio(value="existing", label="已有课题"),
                ft.Radio(value="new", label="新建课题"),
            ]),
            value="existing",
            on_change=on_mode_change,
        )

        result_text = ft.Text("", size=13)

        def do_save(e):
            nonlocal projects
            save_mode_val = save_mode.value
            if save_mode_val == "existing" and project_dd.value:
                pid = int(project_dd.value)
            elif save_mode_val == "new" and new_name_field.value.strip():
                try:
                    proj = library.create_project(
                        new_name_field.value.strip(),
                        new_desc_field.value.strip() or state.topic_desc,
                    )
                    pid = proj.id
                except ValueError as ve:
                    result_text.value = str(ve)
                    result_text.color = ft.Colors.ERROR
                    result_text.update()
                    return
            else:
                result_text.value = "请选择课题或输入新课题名称"
                result_text.color = ft.Colors.ERROR
                result_text.update()
                return

            # 多选模式下仅保存选中论文
            if _search_multi:
                sel_papers = [state.scores[i][0] for i in _search_selected_ids if i < len(state.scores)]
                sel_scores = [(state.scores[i][0], state.scores[i][1]) for i in _search_selected_ids if i < len(state.scores)]
                n = library.save_papers_to_project(pid, sel_papers, sel_scores)
                _search_selected_ids.clear()
            else:
                n = library.save_papers_to_project(pid, state.papers, state.scores)
            result_text.value = f"已保存 {n} 篇论文到文献库"
            result_text.color = ft.Colors.GREEN
            result_text.update()
            dlg.open = False
            dlg.update()

        def close_dlg(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("保存到文献库"),
            content=ft.Column([
                ft.Text(f"将 {len(_search_selected_ids) if _search_multi else len(state.scores)} 篇检索结果保存到："),
                save_mode,
                project_dd,
                new_name_field,
                new_desc_field,
                result_text,
            ], spacing=12, tight=True, height=280),
            actions=[
                ft.TextButton("取消", on_click=close_dlg),
                ft.FilledButton("保存", on_click=do_save),
            ],
        )

        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    save_to_library_btn.on_click = on_save_to_library

    results_area = ft.Column([
        ft.Divider(height=16),
        ft.Text("检索结果", size=22, weight=ft.FontWeight.W_600),
        summary,
        ft.Row([
            search_multi_toggle,
            search_select_all_cb,
            search_select_count,
            save_to_library_btn,
        ], alignment=ft.MainAxisAlignment.END, spacing=8),
        ft.Divider(height=8),
        ft.Column([results_table], expand=True, scroll=ft.ScrollMode.AUTO),
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
            state.secondary_keywords.append(kw)
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

        import threading

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

        # 在主线程读取所有 Flet 控件值，避免后台线程跨线程访问控件
        _max_per = int(max_results_slider.value)
        _year_min = ""
        _year_max = ""
        _use_arxiv = arxiv_switch.value
        _use_openalex = openalex_switch.value
        _top_k = int(top_k_slider.value)
        _ce_candidates = int(ce_candidates_slider.value)

        # 线程间共享结果
        _result: dict = {}       # {"papers": ..., "scores": ...} or {"error": ...}
        _done = threading.Event()

        def _run_in_thread():
            """在独立线程中执行流水线，避免 run_in_executor 嵌套回调丢失。"""
            try:
                papers, scores = _run_pipeline(
                    max_per=_max_per, year_min=_year_min, year_max=_year_max,
                    use_arxiv=_use_arxiv, use_openalex=_use_openalex,
                    top_k=_top_k, ce_candidates=_ce_candidates,
                )
                _result["papers"] = papers
                _result["scores"] = scores
            except Exception as ex:
                _result["error"] = ex
            finally:
                _done.set()

        threading.Thread(target=_run_in_thread, daemon=True).start()

        async def _poll():
            import traceback
            last_status = status_text.value
            while not _done.is_set():
                await asyncio.sleep(0.5)
                # 仅状态变化时才刷新 UI，避免事件循环拥塞
                if state.status_text != last_status:
                    last_status = state.status_text
                    status_text.value = state.status_text
                    status_text.update()

            # 流水线完成，执行一次性 UI 更新
            try:
                if "error" in _result:
                    state.status_text = f"检索失败: {_result['error']}"
                    traceback.print_exception(
                        type(_result["error"]), _result["error"],
                        _result["error"].__traceback__)
                elif not _result.get("papers"):
                    state.status_text = "未找到相关论文"
                    state.papers = []
                    state.scores = []
                    results_table.rows.clear()
                    results_table.update()
                    summary.value = _summary_text()
                    results_area.visible = True
                    results_area.update()
                else:
                    state.papers = _result["papers"]
                    state.scores = _result["scores"]
                    state.status_text = f"完成！共 {len(_result['scores'])} 篇"
                    refresh_results_table()
                    summary.value = _summary_text()
                    results_area.visible = True
                    save_to_library_btn.visible = True
                    search_multi_toggle.visible = True
                    results_area.update()
            finally:
                state.is_searching = False
                progress_bar.visible = False
                search_btn.disabled = False
                status_text.value = state.status_text
                progress_bar.update()
                search_btn.update()
                status_text.update()

        _page.run_task(_poll)

    search_btn = ft.FilledButton(
        content=ft.Text("开始检索"), icon=ft.Icons.SEARCH, on_click=on_start_search,
        style=ft.ButtonStyle(padding=ft.padding.Padding(left=32, top=16, right=32, bottom=16)),
    )

    # 初始化分区（恢复已有状态）
    refresh_all_zones()

    # ── 页面布局：左侧可滚动检索区 + 右侧固定详情侧边栏 ──
    scrollable_left = ft.Column([
        ft.Text("PaperPilot", size=28, weight=ft.FontWeight.W_600),
        ft.Text("智能文献检索与筛选", size=14),
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
        ft.Divider(height=8),
        results_area,
    ], spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)

    return ft.Row([
        scrollable_left,
        ft.VerticalDivider(width=1),
        sidebar,
    ], spacing=0, expand=True)


# ── 文献详情 ──
def show_paper_detail(paper: dict):
    """在右侧侧边栏展示论文详情。"""
    sb = detail_sidebar
    if sb is None:
        return
    from paperpilot.fetcher import get_article_type_label
    sb._title.value = paper.get("title", "")
    source = {"arxiv": "arXiv", "openalex": "OpenAlex", "local_pdf": "本地"}.get(
        paper.get("source", ""), paper.get("source", "")
    )
    type_label = get_article_type_label(paper)
    meta_parts = [
        f"作者: {paper.get('authors', '未知')}",
        f"年份: {paper.get('year', '—')}",
        f"来源: {source}",
        f"类型: {type_label}",
    ]
    journal = paper.get("journal")
    if journal:
        meta_parts.append(f"期刊: {journal}")
    cit = paper.get("cited_by_count")
    if cit is not None:
        meta_parts.append(f"引用次数: {cit}")
    sb._meta.value = "  |  ".join(meta_parts)
    sb._abstract.value = paper.get("abstract", "") or "（无摘要）"
    # 可点击链接
    links = []
    url = paper.get("url")
    if url:
        links.append(ft.TextButton("打开原文", icon=ft.Icons.OPEN_IN_BROWSER,
                                    on_click=lambda e, p=paper: threading.Thread(
                                        target=open_full_reader,
                                        args=(p,),
                                        kwargs={"theme_seed": THEMES[state.theme_name]["seed"],
                                                "dark_mode": state.dark_mode},
                                        daemon=True,
                                    ).start()))
    doi = paper.get("doi")
    if doi:
        import webbrowser
        doi_url = f"https://doi.org/{doi}"
        links.append(ft.TextButton("DOI", icon=ft.Icons.LINK,
                                    on_click=lambda e, u=doi_url: webbrowser.open(u)))
    sb._links.controls = links
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
        ft.DataColumn(ft.Text("引用"), numeric=True, on_sort=lambda e: sort_table("citations")),
        ft.DataColumn(ft.Text("得分"), numeric=True, on_sort=lambda e: sort_table("score")),
        ft.DataColumn(ft.Text("类型")),
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

    key_map = {"title": "title", "authors": "authors", "year": "year",
               "citations": "cited_by_count", "score": "score"}
    key = key_map.get(column, "score")
    reverse = _sort_ascending

    if key == "cited_by_count":
        state.scores = sorted(state.scores, key=lambda x: x[0].get(key) or 0, reverse=not reverse)
    elif key == "score":
        state.scores = sorted(state.scores, key=lambda x: x[1], reverse=reverse)
    else:
        state.scores = sorted(state.scores, key=lambda x: (
            x[0].get(key, "") or ""
        ), reverse=not reverse)

    refresh_results_table()


def _type_badge(paper: dict) -> ft.Container:
    """构建文章类型标签（小色块 + 文字）。"""
    from paperpilot.fetcher import get_article_type_label
    label = get_article_type_label(paper)
    type_colors = {
        "综述": ft.Colors.AMBER,
        "研究论文": ft.Colors.BLUE,
        "书籍章节": ft.Colors.TEAL,
        "书籍": ft.Colors.PURPLE,
        "学位论文": ft.Colors.ORANGE,
        "其他": ft.Colors.OUTLINE,
    }
    color = type_colors.get(label, ft.Colors.OUTLINE)
    return ft.Container(
        ft.Text(label, size=12, color=color, weight=ft.FontWeight.W_600),
        border=_border(color),
        border_radius=4,
        padding=ft.padding.Padding(left=4, top=1, right=4, bottom=1),
    )


def refresh_results_table():
    scored = state.scores
    results_table.rows.clear()

    # 根据多选模式切换列
    if _search_multi:
        results_table.columns = [
            ft.DataColumn(ft.Text("☐")),
            ft.DataColumn(ft.Text("#"), numeric=True),
            ft.DataColumn(ft.Text("标题"), on_sort=lambda e: sort_table("title")),
            ft.DataColumn(ft.Text("作者"), on_sort=lambda e: sort_table("authors")),
            ft.DataColumn(ft.Text("年份"), numeric=True, on_sort=lambda e: sort_table("year")),
            ft.DataColumn(ft.Text("来源")),
            ft.DataColumn(ft.Text("引用"), numeric=True, on_sort=lambda e: sort_table("citations")),
            ft.DataColumn(ft.Text("得分"), numeric=True, on_sort=lambda e: sort_table("score")),
            ft.DataColumn(ft.Text("类型")),
        ]
    else:
        results_table.columns = [
            ft.DataColumn(ft.Text("#"), numeric=True),
            ft.DataColumn(ft.Text("标题"), on_sort=lambda e: sort_table("title")),
            ft.DataColumn(ft.Text("作者"), on_sort=lambda e: sort_table("authors")),
            ft.DataColumn(ft.Text("年份"), numeric=True, on_sort=lambda e: sort_table("year")),
            ft.DataColumn(ft.Text("来源")),
            ft.DataColumn(ft.Text("引用"), numeric=True, on_sort=lambda e: sort_table("citations")),
            ft.DataColumn(ft.Text("得分"), numeric=True, on_sort=lambda e: sort_table("score")),
            ft.DataColumn(ft.Text("类型")),
        ]

    for i, (paper, score) in enumerate(scored):
        year_str = str(paper.get("year") or "—")
        cit = paper.get("cited_by_count")
        cit_str = str(cit) if cit is not None else "—"
        source_label = {"arxiv": "arXiv", "openalex": "OpenAlex", "local_pdf": "本地"}
        src = source_label.get(paper.get("source", ""), paper.get("source", ""))

        color = (
            ft.Colors.GREEN if score >= 0.4
            else ft.Colors.ORANGE if score >= 0.2
            else ft.Colors.OUTLINE
        )
        score_widget = ft.Text(f"{score:.3f}", color=color)

        base_cells = [
            ft.DataCell(ft.Text(str(i + 1))),
            ft.DataCell(ft.Container(
                ft.Text(paper.get("title", "")[:80]),
                on_click=lambda e, p=paper: show_paper_detail(p),
            )),
            ft.DataCell(ft.Text((paper.get("authors") or "")[:40])),
            ft.DataCell(ft.Text(year_str)),
            ft.DataCell(ft.Text(src)),
            ft.DataCell(ft.Text(cit_str)),
            ft.DataCell(score_widget),
            ft.DataCell(_type_badge(paper)),
        ]

        if _search_multi:
            is_checked = i in _search_selected_ids
            cb = ft.Checkbox(
                value=is_checked,
                on_change=lambda e, idx=i: _search_check_handler(e, idx),
            )
            results_table.rows.append(ft.DataRow(cells=[ft.DataCell(cb)] + base_cells))
        else:
            results_table.rows.append(ft.DataRow(cells=base_cells))

    # 更新全选复选框
    if _search_multi and len(scored) > 0:
        from paperpilot.fetcher import get_article_type_label  # keep existing import path
        pass  # search_select_all_cb handled inside build_project_page

    results_table.update()


def build_results_page():
    """文献管理页面：课题列表 + 论文列表 + 状态筛选 + 阅读 + 导出。"""
    # 页面局部状态
    _selected_project_id = None
    _project_papers: list[dict] = []
    _status_filter = "all"

    # ── 左侧：课题列表 ──
    project_list_col = ft.Column(spacing=4, expand=True, scroll=ft.ScrollMode.AUTO)
    selected_project_title = ft.Text("请选择一个课题", size=16, weight=ft.FontWeight.W_500)
    paper_count_text = ft.Text("", size=13)

    def refresh_project_list():
        """从数据库刷新课题列表。"""
        projects = library.get_all_projects()
        project_list_col.controls.clear()
        if not projects:
            project_list_col.controls.append(
                ft.Text("暂无课题，检索后保存即可创建", size=13,
                       color=ft.Colors.OUTLINE)
            )
        else:
            for proj in projects:
                is_active = _selected_project_id == proj.id
                btn = ft.TextButton(
                    content=ft.Row([
                        ft.Icon(ft.Icons.FOLDER, size=16,
                                color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                        ft.Text(proj.name[:20], size=13,
                               weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL,
                               color=ft.Colors.ON_PRIMARY_CONTAINER if is_active else None),
                    ], spacing=6),
                    data=proj.id,
                    on_click=lambda e, pid=proj.id: on_select_project(pid),
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.PRIMARY_CONTAINER if is_active else None,
                        padding=ft.padding.Padding(left=12, top=8, right=12, bottom=8),
                    ),
                )
                project_list_col.controls.append(btn)
        try:
            project_list_col.update()
        except RuntimeError:
            pass

    # 暴露给 page_switcher，切到文献页时自动刷新
    global _refresh_library
    _refresh_library = refresh_project_list

    def on_new_project(e):
        """新建课题对话框。"""
        name_field = ft.TextField(label="课题名称", hint_text="例如：钙钛矿太阳能电池")
        desc_field = ft.TextField(label="课题描述", hint_text="输入1-3句描述", multiline=True, min_lines=2, max_lines=4)
        msg = ft.Text("", size=13)

        def do_create(e):
            if not name_field.value.strip():
                msg.value = "请输入课题名称"
                msg.color = ft.Colors.ERROR
                msg.update()
                return
            try:
                proj = library.create_project(name_field.value.strip(), desc_field.value.strip())
                msg.value = f"已创建「{proj.name}」"
                msg.color = ft.Colors.GREEN
                msg.update()
                refresh_project_list()
                dlg.open = False
                dlg.update()
            except ValueError as ve:
                msg.value = str(ve)
                msg.color = ft.Colors.ERROR
                msg.update()

        def close_dlg(e):
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("新建课题"),
            content=ft.Column([name_field, desc_field, msg], spacing=12, tight=True, height=200),
            actions=[ft.TextButton("取消", on_click=close_dlg), ft.FilledButton("创建", on_click=do_create)],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    def on_delete_project(e):
        """删除当前选中课题。"""
        nonlocal _selected_project_id
        pid = _selected_project_id
        if pid is None:
            return

        def do_delete(e):
            nonlocal _selected_project_id
            library.delete_project(pid)
            _selected_project_id = None
            selected_project_title.value = "请选择一个课题"
            paper_count_text.value = ""
            refresh_project_list()
            refresh_paper_list()
            selected_project_title.update()
            paper_count_text.update()
            dlg.open = False
            dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Text("删除课题将同时删除其关联的论文记录（论文本身不会被删除）。"),
            actions=[ft.TextButton("取消", on_click=lambda e: (setattr(dlg, 'open', False), dlg.update())),
                     ft.FilledButton("确认删除", on_click=do_delete)],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    # ── 右侧：论文列表 ──
    status_filter_dd = ft.Dropdown(
        options=[
            ft.dropdown.Option("all", "全部"),
            ft.dropdown.Option("unread", "未读"),
            ft.dropdown.Option("skimmed", "略读"),
            ft.dropdown.Option("deep_read", "精读"),
        ],
        value="all",
        width=120,
    )

    paper_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("#"), numeric=True),
            ft.DataColumn(ft.Text("标题")),
            ft.DataColumn(ft.Text("作者")),
            ft.DataColumn(ft.Text("年份"), numeric=True),
            ft.DataColumn(ft.Text("得分"), numeric=True),
            ft.DataColumn(ft.Text("状态")),
            ft.DataColumn(ft.Text("操作")),
        ],
        rows=[],
        border=_border(ft.Colors.OUTLINE_VARIANT),
        expand=True,
    )

    empty_hint = ft.Text("", size=13, color=ft.Colors.OUTLINE)

    # ── 多选模式 ──
    _multi_select = False
    _selected_ids: set[int] = set()

    multi_select_bar = ft.Row(visible=False, spacing=8)
    multi_select_toggle = ft.TextButton(
        content=ft.Text("多选", size=13),
        icon=ft.Icons.CHECKLIST,
    )
    multi_select_count = ft.Text("", size=13)

    def _ensure_multi_columns():
        """根据多选模式切换 DataTable 列定义。"""
        if _multi_select:
            paper_table.columns = [
                ft.DataColumn(ft.Text("☐")),
                ft.DataColumn(ft.Text("#"), numeric=True),
                ft.DataColumn(ft.Text("标题")),
                ft.DataColumn(ft.Text("作者")),
                ft.DataColumn(ft.Text("年份"), numeric=True),
                ft.DataColumn(ft.Text("得分"), numeric=True),
                ft.DataColumn(ft.Text("状态")),
                ft.DataColumn(ft.Text("操作")),
            ]
        else:
            paper_table.columns = [
                ft.DataColumn(ft.Text("#"), numeric=True),
                ft.DataColumn(ft.Text("标题")),
                ft.DataColumn(ft.Text("作者")),
                ft.DataColumn(ft.Text("年份"), numeric=True),
                ft.DataColumn(ft.Text("得分"), numeric=True),
                ft.DataColumn(ft.Text("状态")),
                ft.DataColumn(ft.Text("操作")),
            ]

    def on_toggle_multi_select(e):
        nonlocal _multi_select
        _multi_select = not _multi_select
        _selected_ids.clear()
        if _multi_select:
            multi_select_toggle.text = "退出多选"
            multi_select_toggle.icon = ft.Icons.CLOSE
        else:
            multi_select_toggle.text = "多选"
            multi_select_toggle.icon = ft.Icons.CHECKLIST
        multi_select_bar.visible = _multi_select
        _ensure_multi_columns()
        refresh_paper_list()
        multi_select_toggle.update()
        multi_select_bar.update()
        try:
            paper_table.update()
        except RuntimeError:
            pass

    def on_select_all(e):
        if e.control.value:
            _selected_ids.update(p["project_paper_id"] for p in _project_papers)
        else:
            _selected_ids.clear()
        refresh_paper_list()

    def on_check_one(e, pp_id: int):
        if e.control.value:
            _selected_ids.add(pp_id)
        else:
            _selected_ids.discard(pp_id)
        update_count()

    def update_count():
        multi_select_count.value = f"已选 {len(_selected_ids)} 篇"
        multi_select_count.update()

    def on_batch_delete(e):
        if not _selected_ids:
            return

        def do_delete(e):
            n = library.remove_papers_from_project(list(_selected_ids))
            _selected_ids.clear()
            upload_progress.value = f"已删除 {n} 篇"
            upload_progress.color = ft.Colors.GREEN
            upload_progress.update()
            refresh_paper_list()

        def close_dlg(e):
            dlg.open = False; dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Text(f"将删除选中的 {len(_selected_ids)} 篇论文，此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=close_dlg),
                ft.FilledButton("确认删除", on_click=do_delete),
            ],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    multi_select_toggle.on_click = on_toggle_multi_select

    select_all_cb = ft.Checkbox(
        label="全选",
        on_change=on_select_all,
        visible=False,
    )
    _select_all_ref = select_all_cb

    multi_select_bar.controls = [
        _select_all_ref,
        multi_select_count,
        ft.FilledTonalButton(
            content=ft.Text("删除选中"), icon=ft.Icons.DELETE,
            on_click=on_batch_delete,
        ),
    ]

    # ── 上传 & 排序 ──
    upload_progress = ft.Text("", size=12)
    sort_btn = ft.IconButton(
        icon=ft.Icons.SORT,
        tooltip="CE 语义排序",
        disabled=True,
    )

    def on_sort_click(e):
        """对当前课题所有论文跑 CE 精排并持久化分数。"""
        if _selected_project_id is None:
            return
        proj = library.get_project(_selected_project_id)
        if not proj:
            return
        query = (proj.description or "").strip() or proj.name

        papers = library.get_project_papers(_selected_project_id)
        if not papers:
            upload_progress.value = "暂无论文可排序"
            upload_progress.color = ft.Colors.ERROR
            upload_progress.update()
            return

        upload_progress.value = "正在语义排序..."
        upload_progress.color = ft.Colors.OUTLINE
        upload_progress.update()

        # 构建 paper dict 列表（含 pdf_path）
        paper_dicts = []
        for p in papers:
            d = {
                "title": p.get("title", ""),
                "authors": p.get("authors", ""),
                "abstract": p.get("abstract", ""),
                "year": p.get("year"),
                "source": p.get("source", "local_pdf"),
                "url": p.get("url"),
                "doi": p.get("doi"),
                "api_score": None,
                "type": None,
                "cited_by_count": None,
                "journal": None,
                "pdf_path": p.get("pdf_path"),
            }
            paper_dicts.append(d)

        import threading
        _sort_done = threading.Event()
        _sort_result: list = []

        def _run_sort():
            try:
                scored = rank_papers(
                    query=query,
                    papers=paper_dicts,
                    top_k=len(paper_dicts),
                    ce_candidates=len(paper_dicts),
                )
                _sort_result.extend(scored)
            except Exception as ex:
                _sort_result.append(ex)
            finally:
                _sort_done.set()

        threading.Thread(target=_run_sort, daemon=True).start()

        async def _poll_sort():
            import asyncio
            while not _sort_done.is_set():
                await asyncio.sleep(0.3)
            if _sort_result and isinstance(_sort_result[0], Exception):
                upload_progress.value = f"排序失败: {_sort_result[0]}"
                upload_progress.color = ft.Colors.ERROR
            else:
                n = library.update_paper_scores(_selected_project_id, _sort_result)
                upload_progress.value = f"排序完成，已更新 {n} 篇"
                upload_progress.color = ft.Colors.GREEN
            upload_progress.update()
            refresh_paper_list()

        _page.run_task(_poll_sort)

    sort_btn.on_click = on_sort_click

    def _start_upload(file_paths: list[str]):
        """后台提取 PDF 并保存到课题。"""
        if not file_paths:
            return
        total = len(file_paths)
        upload_progress.value = f"正在提取 0/{total}..."
        upload_progress.color = ft.Colors.OUTLINE
        upload_progress.update()

        import threading
        _upload_done = threading.Event()
        _upload_result: dict = {}

        def _run_extract():
            def progress_cb(cur, tot, fname):
                upload_progress.value = f"正在提取 {cur}/{tot}: {fname[:30]}"
                upload_progress.update()

            papers, skipped = extract_pdfs(file_paths, on_progress=progress_cb)
            _upload_result["papers"] = papers
            _upload_result["skipped"] = skipped
            _upload_done.set()

        threading.Thread(target=_run_extract, daemon=True).start()

        async def _poll_upload():
            import asyncio
            while not _upload_done.is_set():
                await asyncio.sleep(0.3)

            papers = _upload_result.get("papers", [])
            skipped = _upload_result.get("skipped", [])

            n = library.save_papers_to_project(_selected_project_id, papers)
            msg_parts = [f"已添加 {n} 篇"]
            if skipped:
                msg_parts.append(f"跳过 {len(skipped)} 篇扫描件/加密文件")
            upload_progress.value = "，".join(msg_parts)
            upload_progress.color = ft.Colors.GREEN
            upload_progress.update()
            refresh_paper_list()

        _page.run_task(_poll_upload)

    # 文件选择 → PowerShell 调用 Windows 原生对话框
    def _run_ps_dialog(script: str) -> str:
        import subprocess, tempfile, os
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", delete=False, encoding="utf-8"
        )
        tmp.write(script)
        tmp.close()
        try:
            r = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", tmp.name],
                capture_output=True, text=True, timeout=120,
            )
            return r.stdout.strip()
        finally:
            os.unlink(tmp.name)

    def _pick_single_file():
        script = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            '$f=New-Object System.Windows.Forms.OpenFileDialog\n'
            "$f.Filter='PDF Files (*.pdf)|*.pdf'\n"
            "$f.Title='选择 PDF 文件'\n"
            "if($f.ShowDialog() -eq 'OK'){Write-Output $f.FileName}\n"
        )
        out = _run_ps_dialog(script)
        if out:
            _start_upload([out])

    def _pick_multiple_files():
        script = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            '$f=New-Object System.Windows.Forms.OpenFileDialog\n'
            "$f.Filter='PDF Files (*.pdf)|*.pdf'\n"
            "$f.Title='选择 PDF 文件'\n"
            '$f.Multiselect=$true\n'
            "if($f.ShowDialog() -eq 'OK'){$f.FileNames|%{Write-Output $_}}\n"
        )
        out = _run_ps_dialog(script)
        if out:
            _start_upload([p for p in out.split("\n") if p.strip()])

    def _pick_folder():
        script = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            '$f=New-Object System.Windows.Forms.FolderBrowserDialog\n'
            "$f.Description='选择包含 PDF 的文件夹'\n"
            "if($f.ShowDialog() -eq 'OK'){Write-Output $f.SelectedPath}\n"
        )
        out = _run_ps_dialog(script)
        if out:
            pdfs = scan_folder(out, recursive=True)
            if pdfs:
                _start_upload(pdfs)
            else:
                upload_progress.value = "所选文件夹中无 PDF 文件"
                upload_progress.color = ft.Colors.ERROR
                upload_progress.update()

    # upload 按钮 → PopupMenu 选择模式
    upload_menu_btn = ft.PopupMenuButton(
        icon=ft.Icons.UPLOAD_FILE,
        tooltip="上传本地论文",
        items=[
            ft.PopupMenuItem(
                content=ft.Text("选择单个文件"),
                on_click=lambda e: _pick_single_file(),
            ),
            ft.PopupMenuItem(
                content=ft.Text("选择多个文件"),
                on_click=lambda e: _pick_multiple_files(),
            ),
            ft.PopupMenuItem(
                content=ft.Text("选择文件夹"),
                on_click=lambda e: _pick_folder(),
            ),
        ],
    )

    def _on_single_delete(e, pp_id):
        """删除单篇论文的确认对话框。"""
        def do_delete(e):
            library.remove_paper_from_project(pp_id)
            refresh_paper_list()

        def close_dlg(e):
            dlg.open = False; dlg.update()

        dlg = ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Text("将删除这篇论文，此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=close_dlg),
                ft.FilledButton("确认删除", on_click=do_delete),
            ],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    def refresh_paper_list():
        """从数据库刷新当前课题的论文列表。"""
        paper_table.rows.clear()
        status_val = status_filter_dd.value
        sf = None if status_val == "all" else status_val
        papers = library.get_project_papers(_selected_project_id, status_filter=sf) if _selected_project_id else []
        _project_papers[:] = papers

        if not papers:
            paper_table.rows.clear()
            empty_hint.value = "此课题暂无保存的论文，请在检索页保存结果到此课题"
            empty_hint.update()
            try:
                paper_table.update()
            except RuntimeError:
                pass
            return

        empty_hint.value = ""

        status_colors = {"unread": ft.Colors.OUTLINE, "skimmed": ft.Colors.AMBER, "deep_read": ft.Colors.GREEN}

        for i, p in enumerate(papers):
            title = (p.get("title") or "")[:60]
            authors = (p.get("authors") or "")[:30]
            year = str(p.get("year") or "—")
            score = p.get("total_score", 0)
            status = p.get("status", "unread")
            pp_id = p["project_paper_id"]

            score_color = ft.Colors.GREEN if score >= 0.4 else ft.Colors.ORANGE if score >= 0.2 else ft.Colors.OUTLINE
            status_color = status_colors.get(status, ft.Colors.OUTLINE)

            # 阅读按钮
            read_btn = ft.IconButton(
                icon=ft.Icons.OPEN_IN_BROWSER,
                tooltip="打开全文",
                on_click=lambda e, paper=p: _on_read_paper(paper),
                icon_size=18,
            )
            if not is_full_reader_available():
                read_btn.disabled = True

            # 状态切换下拉
            status_dd = ft.Dropdown(
                options=[
                    ft.dropdown.Option("unread", "未读"),
                    ft.dropdown.Option("skimmed", "略读"),
                    ft.dropdown.Option("deep_read", "精读"),
                ],
                value=status,
                width=90,
                text_size=12,
            )
            status_dd.on_change = lambda e, ppid=pp_id: _on_status_change(ppid, e.control.value)

            # 操作列
            delete_btn = ft.IconButton(
                icon=ft.Icons.DELETE,
                tooltip="删除",
                icon_size=18,
                on_click=lambda e, pid=pp_id: _on_single_delete(e, pid),
            )
            action_cell = ft.DataCell(ft.Row([read_btn, delete_btn], spacing=2))

            if _multi_select:
                # 多选模式：复选框列 + 操作列仅阅读
                is_checked = pp_id in _selected_ids
                cb = ft.Checkbox(
                    value=is_checked,
                    on_change=lambda e, pid=pp_id: on_check_one(e, pid),
                )
                paper_table.rows.append(ft.DataRow(cells=[
                    ft.DataCell(cb),
                    ft.DataCell(ft.Text(str(i + 1))),
                    ft.DataCell(ft.Text(title)),
                    ft.DataCell(ft.Text(authors)),
                    ft.DataCell(ft.Text(year)),
                    ft.DataCell(ft.Text(f"{score:.3f}", color=score_color)),
                    ft.DataCell(ft.Row([
                        ft.Container(width=8, height=8, border_radius=4, bgcolor=status_color),
                        status_dd,
                    ], spacing=4)),
                    ft.DataCell(read_btn),
                ]))
            else:
                # 正常模式
                paper_table.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(str(i + 1))),
                    ft.DataCell(ft.Text(title)),
                    ft.DataCell(ft.Text(authors)),
                    ft.DataCell(ft.Text(year)),
                    ft.DataCell(ft.Text(f"{score:.3f}", color=score_color)),
                    ft.DataCell(ft.Row([
                        ft.Container(width=8, height=8, border_radius=4, bgcolor=status_color),
                        status_dd,
                    ], spacing=4)),
                    ft.DataCell(ft.Row([read_btn, delete_btn], spacing=2)),
                ]))

        # 更新全选复选框状态
        if _multi_select:
            select_all_cb.value = (len(_selected_ids) == len(papers) and len(papers) > 0)
            select_all_cb.visible = True
            update_count()
        else:
            select_all_cb.visible = False

        try:
            paper_table.update()
        except RuntimeError:
            pass

    def on_select_project(project_id: int | None):
        """选中课题时刷新论文列表。"""
        nonlocal _selected_project_id
        _selected_project_id = project_id
        if project_id is None:
            selected_project_title.value = "请选择一个课题"
            paper_count_text.value = ""
            sort_btn.disabled = True
        else:
            proj = library.get_project(project_id)
            if proj:
                selected_project_title.value = proj.name
                sort_btn.disabled = False
                sort_btn.tooltip = "CE 语义排序"
            else:
                sort_btn.disabled = True
        refresh_project_list()
        refresh_paper_list()
        selected_project_title.update()
        paper_count_text.update()

    def _on_read_paper(paper: dict):
        """打开 PDF 阅读器（后台线程，避免 CDP 抓取阻塞 UI）。"""
        t = threading.Thread(
            target=open_full_reader,
            args=(paper,),
            kwargs={"theme_seed": THEMES[state.theme_name]["seed"], "dark_mode": state.dark_mode},
            daemon=True,
        )
        t.start()

    def _on_status_change(pp_id: int, new_status: str):
        """更新论文状态并刷新列表。"""
        library.update_paper_status(pp_id, new_status)
        refresh_paper_list()

    # 状态筛选回调
    def on_status_filter_change(e):
        nonlocal _status_filter
        _status_filter = status_filter_dd.value
        refresh_paper_list()

    status_filter_dd.on_change = on_status_filter_change

    # ── 导出（B 负责 UI，A 负责数据转换） ──
    def on_export_bibtex(e):
        try:
            from paperpilot.export import to_bibtex
            content = to_bibtex(_project_papers)
            _save_export_file("bib", content)
        except ImportError:
            _show_export_unavailable()

    def on_export_csv(e):
        try:
            from paperpilot.export import to_csv
            content = to_csv(_project_papers)
            _save_export_file("csv", content)
        except ImportError:
            _show_export_unavailable()

    def _save_export_file(ext: str, content: str):
        import subprocess, tempfile
        path = tempfile.mktemp(suffix=f".{ext}", prefix="paperpilot_export_")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            subprocess.Popen(["start", "", path], shell=True)
        except Exception:
            pass

    def _show_export_unavailable():
        dlg = ft.AlertDialog(
            title=ft.Text("导出功能暂不可用"),
            content=ft.Text("导出模块尚未完成，请等待后续更新。"),
            actions=[ft.TextButton("确定", on_click=lambda e: (setattr(dlg, 'open', False), dlg.update()))],
        )
        _page.overlay.append(dlg)
        dlg.open = True
        _page.update()

    # 首次加载课题列表
    refresh_project_list()

    # ── 布局 ──
    left_panel = ft.Container(
        content=ft.Column([
            ft.Row([
                ft.IconButton(icon=ft.Icons.ADD, tooltip="新建课题", on_click=on_new_project),
                ft.IconButton(icon=ft.Icons.DELETE, tooltip="删除课题", on_click=on_delete_project),
                ft.IconButton(icon=ft.Icons.REFRESH, tooltip="刷新列表", on_click=lambda e: refresh_project_list()),
            ], spacing=2),
            project_list_col,
        ], spacing=6),
        width=220,
        padding=ft.padding.Padding(top=8, right=8, bottom=8, left=0),
    )

    right_panel = ft.Container(
        content=ft.Column([
            ft.Row([
                selected_project_title,
                ft.Row([
                    multi_select_toggle,
                    upload_menu_btn,
                    sort_btn,
                    ft.PopupMenuButton(
                        icon=ft.Icons.DOWNLOAD,
                        tooltip="导出",
                        items=[
                            ft.PopupMenuItem(content=ft.Text("BibTeX"), on_click=on_export_bibtex),
                            ft.PopupMenuItem(content=ft.Text("CSV"), on_click=on_export_csv),
                        ],
                    ),
                ], spacing=2),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            paper_count_text,
            upload_progress,
            multi_select_bar,
            ft.Row([
                ft.Text("筛选:", size=13),
                status_filter_dd,
            ], spacing=4),
            ft.Divider(height=8),
            empty_hint,
            ft.Column([paper_table], expand=True, scroll=ft.ScrollMode.AUTO),
        ], spacing=6, expand=True),
        expand=True,
        padding=ft.padding.Padding(top=8, left=8, bottom=8, right=0),
    )

    return ft.Row([
        left_panel,
        ft.VerticalDivider(width=1),
        right_panel,
    ], spacing=0, expand=True)


# ── 设置持久化 ──
def _save_setting(key: str, value):
    """保存单个搜索/数据源设置到 config.yaml。"""
    from paperpilot.config import save_config
    section, field = key.split(".", 1)
    save_config({section: {field: value}})


def _on_slider_saved(e, key: str):
    _save_setting(key, int(e.control.value))


# ── 设置页 ──
arxiv_switch = ft.Switch(label="arXiv", value=True)
arxiv_switch.on_change = lambda e: _save_setting("data_sources.arxiv", e.control.value)
openalex_switch = ft.Switch(label="OpenAlex", value=True)
openalex_switch.on_change = lambda e: _save_setting("data_sources.openalex", e.control.value)
max_results_slider = ft.Slider(min=100, max=500, value=100, divisions=40,
                                label="{value} 篇")
max_results_slider.on_change = lambda e: _on_slider_saved(e, "search.max_results")
top_k_slider = ft.Slider(min=10, max=200, value=50, divisions=19,
                          label="显示 {value} 篇")
top_k_slider.on_change = lambda e: _on_slider_saved(e, "search.top_k")
ce_candidates_slider = ft.Slider(min=10, max=200, value=100, divisions=19,
                                  label="精排候选 {value} 篇")
ce_candidates_slider.on_change = lambda e: _on_slider_saved(e, "search.ce_candidates")


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

    model_status = ft.Text("", size=12)

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

    key_status = ft.Text("", size=12)
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

    save_status = ft.Text("", size=13)

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
        ft.Text("设置", size=22),
        ft.Divider(height=16),
        ft.Text("DeepSeek API", size=16, weight=ft.FontWeight.W_500),
        ft.Text("用于关键词提取和中英翻译，密钥仅存储在本地 config.yaml", size=13),
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
        ft.Text("选择 DeepSeek API 模型，7月后 V3 将下线", size=13),
        _make_model_selector(),
        ft.Divider(height=16),
        ft.Text("数据源", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择从哪些来源获取论文", size=13),
        arxiv_switch,
        openalex_switch,
        ft.Divider(height=16),
        ft.Text("检索数量", size=16, weight=ft.FontWeight.W_500),
        ft.Text("每个来源的最大检索结果数", size=13),
        max_results_slider,
        ft.Container(height=8),
        ft.Text("结果显示与精排", size=16, weight=ft.FontWeight.W_500),
        ft.Text("控制最终显示的论文数量和送入精排的候选数", size=13),
        top_k_slider,
        ce_candidates_slider,
        ft.Divider(height=16),
        ft.Text("外观", size=16, weight=ft.FontWeight.W_500),
        ft.Text("选择配色主题和夜间模式", size=13),
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

    # 加载已保存的所有设置
    from paperpilot.config import load_config
    cfg = load_config()
    ui_config = cfg.get("ui", {})
    state.theme_name = ui_config.get("theme", DEFAULT_THEME)
    state.dark_mode = ui_config.get("dark_mode", False)
    apply_theme(page, state.theme_name, state.dark_mode)

    # 恢复搜索/数据源设置
    search_cfg = cfg.get("search", {})
    if search_cfg.get("max_results"):
        max_results_slider.value = int(search_cfg["max_results"])
    if search_cfg.get("top_k"):
        top_k_slider.value = int(search_cfg["top_k"])
    if search_cfg.get("ce_candidates"):
        ce_candidates_slider.value = int(search_cfg["ce_candidates"])

    ds_cfg = cfg.get("data_sources", {})
    if "arxiv" in ds_cfg:
        arxiv_switch.value = bool(ds_cfg["arxiv"])
    if "openalex" in ds_cfg:
        openalex_switch.value = bool(ds_cfg["openalex"])

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
