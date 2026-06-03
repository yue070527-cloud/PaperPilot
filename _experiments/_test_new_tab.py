"""测试：提取 pdfft URL → 新标签页打开（非 popup）→ 前台页面过 CF 更快"""
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

    for i in range(30):
        time.sleep(1)
        try:
            t = page.title()
            if t and 'Just a moment' not in t and '请稍候' not in t and 'challenge' not in t.lower():
                break
        except Exception:
            pass
    time.sleep(3)
    print(f"  文章页就绪: {time.time()-t1:.0f}s | {page.title()[:80]}")

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
    print(f"  pdfft URL: {pdfft_url[:120] if pdfft_url else 'NOT FOUND'}")

    if not pdfft_url:
        print("未找到 pdfft 链接")
        browser.close()
        proc.terminate()
        sys.exit(1)

    # Step 3: ★ 新标签页打开 pdfft（不是 popup）★
    # 新标签页是正常的前台页面，CF 应该快很多
    t2 = time.time()
    print(f"\n新标签页打开 pdfft（前台页面）...")

    pdf_page = ctx.new_page()
    pdf_page.bring_to_front()
    try:
        pdf_page.goto(pdfft_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # 等 CF + 重定向
    for i in range(90):
        time.sleep(1)
        try:
            pu = pdf_page.url
            if i % 10 == 0:
                print(f"  [{i}s] {pu[:120]}")
            if "sciencedirectassets.com" in pu:
                print(f"  [{i}s] -> assets!")
                break
        except Exception:
            pass

    t_cf = time.time() - t2
    print(f"  CF+重定向耗时: {t_cf:.0f}s")

    try:
        pdf_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(3)

    final_url = pdf_page.url
    if "sciencedirectassets.com" not in final_url:
        print(f"  未重定向到 assets: {final_url[:120]}")
        pdf_page.close()
        browser.close()
        proc.terminate()
        sys.exit(1)

    # Step 4: JS fetch
    t3 = time.time()
    print("JS fetch...")
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
        out = "D:/Desktop/PaperPilot/downloads/sd_new_tab_test.pdf"
        with open(out, "wb") as f:
            f.write(pdf_bytes)
        t_total = time.time() - t_total
        print(f"  [OK] {len(pdf_bytes)} bytes")
        print(f"  总耗时: {t_total:.0f}s | 文章:{time.time()-t1:.0f}s CF:{t_cf:.0f}s fetch:{time.time()-t3:.0f}s")
    else:
        print(f"  [FAIL] {b64[:200] if b64 else 'None'}")

    pdf_page.close()
    browser.close()
    proc.terminate()
