"""Debug SD page loading"""
import sys, time
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from paperpilot.downloader import BrowserSession

URL = "https://www.sciencedirect.com/science/article/pii/S0263224120313221"

session = BrowserSession()
if not session.start():
    print("BrowserSession start failed!")
    sys.exit(1)

page = session.new_page()

print("Navigating...")
try:
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    print("  goto OK")
except Exception as e:
    print(f"  goto error: {e}")

print("Monitoring page state (60s max)...")
for i in range(40):
    time.sleep(1.5)
    try:
        title = (page.title() or "")
        url = (page.url or "")
        body_len = page.evaluate(
            "() => {"
            "  const sel = document.querySelector('#body')"
            "    || document.querySelector('.Body')"
            "    || document.querySelector('article')"
            "    || document.querySelector('main');"
            "  return sel ? sel.textContent.trim().length : 0;"
            "}"
        )
        print(f"  [{i*1.5:.0f}s] title='{title[:80]}' | body_len={body_len} | url={url[:100]}")
        if body_len > 2000:
            print(f"  -> Body ready! {body_len} chars")
            break
    except Exception as e:
        print(f"  [{i*1.5:.0f}s] ERROR: {e}")

print(f"\nFinal URL: {page.url[:150]}")
print(f"Final title: {page.title()[:100]}")

session.stop()
