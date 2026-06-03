"""测试真实 Edge profile 下载 SD PDF（会重启你的 Edge）"""
import sys, time, os
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

URL = "https://www.sciencedirect.com/science/article/pii/S0889157523001370"
print(f"测试: {URL.split('/')[-1]}")
print("注意: 会短暂关闭 Edge，完成后自动恢复")
print()

t0 = time.time()

from paperpilot.cdp_fetcher import CDPFetcher
from paperpilot.cdp_fetcher import _is_port_open, _is_edge_running, CDP_PORT

print(f"Edge 运行中: {_is_edge_running()}")
print(f"CDP 端口 {CDP_PORT} 已开: {_is_port_open(CDP_PORT)}")
print()

try:
    with CDPFetcher(restart_edge_on_exit=True) as f:
        print(f"连接成功, {len(f._browser.contexts)} 个 context")
        paper = {"url": URL, "doi": ""}
        pdf = f.download_pdf(paper, timeout=120)
        elapsed = time.time() - t0
        if pdf:
            out = "D:/Desktop/PaperPilot/downloads/sd_real_profile_test.pdf"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(pdf)
            print(f"\n[OK] {len(pdf)} bytes -> downloads/sd_real_profile_test.pdf")
            print(f"  总耗时: {elapsed:.0f}s")
        else:
            print(f"\n[FAIL] 总耗时: {elapsed:.0f}s")
except Exception as e:
    elapsed = time.time() - t0
    print(f"\n[ERROR] {e}")
    print(f"  总耗时: {elapsed:.0f}s")
