"""
CDP-based paper downloader.

Connects to the user's REAL Edge browser via Chrome DevTools Protocol,
inheriting all cookies, institutional login state (CARSI), and Cloudflare trust.

Usage:
    from paperpilot.cdp_fetcher import CDPFetcher

    with CDPFetcher() as fetcher:
        pdf = fetcher.download_pdf(paper)       # → bytes | None
        text = fetcher.fetch_full_text(paper)    # → str | None
        results = fetcher.batch_download(papers) # → list[dict]

Architecture:
    User's Edge (real profile) <--CDP--> Playwright <-- Python

The user's real Edge profile carries:
    - CARSI institutional login cookies (sciencedirect.com / elsevier.com)
    - Cloudflare trust records (cf_clearance, bot detection history)
    - All other site logins (Wiley, Springer, etc.)

This means: one CF verification per domain per session, not per paper.
Batch downloading 20 SD papers = at most 1 manual click.
"""

import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Edge paths & CDP config ──
CDP_PORT = 9222
_EDGE_EXE = None
for _p in [
    os.path.expandvars("%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe"),
    os.path.expandvars("%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe"),
]:
    if Path(_p).exists():
        _EDGE_EXE = _p
        break

_USER_DATA = None
if (local_app := os.environ.get("LOCALAPPDATA")):
    _ud = Path(local_app) / "Microsoft" / "Edge" / "User Data"
    if _ud.exists():
        _USER_DATA = str(_ud)

# ── Stealth script (same as text_fetcher.py) ──
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


# ══════════════════════════════════════════════════════════════
# Edge lifecycle
# ══════════════════════════════════════════════════════════════

def _is_port_open(port: int = CDP_PORT) -> bool:
    """Check if CDP port is already open (Edge is running with --remote-debugging-port)."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def _is_edge_running() -> bool:
    """Check if ANY Edge process is running."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe"],
            capture_output=True, text=True,
        )
        return "msedge.exe" in result.stdout
    except Exception:
        return False


def _kill_edge():
    """Kill all Edge processes."""
    subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"], capture_output=True)
    time.sleep(1.5)


def _launch_edge_with_cdp(port: int = CDP_PORT) -> Optional[subprocess.Popen]:
    """Launch Edge with CDP enabled, using the user's real profile."""
    if not _EDGE_EXE:
        logger.warning("找不到 Edge")
        return None
    if not _USER_DATA:
        logger.warning("找不到 Edge User Data")
        return None

    args = [
        _EDGE_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_USER_DATA}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for CDP port to open
    for _ in range(15):
        time.sleep(0.5)
        if _is_port_open(port):
            return proc
    logger.warning("Edge CDP 端口启动超时")
    return proc


def _restart_edge_normal() -> Optional[subprocess.Popen]:
    """Restart Edge WITHOUT CDP, restoring normal user session."""
    if not _EDGE_EXE:
        return None
    if not _USER_DATA:
        return None
    args = [
        _EDGE_EXE,
        f"--user-data-dir={_USER_DATA}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ══════════════════════════════════════════════════════════════
# Publisher detection
# ══════════════════════════════════════════════════════════════

def is_sciencedirect(paper: dict) -> bool:
    """Check if paper is from ScienceDirect / Elsevier."""
    doi = paper.get("doi", "")
    url = paper.get("url", "").lower()
    return "10.1016/" in doi or "sciencedirect.com" in url or "elsevier" in url


def _is_arxiv(paper: dict) -> bool:
    doi = paper.get("doi", "")
    url = paper.get("url", "").lower()
    return "10.48550/" in doi or "arxiv.org" in url or "arxiv" in paper.get("source", "").lower()


def _is_nature(paper: dict) -> bool:
    doi = paper.get("doi", "")
    url = paper.get("url", "").lower()
    return "10.1038/" in doi or "nature.com" in url


# ══════════════════════════════════════════════════════════════
# Page helpers
# ══════════════════════════════════════════════════════════════

def _wait_for_cloudflare(page, timeout: int = 30) -> bool:
    """Wait for Cloudflare challenge to clear. Returns True if page passed CF."""
    deadline = time.time() + timeout
    saw_non_cf = False
    # Generic publisher titles that appear during loading (not real article titles)
    _generic_titles = (
        "ScienceDirect", "Elsevier", "ScienceDirect.com",
        "Loading", "loading",
    )
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            title = (page.title() or "")
            title_lower = title.lower()

            # Definite CF markers — still on challenge page
            if title in ("Just a moment...", "请稍候…", ""):
                continue
            if "challenge" in title_lower and "please wait" in title_lower:
                continue

            # Generic publisher titles — page still loading, not ready yet
            if title in _generic_titles:
                continue

            # Check page body for CF challenge text
            body_text = (page.evaluate(
                "() => document.body?.textContent?.substring(0, 3000) || ''") or "").lower()
            if any(m in body_text for m in [
                "are you a robot", "not a robot",
                "verify you are a human", "verification required",
                "checking your browser before accessing",
            ]):
                continue

            # If we've seen a non-CF title and body is also clean, page is loaded
            if not saw_non_cf:
                saw_non_cf = True
                continue  # one more round to confirm
            return True
        except Exception:
            pass
    return False


def _wait_for_article_body(page, timeout: int = 20) -> bool:
    """Wait for article content container to appear (>2000 chars)."""
    for _ in range(timeout):
        time.sleep(1)
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
                return True
        except Exception:
            pass
    return False


def _extract_pdf_link_from_page(page) -> Optional[str]:
    """Find the PDF download link on the current article page."""
    # 1. citation_pdf_url meta tag
    try:
        meta = page.evaluate(
            '() => document.querySelector(\'meta[name="citation_pdf_url"]\')?.content || null'
        )
        if meta:
            return meta
    except Exception:
        pass

    # 2. SD-specific: pdfDownload JSON block
    try:
        content = page.content()
        idx = content.find('"pdfDownload"')
        if idx > 0:
            import json
            block = content[idx:]
            block = block[block.find('{'):]
            depth, end = 0, 0
            in_str, esc = False, False
            for i, c in enumerate(block):
                if esc:
                    esc = False
                    continue
                if c == '\\':
                    esc = True
                    continue
                if c == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            config = json.loads(block[:end])
            md5 = config.get("urlMetadata", {}).get("queryParams", {}).get("md5", "")
            pid = config.get("urlMetadata", {}).get("queryParams", {}).get("pid", "")
            if md5 and pid:
                # Extract PII from page URL
                pii = config.get("pii", "") or config.get("urlMetadata", {}).get("pii", "")
                if not pii and "science/article/pii/" in page.url:
                    pii = page.url.split("/pii/")[1].split("?")[0]
                if pii:
                    return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?md5={md5}&pid={pid}"
    except Exception:
        pass

    # 3. Generic PDF links on the page
    try:
        links = page.evaluate("""
            () => {
                const result = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const h = a.href;
                    if (h.includes('/pdf/') || h.includes('pdfft') || h.endsWith('.pdf')) {
                        result.push(h);
                    }
                }
                return [...new Set(result)];
            }
        """)
        for link in (links or []):
            if link and link.startswith("http"):
                return link
    except Exception:
        pass

    return None


# ══════════════════════════════════════════════════════════════
# Text extraction
# ══════════════════════════════════════════════════════════════

def _extract_article_text(html: str) -> Optional[str]:
    """Extract clean article text from HTML, preferring main content containers."""
    soup = BeautifulSoup(html, "lxml")

    # Try specific selectors first
    for sel_id, sel_class in [
        ("body", None), (None, "Body"), ("article", None),
        (None, "article-body"), (None, "Article"),
    ]:
        if sel_id:
            el = soup.find("div", id=sel_id) or soup.find("section", id=sel_id)
        else:
            el = soup.find("div", class_=sel_class) or soup.find("section", class_=sel_class)
        if not el:
            el = soup.find(sel_id or sel_class)
        if el and len(el.get_text(strip=True)) > 2000:
            # Remove nav/scripts/styles
            for tag in el.find_all(["script", "style", "noscript", "nav", "footer"]):
                tag.decompose()
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 2000:
                return text

    # Fallback: whole body
    body = soup.body
    if body:
        for tag in body.find_all(["script", "style", "noscript"]):
            tag.decompose()
        text = body.get_text(separator="\n", strip=True)
        if len(text) > 1000:
            return text

    return None


# ══════════════════════════════════════════════════════════════
# Main fetcher class
# ══════════════════════════════════════════════════════════════

class CDPFetcher:
    """CDP-based paper downloader using user's real Edge profile.

    Usage:
        with CDPFetcher() as f:
            pdf = f.download_pdf(paper)
            results = f.batch_download(papers, rate_limit=5.0)

    The context manager handles Edge lifecycle:
        enter → ensure Edge with CDP is running → connect
        exit  → disconnect, optionally restart Edge normally
    """

    def __init__(self, restart_edge_on_exit: bool = True):
        self._restart_on_exit = restart_edge_on_exit
        self._edge_proc: Optional[subprocess.Popen] = None
        self._pw = None
        self._browser = None
        self._was_edge_running = False

    # ── context manager ──

    def __enter__(self) -> "CDPFetcher":
        self._connect()
        return self

    def __exit__(self, *args):
        self._disconnect()

    # ── connection management ──

    def _connect(self):
        """Ensure Edge with CDP is running, then connect via Playwright."""
        from playwright.sync_api import sync_playwright

        self._was_edge_running = _is_edge_running()

        if _is_port_open(CDP_PORT):
            logger.info("Edge CDP 端口 %d 已就绪，直连", CDP_PORT)
        else:
            logger.info("Edge CDP 端口未开，重启 Edge 并启用 CDP...")
            if self._was_edge_running:
                _kill_edge()
            self._edge_proc = _launch_edge_with_cdp(CDP_PORT)
            if not self._edge_proc:
                raise RuntimeError("无法启动 Edge，请检查 Edge 是否安装")

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )
        logger.info("CDP 已连接，%d 个 context", len(self._browser.contexts))

    def _disconnect(self):
        """Close CDP connection. Optionally restart Edge normally."""
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
        if self._edge_proc:
            try:
                self._edge_proc.terminate()
            except Exception:
                pass
            self._edge_proc = None

        if self._restart_on_exit and self._was_edge_running:
            logger.info("恢复 Edge 正常模式...")
            _restart_edge_normal()

    # ── page helpers ──

    def _new_page(self):
        """Create a fresh page in the existing browser context."""
        ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        page = ctx.new_page()
        page.add_init_script(_STEALTH_SCRIPT)
        return page

    # ── PDF download ──

    def download_pdf(self, paper: dict, timeout: int = 60) -> Optional[bytes]:
        """Download PDF for a single paper via CDP.

        For ScienceDirect: pdfft endpoint has Cloudflare managed challenge that
        blocks even native browsers. Strategy: click "View PDF" button → capture
        popup to sciencedirectassets.com (no CF) → curl_cffi download.

        For other publishers: navigate to PDF URL directly.

        Returns PDF bytes or None.
        """
        page = self._new_page()
        try:
            url = paper.get("url", "")
            doi = paper.get("doi", "")
            article_url = url or (f"https://doi.org/{doi}" if doi else "")
            if not article_url:
                return None
            article_url = article_url.replace("/abs/", "/")

            is_sd = is_sciencedirect(paper)

            # ── Step 1: Navigate to article page ──
            logger.info("加载文章页: %s", article_url[:100])
            try:
                page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

            # ── Step 2: Wait for page to render ──
            _wait_for_cloudflare(page, timeout=timeout)
            _wait_for_article_body(page, timeout=15)
            time.sleep(2)

            # ── Step 3: PDF download ──
            if is_sd:
                # ScienceDirect: use popup + curl_cffi approach
                pdf_bytes = self._download_sd_pdf(page)
                if pdf_bytes:
                    logger.info("SD PDF 下载成功: %d bytes", len(pdf_bytes))
                    return pdf_bytes
            else:
                # Other publishers: navigate to PDF URL directly
                pdf_url = _extract_pdf_link_from_page(page)
                if pdf_url:
                    logger.info("PDF 链接: %s", pdf_url[:120])
                    pdf_bytes = self._download_pdf_bytes(page, pdf_url)
                    if pdf_bytes:
                        logger.info("PDF 下载成功: %d bytes", len(pdf_bytes))
                        return pdf_bytes

            # ── Step 4: Try JS fetch fallback (works for some publishers) ──
            pdf_bytes = self._js_fetch_pdf(page)
            if pdf_bytes:
                logger.info("JS fetch PDF 成功: %d bytes", len(pdf_bytes))
                return pdf_bytes

            return None
        except Exception as e:
            logger.warning("download_pdf 异常: %s", e)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _bring_page_to_front(self, page) -> None:
        """Bring browser window to OS foreground so CF challenge passes quickly.

        Cloudflare JS challenges check document.hasFocus() — background windows
        get a harder challenge (60-90s). Foreground windows pass in ~10s.
        """
        # Playwright-level: activate tab within browser
        try:
            page.bring_to_front()
        except Exception:
            pass

        # OS-level: enumerate all windows, find Edge ones, bring to front
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            edges = []

            def enum_callback(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(260)
                user32.GetWindowTextW(hwnd, buf, 260)
                title = buf.value
                if not title or len(title) < 3:
                    return True
                # Get process ID for the window
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                edges.append((hwnd, title, pid.value))
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(enum_callback), 0)

            # Find msedge.exe windows
            for hwnd, title, pid in edges:
                try:
                    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
                    if handle:
                        name_buf = ctypes.create_unicode_buffer(260)
                        size = ctypes.c_ulong(260)
                        kernel32.QueryFullProcessImageNameW(handle, 0, name_buf, ctypes.byref(size))
                        kernel32.CloseHandle(handle)
                        if "msedge.exe" in name_buf.value.lower():
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            user32.SetForegroundWindow(hwnd)
                            return
                except Exception:
                    continue
        except Exception:
            pass

    def _download_sd_pdf(self, article_page, timeout: int = 90) -> Optional[bytes]:
        """SD-specific: click 'View PDF' → wait for popup to load PDF → JS re-fetch.

        Flow: click View PDF button → popup opens to pdfft → CF challenge →
        redirect to pdf.sciencedirectassets.com → JS fetch with browser cookies.

        Keeps the browser window in the foreground during CF challenge to
        avoid Cloudflare's background-window penalty (reduces CF wait from
        60-90s to ~10s).
        """
        import base64

        try:
            # Bring article page to front before clicking
            self._bring_page_to_front(article_page)
            time.sleep(1)

            with article_page.context.expect_page(timeout=20000) as popup_info:
                # JS click to bypass React modal overlays
                article_page.evaluate("""
                    () => {
                        const links = document.querySelectorAll('a[href*="pdfft"]');
                        for (const a of links) {
                            if (a.offsetParent !== null && a.textContent.includes('PDF')) {
                                a.click(); return;
                            }
                        }
                        if (links.length > 0) links[0].click();
                    }
                """)

            popup = popup_info.value
            logger.info("SD popup: %s", popup.url[:120])

            # Wait for redirect chain: pdfft → CF → sciencedirectassets.com
            # Keep window in foreground so CF challenge passes quickly
            for i in range(timeout):
                time.sleep(1)
                # Bring to front every 3 seconds
                if i % 3 == 0:
                    self._bring_page_to_front(popup)
                try:
                    if "sciencedirectassets.com" in popup.url:
                        break
                except Exception:
                    pass

            try:
                popup.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(3)

            if "sciencedirectassets.com" not in popup.url:
                logger.warning("SD popup 未能重定向到 assets: %s", popup.url[:120])
                popup.close()
                return None

            logger.info("SD popup 已加载到 assets，开始 JS fetch...")

            # JS re-fetch: browser automatically includes cookies + Referer
            b64 = popup.evaluate("""
                async () => {
                    const url = window.location.href;
                    const resp = await fetch(url, {
                        credentials: 'include',
                        headers: { 'Accept': 'application/pdf' }
                    });
                    if (!resp.ok) return 'STATUS_' + resp.status;
                    const buf = await resp.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    if (bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68) return 'NOT_PDF';
                    let binary = '';
                    const chunkSize = 16384;
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        const chunk = bytes.slice(i, Math.min(i + chunkSize, bytes.length));
                        binary += String.fromCharCode.apply(null, Array.from(chunk));
                    }
                    return btoa(binary);
                }
            """)

            popup.close()

            if b64 and not b64.startswith("STATUS_") and not b64.startswith("NOT_PDF"):
                pdf_bytes = base64.b64decode(b64)
                if pdf_bytes[:5] == b"%PDF-":
                    logger.info("SD PDF 下载成功: %d bytes", len(pdf_bytes))
                    return pdf_bytes
            else:
                logger.warning("SD JS fetch 失败: %s", str(b64)[:100] if b64 else "None")

        except Exception as e:
            logger.warning("SD PDF 下载异常: %s", e)

        return None

    def _download_pdf_bytes(self, page, pdf_url: str) -> Optional[bytes]:
        """Navigate to a PDF URL and capture the bytes."""
        import base64

        # Create new page for the PDF URL
        ctx = page.context
        pdf_page = ctx.new_page()

        try:
            # Use CDP to capture the PDF response body
            cdp = ctx.new_cdp_session(pdf_page)
            cdp.send("Network.enable")

            pdf_body = [None]

            def on_response(params):
                resp = params.get("response", {})
                if resp.get("status") == 200:
                    resp_url = resp.get("url", "")
                    if any(k in resp_url for k in ("/pdfft", "/pdf/", ".pdf")):
                        try:
                            result = cdp.send("Network.getResponseBody", {
                                "requestId": params.get("requestId", "")
                            })
                            body = result.get("body", "")
                            if result.get("base64Encoded"):
                                body = base64.b64decode(body)
                            elif isinstance(body, str):
                                body = body.encode("latin-1")
                            if isinstance(body, bytes) and body[:5] == b"%PDF-":
                                pdf_body[0] = body
                        except Exception:
                            pass

            cdp.on("Network.responseReceived", on_response)

            try:
                pdf_page.goto(pdf_url, wait_until="load", timeout=30000)
            except Exception:
                pass
            time.sleep(5)

            # Check if the page itself is the PDF
            content = pdf_page.content()
            if "%PDF-" in content[:10]:
                return content.encode("latin-1")

            return pdf_body[0]
        finally:
            try:
                pdf_page.close()
            except Exception:
                pass

    def _js_fetch_pdf(self, page) -> Optional[bytes]:
        """Use JavaScript fetch to get PDF from the current page URL."""
        import base64

        try:
            b64 = page.evaluate("""
                async () => {
                    const resp = await fetch(window.location.href, {credentials: 'include'});
                    if (!resp.ok) return null;
                    const buf = await resp.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    // Check PDF magic
                    if (bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68) return null;
                    // Chunked base64 encoding
                    const chunkSize = 8192;
                    let binary = '';
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        binary += String.fromCharCode.apply(null, bytes.slice(i, i + chunkSize));
                    }
                    return btoa(binary);
                }
            """)
            if b64:
                pdf = base64.b64decode(b64)
                if pdf[:5] == b"%PDF-":
                    return pdf
        except Exception:
            pass
        return None

    # ── Full text (HTML fallback) ──

    def fetch_full_text(self, paper: dict, timeout: int = 60) -> Optional[str]:
        """Get full text for a paper. Tries PDF first, falls back to HTML article text.

        Returns clean text string or None.
        """
        page = self._new_page()
        try:
            url = paper.get("url", "")
            doi = paper.get("doi", "")
            article_url = url or (f"https://doi.org/{doi}" if doi else "")
            if not article_url:
                return None
            article_url = article_url.replace("/abs/", "/")

            logger.info("加载页面: %s", article_url[:100])
            try:
                page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

            if not _wait_for_cloudflare(page, timeout=timeout):
                logger.warning("CF 验证超时")
            _wait_for_article_body(page, timeout=20)
            time.sleep(2)

            # Try PDF first
            pdf = self.download_pdf(paper, timeout=timeout)
            if pdf:
                from paperpilot.text_fetcher import extract_pdf_text
                text = extract_pdf_text(pdf)
                if text and self._is_full_text(text):
                    return text

            # HTML fallback
            try:
                html = page.content()
                text = _extract_article_text(html)
                if text and self._is_full_text(text):
                    logger.info("HTML 正文: %d chars", len(text))
                    return text
            except Exception as e:
                logger.debug("HTML 提取失败: %s", e)

            return None
        except Exception as e:
            logger.warning("fetch_full_text 异常: %s", e)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _is_full_text(text: str) -> bool:
        """Check if text qualifies as full paper content."""
        return len(text) > 2000

    # ── Batch download ──

    def batch_download(
        self,
        papers: list[dict],
        rate_limit: float = 5.0,
        on_progress=None,
    ) -> list[dict]:
        """Download PDFs for multiple papers, rate-limited.

        Args:
            papers: list of paper dicts
            rate_limit: seconds between downloads
            on_progress: callback(idx, total, paper, result) for progress updates

        Returns:
            list of paper dicts with added keys:
                pdf_bytes | full_text | download_error
        """
        import random

        results = []
        for i, paper in enumerate(papers):
            if on_progress:
                on_progress(i, len(papers), paper, None)

            paper["pdf_bytes"] = None
            paper["full_text"] = None
            paper["download_error"] = None

            try:
                pdf = self.download_pdf(paper)
                if pdf:
                    paper["pdf_bytes"] = pdf
                else:
                    text = self.fetch_full_text(paper)
                    if text:
                        paper["full_text"] = text
                    else:
                        paper["download_error"] = "PDF 和全文获取均失败"
            except Exception as e:
                paper["download_error"] = str(e)

            results.append(paper)

            if on_progress:
                on_progress(i, len(papers), paper, results[-1])

            # Rate limiting: wait between papers (except last)
            if i < len(papers) - 1:
                delay = rate_limit + random.uniform(-1, 2)
                time.sleep(max(1.0, delay))

        return results
