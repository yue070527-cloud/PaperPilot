"""关键词提取接口。

Phase 1 用 KeyBERT + jieba 中文分词，后续可扩展其他方案。
"""

import os
import re
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import jieba
from keybert import KeyBERT

_MODEL_PATH = str(Path.home() / ".cache/modelscope/sentence-transformers/all-MiniLM-L6-v2")
_MODEL_NAME = "all-MiniLM-L6-v2"
_MODEL = _MODEL_PATH if Path(_MODEL_PATH).exists() else _MODEL_NAME

_CHINESE_STOP = {
    "的", "了", "在", "是", "和", "与", "及", "或", "对", "从", "被", "把", "将",
    "于", "等", "其", "该", "所", "为", "而", "且", "以", "并", "到", "要", "有",
    "更", "不", "也", "都", "就", "能", "会", "可", "已", "还", "只", "但",
    "用于", "基于", "采用", "通过", "一种", "一个", "这个", "那个", "什么",
    "怎么", "如何", "哪些", "哪", "吗", "呢", "吧", "啊", "呀",
    "提出", "证明", "表明", "发现", "使用", "利用", "可以", "能够",
    "研究", "方法", "分析", "实验", "结果", "影响", "作用", "过程",
    "问题", "应用", "发展", "技术", "系统", "模型", "数据", "性能",
    "特性", "结构", "设计", "制备", "计算", "理论", "实践", "特性",
}
_MIN_KEYWORD_LEN = 2


def _segment(text: str) -> str:
    """jieba 分词后用空格连接，供 KeyBERT 的 CountVectorizer 正确切词。"""
    words = jieba.cut(text)
    filtered = [w.strip() for w in words if w.strip() and len(w.strip()) >= _MIN_KEYWORD_LEN]
    return " ".join(filtered)


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


_kw_model = None


def _get_model() -> KeyBERT:
    global _kw_model
    if _kw_model is None:
        _kw_model = KeyBERT(model=_MODEL)
    return _kw_model


def extract_keywords(topic_description: str, top_n: int = 10) -> list[str]:
    """从课题描述中自动提取技术术语。中英文自适应。

    Args:
        topic_description: 课题描述文本（1-3 句话）
        top_n: 返回关键词数量

    Returns:
        关键词字符串列表，如 ["perovskite", "stability", "solar cell"]
    """
    model = _get_model()
    if _has_chinese(topic_description):
        text = _segment(topic_description)
        stop_words = None
    else:
        text = topic_description
        stop_words = "english"
    results = model.extract_keywords(
        text,
        top_n=max(top_n, 15),
        stop_words=stop_words,
        keyphrase_ngram_range=(1, 2),
        use_mmr=True,
        diversity=0.5,
    )
    keywords = []
    for kw, _ in results:
        kw = kw.strip()
        if len(kw) < _MIN_KEYWORD_LEN:
            continue
        if kw in _CHINESE_STOP:
            continue
        parts = kw.split()
        if any(p in _CHINESE_STOP for p in parts):
            continue
        if _has_chinese(topic_description):
            kw = kw.replace(" ", "")
        else:
            kw = " ".join(parts)
        keywords.append(kw)
    return keywords[:top_n]


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
