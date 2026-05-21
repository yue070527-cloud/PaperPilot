"""关键词提取接口。

Phase 1 用 KeyBERT，后续可扩展其他方案。
"""

from keybert import KeyBERT

_kw_model = None


def _get_model() -> KeyBERT:
    global _kw_model
    if _kw_model is None:
        _kw_model = KeyBERT()
    return _kw_model


def extract_keywords(topic_description: str, top_n: int = 10) -> list[str]:
    """从课题描述中自动提取技术术语。

    Args:
        topic_description: 课题描述文本（1-3 句话）
        top_n: 返回关键词数量

    Returns:
        关键词字符串列表，如 ["perovskite", "stability", "solar cell"]
    """
    model = _get_model()
    results = model.extract_keywords(
        topic_description,
        top_n=top_n,
        stop_words="english",
    )
    return [kw for kw, _ in results]


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
