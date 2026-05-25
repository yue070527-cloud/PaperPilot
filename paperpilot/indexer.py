"""向量索引与检索接口。

Phase 1 核心流水线：
    论文摘要 → 本地多语言 Embedding → FAISS IndexFlatIP → 相似度检索
    → Cross-Encoder 精排 → API 分数融合 → 最终排序

使用 paraphrase-multilingual-MiniLM-L12-v2（384维），
本地运行，中英文跨语言匹配，无需网络。
"""

import hashlib
import logging
import os
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from paperpilot.config import config

logger = logging.getLogger(__name__)

_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
# 优先 ModelScope 本地缓存，回退到 HuggingFace 缓存
_MODEL = _MODEL_PATH if Path(_MODEL_PATH).exists() else _MODEL_NAME
_cache_dir = Path(config.get("cache", {}).get("dir", "./cache/api"))
_model: SentenceTransformer | None = None

# Cross-encoder 模型路径（与 bi-encoder 同级缓存目录）
_CE_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/cross-encoder/ms-marco-MiniLM-L-6-v2")
_CE_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_cross_encoder = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL)
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


# ── Cross-Encoder 重排序 ──

def _get_cross_encoder():
    """加载 cross-encoder 模型（懒加载 + 本地缓存优先）。"""
    global _cross_encoder
    if _cross_encoder is not None:
        return _cross_encoder

    from sentence_transformers import CrossEncoder

    # 优先本地缓存
    if Path(_CE_MODEL_PATH).exists():
        logger.info(f"Loading cross-encoder from cache: {_CE_MODEL_PATH}")
        _cross_encoder = CrossEncoder(_CE_MODEL_PATH)
        return _cross_encoder

    # 尝试在线下载（覆盖 OFFLINE 标志）
    logger.info("Cross-encoder not cached, attempting download...")
    try:
        old_hf = os.environ.pop("HF_HUB_OFFLINE", None)
        old_tr = os.environ.pop("TRANSFORMERS_OFFLINE", None)
        _cross_encoder = CrossEncoder(_CE_MODEL_NAME)
    except Exception as e:
        logger.warning(f"Cross-encoder download failed: {e}")
        _cross_encoder = None
    finally:
        if old_hf is not None:
            os.environ["HF_HUB_OFFLINE"] = old_hf
        if old_tr is not None:
            os.environ["TRANSFORMERS_OFFLINE"] = old_tr

    return _cross_encoder


def rerank_with_cross_encoder(
    query: str,
    results: list[tuple[dict, float]],
    top_k: int = 20,
) -> list[tuple[dict, float]]:
    """用 cross-encoder 对 FAISS 粗筛结果精排。

    bi-encoder 召回 → cross-encoder 精排，IR 经典两阶段范式。

    Args:
        query: 课题描述（英文，同语言匹配区分度更高）
        results: FAISS 返回的 [(paper, faiss_score), ...] 列表
        top_k: 最终返回数量

    Returns:
        [(paper, cross_encoder_score), ...]，按分数降序
    """
    ce = _get_cross_encoder()
    if ce is None or not results:
        # Cross-encoder unavailable — normalize FAISS scores to [0, 1]
        scores_arr = np.array([s for _, s in results])
        min_s, max_s = scores_arr.min(), scores_arr.max()
        if max_s > min_s:
            normalized = [(p, float((s - min_s) / (max_s - min_s))) for p, s in results]
        else:
            normalized = [(p, 0.5) for p, _ in results]
        normalized.sort(key=lambda x: -x[1])
        return normalized[:top_k]

    # 标题 + 摘要拼接，CE 同时匹配标题和正文关键词
    def _paper_text(p: dict) -> str:
        title = (p.get("title") or "").strip()
        abstract = (p.get("abstract") or "").strip()
        return f"{title}. {abstract}" if title else abstract

    pairs = [(query, _paper_text(p)) for p, _ in results]
    try:
        scores = ce.predict(pairs, show_progress_bar=False)
        # Normalize to [0, 1]
        min_s, max_s = scores.min(), scores.max()
        if max_s > min_s:
            scores = (scores - min_s) / (max_s - min_s)
        else:
            scores = np.zeros_like(scores)
    except Exception as e:
        logger.warning(f"Cross-encoder prediction failed: {e}")
        return results[:top_k]

    reranked = sorted(
        zip([p for p, _ in results], scores),
        key=lambda x: -x[1],
    )
    return reranked[:top_k]


# ── 关键词匹配加分 ──

def keyword_match_bonus(
    paper: dict,
    primary_kw: list[str],
    secondary_kw: list[str],
    regular_kw: list[str],
    w_primary: float = 1.0,
    w_secondary: float = 0.7,
    w_regular: float = 0.4,
) -> float:
    """计算论文的关键词命中加分（逐层二分：命中一层即得分，不重复计数）。

    在 title + abstract 中搜索关键词，每层最多计一次。
    主关键词层：任一主关键词命中即得分。
    返回原始加分值，范围 0 到 w_primary + w_secondary + w_regular (max 2.1)。
    """
    title = (paper.get("title") or "").lower()
    abstract = (paper.get("abstract") or "").lower()
    text = f"{title} {abstract}"

    bonus = 0.0
    if primary_kw:
        for pk in primary_kw:
            if pk.lower() in text:
                bonus += w_primary
                break
    for kw in secondary_kw:
        if kw.lower() in text:
            bonus += w_secondary
            break
    for kw in regular_kw:
        if kw.lower() in text:
            bonus += w_regular
            break
    return bonus


# ── 分数融合 ──

def fuse_scores(
    results: list[tuple[dict, float]],
    api_weight: float = 0.7,
    primary_kw: list[str] | None = None,
    secondary_kw: list[str] | None = None,
    regular_kw: list[str] | None = None,
    kw_bonus_scale: float = 0.05,
) -> list[tuple[dict, float]]:
    """融合 API 排序分（主）和语义分（辅）+ 关键词匹配加分。

    每篇论文的 api_score 来自 fetcher 层（arXiv/OpenAlex 排序位置归一化），
    semantic_score 来自 cross-encoder 或 FAISS bi-encoder。

    Args:
        results: [(paper, semantic_score), ...] 列表
        api_weight: API 分数权重，默认 0.7
        primary_kw: 主关键词列表（可选，用于匹配加分）
        secondary_kw: 副关键词列表
        regular_kw: 普通关键词列表
        kw_bonus_scale: 关键词加分缩放系数，默认 0.05

    Returns:
        [(paper, fused_score), ...]，按融合分数降序
    """
    secondary_kw = secondary_kw or []
    regular_kw = regular_kw or []

    fused = []
    has_api = any(p.get("api_score") is not None for p, _ in results)
    for paper, sem_score in results:
        api_score = paper.get("api_score")
        if api_score is None:
            api_score = 0.5  # neutral default for sources w/o API score (local PDFs)
        # Safety clamp: ensure both scores are in [0, 1]
        api_score = max(0.0, min(1.0, float(api_score)))
        sem_score = max(0.0, min(1.0, float(sem_score)))
        weight = api_weight if has_api else 0.0
        final = weight * api_score + (1 - weight) * sem_score
        # Add keyword match bonus (small tiebreaker)
        kw_bonus = keyword_match_bonus(paper, primary_kw, secondary_kw, regular_kw)
        final += kw_bonus * kw_bonus_scale
        fused.append((paper, final))
    fused.sort(key=lambda x: -x[1])
    return fused


def rank_papers(
    query: str,
    papers: list[dict],
    top_k: int = 20,
    primary_kw: list[str] | None = None,
    secondary_kw: list[str] | None = None,
    regular_kw: list[str] | None = None,
    kw_bonus_scale: float = 0.05,
) -> list[tuple[dict, float]]:
    """完整的论文排序流水线：API 分粗筛 → cross-encoder 精排 → 关键词加分。

    FAISS 已移除。API 排序分（arXiv/OpenAlex 原始相关性）承担粗筛，
    cross-encoder 承担精排，最终得分 = CE 分 + 关键词匹配加分。

    Args:
        query: 课题描述文本
        papers: 去重后的论文列表
        top_k: 最终返回数量
        primary_kw: 主关键词列表（可选，用于关键词匹配加分）
        secondary_kw: 副关键词列表
        regular_kw: 普通关键词列表
        kw_bonus_scale: 关键词加分缩放系数

    Returns:
        [(paper, final_score), ...]，按分数降序
    """
    if not papers:
        return []

    # Stage 1: API 分粗筛 —— 按 api_score 降序取 top-K 候选
    papers_with_api = [p for p in papers if p.get("api_score") is not None]
    papers_without_api = [p for p in papers if p.get("api_score") is None]
    papers_with_api.sort(key=lambda p: p.get("api_score", 0), reverse=True)
    candidates_k = min(top_k * 5, len(papers))
    candidates = [
        (p, p.get("api_score", 0.5))
        for p in papers_with_api[:candidates_k] + papers_without_api[:candidates_k]
    ]

    # Stage 2: Cross-encoder 精排
    reranked = rerank_with_cross_encoder(query, candidates, top_k=top_k)

    # Stage 3: 关键词匹配加分（不做 API/语义加权融合）
    secondary_kw = secondary_kw or []
    regular_kw = regular_kw or []
    final = []
    for paper, ce_score in reranked:
        score = float(ce_score)
        kw_bonus = keyword_match_bonus(paper, primary_kw, secondary_kw, regular_kw)
        score += kw_bonus * kw_bonus_scale
        final.append((paper, score))
    final.sort(key=lambda x: -x[1])
    return final[:top_k]
