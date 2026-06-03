"""批量测试 SD PDF 下载 v2 — 修复 Edge 标签页恢复 + DOI 重定向等待"""
import sys, time, os, socket, subprocess, base64
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

TEST_URLS = [
    "https://www.sciencedirect.com/science/article/pii/S0263224120313221",
    "https://doi.org/10.1016/j.foodchem.2020.127708",
    "https://doi.org/10.1016/j.foodchem.2023.136528",
    "https://doi.org/10.1016/j.jfca.2023.105263",
    "https://doi.org/10.1016/j.bios.2017.09.028",
]

DEBUG_PORT = 9228
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")

# 每次测试前清空 session 文件，防止标签页恢复
_SESSION = os.path.join(PROFILE, "Default", "Sessions")
_TABS = os.path.join(PROFILE, "Default", "Current Tabs")
_LAST_TABS = os.path.join(PROFILE, "Default", "Last Tabs")

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

from playwright.sync_api import sync_playwright


def test_one(url: str) -> bool:
    pii = url.split("/pii/")[1].split("?")[0] if "/pii/" in url else url.split("/")[-1].replace(".", "_")
    print(f"\n{'='*60}")
    print(f"测试: {pii}")
    print(f"URL: {url[:100]}")
    print(f"{'='*60}")

    # 清 session 防标签页恢复
    for f in [_SESSION, _TABS, _LAST_TABS]:
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass

    print("启动 Edge...")
    proc = subprocess.Popen(
        [edge_exe, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={PROFILE}", "--no-first-run",
         "--no-default-browser-check",
         "--disable-blink-features=AutomationControlled",
         "about:blank"],  # ★ 从空白页启动，手动导航
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
        print("  ✗ 端口超时")
        proc.terminate()
        return False

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            ctx = browser.contexts[0]
            page = ctx.pages[0]

            # ★ 手动导航（绕过标签页恢复问题）
            print(f"导航到文章页...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

            # 等待页面加载完成（含 DOI → linkinghub → SD 重定向链）
            print("等待页面渲染...")
            page_ready = False
            for i in range(120):
                time.sleep(1)
                try:
                    t = page.title()
                    current_url = page.url

                    # 处理各种中间状态
                    if t in ("", "about:blank"):
                        continue
                    if "linkinghub.elsevier.com" in current_url:
                        # DOI 中间页，等待 JS 重定向到 SD
                        if i % 10 == 0:
                            print(f"  [{i}s] linkinghub 重定向中...")
                        continue
                    if "Loading" in t and "linkinghub" in current_url:
                        continue
                    if "Just a moment" in t or "请稍候" in t:
                        continue
                    if "Gateway Time" in t or "Bad Gateway" in t or "Service Unavailable" in t:
                        print(f"  ✗ 服务器错误 [{i}s]: {t[:100]}")
                        break
                    if "Page Not Found" in t or "Not Found" in t:
                        print(f"  ✗ 页面不存在 [{i}s]")
                        break
                    if ("sciencedirect.com" in current_url or "elsevier.com" in current_url) and t:
                        if i > 2:
                            print(f"  就绪 [{i}s]: {t[:100]}")
                            page_ready = True
                            break
                except Exception:
                    pass
            else:
                try:
                    print(f"  超时，最后: {page.title()[:100]} | {page.url[:100]}")
                except Exception:
                    print("  超时，无法获取页面状态")

            if not page_ready:
                browser.close()
                return False

            # 确保不在 linkinghub 中间页
            for _ in range(30):
                if "linkinghub" not in page.url:
                    break
                time.sleep(1)

            time.sleep(3)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            time.sleep(2)

            # 检查是否有 PDF 按钮
            try:
                has_pdf = page.evaluate("""
                    () => {
                        const links = document.querySelectorAll('a[href*="pdfft"]');
                        return links.length;
                    }
                """)
                print(f"  PDF 按钮: {has_pdf} 个")
            except Exception:
                print("  ✗ 页面导航中，无法检测 PDF 按钮")
                browser.close()
                return False

            if has_pdf == 0:
                print("  ✗ 页面上没有 PDF 按钮（可能无权限）")
                browser.close()
                return False

            # 点击 PDF
            print("  点击 View PDF...")
            pdf_bytes = None
            try:
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
                print(f"  Popup: {popup.url[:120]}")

                # 等重定向到 assets
                for i in range(90):
                    time.sleep(1)
                    try:
                        if "sciencedirectassets.com" in popup.url:
                            print(f"  [{i}s] → assets")
                            break
                    except Exception:
                        pass

                try:
                    popup.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                time.sleep(3)

                final = popup.url
                if "sciencedirectassets.com" in final:
                    print("  JS fetch...")
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
                    if b64 and not b64.startswith("STATUS_") and b64 != "NOT_PDF":
                        pdf_bytes = base64.b64decode(b64)
                        if pdf_bytes[:5] == b"%PDF-":
                            out = os.path.join(os.path.expanduser("~"), "Desktop", f"sd_{pii}.pdf")
                            with open(out, "wb") as f:
                                f.write(pdf_bytes)
                            print(f"  ✓ 成功: {len(pdf_bytes)} bytes → {os.path.basename(out)}")
                        else:
                            print(f"  ✗ 非 PDF")
                    else:
                        print(f"  ✗ JS fetch 返回: {str(b64)[:100] if b64 else 'None'}")
                else:
                    print(f"  ✗ 未重定向: {final[:120]}")
                popup.close()
            except Exception as e:
                print(f"  ✗ PDF 流程异常: {e}")

            browser.close()
    finally:
        proc.terminate()
        # 等进程结束
        time.sleep(1)

    return pdf_bytes is not None


# ── 主流程 ──
results = []
for url in TEST_URLS:
    ok = test_one(url)
    results.append((url, ok))

print(f"\n{'='*60}")
print("汇总:")
for url, ok in results:
    status = "✓" if ok else "✗"
    short = url.split("/")[-1][:40]
    print(f"  {status} {short}")
print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} 成功")
