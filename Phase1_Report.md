# PaperPilot 第一阶段报告：智能文献检索与筛选

> 版本：Phase 1（2026-05-26） | 分支：`develop` | 提交：`136ed12`

---

## 一、项目概述

PaperPilot 是面向科研课题攻关的**可解释智能文献工作流系统**。第一阶段实现了从课题输入到论文检索、排序、筛选的完整闭环。

**技术栈**：Python + Flet（UI）+ Sentence Transformers（语义排序）+ DeepSeek API（关键词提取/翻译）+ arXiv / OpenAlex API

---

## 二、项目结构

```
PaperPilot/
├── app.py                      # Flet UI 主程序（~1200 行）
├── config.yaml                 # 用户配置文件
├── paperpilot/
│   ├── __init__.py
│   ├── config.py               # 配置加载器（支持环境变量展开）
│   ├── models.py               # SQLAlchemy 数据模型
│   ├── keywords.py             # 关键词提取编排（DeepSeek + jieba fallback）
│   ├── core_extractor.py       # 核心/辅助关键词提取（DeepSeek API）
│   ├── mt_translator.py        # 中文关键词 → 英文术语翻译（DeepSeek API）
│   ├── fetcher.py              # arXiv / OpenAlex 论文抓取 + 多路检索
│   └── indexer.py              # Cross-Encoder 语义精排 + 关键词加分
├── cache/                      # 本地缓存目录（API 响应 + Embedding）
├── CLAUDE.md                   # 项目协作铁律
└── docs/                       # 文档
```

---

## 三、检索流水线

### 3.1 整体架构

```
课题描述
  ├── DeepSeek 提取核心关键词（1-3 个）
  ├── DeepSeek 提取辅助关键词（5-8 个）
  └── jieba TF-IDF（API 不可用时 fallback）
        │
        ▼
  中文关键词 ──DeepSeek──▶ 英文术语
        │
        ▼
  多主关键词独立检索（每路独立级联）
  ├── arXiv API  ──ThreadPoolExecutor 45s 超时
  └── OpenAlex API ──socket 15s 超时
        │
        ▼
  去重（标题相似度 ≥ 90%）
        │
        ▼
  API 分数粗筛（Top 100-200）
        │
        ▼
  Cross-Encoder 语义精排（mxbai-rerank-base-v2）
        │
        ▼
  关键词匹配加分（三层加权）
        │
        ▼
  客户端筛选（年份 / 类型 / 引用数）
        │
        ▼
  结果展示（DataTable + 详情侧边栏 + 雷达图）
```

### 3.2 三级级联检索策略

| 策略 | 逻辑 | 说明 |
|------|------|------|
| Strategy 0 | 全部核心词 AND + 辅助词 OR | 最严格，召回最少但最相关 |
| Strategy 1 | 主关键词 AND + 其余 OR | 中等严格 |
| Strategy 2 | 全部词 OR | 安全网，保证有结果 |

每条策略按序尝试，结果数 ≥ `min_results`(3篇) 时立即返回，不继续降级。

### 3.3 多主关键词独立检索

每个主关键词独立进行一次完整级联检索，结果合并去重。命中多个主关键词的论文获得 `api_score` 加权（最多 1.3×），自然排在前面。

### 3.4 分数计算

```
最终得分 = CE 分数 + 关键词匹配加分 × 0.12

关键词匹配加分（逐层二分，每层最多计一次）：
  主关键词命中：+1.0
  副关键词命中：+0.7
  辅助关键词命中：+0.4
```

---

## 四、关键模型

| 模型 | 用途 | 大小 | 加载方式 |
|------|------|------|----------|
| `deepseek-v4-flash` | 关键词提取 + 翻译 | API | HTTP 请求，15s 超时 |
| `mxbai-rerank-base-v2` | 语义精排（Cross-Encoder） | 942MB（367M 参数） | 本地懒加载，180s 超时，`max_length=512` |
| `paraphrase-multilingual-MiniLM-L12-v2` | 文本 Embedding（FAISS 备用） | ~420MB | 懒加载，当前未激活 |
| `jieba` | 中文分词（API fallback） | ~50MB | 即用即加载 |

---

## 五、配置说明（config.yaml）

```yaml
deepseek:
  api_key: sk-xxx          # DeepSeek API 密钥（必填）
  model: deepseek-v4-flash # 可选：v4-flash / v4-pro / v3

embedding:
  local_model: paraphrase-multilingual-MiniLM-L12-v2
  cache_dir: ./cache/embedding

data_sources:
  arxiv: true              # 启用 arXiv 检索
  openalex: true           # 启用 OpenAlex 检索
  local_pdf: true          # 本地 PDF 导入

search:
  max_results_per_source: 30
  default_top_k: 20

cache:
  dir: ./cache/api         # API 响应缓存目录
  ttl_hours: 24            # 缓存有效期

ui:
  window_width: 1100
  window_height: 750
  title: PaperPilot
  theme: cyan              # mint / ocean / sand / dusk / rose / cyan
  dark_mode: true
```

### 配置稳定性保障

- **配置与代码分离**：所有 API Key、路径、超时参数配置在 `config.yaml`，代码通过 `load_config()` 读取
- **环境变量支持**：敏感值支持 `${ENV_VAR}` 格式从环境变量读取
- **零硬编码**：业务代码中不出现任何绝对路径或 API Key 字符串
- **递归合并保存**：`save_config()` 只更新指定字段，保留已有结构和注释
- **全局单例**：`config = load_config()` 进程启动时加载一次，避免重复 I/O
- **.gitignore 保护**：`.env`、`.env.local` 已在忽略列表，API Key 不会误提交

---

## 六、开发时间线

| 阶段 | 关键提交 | 内容 |
|------|----------|------|
| **UI 框架** | `c1ea8f8` ~ `525b79e` | Flet 0.85 三标签页框架、DataTable 展示 |
| **检索管道** | `d6f17b5` ~ `9460e9c` | 课题翻译 + arXiv/OpenAlex 并行检索 + 线程安全 |
| **关键词系统** | `9e79734` ~ `4c5c4d1` | 拖拽三区 + 三层关键词 + 权重分层 |
| **排序系统** | `c3c3cbe` ~ `2960a40` | Cross-Encoder 精排 + 分数融合 + FAISS 移除 |
| **筛选 & 修复** | `ec3cf7d` ~ `0a3be83` | 年份/类型/引用筛选 + fetch_multi_primary 修复 |
| **稳定性修复** | `136ed12` | CE 长序列 OOM 修复 + 全链路超时保护 |

---

## 七、开发过程中遇到的问题与解决方案

### 7.1 Cross-Encoder 长序列内存爆炸（最严重）
- **现象**：Python 占用 10GB 内存，排序阶段卡死
- **根因**：`CrossEncoder` 未设 `max_length`，Qwen2 默认 32K tokens。100 篇论文摘要被完整 tokenize（每篇 2000-8000 tokens），注意力 O(n²) 导致激活值达 10GB+
- **修复**：设 `max_length=512`，注意力复杂度降低 244 倍，内存降至 ~1.5GB

### 7.2 arXiv 抓取 5 分钟无响应
- **根因**：① 后台线程跨线程访问 Flet 控件 ② arxiv 库底层 `requests.Session` 无默认超时
- **修复**：① 主线程预读所有 Flet 值传入后台线程 ② `ThreadPoolExecutor` + `future.result(timeout=45)` 包裹 arXiv 调用

### 7.3 OpenAlex 检索结果暴跌（300→18）
- **根因**：`fetch_multi_primary`（多路独立检索）被误改为 `fetch_with_cascade`（所有主关键词 AND），3 个主关键词 AND 导致极少数论文同时匹配
- **修复**：恢复 `fetch_multi_primary`，每个主关键词独立级联检索

### 7.4 关键词 prompt 被改动导致输出过长
- **现象**：搭档反馈关键词"过长、过多"，核心词 3-5 个 + 辅助词过多
- **修复**：回退 prompt 到原始版本（核心 1-3 个、辅助 5-8 个）

### 7.5 URL 按钮无法跳转
- **根因**：Flet `Page.launch_url()` 在 Windows 上不工作
- **修复**：改用 Python 标准库 `webbrowser.open()`

### 7.6 DeepSeek V4 无输出
- **根因**：V4 Flash/Pro 默认开启 reasoning 模式，消耗 reasoning tokens 后无实际输出
- **修复**：所有非推理类 API 调用显式传 `"thinking": {"type": "disabled"}`

### 7.7 翻译失败导致中文灌入英文数据源
- **修复**：翻译结果用 `_has_cjk()` 过滤，中文未翻译的项丢弃而非直接使用

### 7.8 模型加载无超时
- **修复**：Cross-Encoder 加载 180s 超时 + 预测 120s 超时，超时自动回退到纯 API 分数排序

---

## 八、UI 功能清单

- **三标签页导航**：检索 / 文献 / 设置
- **拖拽三区关键词**：主关键词（权重 1.0）/ 副关键词（0.7）/ 辅助关键词（0.4）
- **6 套 Material 3 配色主题**：mint / ocean / sand / dusk / rose / cyan
- **夜间模式**：深色主题全局生效
- **结果筛选栏**：年份范围 / 文章类型（综述/论文/书籍等）/ 来源 / 最低引用数
- **论文详情侧边栏**：固定右侧面板，含标题、作者、年份、来源、类型、期刊、引用数、摘要
- **雷达图**：论文质量多维度可视化
- **检索参数可调**：最大结果数（10-500）/ CE 候选数（10-200）/ Top K（10-200）

---

## 九、健壮性设计

### 超时保护（全链路）
| 环节 | 超时值 | 失败策略 |
|------|--------|----------|
| 关键词提取 API | 15s | 回退 jieba TF-IDF |
| 术语翻译 API | 15s | 丢弃未翻译项 |
| arXiv 抓取 | 45s × 2 次 | 返回空列表 |
| OpenAlex 抓取 | 15s | 返回空列表 |
| CE 模型加载 | 180s | 纯 API 分数排序 |
| CE 预测 | 120s | 纯 API 分数排序 |
| 摘要补齐 | 10s/篇 | 跳过该篇 |

### 降级策略
- DeepSeek API 不可用 → jieba TF-IDF + KeyBERT fallback
- Cross-Encoder 不可用 → API 分数归一化作为最终排序
- 翻译失败 → 保留已翻译项，中文项丢弃
- 任一数据源失败 → 继续使用其他数据源
- 检索结果不足 → 三级策略自动降级到全 OR

### 线程安全
- Flet 控件值在主线程预读取，后台线程仅通过参数接收
- Cross-Encoder 模型加载使用 `threading.Lock` 双检锁
- 后台线程通过 `threading.Event` 通知主线程完成

---

## 十、当前状态与第二阶段展望

### 已完成
- [x] 课题关键词提取（DeepSeek + jieba fallback）
- [x] 中英文术语翻译
- [x] arXiv + OpenAlex 多路独立检索
- [x] 三级级联检索策略
- [x] Cross-Encoder 语义精排
- [x] 关键词匹配三层加分
- [x] 客户端筛选（年份 / 类型 / 引用 / 来源）
- [x] 文章类型标签识别
- [x] 6 套主题 + 夜间模式
- [x] 拖拽三区关键词管理
- [x] 论文详情侧边栏 + 雷达图
- [x] 全链路超时保护

### 第二阶段候选方向
- 本地 PDF 文献解析与索引
- 文献收藏夹与项目管理
- 文献引用网络可视化
- 课题-文献匹配度雷达图增强
- 导出参考文献（BibTeX / EndNote）
- 系统托盘常驻 + 定时推送
- Ollama 本地模型集成（离线模式）

---

## 十一、附录：命令行速查

```bash
# 启动应用
python app.py

# 运行关键词提取测试
python test_keyword_accuracy.py

# 查看当前配置
python -c "from paperpilot.config import config; print(config)"

# 清除缓存
rm -rf cache/api/* cache/embedding/*

# 检查模型文件
ls ~/.cache/modelscope/mixedbread-ai/mxbai-rerank-base-v2/
```
