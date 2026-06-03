"""NEJM PDF 下载测试 — CDP 方案"""
import sys, time, os, socket, subprocess, base64, ctypes
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PDF_URL = "https://www.nejm.org/doi/pdf/10.1056/NEJMoa1914347"
PORT = 9231
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")
os.makedirs(PROFILE, exist_ok=True)

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

from playwright.sync_api import sync_playwright

# 清理标签页恢复文件
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

t0 = time.time()

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


with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = browser.contexts[0]
    page = ctx.pages[0]

    # Step 1: 直接导航到 PDF URL（NEJM 不需要先加载文章页）
    t1 = time.time()
    print(f"导航到: {PDF_URL}")
    try:
        page.goto(PDF_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # Step 2: 等 CF 通过
    t2 = time.time()
    print("等待 CF 通过...")
    for i in range(90):
        time.sleep(1)
        if i % 3 == 0:
            bring_edge_to_front()
        try:
            t = page.title()
            pu = page.url
            if i % 10 == 0 or (t and "just a moment" not in t.lower() and "请稍候" not in t):
                try:
                    hf = page.evaluate("() => document.hasFocus()")
                except Exception:
                    hf = "?"
                print(f"  [{i}s] hasFocus={hf} | title='{t[:60]}' | {pu[:100]}")

            # CF 通过了吗？
            if t and "just a moment" not in t.lower() and "请稍候" not in t and len(t) > 3:
                # 可能是文章标题页或 PDF 渲染页
                if i > 2:  # 至少等几秒确认不是瞬时页面
                    print(f"  [{i}s] CF 已过! title='{t[:80]}'")
                    break
        except Exception:
            pass
    t_cf = time.time() - t2
    print(f"  CF耗时: {t_cf:.0f}s")

    time.sleep(3)

    # Step 3: 看页面状态
    pu = page.url
    print(f"  当前URL: {pu[:150]}")
    print(f"  title: {page.title()[:100]}")

    # Step 4: JS fetch PDF
    t3 = time.time()
    print("JS fetch...")
    b64 = page.evaluate("""
        async () => {
            const url = window.location.href;
            const resp = await fetch(url, {
                credentials: 'include',
                headers: { 'Accept': 'application/pdf' }
            });
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

    if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
        pdf_bytes = base64.b64decode(b64)
        out = "D:/Desktop/PaperPilot/downloads/nejm_test.pdf"
        with open(out, "wb") as f:
            f.write(pdf_bytes)
        t_total = time.time() - t0
        print(f"\n[OK] {len(pdf_bytes)} bytes -> downloads/nejm_test.pdf")
        print(f"总耗时: {t_total:.0f}s (导航:{t_cf:.0f}s fetch:{t_fetch:.0f}s)")
    else:
        # 可能 fetch 当前 URL 不是 PDF，试试别的策略
        print(f"  直接 fetch 失败: {b64[:150] if b64 else 'None'}")

        # 尝试从页面提取真实 PDF 链接
        try:
            pdf_links = page.evaluate("""
                () => {
                    const result = [];
                    for (const a of document.querySelectorAll('a[href]')) {
                        if (a.href.endsWith('.pdf') || a.href.includes('/pdf/')) {
                            result.push(a.href);
                        }
                    }
                    return [...new Set(result)];
                }
            """)
            print(f"  页面PDF链接: {pdf_links}")
        except Exception as e:
            print(f"  提取链接失败: {e}")

    browser.close()
    proc.terminate()
