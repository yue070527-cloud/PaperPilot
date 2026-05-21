"""向量索引与检索接口。

Phase 1 核心流水线：
    论文摘要 → Embedding（本地多语言模型 / Google Gemini）→ FAISS IndexFlatIP → 相似度检索

Embedding 策略：
    - offline（默认）: paraphrase-multilingual-MiniLM-L12-v2，本地运行，中英文跨语言匹配
    - online: Google Gemini text-embedding-004，免费 1500 req/min，需 GEMINI_API_KEY
"""

import hashlib
import os
from pathlib import Path

import faiss
import numpy as np
import requests
from sentence_transformers import SentenceTransformer

from paperpilot.config import config

# 默认本地模型：多语言版 MiniLM，支持中英文跨语言检索
_LOCAL_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_GEMINI_MODEL = "text-embedding-004"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/"

_cache_dir = Path(config.get("cache", {}).get("dir", "./cache/api"))
_embedding_model = None


def _get_offline_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer(_LOCAL_MODEL_NAME)
    return _embedding_model


def _resolve_mode(mode: str) -> str:
    if mode == "auto":
        auto_cfg = config.get("embedding", {}).get("mode", "offline")
        return auto_cfg if auto_cfg != "auto" else "offline"
    return mode


def _cache_path(text: str, mode: str) -> Path:
    key = hashlib.md5(f"{mode}:{text}".encode()).hexdigest()
    return _cache_dir / f"{key}.npy"


def _load_cache(text: str, mode: str) -> np.ndarray | None:
    p = _cache_path(text, mode)
    if p.exists():
        return np.load(p)
    return None


def _save_cache(text: str, mode: str, vec: np.ndarray) -> None:
    p = _cache_path(text, mode)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.save(p, vec)


def _embed_online_batch(texts: list[str]) -> np.ndarray:
    """Google Gemini Embedding API（免费 1500 req/min）。"""
    api_key = os.environ.get("GEMINI_API_KEY") or config.get("gemini", {}).get("api_key", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 未设置，请设置环境变量或在 config.yaml 中配置")

    url = f"{_GEMINI_URL}{_GEMINI_MODEL}:batchEmbedText"
    payload = {"requests": [{"model": f"models/{_GEMINI_MODEL}", "text": t} for t in texts]}
    resp = requests.post(
        url,
        json=payload,
        params={"key": api_key},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    vecs = [
        e["values"]
        for e in sorted(data.get("embeddings", []), key=lambda x: x.get("index", 0))
    ]
    return np.array(vecs, dtype=np.float32)


def _embed_offline_batch(texts: list[str]) -> np.ndarray:
    model = _get_offline_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def embed_text(text: str, mode: str = "online") -> np.ndarray:
    """对单段文本生成 embedding 向量。

    Args:
        text: 输入文本（论文摘要 / 课题描述）
        mode: "online"（DeepSeek API）| "offline"（本地 MiniLM）| "auto"

    Returns:
        1-D numpy array，维度取决于模型
    """
    mode = _resolve_mode(mode)
    cached = _load_cache(text, mode)
    if cached is not None:
        return cached
    vec = embed_batch([text], mode)[0]
    _save_cache(text, mode, vec)
    return vec


def embed_batch(texts: list[str], mode: str = "online") -> np.ndarray:
    """批量生成 embedding，返回 2-D array (n, dim)。"""
    mode = _resolve_mode(mode)
    if mode == "offline":
        vecs = _embed_offline_batch(texts)
    else:
        vecs = _embed_online_batch(texts)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def build_index(papers: list[dict], mode: str = "online"):
    """为论文列表构建 FAISS 索引。

    Args:
        papers: paper dict 列表，每篇需含 abstract
        mode: embedding 模式

    Returns:
        (faiss.Index, list[dict]): 索引对象 + 附带 embedding_id 的 paper 列表
    """
    abstracts = [p.get("abstract", "") or "" for p in papers]
    vecs = embed_batch(abstracts, mode)
    dim = vecs.shape[1]
    idx = faiss.IndexFlatIP(dim)
    idx.add(vecs)
    return idx, papers


def search_similar(
    query: str,
    index,
    papers: list[dict],
    top_k: int = 20,
    mode: str = "online"
) -> list[tuple[dict, float]]:
    """检索与查询最相似的 top_k 篇论文。

    Args:
        query: 课题描述文本
        index: FAISS 索引对象（由 build_index 返回）
        papers: 索引对应的 paper dict 列表
        top_k: 返回数量
        mode: embedding 模式

    Returns:
        [(paper_dict, similarity_score), ...]，按分数降序
    """
    qvec = embed_text(query, mode).reshape(1, -1)
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
