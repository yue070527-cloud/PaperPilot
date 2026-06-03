"""诊断 v10：简化 — 硬等 redirect，然后 JS fetch + curl_cffi 多种方式"""
import sys, time, os, socket, subprocess, base64
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PII = "S0308814620315703"
ARTICLE_URL = f"https://www.sciencedirect.com/science/article/pii/{PII}"
DEBUG_PORT = 9228
PROFILE = os.path.join(os.path.expanduser("~"), "pp_edge_trusted")

print(f"文章: {ARTICLE_URL}")

for p in ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
          "C:/Program Files/Microsoft/Edge/Application/msedge.exe"]:
    if os.path.exists(p):
        edge_exe = p
        break

print("启动 Edge...")
proc = subprocess.Popen(
    [edge_exe, f"--remote-debugging-port={DEBUG_PORT}",
     f"--user-data-dir={PROFILE}", "--no-first-run",
     "--no-default-browser-check",
     "--disable-blink-features=AutomationControlled",
     ARTICLE_URL],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

print("等待端口...")
for i in range(30):
    time.sleep(1)
    try:
        s = socket.create_connection(("127.0.0.1", DEBUG_PORT), timeout=1)
        s.close()
        print(f"  就绪 ({i+1}s)")
        break
    except Exception:
        pass
else:
    print("ERROR: 超时"); proc.terminate(); sys.exit(1)

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
    ctx = browser.contexts[0]
    page = ctx.pages[0]

    for i in range(60):
        time.sleep(1)
        t = page.title()
        if t and 'Just a moment' not in t and '请稍候' not in t and 'challenge' not in t.lower():
            print(f"页面就绪: {t[:100]}")
            break
    time.sleep(3)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)

    # ═════════════════════════════════════════════════════
    # 打开 popup，等待完成加载
    # ═════════════════════════════════════════════════════
    pdf_bytes = None
    try:
        print("\n点击 View PDF...")
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
        print(f"Popup: {popup.url[:150]}")

        # Just wait for the full redirect chain to complete
        print("等待 popup 完成加载（redirect → CF → assets → PDF viewer）...")
        for i in range(90):
            time.sleep(1)
            try:
                pu = popup.url
                if i % 15 == 0:
                    print(f"  [{i}s] {pu[:130]}")
                if 'sciencedirectassets.com' in pu:
                    print(f"  [{i}s] → assets!")
                    break
            except Exception:
                pass

        # Wait for PDF viewer to fully render
        try:
            popup.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        time.sleep(5)

        final_url = popup.url
        print(f"\n最终 URL: {final_url[:200]}")

        if 'sciencedirectassets.com' not in final_url:
            print("未能重定向到 assets，放弃")
            popup.close()
            browser.close()
            sys.exit(0)

        # ═════════════════════════════════════════════════
        # 方案 1: JS fetch with credentials (correct Referer)
        # ═════════════════════════════════════════════════
        print("\n── 方案 1: JS fetch ──")
        try:
            b64 = popup.evaluate("""
                async () => {
                    const url = window.location.href;
                    try {
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
                    } catch(e) { return 'ERROR_' + e.message; }
                }
            """)
            print(f"  JS fetch 结果: {b64[:80] if b64 else 'None'}...")
            if b64 and not b64.startswith("STATUS_") and not b64.startswith("ERROR_") and not b64.startswith("NOT_PDF"):
                pdf_bytes = base64.b64decode(b64)
                if pdf_bytes[:5] == b"%PDF-":
                    print(f"  ✓ PDF: {len(pdf_bytes)} bytes")
        except Exception as e:
            print(f"  失败: {e}")

        # ═════════════════════════════════════════════════
        # 方案 2: CDP Network.getResponseBody (may work now since PDF is loaded)
        # ═════════════════════════════════════════════════
        if not pdf_bytes:
            print("\n── 方案 2: CDP Network ──")
            try:
                cdp = ctx.new_cdp_session(popup)
                cdp.send("Network.enable")

                # Reload to trigger a fresh network request
                captured = [None]
                def on_response(params):
                    resp = params.get("response", {})
                    if resp.get("status") == 200 and "application/pdf" in resp.get("mimeType", ""):
                        captured[0] = params

                cdp.on("Network.responseReceived", on_response)

                # Reload the popup
                print("  重新加载 popup...")
                try:
                    popup.reload(wait_until="load", timeout=30000)
                except Exception:
                    pass
                time.sleep(5)

                if captured[0]:
                    request_id = captured[0].get("requestId", "")
                    print(f"  尝试 getResponseBody...")
                    result = cdp.send("Network.getResponseBody", {"requestId": request_id})
                    body = result.get("body", "")
                    if result.get("base64Encoded"):
                        body = base64.b64decode(body)
                    elif isinstance(body, str):
                        body = body.encode("latin-1")
                    if isinstance(body, bytes) and body[:5] == b"%PDF-":
                        pdf_bytes = body
                        print(f"  ✓ CDP PDF: {len(body)} bytes")
                    else:
                        print(f"  非 PDF: {str(body)[:100]}")
                else:
                    print("  未捕获到 PDF 响应")
            except Exception as e:
                print(f"  失败: {e}")

        # ═════════════════════════════════════════════════
        # 方案 3: Playwright response.body() via page.on
        # ═════════════════════════════════════════════════
        if not pdf_bytes:
            print("\n── 方案 3: popup.on('response') + reload ──")
            try:
                captured_body = [None]

                def on_resp(response):
                    if "application/pdf" in response.headers.get("content-type", ""):
                        try:
                            body = response.body()
                            if body[:5] == b"%PDF-":
                                captured_body[0] = body
                                print(f"  截获 PDF: {len(body)} bytes")
                        except Exception as e:
                            print(f"  body() 异常: {e}")

                popup.on("response", on_resp)

                print("  重新加载...")
                try:
                    popup.reload(wait_until="load", timeout=30000)
                except Exception:
                    pass
                time.sleep(5)

                if captured_body[0]:
                    pdf_bytes = captured_body[0]
            except Exception as e:
                print(f"  失败: {e}")

        # ═════════════════════════════════════════════════
        # 方案 4: curl_cffi with extracted cookies
        # ═════════════════════════════════════════════════
        if not pdf_bytes:
            print("\n── 方案 4: curl_cffi + cookies ──")
            from curl_cffi import requests as cffi

            # Get cookies from popup context
            cookies = ctx.cookies()
            sd_cookies = {c["name"]: c["value"] for c in cookies
                         if "sciencedirect" in c.get("domain", "") or "elsevier" in c.get("domain", "")}
            print(f"  SD cookies: {list(sd_cookies.keys())}")

            UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36"
            try:
                resp = cffi.get(
                    final_url,
                    headers={
                        "User-Agent": UA,
                        "Referer": "https://www.sciencedirect.com/",
                        "Accept": "application/pdf,*/*",
                    },
                    cookies=sd_cookies,
                    impersonate="chrome124",
                    timeout=60,
                )
                print(f"  curl_cffi: status={resp.status_code}, len={len(resp.content)}")
                if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                    pdf_bytes = resp.content
                    print(f"  ✓ curl_cffi: {len(pdf_bytes)} bytes")
                else:
                    print(f"  非 PDF: {resp.content[:200]}")
            except Exception as e:
                print(f"  失败: {e}")

        popup.close()
    except Exception as e:
        print(f"异常: {e}")
        import traceback
        traceback.print_exc()

    browser.close()

if pdf_bytes:
    path = os.path.join(os.path.expanduser("~"), "Desktop", f"sd_{PII}.pdf")
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    print(f"\n✓ PDF 已保存: {path} ({len(pdf_bytes)} bytes)")
else:
    print("\n✗ 所有方案均失败")

print("=== 完成 ===")
