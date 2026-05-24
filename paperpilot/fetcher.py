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
        "api_score": float,   # 0.0-1.0, API 原始排序位置归一化
    }
"""

import os
import re
import time
from difflib import SequenceMatcher

import arxiv
import fitz
import requests


def _build_search_query(keywords: list[str], logic: str = "OR") -> str:
    """Build quoted-phrase query for search APIs (arXiv, OpenAlex).

    Args:
        keywords: list of keyword phrases
        logic: "OR" (default, broad recall) or "AND" (strict, all must match)
    """
    joiner = " AND " if logic == "AND" else " OR "
    return joiner.join(f'"{kw}"' for kw in keywords)


def _build_mixed_query(and_kw: list[str], or_kw: list[str]) -> str:
    """Build mixed boolean query: must-match terms (AND) + should-match terms (OR).

    Examples:
        and_kw=["core1","core2"], or_kw=["reg1","reg2"]
        → "core1" AND "core2" AND ("reg1" OR "reg2")

        and_kw=[], or_kw=["kw1","kw2"]
        → "kw1" OR "kw2"

        and_kw=["primary"], or_kw=[]
        → "primary"
    """
    if and_kw and or_kw:
        and_part = " AND ".join(f'"{kw}"' for kw in and_kw)
        or_part = " OR ".join(f'"{kw}"' for kw in or_kw)
        return f'{and_part} AND ({or_part})'
    elif and_kw:
        return " AND ".join(f'"{kw}"' for kw in and_kw)
    elif or_kw:
        return " OR ".join(f'"{kw}"' for kw in or_kw)
    return ""


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


def _fetch_arxiv_raw(query: str, max_results: int = 30) -> list[dict]:
    """Fetch papers from arXiv with a raw query string (internal helper)."""
    client = arxiv.Client()
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )
    papers = []
    for r in client.results(search):
        papers.append(_parse_arxiv_result(r))
    total = max(len(papers), 1)
    for i, p in enumerate(papers):
        p["api_score"] = 1.0 - (i / total)
    return papers


def fetch_arxiv(keywords: list[str], max_results: int = 30,
                logic: str = "OR") -> list[dict]:
    """通过 arXiv API 检索论文。

    Args:
        keywords: 关键词列表
        max_results: 最大返回数
        logic: "OR"（宽召回，默认）或 "AND"（核心词全部命中）

    Returns:
        paper dict 列表，含 api_score 字段（0.0-1.0，API 排序位置归一化）
    """
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_arxiv_raw(query, max_results)


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


def _fetch_openalex_raw(query: str, max_results: int = 30) -> list[dict]:
    """Fetch papers from OpenAlex with a raw query string (internal helper)."""
    url = "https://api.openalex.org/works"
    papers = []
    per_page = min(50, max_results)
    pages = (max_results + per_page - 1) // per_page
    headers = {"User-Agent": "PaperPilot/1.0 (mailto:paperpilot@example.com)"}
    for page in range(1, pages + 1):
        params = {
            "search": query,
            "per_page": per_page,
            "page": page,
            "mailto": "paperpilot@example.com",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            for retry in range(3):
                if resp.status_code != 429:
                    break
                time.sleep(1 * (retry + 1))
                resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            page_total = len(results)
            for i, w in enumerate(results):
                paper = _parse_openalex_work(w)
                if paper:
                    api_rel = w.get("relevance_score")
                    if api_rel is not None:
                        paper["api_score"] = float(api_rel)
                    else:
                        paper["api_score"] = 1.0 - (i / max(page_total, 1))
                    papers.append(paper)
            if len(papers) >= max_results:
                break
            time.sleep(0.1)
        except requests.RequestException:
            continue
    # Normalize api_score to [0, 1] — OpenAlex relevance_score may exceed [0, 1]
    api_scores = [p.get("api_score") for p in papers if p.get("api_score") is not None]
    if api_scores:
        min_s, max_s = min(api_scores), max(api_scores)
        if max_s > 1.0 or min_s < 0.0:
            if max_s > min_s:
                for p in papers:
                    if p.get("api_score") is not None:
                        p["api_score"] = (p["api_score"] - min_s) / (max_s - min_s)
            else:
                for p in papers:
                    if p.get("api_score") is not None:
                        p["api_score"] = 0.5
    # Fill remaining None with position-based scores
    total = max(len(papers), 1)
    for i, p in enumerate(papers):
        if p.get("api_score") is None:
            p["api_score"] = 1.0 - (i / total)
    return papers[:max_results]


def fetch_openalex(keywords: list[str], max_results: int = 30,
                   logic: str = "OR") -> list[dict]:
    """通过 OpenAlex API 检索论文（免 Key）。

    Args:
        keywords: 关键词列表
        max_results: 最大返回数
        logic: "OR"（宽召回，默认）或 "AND"（核心词全部命中）

    Returns:
        paper dict 列表，含 api_score 字段（0.0-1.0，API 排序位置归一化）
    """
    if not keywords:
        return []
    query = _build_search_query(keywords, logic=logic)
    return _fetch_openalex_raw(query, max_results)


def fetch_with_cascade(
    primary_kw: str | None,
    secondary_kw: list[str],
    regular_kw: list[str],
    source: str = "arxiv",
    max_results: int = 30,
    min_results: int = 3,
) -> tuple[list[dict], int]:
    """三级级联检索：核心AND → 主关键词AND → 全部OR。

    Strategy 0: 全部核心词 AND + 普通词 OR（最严，需用户未选主关键词时核心词全命中）
    Strategy 1: (主关键词) AND (所有其他词 OR)（仅主关键词必须命中）
    Strategy 2: 全部 OR（保底宽召回）

    Args:
        primary_kw: 用户选择的主关键词，None 表示未选择
        secondary_kw: 副关键词（核心词中未被选为主关键词的）
        regular_kw: 普通关键词
        source: "arxiv" 或 "openalex"
        max_results: 每次尝试的最大返回数
        min_results: 触发降级的结果数阈值

    Returns:
        (papers, strategy_level): papers 含 api_score，strategy_level 0/1/2
    """
    fetch_raw = _fetch_arxiv_raw if source == "arxiv" else _fetch_openalex_raw
    all_kw = ([primary_kw] if primary_kw else []) + secondary_kw + regular_kw
    all_core = ([primary_kw] if primary_kw else []) + secondary_kw

    strategies = []

    # Strategy 0: all core AND + regular OR
    if all_core:
        q0 = _build_mixed_query(and_kw=all_core, or_kw=regular_kw)
        strategies.append((0, q0))

    # Strategy 1: primary AND + all others OR (only if primary is set)
    if primary_kw:
        others = secondary_kw + regular_kw
        q1 = _build_mixed_query(and_kw=[primary_kw], or_kw=others)
        strategies.append((1, q1))

    # Strategy 2: all OR (safety net)
    q2 = _build_mixed_query(and_kw=[], or_kw=all_kw)
    strategies.append((2, q2))

    for level, query in strategies:
        if not query:
            continue
        papers = fetch_raw(query, max_results)
        if len(papers) >= min_results or level == strategies[-1][0]:
            return papers, level

    return [], -1


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
