"""单篇前台测试 — 出问题当场修"""
import sys, time, os, socket, subprocess, base64, ctypes
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

URL = "https://www.sciencedirect.com/science/article/pii/S0889157523001370"
PORT = 9229
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")  # 用回有 CF 信任的老 profile
os.makedirs(PROFILE, exist_ok=True)

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p): edge_exe = p; break

from playwright.sync_api import sync_playwright

# 只清理标签页恢复文件，保留 cookies
for f in [os.path.join(PROFILE, "Default", "Current Tabs"),
          os.path.join(PROFILE, "Default", "Last Tabs")]:
    try:
        if os.path.isfile(f): os.remove(f)
    except Exception: pass

print("启动 Edge...")
proc = subprocess.Popen(
    [edge_exe, f"--remote-debugging-port={PORT}",
     f"--user-data-dir={PROFILE}", "--no-first-run",
     "--no-default-browser-check",
     "--disable-blink-features=AutomationControlled", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
for i in range(30):
    time.sleep(1)
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
        s.close(); break
    except Exception: pass
else:
    print("端口超时"); proc.terminate(); sys.exit(1)

# 设置 PID 供 downloader 的前台函数精确定位 (避免把用户其他 Edge 窗口提到前台)
import paperpilot.downloader as _dl
_dl._browser_pid = proc.pid

t0 = time.time()
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = browser.contexts[0]
    page = ctx.pages[0]

    # ── 加载文章页 (加前台加速, 用 PID 过滤的版本) ──
    print("加载文章页...")
    try: page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    except Exception: pass

    for i in range(60):
        time.sleep(1)
        if i % 3 == 0:
            _dl._bring_window_to_front()
        try:
            t = page.title()
            if t in ("", "about:blank", "ScienceDirect", "Elsevier", "Just a moment...", "请稍候…"):
                continue
            if len(t) > 20 and "ScienceDirect" not in t:
                print(f"  [{i}s] 就绪: {t[:80]}")
                break
        except Exception: pass
    time.sleep(2)
    print(f"  页面加载: {time.time()-t0:.0f}s")

    # ── 提取 pdfft，新标签页打开 ──
    pdfft = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href*="pdfft"]');
            for (const a of links) { if (a.offsetParent !== null) return a.href; }
            return links.length > 0 ? links[0].href : null;
        }
    """)
    if not pdfft:
        print("没有 pdfft 链接！")
        browser.close(); proc.terminate(); sys.exit(1)
    print(f"  pdfft: {pdfft[:100]}...")

    # ── 新标签页打开 pdfft ──
    t1 = time.time()
    print("新标签页打开 pdfft...")
    pdf_page = ctx.new_page()
    try: pdf_page.goto(pdfft, wait_until="domcontentloaded", timeout=30000)
    except Exception: pass

    for i in range(60):
        time.sleep(1)
        if i % 3 == 0:
            _dl._bring_window_to_front()
        try:
            pu = pdf_page.url
            if i % 10 == 0:
                try:
                    hf = pdf_page.evaluate("() => document.hasFocus()")
                    print(f"  [{i}s] hasFocus={hf} | {pu[:100]}")
                except Exception:
                    print(f"  [{i}s] {pu[:100]}")
            if "sciencedirectassets.com" in pu:
                print(f"  [{i}s] -> assets!")
                break
        except Exception: pass

    t_cf = time.time() - t1
    print(f"  CF 耗时: {t_cf:.0f}s")

    if "sciencedirectassets.com" not in pdf_page.url:
        print(f"  未重定向! 最后URL: {pdf_page.url[:150]}")
        pdf_page.close(); browser.close(); proc.terminate(); sys.exit(1)

    try: pdf_page.wait_for_load_state("networkidle", timeout=15000)
    except Exception: pass
    time.sleep(2)

    # ── JS fetch ──
    t2 = time.time()
    print("JS fetch...")
    b64 = pdf_page.evaluate("""
        async () => {
            const resp = await fetch(window.location.href, {credentials:'include',headers:{'Accept':'application/pdf'}});
            if(!resp.ok) return 'STATUS_'+resp.status;
            const bytes = new Uint8Array(await resp.arrayBuffer());
            if(bytes[0]!==37||bytes[1]!==80||bytes[2]!==68) return 'NOT_PDF';
            let b=''; for(let i=0;i<bytes.length;i+=16384){b+=String.fromCharCode.apply(null,Array.from(bytes.slice(i,Math.min(i+16384,bytes.length))));}
            return btoa(b);
        }
    """)
    pdf_page.close()

    if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
        pdf_bytes = base64.b64decode(b64)
        out = "D:/Desktop/PaperPilot/downloads/sd_fast_test.pdf"
        with open(out, "wb") as f: f.write(pdf_bytes)
        t_total = time.time() - t0
        print(f"\n[OK] {len(pdf_bytes)} bytes | 总耗时: {t_total:.0f}s (页面:{0:.0f}s CF:{t_cf:.0f}s fetch:{time.time()-t2:.0f}s)")
    else:
        print(f"\n[FAIL] {b64[:200] if b64 else 'None'}")

    browser.close()
    proc.terminate()
