"""测试 Firefox 引擎访问 SD"""
import io, sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

sd_url = "https://www.sciencedirect.com/science/article/pii/S0956566317306371"

print("=== Playwright Firefox 启动 ===")
pw = sync_playwright().start()
browser = pw.firefox.launch(headless=False)
page = browser.new_page()

print("访问文章页...")
t0 = time.time()
try:
    page.goto(sd_url, wait_until="domcontentloaded", timeout=30000)
except Exception as e:
    print(f"goto 异常: {e}")

# 等 Cloudflare 挑战完成
time.sleep(5)

title = page.title()
print(f"标题: {title[:120]}")

result = page.evaluate("""
    () => {
        const body = document.querySelector('#body');
        const article = document.querySelector('article');
        return {
            bodyLen: body ? body.textContent.trim().length : 0,
            articleLen: article ? article.textContent.trim().length : 0,
            preview: (document.body?.textContent || '').substring(0, 500),
        };
    }
""")
print(f"#body: {result['bodyLen']}, article: {result['articleLen']}")
print(f"内容: {result['preview'][:300]}")

# 检查 bot
for b in ['are you a robot', 'not a robot', 'captcha']:
    if b in result['preview'].lower():
        print(f"FAIL: '{b}'")
        break
else:
    if result['bodyLen'] > 5000:
        print("OK! 正文可用")
    elif result['articleLen'] > 5000:
        print("OK! article 可用")

browser.close()
pw.stop()
print("=== 完成 ===")
