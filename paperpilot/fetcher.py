"""论文数据获取接口。

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

import os
import re
import time
from difflib import SequenceMatcher

import arxiv
import fitz
import requests


def _build_arxiv_query(keywords: list[str]) -> str:
    return " OR ".join(f'"{kw}"' for kw in keywords)


def _parse_arxiv_result(r) -> dict:
    authors = ", ".join(a.name for a in r.authors[:10])
    year = r.published.year if r.published else None
    doi = None
    if r.doi:
        doi = r.doi if r.doi.startswith("10.") else None
    return {
        "title": r.title.strip() if r.title else "",
        "authors": authors,
        "abstract": r.summary.strip() if r.summary else "",
        "year": year,
        "source": "arxiv",
        "url": r.entry_id or None,
        "doi": doi,
    }


def fetch_arxiv(keywords: list[str], max_results: int = 30) -> list[dict]:
    """通过 arXiv API 检索论文。

    Args:
        keywords: 关键词列表，多词用 AND 逻辑
        max_results: 最大返回数

    Returns:
        paper dict 列表
    """
    query = _build_arxiv_query(keywords)
    client = arxiv.Client()
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    papers = []
    for r in client.results(search):
        papers.append(_parse_arxiv_result(r))
    return papers


def _parse_openalex_work(w: dict) -> dict | None:
    title = (w.get("title") or "").strip()
    if not title:
        return None
    authorship = w.get("authorships") or []
    authors = ", ".join(
        a.get("author", {}).get("display_name", "")
        for a in authorship[:10]
    )
    year = w.get("publication_year") or None
    doi = w.get("doi") or None
    if doi:
        doi = doi.removeprefix("https://doi.org/")
    abstract = ""
    abstract_inverted = w.get("abstract_inverted_index")
    if abstract_inverted:
        abstract = _decode_inverted_index(abstract_inverted)
    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "year": year,
        "source": "openalex",
        "url": w.get("primary_location", {}).get("landing_page_url") or None,
        "doi": doi,
    }


def _decode_inverted_index(inv: dict) -> str:
    max_pos = max(p[-1] for p in inv.values())
    words = [""] * (max_pos + 1)
    for word, positions in inv.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words)


def fetch_openalex(keywords: list[str], max_results: int = 30) -> list[dict]:
    """通过 OpenAlex API 检索论文（免 Key）。

    Args:
        keywords: 关键词列表
        max_results: 最大返回数

    Returns:
        paper dict 列表
    """
    query = " ".join(keywords)
    url = "https://api.openalex.org/works"
    papers = []
    per_page = min(50, max_results)
    pages = (max_results + per_page - 1) // per_page
    for page in range(1, pages + 1):
        params = {
            "search": query,
            "per_page": per_page,
            "page": page,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for w in data.get("results", []):
                paper = _parse_openalex_work(w)
                if paper:
                    papers.append(paper)
            if len(papers) >= max_results:
                break
            time.sleep(0.1)
        except requests.RequestException:
            continue
    return papers[:max_results]


def _extract_text_from_page(page, max_chars: int = 3000) -> str:
    blocks = page.get_text("blocks")
    blocks = sorted(blocks, key=lambda b: (b[1], b[0]))
    return " ".join(b[4] for b in blocks if b[6] == 0)[:max_chars]


def _guess_title(text: str) -> str:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines[:5]:
        if len(line) > 10:
            return line[:500]
    return lines[0][:500] if lines else ""


def _guess_abstract(text: str) -> str:
    pattern = r"(?i)abstract[\s\-\—:]*\n?"
    match = re.search(pattern, text)
    if match:
        start = match.end()
        rest = text[start:].strip()
        return rest[:2000]
    return text[:2000]


def import_local_pdfs(folder_path: str) -> list[dict]:
    """导入本地 PDF 文件夹，用 PyMuPDF 提取标题和摘要。

    Args:
        folder_path: PDF 文件夹路径

    Returns:
        paper dict 列表，source="local_pdf"
    """
    papers = []
    for filename in os.listdir(folder_path):
        if not filename.lower().endswith(".pdf"):
            continue
        filepath = os.path.join(folder_path, filename)
        try:
            doc = fitz.open(filepath)
            text = ""
            for p in doc:
                text += p.get_text()
                if len(text) > 5000:
                    break
            doc.close()
            title = _guess_title(text)
            abstract = _guess_abstract(text)
            papers.append({
                "title": title,
                "authors": "",
                "abstract": abstract,
                "year": None,
                "source": "local_pdf",
                "url": None,
                "doi": None,
            })
        except Exception:
            continue
    return papers


def _normalize_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower().strip())


def deduplicate(papers: list[dict]) -> list[dict]:
    """去重：按 title 相似度合并重复论文。"""
    seen = []
    for paper in papers:
        t1 = _normalize_title(paper["title"])
        dup = False
        for existing in seen:
            t2 = _normalize_title(existing["title"])
            if t1 == t2 or SequenceMatcher(None, t1, t2).ratio() >= 0.9:
                dup = True
                break
        if not dup:
            seen.append(paper)
    return seen
