"""关键词提取接口。

中文：jieba TF-IDF 生成候选术语 → 本地多语言模型语义打分 → 阈值过滤泛化词
英文：KeyBERT 内置 CountVectorizer n-gram（原生支持空格分词）
"""

import os
import re
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import jieba
import jieba.analyse
import numpy as np
from keybert import KeyBERT
from sentence_transformers import SentenceTransformer

_EMBED_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
_EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_EMBED_MODEL_SRC = _EMBED_MODEL_PATH if Path(_EMBED_MODEL_PATH).exists() else _EMBED_MODEL_NAME

_KEYBERT_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/all-MiniLM-L6-v2")
_KEYBERT_MODEL_NAME = "all-MiniLM-L6-v2"
_KEYBERT_MODEL_SRC = _KEYBERT_MODEL_PATH if Path(_KEYBERT_MODEL_PATH).exists() else _KEYBERT_MODEL_NAME

_CHINESE_STOP = {
    "的", "了", "在", "是", "和", "与", "及", "或", "对", "从", "被", "把", "将",
    "于", "等", "其", "该", "所", "为", "而", "且", "以", "并", "到", "要", "有",
    "更", "不", "也", "都", "就", "能", "会", "可", "已", "还", "只", "但",
    "用于", "基于", "采用", "通过", "一种", "一个", "这个", "那个", "什么",
    "怎么", "如何", "哪些", "哪", "吗", "呢", "吧", "啊", "呀",
    "提出", "证明", "表明", "发现", "使用", "利用", "可以", "能够",
    "研究", "方法", "分析", "实验", "结果", "影响", "作用", "过程",
    "问题", "应用", "发展", "技术", "系统", "模型", "数据", "性能",
    "特性", "结构", "设计", "制备", "计算", "理论", "实践",
    "特征", "策略", "机制", "综述", "进展", "挑战", "现状", "趋势",
    "关键", "主要", "重要", "不同", "相关", "相应", "显著",
}
_MIN_LEN = 2
_CANDIDATE_COUNT = 30
_SCORE_THRESHOLD_RATIO = 0.4

_embed_model: SentenceTransformer | None = None
_kw_model: KeyBERT | None = None


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


def _get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        _embed_model = SentenceTransformer(_EMBED_MODEL_SRC)
    return _embed_model


def _get_kw_model() -> KeyBERT:
    global _kw_model
    if _kw_model is None:
        _kw_model = KeyBERT(model=_KEYBERT_MODEL_SRC)
    return _kw_model


def _chinese_extract(text: str, top_n: int) -> list[str]:
    """中文关键词提取：jieba TF-IDF 候选 → 多语言模型语义打分。"""
    raw = jieba.analyse.extract_tags(text, topK=_CANDIDATE_COUNT)
    candidates = [c.strip() for c in raw if len(c.strip()) >= _MIN_LEN and c.strip() not in _CHINESE_STOP]
    if not candidates:
        return []
    model = _get_embed_model()
    doc_vec = model.encode([text], normalize_embeddings=True)
    cand_vecs = model.encode(candidates, normalize_embeddings=True)
    scores = (cand_vecs @ doc_vec.T).flatten()
    scored = sorted(zip(candidates, scores), key=lambda x: -x[1])
    threshold = scored[0][1] * _SCORE_THRESHOLD_RATIO
    return [kw for kw, s in scored if s >= threshold][:top_n]


def _english_extract(text: str, top_n: int) -> list[str]:
    """英文关键词提取：KeyBERT 内置 n-gram。"""
    model = _get_kw_model()
    results = model.extract_keywords(
        text,
        top_n=top_n,
        stop_words="english",
        keyphrase_ngram_range=(1, 2),
        use_mmr=True,
        diversity=0.5,
    )
    return [kw.strip() for kw, _ in results]


def extract_keywords(topic_description: str, top_n: int = 10) -> list[str]:
    """从课题描述中自动提取技术术语。中英文自适应。

    Args:
        topic_description: 课题描述文本（1-3 句话）
        top_n: 返回关键词数量

    Returns:
        关键词字符串列表
    """
    if _has_chinese(topic_description):
        return _chinese_extract(topic_description, top_n)
    return _english_extract(topic_description, top_n)


def merge_keywords(auto_keywords: list[str], manual_keywords: list[str]) -> list[str]:
    """合并自动提取和手动输入的关键词，去重、去空、保持手动优先。"""
    seen = set()
    merged = []
    for kw in manual_keywords + auto_keywords:
        kw = kw.strip().lower()
        if kw and kw not in seen:
            seen.add(kw)
            merged.append(kw)
    return merged
