# app.py 对接指南

> 基于 `feature/search-improvement` 分支后端改动，整理需要搭档在 UI 端对接的内容。
> 2026-05-24

---

## 一、已实现的后端能力（无需再改）

### fetcher.py

| 函数 | 用途 |
|------|------|
| `fetch_with_cascade(primary, secondary, regular, source, max_results, min_results)` | 三级级联检索，自动降级 |
| `_build_mixed_query(and_kw, or_kw)` | 混合布尔查询（must-AND + should-OR） |

### indexer.py

| 函数 | 用途 |
|------|------|
| `rank_papers(query, papers, top_k, api_weight, primary_kw, secondary_kw, regular_kw, kw_bonus_scale)` | 完整打分流水线（FAISS -> cross-encoder -> fusion + 关键词加分） |
| `fuse_scores(results, api_weight, primary_kw, secondary_kw, regular_kw, kw_bonus_scale)` | API分 + 语义分 + 关键词加分 |
| `keyword_match_bonus(paper, primary, secondary, regular)` | 三层关键词命中加分 |

---

## 二、需要修改的位置

### 1. AppState（app.py:22-31）—— 新增结构化关键词字段

当前 state 只有扁平 `list[str]`，需要增加三层关键词：

```python
class AppState:
    def __init__(self):
        self.topic_name: str = ""
        self.topic_desc: str = ""
        # 旧字段，保留兼容
        self.keywords: list[str] = []
        # 新增：三层关键词
        self.primary_keyword: str | None = None      # 用户手动选择的主关键词
        self.secondary_keywords: list[str] = []       # 核心词中未被选为主关键词的
        self.regular_keywords: list[str] = []          # 普通关键词
        self.papers: list[dict] = []
        self.scores: list[tuple[dict, float]] = []
        self.is_searching: bool = False
        self.status_text: str = ""
        self.selected_paper: dict | None = None
```

### 2. 关键词提取 on_extract（app.py:278-293）—— 保留权重，拆分三层

当前代码丢弃了 `extract_all_keywords` 返回的权重：

```python
# 旧代码（丢弃权重）
weighted = extract_all_keywords(desc, top_n=8)
state.keywords = [kw for kw, _ in weighted]
```

需要改为保留三层结构：

```python
# 新代码
weighted = extract_all_keywords(desc, top_n=8)
# 权重 1.0 = 核心关键词, 权重 0.75 = 普通关键词
core_kw = [kw for kw, w in weighted if w >= 1.0]
regular_kw = [kw for kw, w in weighted if 0 < w < 1.0]
state.secondary_keywords = core_kw      # 初始全部核心词为副关键词
state.regular_keywords = regular_kw
state.primary_keyword = None            # 用户尚未选择主关键词
state.keywords = [kw for kw, _ in weighted]  # 保留兼容
```

### 3. 主关键词选择 UI —— 搭档负责的功能按钮

核心关键词的 Chip 需要支持点击选为"主关键词"：

- 已选为主关键词的 Chip 高亮显示（不同颜色/加粗）
- 点击其他核心词 Chip -> 切换主关键词
- 再次点击已选中的主关键词 Chip -> 取消选择（回退到无主关键词模式）
- 普通关键词的 Chip 不可选为主关键词

状态同步逻辑：

```python
def on_select_primary(kw):
    if state.primary_keyword == kw:
        # 取消选择
        state.primary_keyword = None
        state.secondary_keywords = [k for k, w in weighted if w >= 1.0]
    else:
        # 设为新的主关键词
        state.primary_keyword = kw
        state.secondary_keywords = [k for k in core_kw if k != kw]
```

### 4. _run_pipeline（app.py:45-137）—— 核心改动

需要整体重写检索和打分逻辑。关键变化：

#### 4.1 翻译三层关键词

```python
# 翻译主关键词
primary_en = None
if state.primary_keyword:
    en = translate_terms([state.primary_keyword])
    if en and en[0] and not _has_cjk(en[0]):
        primary_en = en[0]

# 翻译副关键词
secondary_en = [t for t in translate_terms(state.secondary_keywords) if t and not _has_cjk(t)]

# 翻译普通关键词
regular_en = [t for t in translate_terms(state.regular_keywords) if t and not _has_cjk(t)]
```

#### 4.2 用 fetch_with_cascade 替换 fetch_arxiv / fetch_openalex

```python
if arxiv_switch.value:
    state.status_text = "arXiv 抓取中..."
    try:
        arxiv_papers, level = fetch_with_cascade(
            primary_kw=primary_en,
            secondary_kw=secondary_en,
            regular_kw=regular_en,
            source="arxiv",
            max_results=max_per,
            min_results=3,
        )
        print(f"[PaperPilot] arXiv 返回: {len(arxiv_papers)} 篇 (策略{level})")
        papers += arxiv_papers
    except Exception as e:
        print(f"[PaperPilot] arXiv 失败: {e}")

# OpenAlex 同理，source="openalex"
```

如果需要对课题描述做并行检索（当前逻辑保留），可以单独用 `fetch_arxiv([desc_en_query], logic="OR")`。

#### 4.3 用 rank_papers 替换 search_similar

```python
# 旧代码
idx, indexed_papers = build_index(papers)
scores = search_similar(query_for_scoring, idx, indexed_papers, top_k=20)

# 新代码
scores = rank_papers(
    query=query_for_scoring,
    papers=papers,
    top_k=20,
    api_weight=0.7,
    primary_kw=primary_en,
    secondary_kw=secondary_en,
    regular_kw=regular_en,
)
```

#### 4.4 完整的新 _run_pipeline 伪代码

```python
def _run_pipeline():
    papers = []
    max_per = int(max_results_slider.value)

    # 1. 翻译课题描述
    desc_en_query = None
    desc = state.topic_desc.strip()
    desc_en_terms = translate_terms([desc])
    desc_en = [t for t in desc_en_terms if t and not _has_cjk(t)]
    if desc_en:
        desc_en_query = desc_en[0]
    elif not _has_cjk(desc):
        desc_en_query = desc

    # 2. 翻译三层关键词
    primary_en = None
    if state.primary_keyword:
        en = translate_terms([state.primary_keyword])
        if en and en[0] and not _has_cjk(en[0]):
            primary_en = en[0]

    secondary_en = [t for t in translate_terms(state.secondary_keywords) if t and not _has_cjk(t)]
    regular_en = [t for t in translate_terms(state.regular_keywords) if t and not _has_cjk(t)]

    print(f"\n[PaperPilot] 开始检索")
    print(f"[PaperPilot] 主关键词: {primary_en}, 副关键词: {secondary_en}, 普通关键词: {regular_en}")

    # 3. 级联检索
    if arxiv_switch.value:
        state.status_text = "arXiv 抓取中..."
        try:
            arxiv_papers, level = fetch_with_cascade(
                primary_kw=primary_en,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="arxiv",
                max_results=max_per,
                min_results=3,
            )
            print(f"[PaperPilot] arXiv 返回: {len(arxiv_papers)} 篇 (策略{level})")
            papers += arxiv_papers
        except Exception as e:
            print(f"[PaperPilot] arXiv 失败: {e}")

        # 课题描述并行检索（可选，补充召回）
        if desc_en_query:
            try:
                desc_papers = fetch_arxiv([desc_en_query], max_results=max_per, logic="OR")
                print(f"[PaperPilot] arXiv（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except Exception as e:
                print(f"[PaperPilot] arXiv（描述）失败: {e}")

    if openalex_switch.value:
        state.status_text = "OpenAlex 抓取中..."
        try:
            oa_papers, oa_level = fetch_with_cascade(
                primary_kw=primary_en,
                secondary_kw=secondary_en,
                regular_kw=regular_en,
                source="openalex",
                max_results=max_per,
                min_results=3,
            )
            print(f"[PaperPilot] OpenAlex 返回: {len(oa_papers)} 篇 (策略{oa_level})")
            papers += oa_papers
        except Exception as e:
            print(f"[PaperPilot] OpenAlex 失败: {e}")

        # 课题描述并行检索
        if desc_en_query:
            try:
                desc_papers = fetch_openalex([desc_en_query], max_results=max_per, logic="OR")
                print(f"[PaperPilot] OpenAlex（描述）返回: {len(desc_papers)} 篇")
                papers += desc_papers
            except Exception as e:
                print(f"[PaperPilot] OpenAlex（描述）失败: {e}")

    # 4. 去重
    state.status_text = "去重中..."
    papers = deduplicate(papers)
    print(f"[PaperPilot] 去重后: {len(papers)} 篇")

    if not papers:
        print("[PaperPilot] 未找到论文")
        return [], []

    # 5. 排序打分（FAISS -> cross-encoder -> fusion + 关键词加分）
    state.status_text = f"排序中（{len(papers)} 篇）..."
    query_for_scoring = desc_en_query if desc_en_query else state.topic_desc
    scores = rank_papers(
        query=query_for_scoring,
        papers=papers,
        top_k=20,
        api_weight=0.7,
        primary_kw=primary_en,
        secondary_kw=secondary_en,
        regular_kw=regular_en,
    )

    return papers, scores
```

### 5. 手动添加关键词 on_add_keyword（app.py:295-301）

手动添加的关键词进入普通关键词列表：

```python
def on_add_keyword(e):
    kw = (e.control.value or "").strip()
    if kw:
        state.regular_keywords.append(kw)
        state.keywords = merge_keywords(state.keywords, [kw])
        manual_kw_field.value = ""
        manual_kw_field.update()
        refresh_keyword_chips(keywords_row, manual_kw_field)
```

---

## 三、级联策略说明

三级降级逻辑已内置在 `fetch_with_cascade` 中，无需 UI 端额外处理：

| 策略 | 查询结构 | 触发条件 |
|------|----------|----------|
| 0 | `"核心1" AND "核心2" AND ("普通1" OR "普通2" OR ...)` | 首次尝试 |
| 1 | `"主关键词" AND ("副1" OR "副2" OR "普通1" OR ...)` | 策略0结果 < 3篇，且用户选了主关键词 |
| 2 | `"kw1" OR "kw2" OR "kw3" OR ...` | 策略1结果 < 3篇（或无主关键词） |

无主关键词时跳过策略1，直接从策略0 -> 策略2。

---

## 四、关键词加分说明

`rank_papers` 内部在融合阶段自动附加关键词匹配加分：

| 层级 | 权重 | 匹配规则 |
|------|------|----------|
| 主关键词 | 1.0 | title/abstract 完全包含 |
| 副关键词 | 0.7 | 任一命中即得分，不重复计数 |
| 普通关键词 | 0.4 | 任一命中即得分，不重复计数 |

加分缩放系数默认 0.05，即完全命中三层时实际加分为 2.1 * 0.05 = 0.1（在 0-1 的融合分基础上）。

---

## 五、向后兼容

所有新参数带有默认值，旧的扁平 `state.keywords` 字段保持不变。搭档可以渐进式对接：

1. 先对接 `_run_pipeline`（不改 UI 关键字结构），使用 `fetch_with_cascade(primary_kw=None, secondary_kw=all_kw, regular_kw=[])`
2. 再加上主关键词选择 UI
3. 最后接入 `rank_papers` 替换 `search_similar`

---

## 六、注意事项

- **翻译函数 `translate_terms`** 接受 `list[str]`，传入单个主关键词时包装为单元素列表
- **课题描述并行检索**保留现有的 `desc_en_query` 逻辑，单独用 `fetch_arxiv([desc_en_query], logic="OR")` 不与级联混合
- **cross-encoder 模型**部署机器需要预先缓存到 `~/.cache/modelscope/sentence-transformers/cross-encoder/ms-marco-MiniLM-L-6-v2`，否则自动跳过精排步骤（非阻塞）
