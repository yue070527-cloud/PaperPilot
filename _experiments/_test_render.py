"""直接测试 HTML 渲染 — pywebview 窗口"""
import sys, time, subprocess, tempfile, json, os
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

html_path = r"C:\Users\Dyotropic\.paperpilot_html_cache\10.1056_NEJMoa1817226.html"
title = "NEJM — CAR-T Cell Therapy"

from paperpilot.pdf_viewer import _open_html_window, _open_error_window

print("Opening pywebview window...")
print("  (keep this terminal open until you close the reader window)")
ok = _open_html_window(
    html_path=html_path,
    title=title,
    theme_seed="#0097A7",
    dark_mode=True,
    x=None,
    y=None,
)
print(f"Launch result: {ok}")

if ok:
    print("Window should be open now. Close it to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDone.")
