"""向量索引与检索接口。

Phase 1 核心流水线：
    论文摘要 → 本地多语言 Embedding → FAISS IndexFlatIP → 相似度检索

使用 paraphrase-multilingual-MiniLM-L12-v2（384维），
本地运行，中英文跨语言匹配，无需网络。
"""

import hashlib
import os
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from paperpilot.config import config

_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_cache_dir = Path(config.get("cache", {}).get("dir", "./cache/api"))
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _cache_path(text: str) -> Path:
    key = hashlib.md5(text.encode()).hexdigest()
    return _cache_dir / f"{key}.npy"


def embed_text(text: str) -> np.ndarray:
    """对单段文本生成 embedding 向量。

    Args:
        text: 输入文本（论文摘要 / 课题描述）

    Returns:
        1-D numpy array (384,)，L2 归一化
    """
    cached = _load_cache(text)
    if cached is not None:
        return cached
    vec = embed_batch([text])[0]
    _save_cache(text, vec)
    return vec


def embed_batch(texts: list[str]) -> np.ndarray:
    """批量生成 embedding，返回 2-D array (n, 384)，L2 归一化。"""
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    # L2 归一化（用于 FAISS IndexFlatIP 内积 = 余弦相似度）
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def _load_cache(text: str) -> np.ndarray | None:
    p = _cache_path(text)
    if p.exists():
        return np.load(p)
    return None


def _save_cache(text: str, vec: np.ndarray) -> None:
    p = _cache_path(text)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.save(p, vec)


def build_index(papers: list[dict]):
    """为论文列表构建 FAISS 索引。

    Args:
        papers: paper dict 列表，每篇需含 abstract

    Returns:
        (faiss.Index, list[dict]): 索引对象 + paper 列表
    """
    abstracts = [p.get("abstract", "") or "" for p in papers]
    vecs = embed_batch(abstracts)
    dim = vecs.shape[1]
    idx = faiss.IndexFlatIP(dim)
    idx.add(vecs)
    return idx, papers


def search_similar(
    query: str,
    index,
    papers: list[dict],
    top_k: int = 20
) -> list[tuple[dict, float]]:
    """检索与查询最相似的 top_k 篇论文。

    Args:
        query: 课题描述文本
        index: FAISS 索引对象
        papers: 索引对应的 paper dict 列表
        top_k: 返回数量

    Returns:
        [(paper_dict, similarity_score), ...]，按分数降序
    """
    qvec = embed_text(query).reshape(1, -1)
    k = min(top_k, len(papers))
    scores, indices = index.search(qvec, k)
    results = []
    for score, i in zip(scores[0], indices[0]):
        if i < 0 or i >= len(papers):
            continue
        results.append((papers[int(i)], float(score)))
    return results


def save_index(index, path: str) -> None:
    """将 FAISS 索引保存到磁盘。"""
    faiss.write_index(index, str(path))


def load_index(path: str):
    """从磁盘加载 FAISS 索引。"""
    return faiss.read_index(str(path))
