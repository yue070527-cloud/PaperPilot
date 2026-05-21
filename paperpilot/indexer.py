"""向量索引与检索接口 —— 搭档实现。

Phase 1 核心流水线：
    论文摘要 → Embedding（DeepSeek / MiniLM）→ FAISS IndexFlatIP → 相似度检索
"""

import numpy as np


def embed_text(text: str, mode: str = "online") -> np.ndarray:
    """对单段文本生成 embedding 向量。

    Args:
        text: 输入文本（论文摘要 / 课题描述）
        mode: "online"（DeepSeek API）| "offline"（本地 MiniLM）| "auto"

    Returns:
        1-D numpy array，维度取决于模型
    """
    raise NotImplementedError("搭档实现")


def embed_batch(texts: list[str], mode: str = "online") -> np.ndarray:
    """批量生成 embedding，返回 2-D array (n, dim)。"""
    raise NotImplementedError("搭档实现")


def build_index(papers: list[dict], mode: str = "online"):
    """为论文列表构建 FAISS 索引。

    Args:
        papers: paper dict 列表，每篇需含 abstract
        mode: embedding 模式

    Returns:
        (faiss.Index, list[dict]): 索引对象 + 附带 embedding_id 的 paper 列表
    """
    raise NotImplementedError("搭档实现")


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
    raise NotImplementedError("搭档实现")


def save_index(index, path: str) -> None:
    """将 FAISS 索引保存到磁盘。"""
    raise NotImplementedError("搭档实现")


def load_index(path: str):
    """从磁盘加载 FAISS 索引。"""
    raise NotImplementedError("搭档实现")
