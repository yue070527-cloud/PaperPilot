"""测试会话复用：一次 Edge 会话连续下载多篇，对比耗时"""
import sys, time, os, socket, subprocess, base64
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 用直链，跳过 DOI 重定向
TEST_URLS = [
    "https://www.sciencedirect.com/science/article/pii/S0263224120313221",
    "https://www.sciencedirect.com/science/article/pii/S0308814623011469",
    "https://www.sciencedirect.com/science/article/pii/S0889157523001370",
]

DEBUG_PORT = 9228
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")
os.makedirs(PROFILE, exist_ok=True)
DOWNLOAD_DIR = "D:/Desktop/PaperPilot/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

from playwright.sync_api import sync_playwright


def download_one(page, url: str, index: int) -> tuple[bool, float]:
    """下载单篇，返回 (成功, 耗时秒)"""
    pii = url.split("/pii/")[1].split("?")[0]
    t0 = time.time()

    print(f"\n  [{index}] 页面加载: {url[:80]}...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # 等页面渲染
    for i in range(30):
        time.sleep(1)
        try:
            t = page.title()
            if t and 'Just a moment' not in t and '请稍候' not in t and 'challenge' not in t.lower():
                if "ScienceDirect" in t or "Elsevier" in t:
                    break
        except Exception:
            pass
    time.sleep(2)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(1)

    t_page = time.time() - t0
    print(f"  [{index}] 页面就绪: {t_page:.0f}s | {page.title()[:80]}")

    # 点击 PDF
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
        print(f"  [{index}] Popup 打开: {time.time()-t_click:.0f}s")

        # 等重定向到 assets
        t_redirect = time.time()
        for i in range(60):
            time.sleep(1)
            try:
                if "sciencedirectassets.com" in popup.url:
                    break
            except Exception:
                pass
        t_cf = time.time() - t_redirect
        print(f"  [{index}] CF + 重定向: {t_cf:.0f}s")

        try:
            popup.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        if "sciencedirectassets.com" not in popup.url:
            print(f"  [{index}] ✗ 未重定向到 assets")
            popup.close()
            return False, time.time() - t0

        # JS fetch
        t_fetch = time.time()
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
            out = os.path.join(DOWNLOAD_DIR, f"sd_{pii}.pdf")
            with open(out, "wb") as f:
                f.write(pdf_bytes)
            t_total = time.time() - t0
            print(f"  [{index}] ✓ {len(pdf_bytes)} bytes | 总耗时: {t_total:.0f}s (页面:{t_page:.0f}s CF:{t_cf:.0f}s 下载:{time.time()-t_fetch:.0f}s)")
            return True, t_total
        else:
            print(f"  [{index}] ✗ fetch 失败")
            return False, time.time() - t0
    except Exception as e:
        print(f"  [{index}] ✗ 异常: {e}")
        return False, time.time() - t0


# ═══════════════════════════════════════════════════
# 主流程：一次会话，连续下载
# ═══════════════════════════════════════════════════
print("=" * 60)
print("会话复用测试：一次 Edge 启动，连续下载 3 篇")
print("=" * 60)

# 清 session 防标签页恢复
for f in [os.path.join(PROFILE, "Default", "Sessions"),
          os.path.join(PROFILE, "Default", "Current Tabs"),
          os.path.join(PROFILE, "Default", "Last Tabs")]:
    try:
        if os.path.isfile(f):
            os.remove(f)
    except Exception:
        pass

print("启动 Edge（一次）...")
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
else:
    print("端口超时"); proc.terminate(); sys.exit(1)

results = []
try:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = browser.contexts[0]
        page = ctx.pages[0]

        for i, url in enumerate(TEST_URLS):
            ok, elapsed = download_one(page, url, i + 1)
            results.append((url, ok, elapsed))
            # 短暂冷却
            if i < len(TEST_URLS) - 1:
                time.sleep(3)

        browser.close()
finally:
    proc.terminate()

print(f"\n{'='*60}")
print("对比结果:")
print(f"{'='*60}")
for url, ok, elapsed in results:
    status = "✓" if ok else "✗"
    print(f"  {status} {elapsed:6.0f}s  {url.split('/')[-1][:30]}")
if results:
    print(f"\n第 1 篇: {results[0][2]:.0f}s")
    if len(results) > 1:
        avg_later = sum(r[2] for r in results[1:]) / len(results[1:])
        print(f"后续平均: {avg_later:.0f}s")
        if results[0][2] > 0:
            print(f"提速比: {results[0][2]/avg_later:.1f}x" if avg_later < results[0][2] else f"反而慢了")
