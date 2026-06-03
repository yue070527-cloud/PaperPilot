"""测试：force click 绕过 modal + 模拟真实点击，看 CF 是否加速"""
import sys, time, os, socket, subprocess, base64
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

URL = "https://www.sciencedirect.com/science/article/pii/S0889157523001370"
DEBUG_PORT = 9228
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")

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
    for i in range(30):
        time.sleep(1)
        try:
            t = page.title()
            if t and 'Just a moment' not in t and '请稍候' not in t and 'challenge' not in t.lower():
                break
        except Exception:
            pass
    time.sleep(3)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)
    print(f"  文章页就绪: {time.time()-t_total:.0f}s | {page.title()[:80]}")

    # Step 2: 尝试关闭可能的 modal/overlay
    print("尝试关闭弹窗/overlay...")
    try:
        # ScienceDirect 常见的遮罩：cookie banner, 订阅弹窗, 推荐弹窗
        closed = page.evaluate("""
            () => {
                const closed = [];
                // Cookie banner
                for (const btn of document.querySelectorAll('button')) {
                    const text = btn.textContent.toLowerCase().trim();
                    if (['accept', 'accept all', 'accept all cookies', 'agree', 'ok', 'i agree',
                         'got it', 'continue', 'close'].includes(text)) {
                        btn.click();
                        closed.push('cookie:' + text);
                    }
                }
                // Close buttons
                for (const btn of document.querySelectorAll('[aria-label*="close" i], [aria-label*="Close" i], .close, .modal-close, [class*="close-btn"]')) {
                    btn.click();
                    closed.push('close-btn');
                    break;
                }
                // Try Escape key on modal
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                return closed;
            }
        """)
        print(f"  关闭了: {closed}")
    except Exception as e:
        print(f"  关闭弹窗异常: {e}")
    time.sleep(1)

    # Step 3: ★ force click — 模拟真实鼠标点击 ★
    print("\n查找 View PDF 按钮...")
    pdf_link = page.locator('a[href*="pdfft"]').first
    count = page.locator('a[href*="pdfft"]').count()
    print(f"  找到 {count} 个 pdfft 链接")

    # 先用 locator 看看第一个是否可见
    try:
        visible = pdf_link.is_visible()
        print(f"  第一个可见: {visible}")
    except Exception:
        visible = False

    if not visible:
        # 找可见的那个
        print("  找可见的 pdfft 链接...")
        for i in range(count):
            link = page.locator(f'a[href*="pdfft"]').nth(i)
            try:
                if link.is_visible():
                    pdf_link = link
                    print(f"  使用第 {i} 个")
                    break
            except Exception:
                pass

    # force click — 跳过 overlay 检查，模拟真实点击
    t_click = time.time()
    print("force click（模拟真实鼠标点击）...")

    try:
        with page.context.expect_page(timeout=15000) as popup_info:
            pdf_link.click(force=True, timeout=10000)
        popup = popup_info.value
        print(f"  Popup 打开: {time.time()-t_click:.0f}s")
        print(f"  URL: {popup.url[:120]}")
    except Exception as e:
        # 如果 force click 失败，回退到 JS click
        print(f"  force click 失败: {e}")
        print("  回退到 JS click...")
        with page.context.expect_page(timeout=15000) as popup_info:
            page.evaluate("""
                () => {
                    const links = document.querySelectorAll('a[href*="pdfft"]');
                    for (const a of links) {
                        if (a.offsetParent !== null) { a.click(); return; }
                    }
                    links[0].click();
                }
            """)
        popup = popup_info.value
        print(f"  JS Popup: {popup.url[:120]}")

    # 等重定向
    t_cf_start = time.time()
    print("等待 CF + 重定向...")
    for i in range(60):
        time.sleep(1)
        try:
            pu = popup.url
            if i % 10 == 0:
                print(f"  [{i}s] {pu[:120]}")
            if "sciencedirectassets.com" in pu:
                print(f"  [{i}s] -> assets!")
                break
        except Exception:
            pass
    t_cf = time.time() - t_cf_start

    try:
        popup.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(3)

    if "sciencedirectassets.com" not in popup.url:
        print(f"  未重定向: {popup.url[:120]}")
        popup.close()
        browser.close()
        proc.terminate()
        sys.exit(1)

    # JS fetch
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
            for (let i = 0; i < bytes.length; i += 16384) {
                const chunk = bytes.slice(i, Math.min(i + 16384, bytes.length));
                binary += String.fromCharCode.apply(null, Array.from(chunk));
            }
            return btoa(binary);
        }
    """)
    popup.close()

    if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
        pdf_bytes = base64.b64decode(b64)
        out = "D:/Desktop/PaperPilot/downloads/sd_force_click_test.pdf"
        with open(out, "wb") as f:
            f.write(pdf_bytes)
        t_total = time.time() - t_total
        print(f"\n[OK] {len(pdf_bytes)} bytes | 总耗时: {t_total:.0f}s (CF:{t_cf:.0f}s)")
    else:
        print(f"\n[FAIL] {b64[:200] if b64 else 'None'}")

    browser.close()
    proc.terminate()
