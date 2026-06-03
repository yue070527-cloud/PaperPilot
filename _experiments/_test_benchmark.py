"""3 篇 SD 论文测试 — 测量各级耗时"""
import sys, time, os, socket, subprocess, base64, ctypes
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

URLS = [
    "https://www.sciencedirect.com/science/article/pii/S2352554121000516",
    "https://www.sciencedirect.com/science/article/pii/S0039914023005829",
    "https://www.sciencedirect.com/science/article/pii/S0956713522003565",
]
PORT = 9230
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")
os.makedirs(PROFILE, exist_ok=True)

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

from playwright.sync_api import sync_playwright

# 只清理标签页恢复文件
for f in ["Current Tabs", "Last Tabs"]:
    fp = os.path.join(PROFILE, "Default", f)
    try:
        if os.path.isfile(fp): os.remove(fp)
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

total_start = time.time()

# 前台辅助
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

def bring_edge_to_front():
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd): return True
        buf = ctypes.create_unicode_buffer(260)
        user32.GetWindowTextW(hwnd, buf, 260)
        if not buf.value or len(buf.value) < 3: return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x0400|0x0010, False, pid)
        if h:
            nb = ctypes.create_unicode_buffer(260)
            sz = ctypes.c_ulong(260)
            if kernel32.QueryFullProcessImageNameW(h, 0, nb, ctypes.byref(sz)):
                if "msedge.exe" in nb.value.lower():
                    kernel32.CloseHandle(h)
                    user32.ShowWindow(hwnd, 9)
                    user32.SetForegroundWindow(hwnd)
                    return False
            kernel32.CloseHandle(h)
        return True
    WEP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    user32.EnumWindows(WEP(cb), 0)


def wait_article_page(page, timeout=90):
    """等待文章页真正加载完成（含 CF 验证）— 匹配 _test_fast.py 逻辑"""
    for i in range(timeout):
        time.sleep(1)
        if i % 3 == 0:
            bring_edge_to_front()
        try:
            t = page.title()
            if t in ("", "about:blank", "ScienceDirect", "Elsevier",
                     "Just a moment...", "请稍候…"):
                if i % 15 == 0:
                    print(f"    [{i}s] CF中: '{t}'")
                continue
            if len(t) > 20:
                print(f"    [{i}s] 就绪: {t[:80]}")
                return True
        except Exception:
            pass
    return False


def wait_pdfft_redirect(pdf_page, timeout=90):
    """等待 pdfft 页面通过 CF 并重定向到 assets"""
    for i in range(timeout):
        time.sleep(1)
        if i % 3 == 0:
            bring_edge_to_front()
        try:
            pu = pdf_page.url
            if i % 10 == 0:
                hf = pdf_page.evaluate("() => document.hasFocus()")
                print(f"    [{i}s] hasFocus={hf} | {pu[:110]}")
            if "sciencedirectassets.com" in pu:
                print(f"    [{i}s] -> assets!")
                return True
            if "crasolve=1" in pu and i % 10 != 0:
                pass  # waiting for next redirect
        except Exception:
            pass
    return False


with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = browser.contexts[0]
    page = ctx.pages[0]

    results = []

    for idx, url in enumerate(URLS):
        pii = url.split("/pii/")[1].split("?")[0]
        t_start = time.time()
        print(f"\n{'='*50}")
        print(f"[{idx+1}/3] {pii}")
        print(f"{'='*50}")

        # Step 1: 加载文章页
        t1 = time.time()
        print("  加载文章页...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        wait_article_page(page, timeout=90)
        time.sleep(2)
        t_page = time.time() - t1
        print(f"    页面耗时: {t_page:.0f}s")

        # Step 2: pdfft → 新标签页 → 等重定向
        t2 = time.time()
        pdfft = page.evaluate("""
            () => {
                const links = document.querySelectorAll('a[href*="pdfft"]');
                for (const a of links) { if (a.offsetParent !== null) return a.href; }
                return links.length > 0 ? links[0].href : null;
            }
        """)
        if not pdfft:
            print("    [FAIL] 没有 pdfft 链接")
            continue
        print(f"    pdfft: {pdfft[:100]}...")

        print("  新标签页打开 pdfft...")
        pdf_page = ctx.new_page()
        try:
            pdf_page.goto(pdfft, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        if wait_pdfft_redirect(pdf_page, timeout=90):
            t_cf = time.time() - t2
        else:
            t_cf = time.time() - t2

        print(f"    CF耗时: {t_cf:.0f}s")

        if "sciencedirectassets.com" not in pdf_page.url:
            print(f"    [FAIL] 未重定向: {pdf_page.url[:120]}")
            pdf_page.close()
            continue

        try:
            pdf_page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        # Step 3: JS fetch
        t3 = time.time()
        print("  JS fetch...")
        b64 = pdf_page.evaluate("""
            async () => {
                const resp = await fetch(window.location.href,
                    {credentials: 'include', headers: {'Accept': 'application/pdf'}});
                if (!resp.ok) return 'STATUS_' + resp.status;
                const bytes = new Uint8Array(await resp.arrayBuffer());
                if (bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68) return 'NOT_PDF';
                let b = '';
                for (let i = 0; i < bytes.length; i += 16384) {
                    b += String.fromCharCode.apply(null,
                        Array.from(bytes.slice(i, Math.min(i + 16384, bytes.length))));
                }
                return btoa(b);
            }
        """)
        t_fetch = time.time() - t3
        pdf_page.close()

        if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
            pdf_bytes = base64.b64decode(b64)
            out = f"D:/Desktop/PaperPilot/downloads/sd_test_{pii}.pdf"
            with open(out, "wb") as f:
                f.write(pdf_bytes)
            t_total = time.time() - t_start
            print(f"  [OK] {len(pdf_bytes)} bytes -> downloads/sd_test_{pii}.pdf")
            print(f"  >> {t_total:.0f}s (页面:{t_page:.0f}s CF:{t_cf:.0f}s fetch:{t_fetch:.0f}s)")
            results.append((pii, t_total, t_page, t_cf, t_fetch, True))
        else:
            print(f"  [FAIL] {b64[:100] if b64 else 'None'}")
            results.append((pii, time.time() - t_start, t_page, t_cf, 0, False))

    browser.close()
    proc.terminate()

    print(f"\n{'='*60}")
    print("汇总:")
    print(f"{'论文':<35} {'总计':>6} {'页面':>6} {'CF':>6} {'fetch':>6} {'结果':>6}")
    for r in results:
        print(f"  {r[0]:<33} {r[1]:5.0f}s {r[2]:5.0f}s {r[3]:5.0f}s {r[4]:5.0f}s {'OK' if r[5] else 'FAIL':>6}")
    success = [r for r in results if r[5]]
    if success:
        cf_times = [r[3] for r in success]
        total_times = [r[1] for r in success]
        print(f"\n  成功: {len(success)}/3  总耗时平均: {sum(total_times)/len(total_times):.0f}s")
        print(f"  CF平均: {sum(cf_times)/len(cf_times):.0f}s  范围: {min(cf_times)}-{max(cf_times)}s")
    print(f"全流程: {time.time()-total_start:.0f}s")
