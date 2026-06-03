"""测试：强制 Edge 窗口前台 → 看 CF 是否加速"""
import sys, time, os, socket, subprocess, base64, ctypes
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

URL = "https://www.sciencedirect.com/science/article/pii/S0889157523001370"
DEBUG_PORT = 9228
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")

# Windows API
user32 = ctypes.windll.user32
SW_SHOW = 5
SW_RESTORE = 9

def bring_to_front(title_substring="ScienceDirect"):
    """查找包含指定标题的窗口并提到前台"""
    hwnd = user32.FindWindowW(None, None)
    while hwnd:
        buf = ctypes.create_unicode_buffer(260)
        user32.GetWindowTextW(hwnd, buf, 260)
        window_title = buf.value
        if title_substring in window_title:
            # Restore if minimized, then bring to front
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            print(f"  提到前台: {window_title[:80]}")
            return True
        hwnd = user32.FindWindowExW(None, hwnd, None, None)
    return False

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

from playwright.sync_api import sync_playwright

t_total = time.time()
print("启动 Edge...")
proc = subprocess.Popen(
    [edge_exe, f"--remote-debugging-port={DEBUG_PORT}",
     f"--user-data-dir={PROFILE}", "--no-first-run",
     "--no-default-browser-check",
     "--disable-blink-features=AutomationControlled",
     "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

for i in range(30):
    time.sleep(1)
    try:
        s = socket.create_connection(("127.0.0.1", DEBUG_PORT), timeout=1)
        s.close()
        break
    except Exception:
        pass

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
    ctx = browser.contexts[0]
    page = ctx.pages[0]

    # Step 1: 加载文章页
    print("加载文章页...")
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # ★ 立即把窗口提到前台
    bring_to_front("ScienceDirect")
    time.sleep(1)

    for i in range(30):
        time.sleep(1)
        try:
            t = page.title()
            if t and 'Just a moment' not in t and '请稍候' not in t and 'challenge' not in t.lower():
                break
        except Exception:
            pass
    time.sleep(3)
    print(f"  文章页就绪: {time.time()-t_total:.0f}s | {page.title()[:80]}")

    # Step 2: 提取 pdfft，新标签页打开（前台）
    pdfft_url = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href*="pdfft"]');
            for (const a of links) {
                if (a.offsetParent !== null) return a.href;
            }
            return links.length > 0 ? links[0].href : null;
        }
    """)

    # 新标签页
    print(f"\n新标签页打开 pdfft...")
    pdf_page = ctx.new_page()
    try:
        pdf_page.goto(pdfft_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # ★ 关键：每次导航后立即提到前台 ★
    print("提到前台...")
    time.sleep(0.5)
    bring_to_front("ScienceDirect")

    t_cf_start = time.time()
    for i in range(60):
        time.sleep(1)
        # ★ 持续保持前台 ★
        if i % 5 == 0:
            bring_to_front("ScienceDirect")

        try:
            pu = pdf_page.url
            if i % 10 == 0:
                # 检查焦点状态
                try:
                    focused = pdf_page.evaluate("() => document.hasFocus()")
                    print(f"  [{i}s] hasFocus={focused} | {pu[:120]}")
                except Exception:
                    print(f"  [{i}s] {pu[:120]}")
            if "sciencedirectassets.com" in pu:
                print(f"  [{i}s] -> assets!")
                break
        except Exception:
            pass

    t_cf = time.time() - t_cf_start
    print(f"  CF耗时: {t_cf:.0f}s")

    if "sciencedirectassets.com" not in pdf_page.url:
        print(f"  未重定向: {pdf_page.url[:120]}")
        pdf_page.close()
        browser.close()
        proc.terminate()
        sys.exit(1)

    try:
        pdf_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(3)

    # JS fetch
    b64 = pdf_page.evaluate("""
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
            for (let i = 0; i < bytes.length; i += 16384) {
                const chunk = bytes.slice(i, Math.min(i + 16384, bytes.length));
                binary += String.fromCharCode.apply(null, Array.from(chunk));
            }
            return btoa(binary);
        }
    """)

    if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
        pdf_bytes = base64.b64decode(b64)
        out = "D:/Desktop/PaperPilot/downloads/sd_foreground_test.pdf"
        with open(out, "wb") as f:
            f.write(pdf_bytes)
        t_total = time.time() - t_total
        print(f"\n[OK] {len(pdf_bytes)} bytes | 总耗时: {t_total:.0f}s (CF:{t_cf:.0f}s)")
    else:
        print(f"\n[FAIL] {b64[:200] if b64 else 'None'}")

    pdf_page.close()
    browser.close()
    proc.terminate()
