# 搭档任务清单 —— 今晚要完成的事

## 一、拉取代码（5 分钟）

```bash
cd 你们的项目文件夹
git fetch origin
git checkout develop
git pull origin develop
```

拉完后你会看到这些文件：

```
paperpilot/
├── models.py      ✅ 已写好，直接读
├── fetcher.py     ⚠️ 空壳，你要实现
├── indexer.py     ⚠️ 空壳，你要实现
├── keywords.py    ⚠️ 空壳，你要实现
├── config.py      ✅ 已写好，不需要改
config.yaml        ✅ 模板，配一下 API Key
requirements.txt   ✅ 依赖清单
CLAUDE.md          📋 协作规则，你和你的 CC 都要遵守
```

## 二、安装依赖（5 分钟）

```bash
pip install -r requirements.txt
```

如果下载慢，换国内源：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 三、配置 API Key（2 分钟）

打开 `config.yaml`，你会看到：

```yaml
deepseek:
  api_key: ${DEEPSEEK_API_KEY}
```

这个 `${DEEPSEEK_API_KEY}` 会自动从环境变量读取。你需要：

**Windows：**
```powershell
setx DEEPSEEK_API_KEY "你的DeepSeek密钥"
```
设完后关掉终端重新打开。

验证：
```bash
echo $DEEPSEEK_API_KEY   # Git Bash
```
或者：
```bash
pip install python-dotenv   # 可选，或者直接在代码里改
```

> 如果你的 CC 不知道怎么设环境变量，可以先硬编码到 config.yaml 里改 `api_key: "sk-xxxx"`，**但千万别提交这个改动**。`config.yaml` 已经在 .gitignore 里了……等等，没在。你注意别把 key 推上去就行。

## 四、理解你的任务（先看 5 分钟再动手）

你负责**数据层和算法层**，具体三个模块：

### 4.1 `paperpilot/fetcher.py` —— 论文数据获取

需要实现 3 个函数：

| 函数 | 作用 | 关键依赖 |
|------|------|---------|
| `fetch_arxiv(keywords, max_results=30)` | 搜 arXiv | `arxiv` 包 |
| `fetch_openalex(keywords, max_results=30)` | 搜 OpenAlex | `requests` |
| `import_local_pdfs(folder_path)` | 批量导入本地 PDF | `pymupdf` (fitz) |

**所有函数返回统一的 paper dict 格式：**

```python
{
    "title": "论文标题",
    "authors": "作者1, 作者2",    # 逗号分隔的字符串
    "abstract": "摘要内容...",
    "year": 2025,                 # int 或 None
    "source": "arxiv",            # "arxiv" / "openalex" / "local_pdf"
    "url": "https://...",         # str 或 None
    "doi": "10.xxx/...",          # str 或 None
}
```

**提示：** `fetcher.py` 里已经写好了函数签名和 docstring，你只需要把 `raise NotImplementedError` 替换成实际代码。

### 4.2 `paperpilot/indexer.py` —— 向量索引与检索

需要实现 6 个函数，核心流程：

```
文本 → embed_text() → 得到向量 → build_index() →
    → FAISS IndexFlatIP → search_similar() → 返回排序结果
```

| 函数 | 作用 |
|------|------|
| `embed_text(text, mode)` | 单条文本 → 向量（DeepSeek API / 本地 MiniLM） |
| `embed_batch(texts, mode)` | 批量文本 → 向量矩阵 |
| `build_index(papers, mode)` | 论文列表 → FAISS 索引 |
| `search_similar(query, index, papers, top_k, mode)` | 检索 top_k 篇最相关论文 |
| `save_index(index, path)` | 索引存盘 |
| `load_index(path)` | 从磁盘加载索引 |

**Embedding 策略（Phase 1）：**
- 在线模式（默认）：调用 DeepSeek Embedding API
- 离线兜底：用 `sentence-transformers` 加载 `all-MiniLM-L6-v2`（本地 ~80MB）

**FAISS 选择：**
- Phase 1 数据量小（<1000 篇），用 `IndexFlatIP`（内积搜索，精确但慢不了多少）
- 不用 IndexIVF（那是 Phase 3 大数据时才用的）

### 4.3 `paperpilot/keywords.py` —— 关键词提取

需要实现 1 个函数：

```python
def extract_keywords(topic_description: str, top_n: int = 10) -> list[str]
```

用 KeyBERT 从课题描述中提取技术术语。参考用法：

```python
from keybert import KeyBERT
kw_model = KeyBERT()
keywords = kw_model.extract_keywords(
    topic_description,
    top_n=top_n,
    stop_words="english"
)
# 返回 [(keyword, score), ...]，取 keyword 部分
```

> 提示：`merge_keywords()` 我已经帮你写好了，不用动。

---

## 五、如何跟你的 Claude Code 高效沟通

你的 CC 读到了 `CLAUDE.md`，已经知道项目规则。你可以这样跟它说：

### 开工时：

> "请阅读 paperpilot/models.py 理解数据结构，然后实现 paperpilot/fetcher.py 的 fetch_arxiv 函数。注意返回的 paper dict 格式要和文件头部注释一致。"

### 写完一个函数后：

> "检查一下这个函数是否符合 models.py 的 Paper 字段定义，有没有遗漏的字段"

### 遇到报错时：

> "我跑了 fetch_arxiv 报这个错：[贴错误信息]。帮我看看怎么修"

### 联动提醒：

> "如果你要改函数签名，先告诉我，因为搭档那边的 UI 代码依赖这个接口"

### 关键原则：

- **一次只让它做一个函数**，不要一口吃成胖子
- **让它先读 models.py 再写代码**，确保字段名一致
- **写完一个函数先验证**，别三个文件一起写完了再跑
- 如果它写得太复杂，说"简化一下，Phase 1 不需要那么完善"

---

## 六、建议的实现顺序

按依赖关系排：

```
第 1 步：keywords.py（最独立，KeyBERT 不依赖其他模块）
    ↓
第 2 步：fetcher.py（依赖 keywords 提供检索词，但也可以先写死关键词测试）
    ↓
第 3 步：indexer.py（依赖 fetcher 拿到论文才能建索引）
```

每完成一步，commit 一次：

```bash
git add paperpilot/keywords.py
git commit -m "feat: KeyBERT关键词提取实现"
```

---

## 七、完成后怎么验证

在项目根目录下写一个 `test_pipeline.py`（别提交，自己用）：

```python
# 测试 1：关键词提取
from paperpilot.keywords import extract_keywords
kw = extract_keywords("钙钛矿太阳能电池的稳定性研究")
print("关键词:", kw)

# 测试 2：数据抓取
from paperpilot.fetcher import fetch_arxiv
papers = fetch_arxiv(kw, max_results=10)
print(f"抓到 {len(papers)} 篇")
print("第一篇:", papers[0]["title"])

# 测试 3：索引和检索
from paperpilot.indexer import build_index, search_similar
index, indexed_papers = build_index(papers, mode="online")
results = search_similar(
    "钙钛矿太阳能电池的稳定性研究",
    index, indexed_papers, top_k=5
)
for paper, score in results:
    print(f"{score:.3f} | {paper['title']}")
```

三条都跑通 → 你的部分就完成了。

## 八、提交代码

```bash
git add paperpilot/fetcher.py paperpilot/indexer.py paperpilot/keywords.py
git commit -m "feat: 实现数据层——arXiv/OpenAlex抓取、FAISS索引、KeyBERT关键词"
git push origin develop
```

然后在群里发：`[DONE] 数据层三件套已推到 develop，请 pull`

---

## 九、遇到问题的求助优先级

1. **API 连不上 / 超时** → 检查网络、API Key、配额
2. **FAISS 报段错误** → 确认装的是 `faiss-cpu` 不是 `faiss-gpu`
3. **PyMuPDF 导入失败** → `pip install pymupdf`（包名是 pymupdf，导入用 `import fitz`）
4. **sentence-transformers 下载模型卡住** → 让它先下好 `all-MiniLM-L6-v2`，或者切在线模式
5. **不确定怎么实现某个函数** → 把函数签名和 docstring 贴给 CC，说"帮我实现这个"
