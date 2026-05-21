"""论文数据获取接口 —— 搭档实现。

所有函数返回统一的 paper dict 格式：

    {
        "title": str,
        "authors": str,       # 逗号分隔
        "abstract": str,
        "year": int | None,
        "source": str,        # "arxiv" | "openalex" | "local_pdf"
        "url": str | None,
        "doi": str | None,
    }
"""


def fetch_arxiv(keywords: list[str], max_results: int = 30) -> list[dict]:
    """通过 arXiv API 检索论文。

    Args:
        keywords: 关键词列表，多词用 AND 逻辑
        max_results: 最大返回数

    Returns:
        paper dict 列表
    """
    raise NotImplementedError("搭档实现")


def fetch_openalex(keywords: list[str], max_results: int = 30) -> list[dict]:
    """通过 OpenAlex API 检索论文（免 Key）。

    Args:
        keywords: 关键词列表
        max_results: 最大返回数

    Returns:
        paper dict 列表
    """
    raise NotImplementedError("搭档实现")


def import_local_pdfs(folder_path: str) -> list[dict]:
    """导入本地 PDF 文件夹，用 PyMuPDF 提取标题和摘要。

    Args:
        folder_path: PDF 文件夹路径

    Returns:
        paper dict 列表，source="local_pdf"
    """
    raise NotImplementedError("搭档实现")


def deduplicate(papers: list[dict]) -> list[dict]:
    """去重：按 title 相似度合并重复论文。"""
    raise NotImplementedError("搭档实现")
