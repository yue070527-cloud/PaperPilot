# PaperPilot 论文阅读系统 — 数据读取功能文档

> 最后更新: 2026-06-03 | 分支: feature/ai-service

## 功能概览

论文全文获取分为两条路径，覆盖所有主流出版商：

| 路径 | 适用对象 | 格式 | 速度 |
|------|----------|------|------|
| **直链 PDF** | arXiv, Nature, Springer, ACS, Wiley, IEEE, IOP, RSC, Science | PDF (PDF.js 渲染) | 2-5s |
| **HTML 全文提取** | ScienceDirect, NEJM, Science (CF 保护站点) | HTML (pywebview 渲染) | 9-16s |

## 核心文件

| 文件 | 职责 |
|------|------|
| `paperpilot/downloader.py` | PDF 直链下载 + CDP HTML 全文提取 + BrowserSession |
| `paperpilot/pdf_viewer.py` | PDF.js / HTML pywebview 双模式渲染窗口 |

## 架构流程

```
open_full_reader(paper)
  │
  ├─ 1. 本地 PDF → PDF.js 窗口
  ├─ 2. cache_pdf() → 直链 PDF → PDF.js 窗口
  ├─ 3. fetch_full_text() → HTML 提取 → pywebview 深色阅读窗口
  └─ 4. 错误页面
```

---

## 一、直链 PDF 下载 (`download_pdf` / `cache_pdf`)

### 下载链路（5 步回退）

1. arXiv 直链: `arxiv.org/pdf/{id}.pdf`
2. Nature 直链: `nature.com/articles/{id}.pdf`
3. Springer 直链: `link.springer.com/content/pdf/{doi}.pdf`
4. Guessed URL: ACS/Science/Wiley/IEEE/IOP/RSC/NEJM 的 PDF 直链
5. `citation_pdf_url` meta 标签: 从 DOI 页面提取

### 缓存

- 目录: `~/.paperpilot_pdf_cache/`
- 上限: 512MB，超出自动删最旧文件
- 文件名: `{doi}.pdf` 或 `{hash}.pdf`
- **缓存命中时不发起网络请求**

---

## 二、HTML 全文提取 (`fetch_full_text`)

### 工作原理

1. 启动屏幕外 CDP Edge 浏览器（持久化 profile）
2. 导航到文章页，等待 JS 渲染完成
3. 正文容器选择器提取 HTML
4. 图片通过 in-page JS `fetch()` 转为 base64 data URI
5. 移除导航/页脚/参考文献/侧边栏等非内容元素
6. 组装为自包含深色模式 HTML 文件，缓存到本地

### 出版商选择器

```python
"sciencedirect": "#body"           # SD 专用，最稳定
"nejm":          "article, ..."    # 通用选择器
"nature":        "article, ..."
# 其他: "article, main, [class*='article-body']"
```

### 缓存

- 目录: `~/.paperpilot_html_cache/`
- 文件名: `{doi}.html` 或 `{hash}.html`
- **缓存命中时不启动浏览器**

### 浏览器窗口策略

- **默认**: 窗口在屏幕外 (`--window-position=-32000,-32000`)，完全无感
- **CF 兜底**: 若 CF 挑战持续超过 12 秒，临时将窗口移至屏幕内 + 前台，通过后立即隐藏
- **Profile**: `~/pp_edge_trusted/`，保存 cookie/CARSI 登录/CF 信任记录

### 反检测措施

- Playwright `add_init_script` 注入反 webdriver 检测脚本
- `--disable-blink-features=AutomationControlled`
- 持久化 profile 积累 CF 信任

---

## 三、渲染 (`pdf_viewer.py`)

### PDF 模式

- 技术: pywebview WebView2 + PDF.js (CDN)
- 默认深色主题，种子色 `#0097A7`
- 支持缩放、搜索、打印

### HTML 模式

- 自包含 HTML 文件（图片 base64 内嵌）
- 深色主题 + 衬线字体（Georgia / Noto Serif SC）
- 窗口控制: 最小化/最大化/关闭
- 样式与 PDF.js 阅读器一致

---

## 四、注意事项 / 踩坑记录

### 1. Edge 僵尸进程

- **现象**: 测试中断后残留的 Edge 进程占用 profile，下次提取超时（93s+）
- **解决**: `BrowserSession.start()` 启动前自动调用 `_kill_profile_zombies()`，用 `psutil` 查找并杀掉占用同一 profile 的进程
- **预防**: 始终通过 `BrowserSession.stop()` 正常关闭，避免直接 kill Python 进程

### 2. Cloudflare 托管挑战

- ScienceDirect 和部分站点使用 CF Managed Challenge
- CF 检测 `document.hasFocus()`，后台窗口分配更难/更慢的挑战
- Profile 积累信任后挑战大幅减少（同站第二篇起基本秒过）
- 冷启动首篇可能超时，重试即过（cookie 已落盘）
- **不要**在 SD 页面反复刷新触发 CF 风控

### 3. ScienceDirect URL 格式

- 优先使用 `linkinghub.elsevier.com/retrieve/pii/{PII}` 重定向格式
- SD 直链 `sciencedirect.com/science/article/pii/{PII}` 也可用
- `?via%3Dihub` 参数不影响结果
- `/abs/` 路径会被自动替换为 `/` 以确保完整文章页

### 4. 图片提取限制

- 跳过 `width < 50px` 的小图标和 1px 追踪像素
- 跳过 `< 1KB` 的图片
- 跨域图片需要 CDP 浏览器环境（`credentials: include`）
- base64 编码会增大 HTML 文件体积（典型: 117KB-1811KB）

### 5. Profile 目录

- `~/pp_edge_trusted/` — 存放浏览器 cookie、登录状态、CF 信任
- **不要手动删除**该目录，否则 CF 信任归零，需要重新积累
- **不要**在多个并发进程中使用同一 profile

### 6. 网络环境要求

- 直链 PDF: 需要能访问对应出版商
- HTML 提取: 需要能访问目标网站 + CF 挑战通过
- 校园网/CARSI 环境可登录机构订阅，获得更多 PDF 访问权限
- 断网时所有方法返回 None，不会抛异常

### 7. 性能基线

| 操作 | 首篇（冷 profile） | 后续 |
|------|-------------------|------|
| SD HTML 提取 | 可能超时→重试即过 | 9-11s |
| NEJM HTML 提取 | ~12s | ~12s |
| Science HTML 提取 | ~16s | ~9s |
| 直链 PDF | 2-5s | 2-5s |
| 缓存命中 | <0.1s | <0.1s |

### 8. 依赖项

```
curl_cffi>=0.5.0       # 直链 HTTP（TLS 指纹）
PyMuPDF>=1.23.0        # PDF 文本提取 + 预览
beautifulsoup4>=4.12.0 # HTML 解析
lxml>=4.9.0            # bs4 后端
playwright>=1.40.0     # CDP 浏览器控制
psutil>=5.9.0          # 僵尸进程清理
pywebview>=4.0         # 阅读窗口
```

### 9. 待改进

- 大批次提取时建议串行（同一 profile 不支持并发）
- HTML 排版未保留原始分栏布局
- 数学公式/表格可能提取不完整
- 无 PDF 下载回退的站点（如 Taylor & Francis）需要补充选择器
