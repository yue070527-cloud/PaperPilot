"""测试：关闭多余标签页 + 等待页面真正加载"""
import sys, time, os
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import paperpilot._cdp_fetcher_v1_backup as cdp

URL = "https://www.sciencedirect.com/science/article/pii/S0889157523001370"
print(f"测试: {URL.split('/')[-1]}")

t0 = time.time()
with cdp.CDPFetcher(restart_edge_on_exit=True) as f:
    ctx = f._browser.contexts[0]
    print(f"连接成功, {len(ctx.pages)} 个 page, 耗时: {time.time()-t0:.0f}s")

    # 关掉恢复的标签页（除第一个外）
    if len(ctx.pages) > 1:
        for p in ctx.pages[1:]:
            try:
                p.close()
            except Exception:
                pass
        print(f"  关闭多余标签页, 剩余: {len(ctx.pages)}")

    page = ctx.pages[0]
    print(f"当前页面: {page.url[:80]}")

    # 导航
    print("导航到文章页...")
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  goto: {e}")

    # 等待页面实际加载完成 — 检查标题是否变成真正的文章标题
    print("等待页面加载...")
    for i in range(90):
        time.sleep(1)
        try:
            t = page.title()
            # 跳过各种中间态
            if t in ("", "about:blank", "ScienceDirect", "Elsevier", "ScienceDirect.com"):
                if i % 15 == 0:
                    print(f"  [{i}s] 标题: '{t}' (加载中...)")
                continue
            if "Just a moment" in t or "请稍候" in t:
                if i % 15 == 0:
                    print(f"  [{i}s] CF: {t[:60]}")
                continue
            # 真文章标题
            if len(t) > 20:
                print(f"  [{i}s] 就绪: {t[:100]}")
                break
        except Exception:
            pass

    time.sleep(3)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    time.sleep(2)

    t = page.title()
    print(f"  最终标题: {t[:100]}")

    # 检查 pdfft
    try:
        count = page.evaluate("() => document.querySelectorAll('a[href*=\"pdfft\"]').length")
        print(f"  pdfft 链接: {count}")
    except Exception as e:
        print(f"  失败: {e}")

    if count == 0:
        print("没有 PDF 链接，退出")
    else:
        # 尝试下载
        f._bring_page_to_front(page)
        time.sleep(1)

        try:
            with page.context.expect_page(timeout=20000) as popup_info:
                page.evaluate("""
                    () => {
                        const links = document.querySelectorAll('a[href*="pdfft"]');
                        for (const a of links) {
                            if (a.offsetParent !== null && a.textContent.includes('PDF')) {
                                a.click(); return;
                            }
                        }
                        links[0].click();
                    }
                """)
            popup = popup_info.value
            print(f"  Popup: {popup.url[:120]}")

            for i in range(60):
                time.sleep(1)
                if i % 3 == 0:
                    f._bring_page_to_front(popup)
                try:
                    if "sciencedirectassets.com" in popup.url:
                        print(f"  [{i}s] -> assets!")
                        break
                except Exception:
                    pass

            if "sciencedirectassets.com" in popup.url:
                import base64
                b64 = popup.evaluate("""
                    async () => {
                        const url = window.location.href;
                        const resp = await fetch(url, {credentials: 'include', headers: {'Accept': 'application/pdf'}});
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
                    out = "D:/Desktop/PaperPilot/downloads/sd_backup_v3.pdf"
                    with open(out, "wb") as fh:
                        fh.write(pdf_bytes)
                    print(f"  [OK] {len(pdf_bytes)} bytes -> downloads/sd_backup_v3.pdf")
            else:
                print(f"  未重定向: {popup.url[:120]}")
            popup.close()
        except Exception as e:
            print(f"  Popup 异常: {e}")

    elapsed = time.time() - t0
    print(f"\n总耗时: {elapsed:.0f}s")
