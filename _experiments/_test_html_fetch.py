"""HTML 全文提取测试 — 回归测试"""
import sys, time, os
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from paperpilot.downloader import fetch_full_text, download_pdf, detect_publisher

PAPERS = [
    {
        "doi": "10.1016/j.measurement.2020.108716",
        "url": "https://linkinghub.elsevier.com/retrieve/pii/S0263224120313221",
        "title": "SD via linkinghub",
    },
    {
        "doi": "10.1016/j.measurement.2020.108716",
        "url": "https://www.sciencedirect.com/science/article/pii/S0263224120313221",
        "title": "SD direct",
    },
]

for paper in PAPERS:
    print(f"\n{'='*60}")
    doi = paper["doi"]
    publisher = detect_publisher(paper)
    print(f"[{paper['title']}] 出版商: {publisher}")

    t0 = time.time()
    pdf = download_pdf(paper)
    if pdf:
        print(f"  直链 PDF: {len(pdf)} bytes | {time.time()-t0:.0f}s")
        continue

    print(f"  直链未命中 ({time.time()-t0:.0f}s)，走 HTML...")
    t0 = time.time()
    path = fetch_full_text(paper, timeout=60)
    elapsed = time.time() - t0

    if path:
        size_kb = os.path.getsize(path) / 1024
        print(f"  [OK] {size_kb:.0f} KB | {elapsed:.0f}s")
    else:
        print(f"  [FAIL] {elapsed:.0f}s")
