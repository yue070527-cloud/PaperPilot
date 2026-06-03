"""A/B 测试：前台加速 vs 无前台，对比耗时"""
import sys, time, os, socket, subprocess, base64
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

URLS = [
    "https://www.sciencedirect.com/science/article/pii/S0889157523001370",
    "https://www.sciencedirect.com/science/article/pii/S0308814623011469",
    "https://www.sciencedirect.com/science/article/pii/S0263224120313221",
]
DEBUG_PORT = 9228
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")
DOWNLOAD_DIR = "D:/Desktop/PaperPilot/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

from playwright.sync_api import sync_playwright


def run_test(name: str, use_foreground: bool) -> list[tuple[str, float, bool]]:
    """返回 [(论文名, 耗时秒, 成功)]"""
    print(f"\n{'#'*60}")
    print(f"# {name}")
    print(f"{'#'*60}")

    # 清 session
    for f in [os.path.join(PROFILE, "Default", "Sessions"),
              os.path.join(PROFILE, "Default", "Current Tabs"),
              os.path.join(PROFILE, "Default", "Last Tabs")]:
        try:
            if os.path.isfile(f): os.remove(f)
        except Exception: pass

    print("启动 Edge...")
    proc = subprocess.Popen(
        [edge_exe, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={PROFILE}", "--no-first-run",
         "--no-default-browser-check",
         "--disable-blink-features=AutomationControlled", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    for i in range(30):
        time.sleep(1)
        try:
            s = socket.create_connection(("127.0.0.1", DEBUG_PORT), timeout=1)
            s.close(); break
        except Exception: pass
    else:
        proc.terminate(); return []

    results = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0]

            for idx, url in enumerate(URLS):
                pii = url.split("/pii/")[1].split("?")[0]
                t_start = time.time()
                print(f"\n  [{idx+1}/3] {pii}")

                # 1. 加载文章页
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass

                # 等文章页就绪
                for i in range(90):
                    time.sleep(1)
                    try:
                        t = page.title()
                        if t not in ("", "about:blank", "ScienceDirect", "Elsevier",
                                      "ScienceDirect.com", "Just a moment...", "请稍候…"):
                            if len(t) > 20:
                                break
                    except Exception:
                        pass
                time.sleep(2)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                time.sleep(1)
                t_page = time.time() - t_start
                print(f"    页面: {t_page:.0f}s | {page.title()[:70]}")

                # 2. 点击 PDF
                ok = False
                try:
                    t_click = time.time()
                    with page.context.expect_page(timeout=15000) as popup_info:
                        page.evaluate("""
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

                    # ★ 等重定向，可选前台加速 ★
                    for i in range(90):
                        time.sleep(1)
                        if use_foreground and i % 3 == 0:
                            try:
                                popup.bring_to_front()
                                import ctypes
                                user32 = ctypes.windll.user32
                                kernel32 = ctypes.windll.kernel32
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
                                        s = ctypes.c_ulong(260)
                                        kernel32.QueryFullProcessImageNameW(h, 0, nb, ctypes.byref(s))
                                        kernel32.CloseHandle(h)
                                        if "msedge.exe" in nb.value.lower():
                                            user32.ShowWindow(hwnd, 9)
                                            user32.SetForegroundWindow(hwnd)
                                            return False
                                    return True
                                WEP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                                user32.EnumWindows(WEP(cb), 0)
                            except Exception:
                                pass

                        try:
                            if "sciencedirectassets.com" in popup.url:
                                break
                        except Exception:
                            pass
                    t_cf = time.time() - t_click

                    if "sciencedirectassets.com" in popup.url:
                        try:
                            popup.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        time.sleep(2)

                        b64 = popup.evaluate("""
                            async () => {
                                const url = window.location.href;
                                const resp = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/pdf'}});
                                if (!resp.ok) return 'STATUS_'+resp.status;
                                const buf = await resp.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                if (bytes[0]!==37||bytes[1]!==80||bytes[2]!==68) return 'NOT_PDF';
                                let b='';
                                for(let i=0;i<bytes.length;i+=16384){b+=String.fromCharCode.apply(null,Array.from(bytes.slice(i,Math.min(i+16384,bytes.length))));}
                                return btoa(b);
                            }
                        """)
                        if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
                            pdf_bytes = base64.b64decode(b64)
                            out = os.path.join(DOWNLOAD_DIR, f"sd_ab_{pii}.pdf")
                            with open(out, "wb") as f:
                                f.write(pdf_bytes)
                            t_total = time.time() - t_start
                            print(f"    CF: {t_cf:.0f}s | [OK] {len(pdf_bytes)} bytes | 总: {t_total:.0f}s")
                            ok = True
                        else:
                            print(f"    CF: {t_cf:.0f}s | [FAIL] fetch")
                    else:
                        print(f"    CF: {t_cf:.0f}s | [FAIL] 未重定向")
                    popup.close()
                except Exception as e:
                    print(f"    [FAIL] 异常: {e}")

                results.append((pii, time.time() - t_start, ok))

                if idx < len(URLS) - 1:
                    time.sleep(2)

            browser.close()
    finally:
        proc.terminate()
        time.sleep(1)

    return results


# ── 跑两次 ──
print("=" * 60)
print("A/B 测试：无前台 vs 有前台")
print("=" * 60)

results_no_fg = run_test("A组: 无前台加速", use_foreground=False)
results_fg = run_test("B组: 有前台加速", use_foreground=True)

print(f"\n{'='*60}")
print("对比结果:")
print(f"{'='*60}")
print(f"{'论文':<35} {'无前台':>8} {'有前台':>8} {'节省':>8}")
for i in range(len(URLS)):
    name = URLS[i].split("/")[-1][:30]
    t_no = results_no_fg[i][1] if i < len(results_no_fg) else 0
    t_fg = results_fg[i][1] if i < len(results_fg) else 0
    if t_no and t_fg:
        saved = t_no - t_fg
        print(f"  {name:<33} {t_no:6.0f}s {t_fg:6.0f}s {saved:6.0f}s ({saved/t_no*100:.0f}%)")

avg_no = sum(r[1] for r in results_no_fg) / len(results_no_fg) if results_no_fg else 0
avg_fg = sum(r[1] for r in results_fg) / len(results_fg) if results_fg else 0
if avg_no and avg_fg:
    print(f"\n  平均: {avg_no:.0f}s → {avg_fg:.0f}s (节省 {avg_no-avg_fg:.0f}s, 提速 {avg_no/avg_fg:.1f}x)")
