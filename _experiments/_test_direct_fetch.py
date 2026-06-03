"""测试：不弹 popup，直接从文章页 JS fetch pdfft → 跟随重定向 → 拿 PDF"""
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
    t1 = time.time()
    print(f"加载文章页...")
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    for i in range(60):
        time.sleep(1)
        try:
            t = page.title()
            if t and 'Just a moment' not in t and '请稍候' not in t and 'challenge' not in t.lower():
                break
        except Exception:
            pass
    time.sleep(3)
    print(f"  页面就绪: {time.time()-t1:.0f}s | {page.title()[:80]}")

    # Step 2: 提取 pdfft URL
    pdfft_url = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href*="pdfft"]');
            for (const a of links) {
                if (a.offsetParent !== null) return a.href;
            }
            return links.length > 0 ? links[0].href : null;
        }
    """)
    print(f"  pdfft URL: {pdfft_url[:120]}")

    if not pdfft_url:
        print("未找到 pdfft 链接")
        browser.close()
        proc.terminate()
        sys.exit(1)

    # Step 3: ★ 关键 — 直接从文章页 JS fetch pdfft ★
    # 文章页已有 CF cookie，fetch 会自动带 cookie + 跟随重定向
    t2 = time.time()
    print("直接从文章页 JS fetch pdfft（无 popup）...")

    # 方法 A: fetch 带 redirect: 'follow'，拿最终响应
    b64 = page.evaluate("""
        async (pdfftUrl) => {
            // 第一次 fetch: pdfft → 跟随重定向到 assets
            const resp = await fetch(pdfftUrl, {
                credentials: 'include',
                redirect: 'follow',
                headers: { 'Accept': 'application/pdf,text/html,*/*' }
            });
            const finalUrl = resp.url;
            const contentType = resp.headers.get('content-type') || '';

            // 如果最终 URL 是 assets 且内容是 PDF，直接取
            if (finalUrl.includes('sciencedirectassets.com') && contentType.includes('pdf')) {
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                if (bytes[0] === 37 && bytes[1] === 80 && bytes[2] === 68) {
                    let binary = '';
                    for (let i = 0; i < bytes.length; i += 16384) {
                        const chunk = bytes.slice(i, Math.min(i + 16384, bytes.length));
                        binary += String.fromCharCode.apply(null, Array.from(chunk));
                    }
                    return 'OK:' + btoa(binary);
                }
            }

            // 如果没到 assets（被 CF 拦住），返回信息
            const text = await resp.text();
            return 'INFO: finalUrl=' + finalUrl + ' contentType=' + contentType +
                   ' bodyLen=' + text.length + ' head=' + text.substring(0, 200);
        }
    """, pdfft_url)

    t_page = t1 - t_total
    t_fetch = time.time() - t2
    print(f"  fetch 耗时: {t_fetch:.0f}s")

    if b64 and b64.startswith("OK:"):
        pdf_bytes = base64.b64decode(b64[3:])
        out = "D:/Desktop/PaperPilot/downloads/sd_direct_fetch_test.pdf"
        with open(out, "wb") as f:
            f.write(pdf_bytes)
        t_total = time.time() - t_total
        print(f"  ✓ 成功! {len(pdf_bytes)} bytes | 总耗时: {t_total:.0f}s (页面:{t_page:.0f}s fetch:{t_fetch:.0f}s)")
    else:
        t_total = time.time() - t_total
        print(f"  ✗ 失败: {b64[:300] if b64 else 'None'}")
        print(f"  总耗时: {t_total:.0f}s")

    browser.close()
    proc.terminate()
