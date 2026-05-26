# term_mapper.py — 中英文关键词桥接模块

## 问题

用户输入纯中文课题 → jieba 提取中文关键词 → arXiv/OpenAlex（英文数据库）搜索 → 0 篇。

根因：搜索端用的是中文关键词撞英文数据库，而我们已经有的多语言 Embedding 模型（`paraphrase-multilingual-MiniLM-L12-v2`）只用在排序端，没用在搜索入口。

## 解决思路

用已有的多语言模型做关键词桥接：

```
中文关键词 → 384 维向量 → 在预建英文术语向量库中找最近邻 → 英文术语 → 搜 arXiv/OpenAlex
```

不依赖翻译 API，纯本地运行。

---

## 需要做什么

### 1. 生成英文术语向量库文件（一次性离线脚本）

**输入**：从 arXiv 高频论文标题中提取的英文名词短语（2-gram / 3-gram）

**生成步骤**：

1. 通过 arXiv API 批量抓取近期论文元数据（建议 ≥ 5000 篇，覆盖主流学科）
   - 搜索词用宽泛词：`physics OR chemistry OR materials OR biology OR computer science OR engineering`
   - 只取 `title` 字段，每批 100 篇，共抓 5000-10000 篇

2. 从标题中提取名词短语（n-gram），过滤掉停用词和低质量词
   ```python
   from sklearn.feature_extraction.text import CountVectorizer
   
   # 所有标题拼成一个 corpus
   titles = [paper.title for paper in all_papers]
   
   # 提取 1-3 gram，按频率取前 5000
   vectorizer = CountVectorizer(
       ngram_range=(1, 3),
       stop_words="english",
       max_features=5000,
   )
   vectorizer.fit_transform(titles)
   terms = vectorizer.get_feature_names_out()
   ```

3. 用 `paraphrase-multilingual-MiniLM-L12-v2` 对所有术语生成 embedding
   ```python
   from sentence_transformers import SentenceTransformer
   model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
   vecs = model.encode(terms.tolist(), normalize_embeddings=True)
   ```

4. 保存为两个文件（放在 `paperpilot/` 目录下）
   - `terms_en.npy` — 术语列表 (`numpy.ndarray`, dtype=str, shape=(N,))
   - `terms_en_vecs.npy` — 向量矩阵 (`numpy.ndarray`, dtype=float32, shape=(N, 384))

   存储大小预估：5000 × 384 × 4B ≈ **8 MB**

---

### 2. 实现 `paperpilot/term_mapper.py`

**API 合约（函数签名，不可改——B 方 UI 层依赖这些接口）**：

```python
"""中文关键词 → 英文术语桥接模块。

使用本地多语言 Embedding 模型做跨语言语义映射，
不依赖翻译 API，纯离线运行。
"""

import numpy as np
from sentence_transformers import SentenceTransformer

_EMBED_MODEL = None
_TERMS: np.ndarray | None = None      # shape (N,)  dtype=str
_TERM_VECS: np.ndarray | None = None  # shape (N, 384) dtype=float32
_FAISS_INDEX = None                    # FAISS IndexFlatIP


def _load_resources():
    """延迟加载模型和术语向量库（首次调用时自动触发）。"""
    ...


def map_chinese_to_english(
    chinese_keywords: list[str],
    top_k: int = 3,
) -> list[str]:
    """将中文关键词列表映射为英文术语列表。

    Args:
        chinese_keywords: 中文关键词列表，如 ["钙钛矿", "太阳能", "电池"]
        top_k: 每个中文词映射几个英文术语

    Returns:
        英文术语列表（已去重），如 ["perovskite", "solar cell", "battery", ...]
    """
    ...


def map_chinese_to_english(
    chinese_keywords: list[str],
    top_k: int = 3,
) -> list[str]:
    """将中文关键词列表映射为英文术语列表。

    Args:
        chinese_keywords: 中文关键词列表，如 ["钙钛矿", "太阳能", "电池"]
        top_k: 每个中文词映射几个英文术语

    Returns:
        英文术语列表（已去重），如 ["perovskite", "solar cell", "battery", ...]

    实现逻辑：
        1. 对每个中文关键词用多语言模型生成 384 维向量
        2. 在 terms_en_vecs 中用 FAISS IndexFlatIP 找最近邻 top_k
        3. 从 terms_en 中取出对应英文术语
        4. 去重后返回
    """
    ...
```

**关键细节**：

- 模型加载走单例模式，和 `keywords.py` / `indexer.py` 风格一致
- 模型源：`paraphrase-multilingual-MiniLM-L12-v2`（已在 ModelScope 缓存，`indexer.py` 里用了同一个模型）
- 术语向量库路径：`Path(__file__).parent / "terms_en.npy"` 和 `Path(__file__).parent / "terms_en_vecs.npy"`
- 如果向量库文件不存在，`map_chinese_to_english` 应返回原列表（优雅降级，不 crash）

---

### 3. B 方（UI 层）如何调用

在 `app.py` 的 `_run_pipeline()` 中：

```python
from paperpilot.term_mapper import map_chinese_to_english

def _run_pipeline():
    papers = []
    max_per = int(max_results_slider.value)

    # 新增：中文关键词自动映射为英文术语
    search_terms = state.keywords
    if _has_chinese(" ".join(state.keywords)):
        try:
            en_terms = map_chinese_to_english(state.keywords, top_k=3)
            search_terms = list(dict.fromkeys(state.keywords + en_terms))
            # 去重但保持顺序：中文词先、英文词后
        except Exception:
            pass  # 映射失败则用原关键词，不阻塞搜索

    # 用 search_terms 去搜 arXiv/OpenAlex
    ...
```

B 方只调 `map_chinese_to_english()` 这一个函数，其他都是 A 方内部实现。

---

### 4. 文件清单

| 文件 | 大小 | 说明 |
|------|------|------|
| `paperpilot/term_mapper.py` | ~60 行 | 映射接口 |
| `paperpilot/terms_en.npy` | ~60 KB | 英文术语列表 |
| `paperpilot/terms_en_vecs.npy` | ~8 MB | 术语向量矩阵 |
| `scripts/build_term_lib.py` | ~40 行 | 一次性生成脚本（可选提交） |

---

### 5. 验收标准

跑以下脚本不报错：

```python
from paperpilot.term_mapper import map_chinese_to_english

result = map_chinese_to_english(["钙钛矿", "太阳能", "电池"], top_k=3)
print(result)
# 期望输出类似：["perovskite", "solar cell", "photovoltaic", "battery", "electrode", ...]
assert len(result) > 0, "应该返回至少 1 个英文术语"
assert all(isinstance(t, str) and all(ord(c) < 128 for c in t) for t in result), "应该全是英文/ASCII"
print("PASS")
```

---

### 6. Git 注意事项

- 术语向量库文件（约 8MB）较大，建议用 Git LFS 或确认 `.gitignore` 不排斥 `.npy` 文件
- `term_mapper.py` 走正常 commit 流程
- 分支：从 `develop` 切 `feature/term-mapper`，完成后提 PR
- 函数签名变更前先在群内通知 B 方
