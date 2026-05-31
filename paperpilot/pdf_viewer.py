"""PDF 浏览模块：pywebview + PDF.js 统一渲染 + PyMuPDF 轻量预览。

方案 B 核心：所有论文通过 PDF.js 在 pywebview 独立窗口中渲染，
配合自定义 HTML 模板实现与 Flet 主窗口一致的主题外观。

依赖：
- pywebview>=4.0（独立阅读窗口）
- fitz / PyMuPDF（轻量预览，已有）
- PDF.js（CDN 加载，无需本地文件）

A 搭档写完 text_fetcher.py 后，open_full_reader 自动获得 PDF 下载能力，
无需改动本文件。
"""

import base64
import os
import re
import threading

import fitz  # PyMuPDF
import flet as ft

try:
    import webview as _webview
except ImportError:
    _webview = None  # pywebview 未安装时灰显按钮

try:
    from paperpilot.text_fetcher import download_pdf as _download_pdf
except ImportError:
    _download_pdf = None  # A 搭档还没写，暂时不可用


# ── 颜色工具 ──

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """#0097A7 → (0, 151, 167)"""
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _blend_with(hex_color: str, factor: float, towards: str = "#000000") -> str:
    """将颜色向 towards 混合，factor 0→原色，1→towards。"""
    r1, g1, b1 = _hex_to_rgb(hex_color)
    r2, g2, b2 = _hex_to_rgb(towards)
    r = int(r1 + (r2 - r1) * factor)
    g = int(g1 + (g2 - g1) * factor)
    b = int(b1 + (b2 - b1) * factor)
    return _rgb_to_hex(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


# ── PDF.js HTML 模板 ──

_PDFJS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<style>
:root {
    --seed: __SEED__;
    --bg: __BG__;
    --text: __TEXT__;
    --titlebar-bg: __TITLEBAR_BG__;
    --titlebar-h: 38px;
    --btn-hover: __BTN_HOVER__;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, sans-serif;
    overflow: hidden; user-select: none;
}
/* 自定义标题栏 */
.titlebar {
    height: var(--titlebar-h); background: var(--titlebar-bg);
    display: flex; align-items: center; padding: 0 6px;
    -webkit-app-region: drag;
}
.pywebview-drag-region {
    flex: 1; height: 100%; display: flex; align-items: center;
    padding-left: 10px; font-size: 13px; font-weight: 500;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.win-btn {
    width: 34px; height: 28px; border: none; background: transparent;
    color: var(--text); cursor: pointer; border-radius: 4px;
    font-size: 14px; display: flex; align-items: center; justify-content: center;
    -webkit-app-region: no-drag;
}
.win-btn:hover { background: var(--btn-hover); }
.win-btn.close:hover { background: #E81123; color: #fff; }
/* 页码导航栏 */
.controls {
    height: 32px; background: var(--titlebar-bg);
    display: flex; align-items: center; justify-content: center;
    gap: 10px; font-size: 13px;
}
.controls button {
    background: var(--btn-hover); border: none; color: var(--text);
    width: 28px; height: 24px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
.controls button:hover { background: color-mix(in srgb, var(--seed) 40%, var(--btn-hover)); }
/* PDF 画布 */
#viewer {
    height: calc(100vh - var(--titlebar-h) - 32px);
    overflow-y: auto; display: flex; flex-direction: column;
    align-items: center; padding: 12px;
}
canvas {
    margin: 4px 0;
    box-shadow: 0 2px 12px rgba(0,0,0,0.25);
    border-radius: 2px;
}
/* loading / error */
#status {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    font-size: 15px; opacity: 0.8;
}
</style>
</head>
<body>
<div class="titlebar">
    <div class="pywebview-drag-region" id="title-text">__TITLE__</div>
    <button class="win-btn" onclick="pywebview.window.minimize()" title="最小化">&#x2014;</button>
    <button class="win-btn" onclick="pywebview.window.maximize()" title="最大化">&#x25A1;</button>
    <button class="win-btn close" onclick="pywebview.window.close()" title="关闭">&#x2715;</button>
</div>
<div class="controls">
    <button onclick="prevPage()" title="上一页">&#x25C0;</button>
    <span id="page-info">-- / --</span>
    <button onclick="nextPage()" title="下一页">&#x25B6;</button>
    <input type="number" id="goto-input" min="1" max="1"
           style="width:50px;text-align:center;background:var(--btn-hover);border:none;color:var(--text);border-radius:4px;padding:2px 4px;">
    <button onclick="gotoPage()">跳转</button>
</div>
<div id="viewer"></div>
<div id="status">Loading PDF...</div>
<script>
pdfjsLib.GlobalWorkerOptions.workerSrc =
    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

var __PDF_DATA__ = '__PDF_BASE64__';
var __PDF_SRC__ = '__PDF_PATH__';

var currentPage = 1;
var totalPages = 1;
var pdfDoc = null;

function renderPage(num) {
    pdfDoc.getPage(num).then(function(page) {
        var scale = 1.5;
        var vp = page.getViewport({scale: scale});
        var canvas = document.createElement('canvas');
        canvas.id = 'page-' + num;
        canvas.width = vp.width;
        canvas.height = vp.height;
        var ctx = canvas.getContext('2d');
        page.render({canvasContext: ctx, viewport: vp}).promise.then(function() {
            // Remove old canvas for this page
            var old = document.getElementById('page-' + num);
            if (old && old.parentNode) old.parentNode.removeChild(old);
            var existing = document.getElementById('page-' + num);
            if (!existing) {
                document.getElementById('viewer').appendChild(canvas);
            }
            // Scroll to page
            canvas.scrollIntoView({behavior: 'smooth', block: 'start'});
        });
    });
}

function loadPdf(src) {
    pdfjsLib.getDocument(src).promise.then(function(pdf) {
        pdfDoc = pdf;
        totalPages = pdf.numPages;
        document.getElementById('title-text').textContent += ' (' + totalPages + ' pp)';
        document.getElementById('goto-input').max = totalPages;
        document.getElementById('page-info').textContent = '1 / ' + totalPages;
        document.getElementById('status').style.display = 'none';
        renderPage(1);

        // Lazy render all other pages
        for (var i = 2; i <= totalPages; i++) {
            renderPage(i);
        }
    }).catch(function(e) {
        document.getElementById('status').textContent = 'Error: ' + e.message;
    });
}

function prevPage() {
    if (currentPage <= 1) return;
    currentPage--;
    scrollToPage(currentPage);
}
function nextPage() {
    if (currentPage >= totalPages) return;
    currentPage++;
    scrollToPage(currentPage);
}
function gotoPage() {
    var n = parseInt(document.getElementById('goto-input').value);
    if (n >= 1 && n <= totalPages) {
        currentPage = n;
        scrollToPage(n);
    }
}
function scrollToPage(n) {
    currentPage = n;
    document.getElementById('page-info').textContent = n + ' / ' + totalPages;
    document.getElementById('goto-input').value = n;
    var canvas = document.getElementById('page-' + n);
    if (canvas) {
        canvas.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
}

// Start
var pdfSrc;
if (__PDF_DATA__) {
    // base64 data → Uint8Array
    var binary = atob(__PDF_DATA__);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    pdfSrc = {data: bytes};
} else if (__PDF_SRC__) {
    pdfSrc = __PDF_SRC__;
}
if (pdfSrc) loadPdf(pdfSrc);
</script>
</body>
</html>
"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PaperPilot</title>
<style>
body { display:flex; align-items:center; justify-content:center;
       height:100vh; margin:0; font-family:"Segoe UI",sans-serif;
       background:__BG__; color:__TEXT__; }
.box { text-align:center; max-width:400px; padding:40px; }
h2 { font-weight:500; } p { opacity:0.7; margin-top:12px; }
</style></head>
<body><div class="box">
<h2>__MESSAGE__</h2><p>__DETAIL__</p></div></body></html>
"""


def _build_reader_html(
    pdf_path: str | None,
    title: str,
    seed_color: str,
    dark_mode: bool,
) -> str:
    """生成 PDF.js 阅读器的完整 HTML 页面。

    Args:
        pdf_path: 本地 PDF 文件路径，None 表示无文件可读
        title: 论文标题
        seed_color: 主题种子色
        dark_mode: 是否夜间模式
    """
    if dark_mode:
        bg = _blend_with(seed_color, 0.92, "#000000")
        text = "#e0e0e0"
        titlebar_bg = _blend_with(seed_color, 0.7, "#000000")
        btn_hover = _blend_with(seed_color, 0.4, "#000000")
    else:
        bg = "#ffffff"
        text = "#1a1a1a"
        titlebar_bg = _blend_with(seed_color, 0.3, "#ffffff")
        btn_hover = _blend_with(seed_color, 0.1, "#ffffff")

    pdf_base64 = ""
    if pdf_path and os.path.isfile(pdf_path):
        try:
            with open(pdf_path, "rb") as f:
                pdf_base64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            pdf_base64 = ""
            pdf_path = None

    safe_title = title.replace("\\", "\\\\").replace("`", "\\`")

    html = _PDFJS_HTML
    html = html.replace("__TITLE__", safe_title)
    html = html.replace("__SEED__", seed_color)
    html = html.replace("__BG__", bg)
    html = html.replace("__TEXT__", text)
    html = html.replace("__TITLEBAR_BG__", titlebar_bg)
    html = html.replace("__BTN_HOVER__", btn_hover)

    # JS: inject PDF data
    if pdf_base64:
        html = html.replace("'__PDF_BASE64__'", f"'{pdf_base64}'")
        html = html.replace("'__PDF_PATH__'", "''")
    else:
        html = html.replace("'__PDF_BASE64__'", "''")
        # Encode local file path as file:// URL (works in WebView2)
        if pdf_path:
            abs_path = os.path.abspath(pdf_path).replace("\\", "/")
            html = html.replace("'__PDF_PATH__'", f"'file:///{abs_path}'")
        else:
            html = html.replace("'__PDF_PATH__'", "''")

    return html


def _build_error_html(message: str, detail: str, dark_mode: bool, seed_color: str) -> str:
    bg = _blend_with(seed_color, 0.92, "#000000") if dark_mode else "#ffffff"
    text = "#e0e0e0" if dark_mode else "#1a1a1a"
    return (
        _ERROR_HTML.replace("__MESSAGE__", message)
        .replace("__DETAIL__", detail)
        .replace("__BG__", bg)
        .replace("__TEXT__", text)
    )


# ── 方案 B：pywebview 独立阅读窗口 ──

def _create_window(create_kwargs: dict) -> bool:
    """在独立子进程中启动 pywebview 窗口，避免与 Flet 的主线程冲突。"""
    import json
    import subprocess
    import sys
    import tempfile

    # 将参数写入临时 JSON 文件（HTML 等大文本走文件而非命令行）
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(create_kwargs, f, ensure_ascii=False)
        args_path = f.name

    script = (
        "import json,webview,os\n"
        f"with open({args_path!r},'r',encoding='utf-8') as f:\n"
        "  k=json.load(f)\n"
        f"os.unlink({args_path!r})\n"
        "webview.create_window(**k)\n"
        "webview.start()\n"
    )

    subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return True


def open_full_reader(
    paper: dict,
    theme_seed: str = "#0097A7",
    dark_mode: bool = True,
    x: int | None = None,
    y: int | None = None,
) -> bool:
    """打开 pywebview 独立阅读窗口。

    PDF 来源优先级：
    1. paper["pdf_path"] — 已下载到本地的 PDF 文件 → PDF.js 渲染
    2. text_fetcher.download_pdf(paper) — A 搭档提供，下载并缓存后 PDF.js 渲染
    3. paper["url"] — 论文链接 → pywebview 直接加载网页
    4. paper["doi"] → https://doi.org/{doi}
    5. 都没有 → 显示错误提示

    Args:
        paper: paper dict 格式（跨模块数据协议）
        theme_seed: 主题种子色，与 Flet 主窗口一致
        dark_mode: 是否夜间模式
        x, y: 窗口位置（屏幕坐标），None 则居中

    Returns:
        True 表示成功打开阅读窗口，False 表示 pywebview 不可用
    """
    if _webview is None:
        return False

    pdf_path = paper.get("pdf_path")
    url = paper.get("url")
    doi = paper.get("doi")
    title = (paper.get("title") or "PaperPilot Reader").strip()

    # 1. 本地 PDF 文件
    if pdf_path and os.path.isfile(pdf_path):
        return _open_pdfjs_window(pdf_path, title, theme_seed, dark_mode, x, y)

    # 2. 通过 A 的 text_fetcher 下载
    if _download_pdf is not None:
        try:
            pdf_path = _download_pdf(paper)
            if pdf_path and os.path.isfile(pdf_path):
                return _open_pdfjs_window(pdf_path, title, theme_seed, dark_mode, x, y)
        except Exception:
            pass  # 下载失败，继续下一级

    # 3. 直接加载 URL（网页 / arXiv HTML / 出版商页面）
    if url:
        return _open_url_window(url, title, x, y)

    # 4. DOI
    if doi:
        doi_url = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        return _open_url_window(doi_url, title, x, y)

    # 5. 无法获取全文
    return _open_error_window(title, theme_seed, dark_mode, x, y)


def _open_pdfjs_window(
    pdf_path: str,
    title: str,
    theme_seed: str,
    dark_mode: bool,
    x: int | None,
    y: int | None,
) -> bool:
    """生成 PDF.js HTML 并在 pywebview 窗口中打开。"""
    html = _build_reader_html(pdf_path, title, theme_seed, dark_mode)
    return _create_window({
        "title": title,
        "html": html,
        "frameless": True,
        "width": 900,
        "height": 700,
        "x": x,
        "y": y,
        "min_size": (400, 300),
    })


def _open_url_window(url: str, title: str, x: int | None, y: int | None) -> bool:
    """用 pywebview 直接加载远程 URL。"""
    return _create_window({
        "title": title,
        "url": url,
        "width": 1000,
        "height": 750,
        "x": x,
        "y": y,
        "min_size": (400, 300),
    })


def _open_error_window(
    title: str,
    theme_seed: str,
    dark_mode: bool,
    x: int | None,
    y: int | None,
) -> bool:
    """显示'无法获取全文'的错误提示窗口。"""
    html = _build_error_html(
        message="无法获取全文",
        detail="该论文没有可用的本地 PDF 文件或远程链接。",
        dark_mode=dark_mode,
        seed_color=theme_seed,
    )
    return _create_window({
        "title": title,
        "html": html,
        "frameless": True,
        "width": 500,
        "height": 300,
        "x": x,
        "y": y,
    })


# ── 方案 A：Flet 内嵌 PyMuPDF 轻量预览 ──

def render_preview(pdf_path: str, max_pages: int = 5) -> list[ft.Image]:
    """将 PDF 前 N 页转为 Flet Image 控件列表，用于内嵌快速预览。

    Args:
        pdf_path: 本地 PDF 文件路径
        max_pages: 最多渲染页数

    Returns:
        ft.Image 列表，可直接添加到 Flet Column 中展示
    """
    if not pdf_path or not os.path.isfile(pdf_path):
        return [ft.Text("PDF 文件不存在", italic=True, color=ft.Colors.OUTLINE)]

    images = []
    try:
        doc = fitz.open(pdf_path)
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            pix = page.get_pixmap(dpi=120)
            images.append(
                ft.Image(
                    src=f"data:image/png;base64,{base64.b64encode(pix.tobytes('png')).decode()}",
                    fit="contain",
                )
            )
        doc.close()
    except Exception as e:
        return [ft.Text(f"PDF 预览失败: {e}", italic=True, color=ft.Colors.ERROR)]

    return images or [ft.Text("PDF 无内容", italic=True, color=ft.Colors.OUTLINE)]


# ── 可用性检查 ──

def is_full_reader_available() -> bool:
    """检查 pywebview 是否可用（用于 UI 中按钮的 disabled 状态）。"""
    return _webview is not None


def is_text_fetcher_available() -> bool:
    """检查 text_fetcher 是否可用（A 搭档是否已完成）。"""
    return _download_pdf is not None
