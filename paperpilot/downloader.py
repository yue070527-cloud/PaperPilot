"""
统一论文 PDF 下载 — 直链 HTTP。

支持的出版商:
  - arXiv, Nature, Springer (直链)
  - ACS, Wiley, IEEE, IOP, RSC, Science (guessed URL)
  - 其他: citation_pdf_url meta 标签

用法:
    from paperpilot.downloader import cache_pdf

    path = cache_pdf(paper)       # 下载 + 缓存, 返回本地路径

依赖:
    pip install curl_cffi PyMuPDF beautifulsoup4 lxml
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

import fitz
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────

_ARXIV_PDF = "https://arxiv.org/pdf/{}.pdf"
_NATURE_PDF = "https://www.nature.com/articles/{}.pdf"
_SPRINGER_PDF = "https://link.springer.com/content/pdf/{}.pdf"
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")

DEFAULT_CACHE_DIR = Path.home() / ".paperpilot_pdf_cache"
MAX_CACHE_MB = 512

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ══════════════════════════════════════════════════════════════════
# 出版商检测
# ══════════════════════════════════════════════════════════════════

_PUBLISHERS = [
    ("sciencedirect", lambda d, u: "sciencedirect.com" in u or "elsevier" in u or "10.1016/" in d),
    ("nejm",          lambda d, u: "nejm.org" in u or "10.1056/" in d),
    ("arxiv",         lambda d, u: "arxiv.org" in u or "10.48550/" in d),
    ("nature",        lambda d, u: "nature.com" in u or "10.1038/" in d),
    ("springer",      lambda d, u: "link.springer.com" in u),
    ("acs",           lambda d, u: "pubs.acs.org" in u or "10.1021/" in d),
    ("wiley",         lambda d, u: "onlinelibrary.wiley.com" in u or "10.1002/" in d),
    ("science",       lambda d, u: "science.org" in u or "10.1126/" in d),
    ("ieee",          lambda d, u: "ieeexplore.ieee.org" in u or "10.1109/" in d),
    ("iop",           lambda d, u: "iop.org" in u or "10.1088/" in d),
    ("rsc",           lambda d, u: "rsc.org" in u or "10.1039/" in d),
]


def detect_publisher(paper: dict) -> str:
    """根据 paper 的 doi/url 检测出版商。"""
    doi = paper.get("doi", "") or ""
    url = (paper.get("url", "") or "").lower()
    for name, check in _PUBLISHERS:
        if check(doi, url):
            return name
    return "unknown"


# ══════════════════════════════════════════════════════════════════
# 直链下载
# ══════════════════════════════════════════════════════════════════

def _direct_download(paper: dict) -> bytes | None:
    """纯 HTTP 直链下载 PDF。

    链路: arXiv → Nature → Springer → Guessed URL → citation_pdf_url meta

    Returns:
        PDF bytes 或 None
    """
    from curl_cffi import requests as cffi

    doi = paper.get("doi", "")
    url = paper.get("url", "")

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    }

    # ① arXiv
    arxiv_id = None
    m = _ARXIV_ID_RE.search(doi or url or "")
    if m:
        arxiv_id = m.group(1)
    if arxiv_id:
        try:
            resp = cffi.get(_ARXIV_PDF.format(arxiv_id), headers=headers,
                          timeout=15, impersonate="chrome120")
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: arXiv 直链")
                return resp.content
        except Exception:
            pass

    # ② Nature
    nature_id = None
    m = re.search(r"nature\.com/articles/([^/?\s]+)", url)
    if m:
        nature_id = m.group(1)
    if nature_id:
        try:
            resp = cffi.get(_NATURE_PDF.format(nature_id), headers=headers,
                          timeout=15, impersonate="chrome120")
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: Nature 直链")
                return resp.content
        except Exception:
            pass

    # ③ Springer
    if doi:
        try:
            resp = cffi.get(_SPRINGER_PDF.format(doi), headers=headers,
                          timeout=15, impersonate="chrome120")
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: Springer 直链")
                return resp.content
        except Exception:
            pass

    # ④ Guessed PDF URL (ACS, Science, Wiley, IOP, RSC…)
    guessed = _guess_pdf_url(paper)
    if guessed:
        try:
            imp = "chrome124" if "wiley.com" in guessed else "chrome120"
            resp = cffi.get(guessed, headers=headers, timeout=15, impersonate=imp)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: %s", guessed[:80])
                return resp.content
        except Exception:
            pass

    # ⑤ citation_pdf_url meta tag
    if doi:
        try:
            doi_url = f"https://doi.org/{doi}"
            resp = cffi.get(doi_url, headers=headers, timeout=15,
                          impersonate="chrome120", allow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
                if meta and meta.get("content"):
                    pdf_url = meta["content"]
                    pdf_resp = cffi.get(pdf_url, headers=headers, timeout=15,
                                      impersonate="chrome120")
                    if pdf_resp.status_code == 200 and pdf_resp.content[:5] == b"%PDF-":
                        logger.info("PDF 来源: citation_pdf_url")
                        return pdf_resp.content
        except Exception:
            pass

    return None


def _guess_pdf_url(paper: dict) -> str | None:
    """根据 DOI/URL 构造常见出版商 PDF 直链。"""
    doi = paper.get("doi", "")
    url = (paper.get("url", "") or "").lower()

    if "10.1021/" in doi or "pubs.acs.org" in url:
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    if "10.1126/" in doi or "science.org" in url:
        return f"https://www.science.org/doi/pdf/{doi}?download=true"
    if "10.1002/" in doi or "wiley.com" in url:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if "10.1109/" in doi or "ieeexplore.ieee.org" in url:
        return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={doi}"
    if "10.1088/" in doi or "iop.org" in url:
        return f"https://iopscience.iop.org/article/{doi}/pdf"
    if "10.1039/" in doi or "rsc.org" in url:
        return f"https://pubs.rsc.org/en/content/articlepdf/{doi.split('/')[-1]}"
    if "10.1056/" in doi or "nejm.org" in url:
        return f"https://www.nejm.org/doi/pdf/{doi}"

    return None


# ══════════════════════════════════════════════════════════════════
# 公开 API
# ══════════════════════════════════════════════════════════════════

def download_pdf(paper: dict) -> bytes | None:
    """下载论文 PDF（直链 HTTP）。

    支持的出版商: arXiv, Nature, Springer, ACS, Wiley, IEEE, IOP, RSC, Science

    Returns:
        PDF bytes, 或 None（直链不可用）
    """
    return _direct_download(paper)


def cache_pdf(
    paper: dict,
    cache_dir: str | Path | None = None,
) -> str | None:
    """下载 PDF 并缓存到本地, 返回文件路径。

    缓存命中时直接返回路径, 不重复下载。
    文件以 DOI 命名, 超出 512MB 自动清理最旧文件。

    Args:
        paper: paper dict
        cache_dir: 缓存目录, 默认 ~/.paperpilot_pdf_cache/

    Returns:
        PDF 文件路径, 或 None
    """
    target = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    doi = paper.get("doi", "")
    if doi:
        safe = doi.replace("/", "_").replace("\\", "_")
        filename = f"{safe}.pdf"
    else:
        title = paper.get("title", "") or paper.get("url", "") or str(id(paper))
        h = hashlib.sha256(title.encode()).hexdigest()[:16]
        filename = f"{h}.pdf"

    filepath = target / filename

    # 缓存命中
    if filepath.exists() and filepath.stat().st_size > 0:
        logger.info("PDF 缓存命中: %s", filepath)
        return str(filepath)

    # 下载
    pdf_bytes = download_pdf(paper)
    if not pdf_bytes:
        return None

    target.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(pdf_bytes)
    logger.info("PDF 已缓存: %s (%d bytes)", filepath, len(pdf_bytes))

    _cleanup_cache(target)
    return str(filepath)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """从 PDF 字节流提取纯文本 (PyMuPDF)。"""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = []
        total = 0
        for page in doc:
            t = page.get_text()
            parts.append(t)
            total += len(t)
            if total >= 150000:
                break
        doc.close()
        return "".join(parts)
    except Exception:
        return ""


def _cleanup_cache(cache_dir: Path, max_mb: int = MAX_CACHE_MB) -> None:
    """清理缓存目录，超出上限时删除最旧文件。"""
    files = sorted(cache_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files) / (1024 * 1024)
    while total > max_mb and len(files) > 1:
        oldest = files.pop(0)
        total -= oldest.stat().st_size / (1024 * 1024)
        oldest.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _is_port_open(port: int) -> bool:
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def _find_free_port(start: int = 9230, end: int = 9299) -> int:
    for p in range(start, end):
        if not _is_port_open(p):
            return p
    return start


def _bring_window_to_front() -> None:
    """跨平台: 将浏览器窗口提到前台 (加速 Cloudflare JS 挑战)。"""
    import sys as _sys
    if _sys.platform == "win32":
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        def enum_callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(260)
            user32.GetWindowTextW(hwnd, buf, 260)
            title = buf.value
            if not title or len(title) < 3:
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
            if h:
                nb = ctypes.create_unicode_buffer(260)
                sz = ctypes.c_ulong(260)
                if kernel32.QueryFullProcessImageNameW(h, 0, nb, ctypes.byref(sz)):
                    name = nb.value.lower()
                    if any(k in name for k in ("msedge", "chrome", "chromium")):
                        kernel32.CloseHandle(h)
                        user32.ShowWindow(hwnd, 9)
                        user32.SetForegroundWindow(hwnd)
                        return False
                kernel32.CloseHandle(h)
            return True

        WEP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(WEP(enum_callback), 0)


def _find_chromium_browser() -> Path | None:
    import os as _os
    import sys as _sys

    def _check(path: str) -> Path | None:
        p = Path(_os.path.expandvars(path)) if "$" in path or "%" in path else Path(path)
        return p if p.exists() else None

    if _sys.platform == "win32":
        candidates = [
            "%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe",
            "%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe",
            "%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe",
            "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe",
            "%LOCALAPPDATA%\\Microsoft\\Edge\\Application\\msedge.exe",
            "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe",
        ]
    elif _sys.platform == "darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    for c in candidates:
        result = _check(c)
        if result:
            return result
    return None


def _kill_profile_zombies(profile_dir: Path) -> int:
    """杀掉占用指定 profile 目录的僵尸 Edge 进程，避免 profile 锁冲突。"""
    import psutil
    target = str(profile_dir.resolve())
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = proc.info
            name = (info["name"] or "").lower()
            cmd = " ".join(info["cmdline"] or [])
            if name in ("msedge.exe", "msedge", "chrome.exe", "chrome") and target in cmd:
                proc.kill()
                killed += 1
        except Exception:
            pass
    if killed:
        logger.info("BrowserSession: 清理 %d 个僵尸进程", killed)
    return killed


# ══════════════════════════════════════════════════════════════════
# BrowserSession — 屏幕外 CDP 浏览器
# ══════════════════════════════════════════════════════════════════

DEFAULT_PROFILE = Path.home() / "pp_edge_trusted"

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ];
        arr.item = i => arr[i] || null;
        arr.namedItem = n => arr.find(p => p.name === n) || null;
        arr.refresh = () => {};
        Object.setPrototypeOf(arr, PluginArray.prototype);
        return arr;
    },
});
window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
delete window.__playwright__binding__;
delete window.__pwInitScripts;
delete window.__playwright__;
"""


class BrowserSession:
    """屏幕外 CDP 浏览器，持久化 profile（cookie/CARSI/CF 信任复用）。"""

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        port: int | None = None,
    ):
        self._profile = Path(profile_dir) if profile_dir else DEFAULT_PROFILE
        self._port = port or _find_free_port()
        self._proc: subprocess.Popen | None = None
        self._pw = None
        self._browser = None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    # ── 生命周期 ──

    def start(self) -> bool:
        """启动屏幕外 Edge 并连接 Playwright。"""
        import subprocess as _sp
        _kill_profile_zombies(self._profile)
        browser_path = _find_chromium_browser()
        if not browser_path:
            logger.error("BrowserSession: 未找到浏览器")
            return False

        self._profile.mkdir(parents=True, exist_ok=True)

        # 清理标签页恢复文件，防止 Edge 恢复上次访问的页面而非打开 about:blank
        for f in ("Current Tabs", "Last Tabs", "Current Session", "Last Session"):
            fp = self._profile / "Default" / f
            try:
                if fp.is_file():
                    fp.unlink()
            except Exception:
                pass
        sessions_dir = self._profile / "Default" / "Sessions"
        if sessions_dir.exists():
            try:
                import shutil
                shutil.rmtree(sessions_dir, ignore_errors=True)
            except Exception:
                pass

        args = [
            str(browser_path),
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--restore-last-session=false",
            "--disable-session-crashed-bubble",
            "--disable-features=RestoreSession",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1024,768",
            "--window-position=-32000,-32000",
            "about:blank",
        ]
        try:
            self._proc = _sp.Popen(
                args,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
        except Exception as e:
            logger.error("BrowserSession: 启动失败 — %s", e)
            return False

        # 等待 CDP 就绪
        for _ in range(30):
            time.sleep(0.5)
            if _is_port_open(self._port):
                break
        else:
            logger.warning("BrowserSession: CDP 端口启动超时")
            return True  # 继续尝试连接

        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        logger.info("BrowserSession: 已就绪 port=%d", self._port)
        return True

    def stop(self) -> None:
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()

    # ── 页面操作 ──

    def new_page(self):
        """创建带反检测脚本的页面。"""
        ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(_STEALTH_SCRIPT)
        return page

    def navigate(self, page, url: str, timeout: int = 30) -> bool:
        """导航到 URL，等 domcontentloaded。返回是否成功。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            return True
        except Exception:
            return False

    def _set_window_visible(self, page, visible: bool) -> None:
        """CDP: 将浏览器窗口移到屏幕内或屏幕外。"""
        try:
            cdp = page.context.new_cdp_session(page)
            result = cdp.send("Browser.getWindowForTarget")
            win_id = result.get("windowId")
            bounds = result.get("bounds", {})
            w = bounds.get("width", 1024)
            h = bounds.get("height", 768)
            left = 100 if visible else -32000
            top = 100 if visible else -32000
            cdp.send("Browser.setWindowBounds", {
                "windowId": win_id,
                "bounds": {"left": left, "top": top, "width": w, "height": h},
            })
        except Exception:
            pass

    def wait_for_content(
        self, page, timeout: int = 60
    ) -> bool:
        """等待页面正文渲染（过 CF + JS 加载）。

        窗口默认屏幕外，仅当 CF 挑战卡住超过 12 秒时才临时拉到屏幕内，
        挑战通过后立即隐藏——尽可能减少闪窗。
        """
        deadline = time.time() + timeout
        saw_body = False
        cf_stuck_since = 0.0  # CF 首次出现时间
        window_visible = False
        while time.time() < deadline:
            time.sleep(1.5)
            title = ""
            try:
                title = (page.title() or "").lower()
            except Exception:
                pass

            # 页面尚未完成初始加载（title 为空），继续等待
            if title == "":
                continue

            # CF 挑战进行中（精确匹配，避免学术标题中 "challenges" 误触发）
            cf_titles = ("just a moment...", "请稍候…", "checking your browser",
                         "attention required", "ddos-guard", "one more step")
            cf_active = title in cf_titles or "cloudflare" in title

            if cf_active:
                # CF 持续超过 12 秒 → 临时拉窗口到屏幕内加速
                now = time.time()
                if cf_stuck_since == 0:
                    cf_stuck_since = now
                elif now - cf_stuck_since > 12 and not window_visible:
                    self._set_window_visible(page, True)
                    _bring_window_to_front()
                    window_visible = True
                continue

            cf_stuck_since = 0.0
            # CF 过了就把窗口藏回去
            if window_visible:
                self._set_window_visible(page, False)
                window_visible = False

            try:
                has_body = page.evaluate("""
                    () => {
                        const sel = document.querySelector('#body')
                            || document.querySelector('.Body')
                            || document.querySelector('article')
                            || document.querySelector('main')
                            || document.querySelector('[class*="article-body"]');
                        if (!sel) return false;
                        return sel.textContent.trim().length > 2000;
                    }
                """)
                if has_body:
                    if not saw_body:
                        saw_body = True
                        continue
                    return True
            except Exception:
                pass

        # 超时前兜底：如果窗口还是可见的，藏回去
        if window_visible:
            try:
                self._set_window_visible(page, False)
            except Exception:
                pass
        return False


# ══════════════════════════════════════════════════════════════════
# HTML 全文提取
# ══════════════════════════════════════════════════════════════════

HTML_CACHE_DIR = Path.home() / ".paperpilot_html_cache"


def fetch_full_text(
    paper: dict,
    timeout: int = 60,
) -> str | None:
    """提取论文全文为自包含 HTML 文件（文字 + 图片 base64）。

    流程：屏幕外 CDP 浏览器 → 文章页 → 等 CF + JS 渲染 →
          提取正文容器 HTML → 图片转 base64 → 缓存为 HTML 文件

    Args:
        paper: paper dict，需含 url / doi
        timeout: 最长等待秒数

    Returns:
        缓存 HTML 文件路径，或 None
    """
    doi = paper.get("doi", "")
    url = paper.get("url", "") or (f"https://doi.org/{doi}" if doi else "")
    if not url:
        return None

    # 缓存命中
    cache_path = _html_cache_path(paper)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("HTML 缓存命中: %s", cache_path)
        return str(cache_path)

    publisher = detect_publisher(paper)
    logger.info("HTML 提取 [%s]: %s", publisher, url[:80])

    session = BrowserSession()
    try:
        if not session.start():
            return None

        page = session.new_page()

        # SD: 用 / 代替 /abs/，确保看到完整文章页
        article_url = url.replace("/abs/", "/")
        session.navigate(page, article_url)

        if not session.wait_for_content(page, timeout=timeout):
            logger.warning("HTML 提取: 等待正文超时 [%s]", publisher)
            session.stop()
            return None

        time.sleep(2)

        # 提取正文 HTML（含图片 base64）
        html = _extract_article_html(page, publisher)
        if not html:
            session.stop()
            return None

        # 组装完整 HTML 页面
        title = paper.get("title", "") or _safe_page_title(page)
        full_html = _build_article_page(title, html)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(full_html, encoding="utf-8")
        logger.info("HTML 已缓存: %s (%d chars)", cache_path, len(full_html))

        session.stop()
        return str(cache_path)

    except Exception as e:
        logger.warning("HTML 提取异常: %s", e)
        try:
            session.stop()
        except Exception:
            pass
        return None


def _html_cache_path(paper: dict) -> Path:
    doi = paper.get("doi", "")
    if doi:
        safe = doi.replace("/", "_").replace("\\", "_")
        return HTML_CACHE_DIR / f"{safe}.html"
    title = paper.get("title", "") or paper.get("url", "") or str(id(paper))
    h = hashlib.sha256(title.encode()).hexdigest()[:16]
    return HTML_CACHE_DIR / f"{h}.html"


def _safe_page_title(page) -> str:
    try:
        t = page.title()
        return t if t and len(t) > 3 else "PaperPilot"
    except Exception:
        return "PaperPilot"


def _extract_article_html(page, publisher: str) -> str | None:
    """从已加载的页面中提取正文 HTML，图片转 base64。

    不同出版商使用不同的正文选择器。
    """
    selectors = {
        "sciencedirect": "#body",
        "nejm": "article, .article-body, main",
        "nature": "article, .c-article-body, main",
        "springer": ".c-article-body, article, main",
        "acs": "article, .article-body, main",
        "wiley": "article, .article-body, main",
        "science": "article, .article-body, main",
        "ieee": "article, .article-body, main",
        "iop": "article, .article-body, main",
        "rsc": "article, .article-body, main",
    }
    sel = selectors.get(publisher, "article, main, [class*='article-body']")

    html = page.evaluate(f"""
        async () => {{
            // 找到正文容器
            let body = null;
            const selectors = {sel!r}.split(',').map(s => s.trim());
            for (const s of selectors) {{
                body = document.querySelector(s);
                if (body && body.textContent.trim().length > 2000) break;
                body = null;
            }}
            if (!body) body = document.body;
            if (!body) return null;

            const clone = body.cloneNode(true);

            // 移除不需要的元素
            const remove = clone.querySelectorAll(
                'script, noscript, template, style, '
                + 'nav, header, footer, '
                + '.nav, .navbar, .header, .footer, .sidebar, '
                + '.references, .citation, .bibliography, '
                + '.related, .recommend, .share, .toolbar, .menu, '
                + '.metrics, .journal-info, .rights, .permissions, '
                + 'button, .btn, [role="button"], '
                + '.sr-only, .visually-hidden'
            );
            remove.forEach(el => el.remove());

            // 图片转 base64
            const images = clone.querySelectorAll('img');
            for (const img of images) {{
                const src = img.src || img.getAttribute('data-src') || '';
                if (!src || src.startsWith('data:')) continue;
                // 跳过小图标 / 1px 追踪像素
                if (img.naturalWidth < 50 || img.width < 50) continue;
                try {{
                    const resp = await fetch(src, {{ mode: 'cors', credentials: 'include' }});
                    if (!resp.ok) continue;
                    const blob = await resp.blob();
                    if (blob.size < 1024) continue;  // 跳过小于 1KB 的图
                    const dataUrl = await new Promise((resolve, reject) => {{
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    }});
                    img.src = dataUrl;
                    // 移除懒加载属性，确保 pywebview 中正常显示
                    img.removeAttribute('loading');
                    img.removeAttribute('data-src');
                    img.style.maxWidth = '100%';
                    img.style.height = 'auto';
                }} catch(e) {{}}
            }}

            // 修复表格样式
            clone.querySelectorAll('table').forEach(t => {{
                t.style.width = '100%';
                t.style.overflowX = 'auto';
                t.style.display = 'block';
            }});

            return clone.outerHTML;
        }}
    """)

    if html and len(html) > 500:
        return html
    return None


def _build_article_page(title: str, body_html: str) -> str:
    """组装完整的阅读页面。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape_html(title)}</title>
<style>
  :root {{
    --bg: #1a1a2e;
    --text: #d4d4d8;
    --heading: #e4e4e7;
    --link: #60a5fa;
    --border: #27272a;
    --caption: #71717a;
    --code-bg: #27272a;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Georgia, 'Noto Serif SC', 'Source Han Serif SC', serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.8;
    padding: 40px 24px;
    max-width: 860px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 1.6em; color: var(--heading); margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }}
  h2 {{ font-size: 1.25em; color: var(--heading); margin: 28px 0 12px; }}
  h3 {{ font-size: 1.1em; color: var(--heading); margin: 20px 0 8px; }}
  h4, h5, h6 {{ color: var(--heading); margin: 16px 0 6px; }}
  p {{ margin: 10px 0; }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  img {{ max-width: 100%; height: auto; display: block; margin: 16px auto; border-radius: 4px; }}
  figure {{ margin: 20px 0; text-align: center; }}
  figcaption {{ font-size: 0.85em; color: var(--caption); margin-top: 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; overflow-x: auto; display: block; }}
  th, td {{ border: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
  th {{ background: var(--code-bg); }}
  code {{ background: var(--code-bg); padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  pre {{ background: var(--code-bg); padding: 16px; border-radius: 6px; overflow-x: auto; margin: 12px 0; }}
  blockquote {{ border-left: 3px solid var(--border); padding-left: 16px; color: var(--caption); margin: 12px 0; }}
  ul, ol {{ margin: 10px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
  section {{ margin: 16px 0; }}
  .abstract, .keywords {{ background: var(--code-bg); padding: 12px 16px; border-radius: 6px; margin: 12px 0; }}
  @media (max-width: 640px) {{
    body {{ padding: 16px 12px; font-size: 0.95em; }}
  }}
</style>
</head>
<body>
<h1>{_escape_html(title)}</h1>
{body_html}
</body>
</html>"""


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
