"""论文全文获取工具 — curl_cffi + CDP 双引擎，PDF 优先。

完整链路（每级失败自动降级）：
  Ⓐ PDF 优先（保留排版/图表，供 PDF.js 渲染）
    ① arXiv PDF          → curl_cffi 直链下载
    ② Nature PDF         → curl_cffi 直链下载
    ③ Springer PDF       → curl_cffi 直链下载
    ④ citation_pdf_url   → curl_cffi 解析 meta 标签 → 下载 PDF
    ⑤ CDP PDF 下载       → Playwright + Edge，拦截/抓取 PDF 字节
  Ⓑ 文本兜底（PDF 不可用时）
    ⑥ arXiv HTML         → curl_cffi 抓取 HTML 正文
    ⑦ curl_cffi HTML     → Session 保持 cookie，提取正文容器
    ⑧ CDP HTML           → Playwright + Edge，JS 渲染后提取正文
  Ⓒ 全部失败             → 返回 None

全程纯内存操作，PDF 字节在内存中处理，不写磁盘（可选 save_to 参数落盘）。
"""

import atexit
import logging
import re
import subprocess
import threading
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
_DOI_RE = re.compile(r"10\.\d{4,}/[^\s\"'<>]+")

_ARXIV_PDF = "https://arxiv.org/pdf/{}.pdf"
_ARXIV_HTML = "https://arxiv.org/html/{}"
_SPRINGER_PDF = "https://link.springer.com/content/pdf/{}.pdf"
_NATURE_PDF = "https://www.nature.com/articles/{}.pdf"

_MAX_CHARS = 150000  # 软截断失败时的硬上限（≈100K tokens，1M 窗口内安全）
_FULL_TEXT_MIN = 3000
TIMEOUT = 15

# 参考文献/致谢等段落标题（用于软截断）
_REF_SECTION_RE = re.compile(
    r"\n(?:References?|REFERENCES?|Bibliography|BIBLIOGRAPHY|"
    r"Literature Cited|References and Notes|"
    r"Acknowledgments?|ACKNOWLEDGMENTS?|"
    r"Author Contributions|AUTHOR CONTRIBUTIONS|"
    r"Supplementary|SUPPLEMENTARY|"
    r"Data Availability|DATA AVAILABILITY|"
    r"Competing Interests|COMPETING INTERESTS|"
    r"Funding|FUNDING|"
    r"Notes and references|NOTES AND REFERENCES"
    r")\n"
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*",
           "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}

_ARTICLE_SELECTORS = [
    {"name": "div", "attrs": {"class": "c-article-body"}},
    {"name": "div", "attrs": {"class": "article-body"}},
    {"name": "div", "attrs": {"class": "article-text"}},
    {"name": "section", "attrs": {"class": "article-body"}},
    {"name": "article"},
    {"name": "main"},
    {"name": "div", "attrs": {"id": "body"}},
]

_SKIP_CLASSES = re.compile(
    r"ref(erence)?s?|bibliography|citation|acknowledgment|supplement|"
    r"appendix|figure|fig-|table-wrap|caption|footnote|author-affili|"
    r"nav|header|footer|sidebar|related|recommend|share|toolbar|menu|"
    r"metrics|journal-info|rights|permissions",
    re.IGNORECASE,
)

# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def _extract_arxiv_id(paper: dict) -> str | None:
    url = paper.get("url", "") or ""
    m = _ARXIV_ID_RE.search(url)
    if m:
        return m.group(1)
    doi = paper.get("doi", "") or ""
    if doi.startswith("10.48550/"):
        return doi.removeprefix("10.48550/")
    return None


def _extract_article_id(paper: dict) -> str | None:
    """从 nature.com URL 提取文章 ID。"""
    url = paper.get("url", "") or ""
    m = re.search(r"nature\.com/articles/([^/?\s]+)", url)
    return m.group(1) if m else None


def is_sciencedirect(paper: dict) -> bool:
    """判断是否为 ScienceDirect/Elsevier 论文。"""
    doi = paper.get("doi", "")
    url = paper.get("url", "").lower()
    return "10.1016/" in doi or "sciencedirect.com" in url or "elsevier" in url


def _clean_article(element) -> str:
    for tag in element.find_all(
        ["div", "section", "nav", "aside", "footer", "ol", "ul"],
        class_=_SKIP_CLASSES,
    ):
        tag.decompose()
    for tag in element.find_all(["div", "section"], id=_SKIP_CLASSES):
        tag.decompose()
    return _truncate_before_references(element.get_text(separator="\n", strip=True))


def _extract_article_html(html: str) -> str | None:
    """从 HTML 字符串提取正文文本。"""
    soup = BeautifulSoup(html, "lxml")
    for sel in _ARTICLE_SELECTORS:
        tag = soup.find(**sel)
        if tag and len(tag.get_text(strip=True)) > 1000:
            return _clean_article(tag)
    best, best_n = None, 0
    for div in soup.find_all("div"):
        t = div.get_text(strip=True)
        if len(t) > best_n:
            best_n, best = len(t), div
    return _clean_article(best) if best and best_n > 2000 else None


def _is_full_text(text: str) -> bool:
    return len(text.strip()) > _FULL_TEXT_MIN


def _truncate_before_references(text: str, max_chars: int = _MAX_CHARS) -> str:
    """在参考文献/致谢处软截断，找不到则 fallback 到硬截断。

    要求匹配位置至少在文本 30% 之后，防止标题/摘要中的误匹配。
    """
    m = _REF_SECTION_RE.search(text)
    if m and m.start() > len(text) * 0.3:
        return text[:m.start()].strip()
    return text[:max_chars] if len(text) > max_chars else text


# ══════════════════════════════════════════════════════════════
# Ⓐ PDF 下载链
# ══════════════════════════════════════════════════════════════

def _download_arxiv_pdf(arxiv_id: str) -> bytes | None:
    url = _ARXIV_PDF.format(arxiv_id)
    try:
        resp = cffi.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="chrome120")
        if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
            return resp.content
    except Exception:
        pass
    return None


def _download_nature_pdf(article_id: str) -> bytes | None:
    url = _NATURE_PDF.format(article_id)
    try:
        resp = cffi.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="chrome120")
        if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
            return resp.content
    except Exception:
        pass
    return None


def _download_springer_pdf(doi: str) -> bytes | None:
    url = _SPRINGER_PDF.format(doi)
    try:
        resp = cffi.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="chrome120")
        if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
            return resp.content
    except Exception:
        pass
    return None


def _download_pdf_via_meta(doi: str) -> bytes | None:
    """通过 citation_pdf_url meta 标签获取 PDF。"""
    url = f"https://doi.org/{doi}"
    try:
        resp = cffi.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="chrome120",
                        allow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if meta and meta.get("content"):
            pdf_url = meta["content"]
            pdf_resp = cffi.get(pdf_url, headers=HEADERS, timeout=TIMEOUT,
                                impersonate="chrome120")
            if pdf_resp.status_code == 200 and pdf_resp.content[:5] == b"%PDF-":
                return pdf_resp.content
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════
# CDP 基础设施 — Playwright 持久化浏览器
# ══════════════════════════════════════════════════════════════

_pw = None
_pw_browser = None
_pw_lock = threading.Lock()
_cdp_executor = None


def _get_cdp_executor():
    global _cdp_executor
    if _cdp_executor is None:
        _cdp_executor = ThreadPoolExecutor(max_workers=1)
    return _cdp_executor


def _ensure_browser():
    """Playwright 启动 Edge（headless=False + 屏幕外藏窗，绕过 Cloudflare），持久化复用。

    首次冷启动 2-5s，后续复用 <0.5s。
    """
    global _pw, _pw_browser
    with _pw_lock:
        if _pw_browser is not None and _pw_browser.is_connected():
            return _pw_browser
        _teardown_browser_locked()
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _pw_browser = _pw.chromium.launch(
            channel="msedge",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=-32000,-32000",
                "--window-size=800,600",
            ],
        )
        logger.info("Playwright 浏览器已启动（持久化复用）")
        return _pw_browser


_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
    });
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                { name: 'Widevine Content Decryption Module', filename: 'widevinecdmadapter.dll', description: 'Widevine Content Decryption Module' },
            ];
            arr.item = i => arr[i] || null;
            arr.namedItem = n => arr.find(p => p.name === n) || null;
            arr.refresh = () => {};
            Object.setPrototypeOf(arr, PluginArray.prototype);
            return arr;
        },
    });
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
            const arr = [
                { type: 'application/pdf', suffixes: 'pdf', description: '' },
                { type: 'text/pdf', suffixes: 'pdf', description: '' },
            ];
            arr.item = i => arr[i] || null;
            arr.namedItem = n => arr.find(m => m.type === n) || null;
            Object.setPrototypeOf(arr, MimeTypeArray.prototype);
            return arr;
        },
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'zh-CN'] });
    Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', { get: () => 1920 });
        Object.defineProperty(window, 'outerHeight', { get: () => 1080 });
    }
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
    const _q = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) => (
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission, onchange: null })
            : _q(p)
    );
    delete window.__playwright__binding__;
    delete window.__pwInitScripts;
    delete window.__playwright__;
"""


def _new_stealth_page(browser):
    """创建带反检测脚本的新页面。"""
    page = browser.new_page(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    page.add_init_script(_STEALTH_SCRIPT)
    return page


def _teardown_browser_locked():
    global _pw, _pw_browser
    if _pw_browser is not None:
        try:
            _pw_browser.close()
        except Exception:
            pass
        _pw_browser = None
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None


def _teardown_browser():
    with _pw_lock:
        _teardown_browser_locked()


atexit.register(_teardown_browser)


def _download_pdf_via_cdp(paper: dict) -> bytes | None:
    """PDF 下载：Playwright 启动 Edge（非 headless + 反检测）→ 文章页过 Cloudflare → 下 PDF。

    headless=False 是绕过 Cloudflare 检测的关键。窗口放到屏幕外不可见。
    """
    # ScienceDirect: pdfft 端点被 Cloudflare WAF 额外拦截，原生 Chromium 也解不了托管挑战，
    # 直接跳过 PDF 尝试，节省 ~30s。
    if is_sciencedirect(paper):
        return None

    doi = paper.get("doi", "")
    url = paper.get("url", "")
    guessed_pdf = _guess_pdf_url(paper)

    browser = _ensure_browser()
    page = _new_stealth_page(browser)

    # Step 1: 先走文章页，让 Cloudflare 验证通过（关键：不能直接跳 PDF URL）
    article_url = url or (f"https://doi.org/{doi}" if doi else "")
    if not article_url:
        return None

    try:
        if doi:
            page.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded",
                      timeout=20000)
        else:
            page.goto(article_url, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(5)  # 等 Cloudflare 挑战 + JS 渲染

    # Step 2: 从文章页收集 PDF URL
    pdf_urls = []
    if guessed_pdf:
        pdf_urls.append(guessed_pdf)
    # citation_pdf_url meta
    try:
        meta_pdf = page.evaluate("""
            () => {
                const m = document.querySelector('meta[name="citation_pdf_url"]');
                return m ? m.content : null;
            }
        """)
        if meta_pdf and meta_pdf not in pdf_urls:
            pdf_urls.append(meta_pdf)
    except Exception:
        pass
    # 页面上的 PDF 链接
    try:
        found = page.evaluate("""
            () => {
                const links = [];
                for (const a of document.querySelectorAll('a')) {
                    const h = a.href || '';
                    if ((h.includes('/pdf/') || h.endsWith('.pdf')) && !links.includes(h))
                        links.push(h);
                }
                return links;
            }
        """)
        for l in found:
            if l not in pdf_urls:
                pdf_urls.append(l)
    except Exception:
        pass

    # Step 3: 先在文章页点击 PDF 按钮/链接（浏览器自然处理 cookie）
    result = _cdp_click_pdf_button(page)
    if result:
        return result

    # Step 4: 逐一尝试 PDF URL（从文章页 JS fetch）
    for p_url in pdf_urls:
        result = _cdp_fetch_pdf(page, p_url) or _cdp_download_pdf(page, p_url)
        if result:
            return result

    # 兜底：JS fetch 文章页本身
    result = _cdp_fetch_pdf(page, article_url)
    if result:
        return result

    return None


def _guess_pdf_url(paper: dict) -> str | None:
    """根据 DOI/URL 构造常见出版商 PDF 直链（curl_cffi 不可达时走 CDP 验证）。"""
    doi = paper.get("doi", "")
    url = paper.get("url", "").lower()

    # Science
    if "10.1126" in doi or "science.org" in url:
        return f"https://www.science.org/doi/pdf/{doi}?download=true"
    # ACS
    if "10.1021" in doi or "pubs.acs.org" in url:
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    # IOP Science
    if "10.1088" in doi or "iop.org" in url:
        return f"https://iopscience.iop.org/article/{doi}/pdf"
    # Wiley
    if "10.1002" in doi or "wiley.com" in url:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    # RSC (Royal Society of Chemistry)
    if "10.1039" in doi or "rsc.org" in url:
        return f"https://pubs.rsc.org/en/content/articlepdf/{doi.split('/')[-1]}"

    return None


def _impersonation_for(paper: dict) -> str:
    """根据出版商选择 curl_cffi impersonation 版本。"""
    doi = paper.get("doi", "")
    url = paper.get("url", "").lower()
    if "10.1002" in doi or "wiley.com" in url:
        return "chrome124"
    return "chrome120"


def _cdp_click_pdf_button(page) -> bytes | None:
    """在文章页上找到 PDF 按钮并点击，拦截下载或跳转后抓取 PDF。

    优先点击按钮（PDF、Download 等），其次是链接。浏览器原生处理 cookie/Cloudflare。
    """
    import tempfile, os as _os
    try:
        # 等待可能的 JS 渲染
        time.sleep(2)
        # 找 PDF 相关的按钮或链接
        clicked = page.evaluate("""
            () => {
                const els = document.querySelectorAll('a, button, div[role="button"], span[role="button"]');
                for (const el of els) {
                    const text = (el.textContent || '').toLowerCase().trim();
                    const href = (el.href || '').toLowerCase();
                    const cls = (el.className || '').toString().toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    // 匹配 PDF 按钮：文本含 pdf/download 或 class/id 含 pdf
                    if (text === 'pdf' || text === 'download pdf' || text === 'pdf ('
                        || id === 'pdf' || cls.includes('pdf-btn') || cls.includes('pdf-download')
                        || (href.includes('/pdf/') && text.length < 20)) {
                        el.click();
                        return true;
                    }
                }
                // 兜底：点击任何含 /pdf/ 的链接
                for (const el of els) {
                    const href = (el.href || '').toLowerCase();
                    if (href.includes('/pdf/')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }
        """)
        if not clicked:
            return None

        # 等待跳转或下载
        time.sleep(3)
        # 检查当前 URL
        new_url = page.evaluate("() => window.location.href")
        logger.debug("CDP click 后 URL: %.100s", new_url[:100])

        # 尝试拦截下载
        try:
            with page.expect_download(timeout=10000) as dl:
                pass  # download 可能已经触发
            download = dl.value
            temp = Path.home() / ".paperpilot_temp.pdf"
            download.save_as(str(temp))
            pdf_bytes = temp.read_bytes()
            temp.unlink(missing_ok=True)
            if pdf_bytes[:5] == b"%PDF-":
                logger.info("CDP 点击 PDF 按钮成功: %s bytes", len(pdf_bytes))
                return pdf_bytes
        except Exception:
            pass

        # 下载未触发，试试从新页面 JS fetch
        return _cdp_fetch_pdf(page, new_url)
    except Exception as e:
        logger.debug("CDP click PDF 失败: %s", e)
        return None


def _cdp_download_pdf(page, pdf_url: str) -> bytes | None:
    """通过 Playwright expect_download 拦截 PDF 下载；失败则导航 + JS fetch。

    分两步：先用 expect_download 拦截下载事件（适用于直接触发下载的出版商），
    超时后页面已导航到 PDF URL，等待 Cloudflare → JS fetch 获取内容。
    """
    logger.info("CDP: 尝试下载 PDF: %.80s", pdf_url)
    # Step A: 尝试拦截下载事件（Science 等会触发 Content-Disposition: attachment）
    try:
        with page.expect_download(timeout=15000) as dl:
            try:
                page.goto(pdf_url, timeout=15000)
            except Exception:
                pass
        download = dl.value
        temp = Path.home() / ".paperpilot_temp.pdf"
        download.save_as(str(temp))
        pdf_bytes = temp.read_bytes()
        temp.unlink(missing_ok=True)
        if pdf_bytes[:5] == b"%PDF-":
            logger.info("CDP PDF 下载成功: %s bytes", len(pdf_bytes))
            return pdf_bytes
    except Exception:
        pass  # 下载未触发（PDF 内联显示），页面已导航到 pdf_url

    # Step B: 页面可能正在显示 Cloudflare 挑战 → 等待解析 → JS fetch
    time.sleep(3)
    try:
        content = page.content()[:500]
        if "安全" in content or "verification" in content.lower() or "challenge" in content.lower():
            logger.debug("CDP PDF URL 触发 Cloudflare 验证，等待...")
            time.sleep(8)
    except Exception:
        pass

    return _cdp_fetch_pdf(page, pdf_url)


def _cdp_fetch_pdf(page, url: str | None = None) -> bytes | None:
    """通过 JS fetch 获取 PDF 字节（分块编码，支持大文件）。

    Args:
        page: Playwright page 对象
        url: 要 fetch 的 URL，None 则用当前页面 URL
    """
    import base64
    target = url or "window.location.href"

    try:
        b64 = page.evaluate(f"""
            async () => {{
                const resp = await fetch({target!r} || window.location.href);
                if (!resp.ok) return null;
                const ct = resp.headers.get('content-type') || '';
                // 检查是否是 PDF
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                // 验证 PDF 头
                if (bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68 || bytes[3] !== 70)
                    return null;
                // 分块编码 base64，避免大文件字符串溢出
                const chunkSize = 8192;
                let binary = '';
                for (let i = 0; i < bytes.length; i += chunkSize) {{
                    const chunk = bytes.slice(i, i + chunkSize);
                    binary += String.fromCharCode.apply(null, chunk);
                }}
                return btoa(binary);
            }}
        """)
        if b64:
            pdf_bytes = base64.b64decode(b64)
            if pdf_bytes[:5] == b"%PDF-":
                logger.info("CDP JS fetch PDF 成功 (%s): %s bytes",
                           "当前页" if url is None else url[:80], len(pdf_bytes))
                return pdf_bytes
    except Exception as e:
        logger.warning("CDP JS fetch 失败 (%.80s): %s", target if isinstance(target, str) else url or "", e)
    return None


# ══════════════════════════════════════════════════════════════
# Ⓑ 文本兜底链
# ══════════════════════════════════════════════════════════════

def _fetch_arxiv_html(arxiv_id: str) -> str | None:
    url = _ARXIV_HTML.format(arxiv_id)
    try:
        resp = cffi.get(url, headers=HEADERS, timeout=TIMEOUT, impersonate="chrome120")
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        main = soup.find("div", class_="ltx_page_main") or soup.find("article")
        if not main:
            return None
        for tag in main.find_all(["span", "div"],
                                 class_=re.compile(r"ltx_bib|ltx_references")):
            tag.decompose()
        return _truncate_before_references(main.get_text(separator="\n", strip=True))
    except Exception:
        return None


def _fetch_html_full_text(paper: dict) -> str | None:
    """curl_cffi Session → HTML 正文提取。"""
    doi = paper.get("doi", "")
    url = paper.get("url", "")

    session = cffi.Session()
    session.headers.update(HEADERS)

    try:
        # 先走 doi.org 建立 cookie 链
        if doi:
            try:
                session.get(f"https://doi.org/{doi}", timeout=TIMEOUT,
                            impersonate="chrome120", allow_redirects=True)
            except Exception:
                pass

        page_url = url or f"https://doi.org/{doi}"
        resp = session.get(page_url, timeout=TIMEOUT, impersonate="chrome120",
                          allow_redirects=True)
        if resp.status_code != 200 or len(resp.text) < 5000:
            return None

        return _extract_article_html(resp.text)
    except Exception:
        return None


def _fetch_html_via_cdp(paper: dict) -> str | None:
    """HTML 正文提取 — Playwright 启动 Edge（反检测），JS 渲染后抓取。"""
    doi = paper.get("doi", "")
    url = paper.get("url", "")

    browser = _ensure_browser()
    page_url = url or (f"https://doi.org/{doi}" if doi else "")
    if not page_url:
        return None

    is_sd = is_sciencedirect(paper)

    if is_sd:
        # ScienceDirect: 用全新隐身 context（避免持久化 profile 的 CF 污染 cookie 残留）
        # CF 托管挑战在干净 context 里一般 5-10s 自动通过
        sd_context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = sd_context.new_page()
        page.add_init_script(_STEALTH_SCRIPT)

        page_url = page_url.replace("/abs/", "/")
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        _wait_for_sd_article(page, timeout=45)
        content = page.content()
        text = _extract_sd_body(content)
        sd_context.close()
        if text and _is_full_text(text):
            return text
    else:
        page = _new_stealth_page(browser)
        # 其他出版商：doi.org cookie 链 + 通用提取
        if doi:
            try:
                page.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded",
                          timeout=20000)
                time.sleep(5)
            except Exception:
                pass
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        _wait_for_content(page, timeout=20)
        content = page.content()
        text = _extract_article_html(content)
        if text and _is_full_text(text):
            return text

    # 兜底：全页文本提取
    try:
        content = page.content() if 'page' in dir() else ""
    except Exception:
        content = ""
    if content:
        soup = BeautifulSoup(content, "lxml")
        if soup.body:
            raw = soup.body.get_text(separator="\n", strip=True)
            if _is_full_text(raw):
                return _truncate_before_references(raw)

    return None


def _wait_for_sd_article(page, timeout: int = 45):
    """等待 ScienceDirect 文章页完整渲染（CF 挑战 + div#body 正文）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        title = page.title()
        # CF 挑战中：标题为 "请稍候…" / "Just a moment..." / "ScienceDirect"
        if title in ('ScienceDirect', '请稍候…', 'Just a moment...') or 'challenge' in title.lower():
            continue
        # 检查 div#body 是否有正文
        try:
            content = page.content()
            soup = BeautifulSoup(content, "lxml")
            body = soup.find("div", id="body") or soup.find("div", class_="Body")
            if body and len(body.get_text(strip=True)) > 5000:
                return
        except Exception:
            pass


def _extract_sd_body(html: str) -> str | None:
    """从 ScienceDirect 文章页提取正文（div#body 优先）。"""
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("div", id="body") or soup.find("div", class_="Body")
    if not body:
        article = soup.find("article")
        if article:
            body = article
        else:
            return None
    return _clean_article(body)


def _wait_for_content(page, timeout: int = 20):
    """等待页面正文出现（Cloudflare + JS 渲染），Playwright 事件驱动。"""
    try:
        page.wait_for_function("""
            () => {
                const a = document.querySelector('article');
                if (a && a.textContent.trim().length > 500) return true;
                const m = document.querySelector('main');
                return m && m.textContent.trim().length > 1000;
            }
        """, timeout=timeout * 1000)
    except Exception:
        pass



# ══════════════════════════════════════════════════════════════
# 通用页面提取（可见 Edge 兜底用）
# ══════════════════════════════════════════════════════════════

def _extract_pdf_from_page(page) -> bytes | None:
    """从任意已加载页面提取 PDF：meta 标签 → PDF 按钮 → JS fetch → 链接扫描。"""
    # 1. citation_pdf_url meta
    try:
        meta_pdf = page.evaluate(
            '() => { const m = document.querySelector(\'meta[name="citation_pdf_url"]\');'
            ' return m ? m.content : null; }'
        )
        if meta_pdf:
            result = _cdp_download_pdf(page, meta_pdf) or _cdp_fetch_pdf(page, meta_pdf)
            if result:
                return result
    except Exception:
        pass

    # 2. 点击 PDF 按钮
    result = _cdp_click_pdf_button(page)
    if result:
        return result

    # 3. JS fetch 当前页（可能直接是 PDF URL）
    result = _cdp_fetch_pdf(page)
    if result:
        return result

    # 4. 扫描页面上的 PDF 链接
    try:
        pdf_links = page.evaluate("""
            () => {
                const links = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const h = a.href;
                    if (h.endsWith('.pdf') || h.includes('/pdf/') || h.includes('pdfft'))
                        links.push(h);
                }
                return [...new Set(links)];
            }
        """)
        for link in (pdf_links or [])[:5]:
            result = _cdp_download_pdf(page, link) or _cdp_fetch_pdf(page, link)
            if result:
                return result
    except Exception:
        pass

    return None


def _extract_html_from_page(page) -> str | None:
    """从任意已加载页面提取格式化的 HTML 正文（含图片/表格样式）。"""
    try:
        html = page.evaluate("""
            () => {
                const selectors = [
                    '#body', '.Body', 'article',
                    '[class*="article-body"]', '[class*="Article"]',
                    'main', '.c-article-body', '.article-text'
                ];
                let body = null;
                for (const sel of selectors) {
                    body = document.querySelector(sel);
                    if (body && body.textContent.trim().length > 500) break;
                    body = null;
                }
                if (!body) body = document.body;
                if (!body) return null;
                const clone = body.cloneNode(true);
                clone.querySelectorAll('script, noscript, template, style').forEach(e => e.remove());
                clone.querySelectorAll('img').forEach(img => {
                    img.style.maxWidth = '100%';
                    img.style.height = 'auto';
                    img.style.display = 'block';
                    img.style.margin = '16px auto';
                    if (!img.hasAttribute('loading')) img.setAttribute('loading', 'lazy');
                    if (!img.src && img.dataset.src) img.src = img.dataset.src;
                });
                clone.querySelectorAll('table').forEach(t => {
                    t.style.width = '100%';
                    t.style.overflowX = 'auto';
                    t.style.display = 'block';
                });
                return clone.innerHTML;
            }
        """)
        return html or None
    except Exception as e:
        logger.debug("HTML 提取失败: %s", e)
        return None


# ══════════════════════════════════════════════════════════════
# 公开 API
# ══════════════════════════════════════════════════════════════

def download_pdf(paper: dict, save_to: str | Path | None = None) -> bytes | None:
    """下载论文 PDF，返回字节流（纯内存操作）。

    链路：arXiv → Nature → Springer → citation_pdf_url → CDP

    如需落盘（供 PDF.js 渲染），传入 save_to 指定路径，
    或使用 cache_pdf() 自动缓存到本地。

    Args:
        paper: paper dict，需含 url / doi
        save_to: 可选，指定保存路径

    Returns:
        PDF 字节流，或 None
    """
    doi = paper.get("doi", "")
    url = paper.get("url", "")

    # ① arXiv
    arxiv_id = _extract_arxiv_id(paper)
    if arxiv_id:
        pdf = _download_arxiv_pdf(arxiv_id)
        if pdf:
            logger.info("PDF 来源: arXiv")
            if save_to:
                Path(save_to).write_bytes(pdf)
            return pdf

    # ② Nature 直链
    article_id = _extract_article_id(paper)
    if article_id:
        pdf = _download_nature_pdf(article_id)
        if pdf:
            logger.info("PDF 来源: Nature 直链")
            if save_to:
                Path(save_to).write_bytes(pdf)
            return pdf

    # ③ Springer 直链
    if doi:
        pdf = _download_springer_pdf(doi)
        if pdf:
            logger.info("PDF 来源: Springer 直链")
            if save_to:
                Path(save_to).write_bytes(pdf)
            return pdf

    # ④ Guessed PDF 直链（curl_cffi 快速通道，ACS 等 2-3s 完成）
    guessed = _guess_pdf_url(paper)
    if guessed:
        try:
            imp = _impersonation_for(paper)
            resp = cffi.get(guessed, headers=HEADERS, timeout=TIMEOUT, impersonate=imp)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                logger.info("PDF 来源: 直链 curl_cffi (%s)", guessed[:60])
                if save_to:
                    Path(save_to).write_bytes(resp.content)
                return resp.content
        except Exception:
            pass

    # ⑤ citation_pdf_url meta
    if doi:
        pdf = _download_pdf_via_meta(doi)
        if pdf:
            logger.info("PDF 来源: citation_pdf_url")
            if save_to:
                Path(save_to).write_bytes(pdf)
            return pdf

    # ⑥ CDP PDF（持久单线程执行器，避免 Playwright Sync API 跨线程问题）
    try:
        pdf = _get_cdp_executor().submit(_download_pdf_via_cdp, paper).result(timeout=90)
        if pdf:
            logger.info("PDF 来源: CDP")
            if save_to:
                Path(save_to).write_bytes(pdf)
            return pdf
    except Exception as e:
        logger.warning("CDP PDF 失败: %s", e)

    return None


_DEFAULT_CACHE_DIR = Path.home() / ".paperpilot_pdf_cache"
_MAX_CACHE_MB = 500


def cache_pdf(paper: dict, cache_dir: str | Path | None = None) -> str | None:
    """下载论文 PDF 并缓存到本地，返回文件路径供 PDF.js 渲染。

    先检查缓存，命中直接返回路径；未命中才走 download_pdf() 下载。
    写入后自动清理超出 500MB 的最旧文件。

    Args:
        paper: paper dict，需含 doi / url
        cache_dir: 缓存目录，默认 ~/.paperpilot_pdf_cache/

    Returns:
        PDF 文件路径，或 None
    """
    target_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
    doi = paper.get("doi", "")
    filename = _cache_filename(paper, doi)
    filepath = target_dir / filename

    # 缓存命中：文件存在且非空 → 直接返回
    if filepath.exists() and filepath.stat().st_size > 0:
        logger.info("PDF 缓存命中: %s", filepath)
        return str(filepath)

    pdf_bytes = download_pdf(paper)
    if not pdf_bytes:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(pdf_bytes)
    logger.info("PDF 已缓存: %s (%s bytes)", filepath, len(pdf_bytes))

    _cleanup_cache(target_dir)
    return str(filepath)


def _cleanup_cache(cache_dir: Path, max_mb: int = _MAX_CACHE_MB) -> None:
    """清理缓存目录，超出上限时删除最旧文件。"""
    files = sorted(cache_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime)
    total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
    while total_mb > max_mb and len(files) > 1:
        oldest = files.pop(0)
        total_mb -= oldest.stat().st_size / (1024 * 1024)
        oldest.unlink(missing_ok=True)
        logger.info("缓存清理: 删除 %s", oldest.name)


def _cache_filename(paper: dict, doi: str) -> str:
    """生成缓存文件名：DOI 优先，否则用标题/URL hash。"""
    if doi:
        safe_doi = doi.replace("/", "_").replace("\\", "_")
        return f"{safe_doi}.pdf"
    title = paper.get("title", "") or paper.get("url", "") or ""
    if title:
        import hashlib
        h = hashlib.sha256(title.encode()).hexdigest()[:16]
        return f"{h}.pdf"
    return f"paper_{id(paper)}.pdf"


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """从 PDF 字节流提取纯文本（PyMuPDF，纯内存操作）。

    全量提取后软截断到参考文献/致谢之前，避免 AI 读取无用的引用列表。
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        parts = []
        total = 0
        for page in doc:
            t = page.get_text()
            parts.append(t)
            total += len(t)
            if total >= _MAX_CHARS:  # 硬上限防 OOM
                break
        doc.close()
        return _truncate_before_references("".join(parts))
    except Exception:
        return ""


def fetch_full_text(paper: dict, timeout: int = TIMEOUT) -> str | None:
    """获取论文全文文本（PDF 优先，文本兜底）。

    链路：
      PDF: arXiv → Nature → Springer → meta → CDP
      ↓ 全部失败
      文本: arXiv HTML → curl_cffi HTML → CDP HTML
      ↓ 全部失败
      返回 None

    Args:
        paper: paper dict，需含 url / doi / source
        timeout: HTTP 超时秒数

    Returns:
        全文文本，或 None（调用方应回退用 abstract）
    """
    # ── 方案 A: PDF 下载 → 提取文本 ──
    pdf_bytes = download_pdf(paper)
    if pdf_bytes:
        text = extract_pdf_text(pdf_bytes)
        if _is_full_text(text):
            return text
        # PDF 文本太短（可能图片型 PDF），不放弃，继续尝试 HTML

    # ── 方案 B: 本地 PDF ──
    source = paper.get("source", "")
    local_url = paper.get("url", "")
    if source == "local_pdf" and local_url and Path(local_url).exists():
        try:
            with open(local_url, "rb") as f:
                local_bytes = f.read()
            text = extract_pdf_text(local_bytes)
            if _is_full_text(text):
                return text
        except OSError:
            pass

    # ── 方案 C: arXiv HTML ──
    arxiv_id = _extract_arxiv_id(paper)
    if arxiv_id:
        text = _fetch_arxiv_html(arxiv_id)
        if text and _is_full_text(text):
            return text

    # ── 方案 D: curl_cffi HTML 正文 ──
    text = _fetch_html_full_text(paper)
    if text and _is_full_text(text):
        return text

    # ── 方案 E: CDP HTML 正文（持久单线程执行器，避免 Playwright Sync API 跨线程问题）──
    try:
        text = _get_cdp_executor().submit(_fetch_html_via_cdp, paper).result(timeout=60)
        if text and _is_full_text(text):
            return text
    except Exception as e:
        logger.warning("CDP HTML 失败: %s", e)

    return None
