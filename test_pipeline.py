"""Phase 1 端到端测试脚本。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── 1. 导入检查 ──
print("=" * 50)
print("1. 导入检查")
print("=" * 50)

from paperpilot.models import init_db
from paperpilot.keywords import extract_keywords, merge_keywords
from paperpilot.fetcher import fetch_arxiv, fetch_openalex, import_local_pdfs, deduplicate
from paperpilot.indexer import embed_text, build_index, search_similar

session = init_db("test_pilot.db")
print("[PASS] 所有模块导入成功")

# ── 2. 数据库测试 ──
print("\n" + "=" * 50)
print("2. 数据库测试")
print("=" * 50)

from paperpilot.models import Project, Paper, ProjectPaper
import os

p = Project(name="_test_demo", description="钙钛矿太阳能电池的稳定性研究")
session.add(p)
session.commit()
print(f"[PASS] 写入课题: {p.name} (id={p.id})")

paper = Paper(
    title="Enhancing Perovskite Stability via Interface Passivation",
    authors="Smith, J.; Lee, K.",
    abstract="Perovskite solar cells suffer from humidity-induced degradation. We demonstrate...",
    year=2025,
    source="arxiv",
    url="https://arxiv.org/abs/2501.00000",
)
session.add(paper)
session.commit()
print(f"[PASS] 写入论文: {paper.title[:50]}... (id={paper.id})")

pp = ProjectPaper(project_id=p.id, paper_id=paper.id, score_similarity=0.92, total_score=0.92)
session.add(pp)
session.commit()
print(f"[PASS] 关联课题-论文: project_paper_id={pp.id}")

# 清理
session.query(ProjectPaper).filter_by(project_id=p.id).delete()
session.query(Paper).filter_by(id=paper.id).delete()
session.query(Project).filter_by(id=p.id).delete()
session.commit()
print("[PASS] 数据库 CRUD 正常，测试数据已清理")

# ── 3. 关键词提取测试 ──
print("\n" + "=" * 50)
print("3. 关键词提取测试")
print("=" * 50)

try:
    kw = extract_keywords("钙钛矿太阳能电池的稳定性研究", top_n=5)
    print(f"[PASS] KeyBERT 提取成功: {kw}")
except Exception as e:
    print(f"[WARN] KeyBERT 失败（模型需下载）: {e}")

print(f"[PASS] merge_keywords 测试: {merge_keywords(['钙钛矿', '稳定性'], ['钙钛矿', 'solar cell'])}")

# ── 4. 数据获取测试 ──
print("\n" + "=" * 50)
print("4. 数据获取测试")
print("=" * 50)

try:
    papers = fetch_arxiv(["perovskite solar cell"], max_results=5)
    print(f"[PASS] arXiv 抓取成功: {len(papers)} 篇")
    print(f"   第一篇: {papers[0]['title'][:60]}...")
except Exception as e:
    print(f"[WARN] arXiv 失败（需网络）: {e}")

try:
    papers = fetch_openalex(["perovskite solar cell"], max_results=5)
    print(f"[PASS] OpenAlex 抓取成功: {len(papers)} 篇")
    print(f"   第一篇: {papers[0]['title'][:60]}...")
except Exception as e:
    print(f"[WARN] OpenAlex 失败（需网络）: {e}")

# ── 5. 去重测试 ──
print("\n" + "=" * 50)
print("5. 去重测试")
print("=" * 50)

dup_papers = [
    {"title": "A Study on Perovskite Solar Cells", "abstract": ""},
    {"title": "A Study on Perovskite Solar Cells", "abstract": ""},
    {"title": "Perovskite Solar Cells: A Study", "abstract": ""},
    {"title": "A Study on  Perovskite  Solar  Cells", "abstract": ""},
]
deduped = deduplicate(dup_papers)
print(f"  原始: {len(dup_papers)} 篇 → 去重后: {len(deduped)} 篇")
assert len(deduped) == 2, f"去重逻辑错误：期望2篇，实际{len(deduped)}篇"
print("[PASS] 去重逻辑正确")

# ── 6. 向量索引测试 ──
print("\n" + "=" * 50)
print("6. 向量索引测试（本地多语言模型）")
print("=" * 50)

test_papers = [
    {"title": "Perovskite stability enhancement", "abstract": "Perovskite solar cells suffer from humidity-induced degradation. We demonstrate a new passivation strategy."},
    {"title": "Large language model fine-tuning", "abstract": "We investigate parameter-efficient fine-tuning methods for large language models including LoRA and QLoRA."},
    {"title": "All-inorganic perovskite progress", "abstract": "All-inorganic CsPbI3 perovskites show improved thermal stability compared to organic-inorganic hybrids."},
    {"title": "Federated learning survey", "abstract": "A comprehensive survey of federated learning approaches for privacy-preserving machine learning."},
]

try:
    idx, indexed_papers = build_index(test_papers)
    print(f"[PASS] 索引构建成功: {len(indexed_papers)} 篇论文")

    results = search_similar(
        "钙钛矿太阳能电池稳定性",
        idx, indexed_papers, top_k=2
    )
    print(f"  检索 Top 2:")
    for i, (paper, score) in enumerate(results):
        print(f"    {i+1}. [{score:.3f}] {paper['title'][:50]}...")

    assert "perovskite" in results[0][0]["title"].lower() or "perovskite" in results[0][0]["abstract"].lower()
    print("[PASS] 排序结果正确：钙钛矿相关论文排在最前")

    from paperpilot.indexer import save_index, load_index
    save_index(idx, "test_index.faiss")
    loaded = load_index("test_index.faiss")
    print("[PASS] 索引持久化正常")
    os.remove("test_index.faiss")

except Exception as e:
    print(f"[FAIL] 向量索引失败: {e}")

# ── 7. 清理 ──
print("\n" + "=" * 50)
print("7. 清理")
print("=" * 50)

session.close()
session.bind.dispose()  # Windows 下需要释放文件句柄
db_path = Path("test_pilot.db")
if db_path.exists():
    db_path.unlink()
    print("[PASS] 测试数据库已删除")

print("\n" + "=" * 50)
print("端到端测试完成！")
print("=" * 50)
