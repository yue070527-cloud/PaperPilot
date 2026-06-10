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

    # 提取第一页文字块（含字号信息），用于标题检测
    first_page_blocks = doc[0].get_text("dict").get("blocks", []) if len(doc) > 0 else []

    # 元数据提取
    metadata = doc.metadata or {}
    doc.close()

    try:
        title = _extract_title(metadata, file_path, full_text, first_page_blocks)
    except Exception:
        title = os.path.splitext(os.path.basename(file_path))[0]
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

def _has_mojibake(s: str) -> bool:
    """检测疑似编码乱码（UTF-8 中文被错误解码为 latin-1 的典型特征）。

    Latin-1 字母区 (U+00C0-U+00FF) 占比 > 40% 且无 CJK 字符时，
    极可能是中文被当作 latin-1 解码。真实法语/德语等重音字母占比
    通常不超过 20%，不会触发此检查。
    """
    if not s:
        return False
    garbled_range = sum(1 for c in s if 'À' <= c <= 'ÿ')
    cjk = sum(1 for c in s if '一' <= c <= '鿿')
    length = len(s)
    if length > 0 and garbled_range / length > 0.4 and cjk == 0:
        return True
    return False


def _is_valid_title(s: str) -> bool:
    """检查字符串是否像有效标题（排除乱码、纯数字/符号）。"""
    if len(s) < 5 or len(s) > 500:
        return False
    if s.lower().startswith("microsoft word"):
        return False
    if _has_mojibake(s):
        return False
    # 字母/数字/常见标点占的比例过低 → 疑似乱码
    valid_chars = sum(c.isalnum() or c.isspace() or c in ".-,;:!?()[]'\"&/@#*+=<>_" for c in s)
    if len(s) > 0 and valid_chars / len(s) < 0.4:
        return False
    # 至少要有几个字母/中文字符
    letters = sum(c.isalpha() for c in s)
    if letters < 3:
        return False
    return True


def _extract_title_from_blocks(blocks: list) -> str | None:
    """从 PyMuPDF 第一页文字块中，按最大字号定位标题。"""
    candidates = []  # (font_size, y_pos, text)
    for block in blocks:
        if block.get("type") != 0:  # 只处理文字块
            continue
        bbox = block.get("bbox", (0, 0, 0, 0))
        for line in block.get("lines", []):
            line_text_parts = []
            max_size = 0
            for span in line.get("spans", []):
                line_text_parts.append(span.get("text", "").strip())
                max_size = max(max_size, span.get("size", 0))
            line_text = " ".join(p for p in line_text_parts if p).strip()
            if line_text and max_size > 0 and len(line_text) >= 3:
                candidates.append((max_size, bbox[1], line_text))

    if not candidates:
        return None

    # 按字号降序、y 坐标升序（最大字号的最高行优先）
    candidates.sort(key=lambda x: (-x[0], x[1]))

    # 取最大字号 ±5% 内的所有行，按 y 排序拼接
    best_size = candidates[0][0]
    title_lines = [(y, text) for size, y, text in candidates if size >= best_size * 0.95]
    title_lines.sort(key=lambda x: x[0])  # 按 y 坐标从上到下

    title = " ".join(text for _, text in title_lines)
    title = re.sub(r"\s+", " ", title).strip()

    if len(title) >= 10 and _is_valid_title(title):
        return title[:500]
    return None


def _extract_title(metadata: dict, file_path: str, text: str,
                   first_page_blocks: list | None = None) -> str:
    """从 metadata / 字号检测 / 正文 / 文件名推断标题。

    metadata 优先，但乱码会被 _is_valid_title 过滤，
    从而自然回退到字号检测。
    """
    # 1. PDF metadata title（通过 _is_valid_title 过滤乱码）
    mt = (metadata.get("title") or "").strip()
    if _is_valid_title(mt):
        fname_no_ext = os.path.splitext(os.path.basename(file_path))[0]
        if mt.lower() != fname_no_ext.lower():
            return mt[:500]

    # 2. 第一页最大字号检测（metadata 不可用时最可靠的回退）
    if first_page_blocks:
        title = _extract_title_from_blocks(first_page_blocks)
        if title:
            return title

    # 3. 正文首行（长于 20 字符的非空行）回退
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) > 20 and not stripped.lower().startswith(("abstract", "doi:", "http")):
            return stripped[:500]

    # 4. 文件名（最后回退）
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
