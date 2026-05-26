# A 方（数据层）改动总结

## 概述

完成 Phase 1 数据层三个模块的实现，并配合 B 方做了两轮修正：砍掉在线 Embedding、消除 HuggingFace 离线警告。

---

## 1. keywords.py — 关键词提取

**最终状态**：

- KeyBERT 模型源改为 ModelScope 本地缓存：`~/.cache/modelscope/sentence-transformers/all-MiniLM-L6-v2`
- 导入前强制设置 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，消除 HuggingFace 连接警告
- `extract_keywords()` — 单例缓存 KeyBERT 实例，提取后返回纯关键词列表
- `merge_keywords()` — B 方已写好，未改动

**函数签名**（无变化）：
```python
extract_keywords(topic_description: str, top_n: int = 10) -> list[str]
merge_keywords(auto_keywords: list[str], manual_keywords: list[str]) -> list[str]
```

---

## 2. fetcher.py — 论文数据获取

**全部重新实现**，4 个函数：

| 函数 | 实现要点 |
|------|---------|
| `fetch_arxiv(keywords, max_results)` | `arxiv` 包，多词 AND 逻辑，按相关性排序 |
| `fetch_openalex(keywords, max_results)` | `requests` 调 OpenAlex API，免 Key，支持分页 |
| `import_local_pdfs(folder_path)` | `pymupdf` (fitz) 提取文本，自动猜测标题/摘要 |
| `deduplicate(papers)` | 标题归一化 + SequenceMatcher 模糊去重（阈值 0.9） |

**辅助函数**（8 个内部函数）：`_build_arxiv_query`, `_parse_arxiv_result`, `_parse_openalex_work`, `_decode_inverted_index`, `_extract_text_from_page`, `_guess_title`, `_guess_abstract`, `_normalize_title`

**所有函数返回统一 paper dict**：
```python
{
    "title": str, "authors": str, "abstract": str,
    "year": int | None, "source": str, "url": str | None, "doi": str | None
}
```

---

## 3. indexer.py — 向量索引与检索

**初版**实现了 DeepSeek/MiniLM 双源 Embedding，**B 方已重写为纯本地方案**。我的最终修正：

- 模型路径改为 ModelScope 本地缓存：`~/.cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- 添加离线环境变量强制设置
- 归一化后使用 FAISS IndexFlatIP（内积=余弦相似度）

**当前函数签名**（B 方定稿，无 mode 参数）：
```python
embed_text(text: str) -> np.ndarray                          # 384 维，L2 归一化
embed_batch(texts: list[str]) -> np.ndarray                   # (n, 384)
build_index(papers: list[dict]) -> (faiss.Index, list[dict])
search_similar(query, index, papers, top_k) -> list[tuple[dict, float]]
save_index(index, path: str) -> None
load_index(path: str) -> faiss.Index
```

---

## 4. 模型下载

两个 SentenceTransformer 模型均从 ModelScope（modelscope.cn）下载到本地，避免 HuggingFace 网络问题：

| 模型 | 大小 | 路径 |
|------|------|------|
| `paraphrase-multilingual-MiniLM-L12-v2` | ~449MB | `~/.cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| `all-MiniLM-L6-v2` | ~87MB | `~/.cache/modelscope/sentence-transformers/all-MiniLM-L6-v2` |

---

## 5. Git 提交记录

```
59b3d29 fix: KeyBERT使用本地ModelScope模型，消除all-MiniLM-L6-v2下载失败
6ba3da6 fix: 消除HuggingFace离线警告——强制OFFLINE模式并切换本地模型路径
c5d4a98 refactor: 砍掉所有在线Embedding依赖，纯本地多语言模型（B方）
b0d5152 refactor: 替换Embedding方案——本地多语言MiniLM+Gemini API备选（B方）
3647a07 fix: 修复build_index中FAISS IndexIDMap空索引错误；添加索引缓存（B方）
7b6b1c0 feat: 实现数据层——KeyBERT关键词、arXiv/OpenAlex抓取、FAISS索引（A方初版）
```

---

## 6. 环境依赖

通过 `pip install modelscope` 安装 ModelScope SDK 后，运行以下命令下载模型（已完成）：

```python
from modelscope import snapshot_download
snapshot_download('sentence-transformers/all-MiniLM-L6-v2', cache_dir='~/.cache/modelscope')
snapshot_download('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='~/.cache/modelscope')
```

---

## 7. 验证结果

`test_pipeline.py` 全部 7 项 PASS：
- 数据库 CRUD ✓
- 关键词提取（中英文） ✓
- arXiv / OpenAlex 抓取 ✓
- 去重（模糊匹配） ✓
- 向量索引 + 跨语言检索（中文查询 → 英文摘要） ✓
- 索引持久化 ✓
- 清理 ✓
