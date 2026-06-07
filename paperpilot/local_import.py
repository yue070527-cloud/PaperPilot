"""本地 PDF 导入与文本提取。

从用户磁盘读取 PDF，提取标题/作者/摘要，构建标准 paper dict，
供后续 CE 排序、PDF 渲染、文献库保存使用。
"""

import os
import re

import fitz  # PyMuPDF


def scan_folder(folder_path: str, recursive: bool = True) -> list[str]:
    """扫描文件夹中所有 .pdf 文件。

    Args:
        folder_path: 文件夹路径
        recursive: 是否递归子文件夹，默认 True

    Returns:
        按文件名排序的 .pdf 绝对路径列表
    """
    pdfs: list[str] = []
    if recursive:
        for root, _, files in os.walk(folder_path):
            for f in files:
                if f.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(root, f))
    else:
        try:
            for entry in os.scandir(folder_path):
                if entry.is_file() and entry.name.lower().endswith(".pdf"):
                    pdfs.append(entry.path)
        except OSError:
            pass

    pdfs.sort(key=lambda p: os.path.basename(p).lower())
    return pdfs


def extract_pdf(file_path: str) -> dict | None:
    """从单个 PDF 文件中提取文本和元数据，构建 paper dict。

    扫描件（无文字层）或加密/损坏的 PDF 返回 None。

    Returns:
        paper dict 含 pdf_path 字段，或 None
    """
    try:
        doc = fitz.open(file_path)
    except Exception:
        return None

    if len(doc) == 0:
        doc.close()
        return None

    # 提取全文文本（前 8000 字符用于摘要推断）
    full_text = ""
    for page in doc:
        full_text += page.get_text()
        if len(full_text) > 8000:
            break

    # 扫描件检测：跳过第 1 页（可能是封面图），检查前 5 页文字总量
    # 阈值降低到 50 字符，避免误判图片较多的正常论文
    text_check = ""
    start_page = 1 if len(doc) > 1 else 0  # 跳过可能的封面页
    for i in range(start_page, min(start_page + 5, len(doc))):
        text_check += doc[i].get_text()
    if len(text_check.strip()) < 50:
        doc.close()
        return None

    # 元数据提取
    metadata = doc.metadata or {}
    doc.close()

    title = _extract_title(metadata, file_path, full_text)
    authors = _extract_authors(metadata)
    abstract = _extract_abstract(full_text)
    year = _extract_year(metadata, full_text)

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "year": year,
        "source": "local_pdf",
        "url": None,
        "doi": metadata.get("doi") or None,
        "api_score": None,       # 本地文件无 API 分
        "type": None,
        "cited_by_count": None,
        "journal": metadata.get("journal") or None,
        "openalex_id": None,
        "pdf_path": os.path.abspath(file_path),
    }


def extract_pdfs(
    file_paths: list[str],
    on_progress=None,
) -> tuple[list[dict], list[str]]:
    """批量提取 PDF，返回 (有效论文列表, 跳过的文件名列表)。

    Args:
        file_paths: PDF 文件路径列表
        on_progress: 可选回调 (current, total, filename) -> None

    Returns:
        (papers, skipped_names) — papers 含 pdf_path 字段
    """
    papers: list[dict] = []
    skipped: list[str] = []
    total = len(file_paths)

    for i, path in enumerate(file_paths):
        fname = os.path.basename(path)
        if on_progress:
            on_progress(i + 1, total, fname)

        result = extract_pdf(path)
        if result is None:
            skipped.append(fname)
        else:
            papers.append(result)

    return papers, skipped


# ── 内部辅助 ──

def _extract_title(metadata: dict, file_path: str, text: str) -> str:
    """从 metadata、文件名、正文推断标题。"""
    # 1. PDF metadata title
    mt = (metadata.get("title") or "").strip()
    if mt and len(mt) > 5 and not mt.startswith("Microsoft Word"):
        # 有些 metadata title 是文件名，需要排除
        fname_no_ext = os.path.splitext(os.path.basename(file_path))[0]
        if mt.lower() != fname_no_ext.lower():
            return mt[:500]

    # 2. 正文首行（长于 20 字符的非空行）
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) > 20 and not stripped.lower().startswith(("abstract", "doi:", "http")):
            return stripped[:500]

    # 3. 文件名（去掉扩展名和下划线/连字符替换）
    name = os.path.splitext(os.path.basename(file_path))[0]
    name = name.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:500] if name else "Untitled"


def _extract_authors(metadata: dict) -> str:
    """从 PDF metadata 提取作者。"""
    authors = (metadata.get("author") or "").strip()
    if not authors:
        return "未知"
    # 截断过长作者列表
    if len(authors) > 500:
        authors = authors[:497] + "..."
    return authors


def _extract_abstract(text: str) -> str:
    """从正文推断摘要。

    策略：
    1. 查找 "Abstract" 标记 → 取标记后内容
    2. 无标记时 → 取正文前 2000 字符
    """
    # 尝试匹配 abstract 标记
    pattern = r"(?im)^\s*abstract[\s\-_:]*\n?"
    m = re.search(pattern, text)
    if m:
        start = m.end()
        after = text[start:].strip()
        # 取到下一个节标题或 2000 字符
        section_break = re.search(r"\n\s*(?:\d+\.|[IVX]+\.)?\s*(?:Introduction|引言|1\.)", after)
        if section_break:
            return after[:section_break.start()].strip()[:2000]
        return after[:2000]

    # 无标记，取前 2000 字符
    return text.strip()[:2000]


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _extract_year(metadata: dict, text: str) -> int | None:
    """从 metadata 或正文中推断发表年份。"""
    # 1. metadata — 先处理 PDF 日期格式 D:YYYYMMDD...
    for key in ("creationDate", "modDate", "date"):
        val = str(metadata.get(key, ""))
        if not val:
            continue
        # PDF 日期: "D:20160125173023+05'30'" → 提取开头的年份
        m = re.match(r"D:(\d{4})", val)
        if m:
            year = int(m.group(1))
            if 1900 <= year <= 2100:
                return year
        m = _YEAR_RE.search(val)
        if m:
            return int(m.group())

    # 2. 正文前部搜索
    head = text[:2000]
    m = _YEAR_RE.search(head)
    if m:
        return int(m.group())

    return None
