"""AI 服务层 — DeepSeek API 封装 + 论文精读。

Deep Read 采用 RLM 分层阅读策略（借鉴 Feynman）：
    - < 8000 字符：直接全文注入
    - 8000-60000 字符：滑动窗口 + 渐进笔记 + 合成
    - > 60000 字符：切块分析后合成

复用 downloader.py 的 PDF/HTML 获取能力，不重复造轮子。
"""

import hashlib
import json
import logging
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

from paperpilot.config import load_config

logger = logging.getLogger(__name__)

_API_URL = "https://api.deepseek.com/v1/chat/completions"
_DEFAULT_MODEL = "deepseek-v4-flash"
_THINKING_DISABLED = {"type": "disabled"}
_DEEP_READ_DIR = Path("outputs/deep_read")

# ── RLM 参数 ──
_WINDOW_SIZE = 6000    # 每窗字符数
_OVERLAP = 500         # 窗间重叠
_TIER1_MAX = 8000      # 直接注入阈值
_TIER2_MAX = 60000     # 窗口滑读上限


# ── 全文获取 ──

def _extract_text_from_html(html_path: str) -> str | None:
    """从 downloader.fetch_full_text 缓存的 HTML 文件中提取纯文本。

    文件是自包含 HTML（含图片 base64），需移除标签和脚本。
    """
    try:
        p = Path(html_path)
        if not p.is_file():
            return None
        html = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.select("script, style, nav, footer, img, svg"):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        # 回退：正则移除标签
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&[a-z]+;", " ", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l.strip() for l in text.split("\n")]
    text = "\n".join(l for l in lines if l)
    return text if len(text) >= 200 else None


def get_full_text_for_paper(paper: dict) -> tuple[str | None, str]:
    """三级链路获取论文全文，供 deep_read 使用。

    优先级：PDF 提取 > arXiv/出版方 HTML > 不可用

    Args:
        paper: paper dict，需含 pdf_path / url / doi 等

    Returns:
        (full_text, source) — source 为 "pdf" / "html" / "unavailable"
    """
    from paperpilot.downloader import cache_pdf, extract_pdf_text, fetch_full_text

    # 1. PDF 缓存 + 提取
    pdf_path = paper.get("pdf_path") or cache_pdf(paper)
    if pdf_path:
        p = Path(pdf_path)
        if p.is_file():
            try:
                pdf_bytes = p.read_bytes()
                text = extract_pdf_text(pdf_bytes)
                if text and len(text.strip()) >= 100:
                    return text.strip(), "pdf"
            except Exception as e:
                logger.warning(f"PDF extraction failed: {e}")

    # 2. HTML 全文（缓存优先 → arXiv HTTP 快速路径 → CDP 浏览器）
    html_path = fetch_full_text(paper)
    if html_path:
        text = _extract_text_from_html(html_path)
        if text and len(text.strip()) >= 500:
            return text.strip(), "html"
        elif text and len(text.strip()) >= 100:
            # 正文过短（仅摘要页），标记为不可用，建议用户下载 PDF
            logger.info("HTML 正文过短 (%d chars)，跳过，建议下载 PDF", len(text.strip()))
            return None, "html_truncated"

    return None, "unavailable"


# ── AIService ──

class AIService:
    """DeepSeek API 封装，提供论文精读等 AI 能力。"""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key
        self._model = model

    @property
    def is_available(self) -> bool:
        key, _ = self._resolve_key_model()
        return bool(key)

    def _resolve_key_model(self) -> tuple[str, str]:
        key = self._api_key
        model = self._model
        if not key or not model:
            cfg = load_config()
            ds = cfg.get("deepseek", {})
            if not key:
                key = ds.get("api_key", "").strip()
            if not model:
                model = ds.get("model", "").strip() or _DEFAULT_MODEL
        return key, model

    def _call_api(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: int = 120,
    ) -> str:
        """通用 API 调用，含重试和 V4 thinking 禁用。"""
        api_key, model = self._resolve_key_model()
        if not api_key:
            return ""

        payload = {
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if "v4" in model.lower():
            payload["thinking"] = _THINKING_DISABLED

        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    _API_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return body.get("choices", [{}])[0].get("message", {}).get("content", "")
            except (urllib.error.URLError, urllib.error.HTTPError) as e:
                if attempt == 1:
                    logger.warning(f"API call failed (attempt 1): {e}, retrying...")
                    continue
                logger.warning(f"API call failed (attempt 2): {e}")
            except json.JSONDecodeError as e:
                logger.warning(f"API response parse error: {e}")
                break

        return ""

    def _parse_json_response(self, content: str) -> dict:
        """从 LLM 回复中提取 JSON 块，失败返回空 dict。"""
        if not content:
            return {}
        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试找 { ... } 块
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        logger.warning("Failed to parse JSON from API response")
        return {}

    # ── RLM 分层阅读 ──

    def _rlm_window_read(
        self,
        full_text: str,
        title: str,
        window_size: int = _WINDOW_SIZE,
        overlap: int = _OVERLAP,
    ) -> str:
        """滑动窗口阅读长文本，每窗写渐进笔记，返回合成笔记。

        参照 Feynman Tier 2：文档留在内存，每窗读取 → 提取要点 → 追加笔记，
        全部读完后用笔记合成最终分析。
        """
        text_len = len(full_text)
        notes_parts: list[str] = []
        step = window_size - overlap
        total_windows = max(1, (text_len - overlap) // step)

        system_prompt = (
            "你是一位资深学术审稿人。请仔细阅读论文片段，提取关键信息。\n\n"
            "用中文输出，格式如下：\n"
            "- 核心主张：...\n"
            "- 关键方法/数据：...\n"
            "- 可能的创新点：...\n"
            "- 可疑的局限：...\n\n"
            "只基于当前片段分析，不要编造。如果片段是从论文中间开始的，"
            "直接分析看到的内容即可。"
        )

        for i in range(total_windows):
            start = i * step
            end = min(start + window_size, text_len)
            chunk = full_text[start:end]

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"论文标题：《{title}》\n"
                    f"—— 片段 {i + 1}/{total_windows} ——\n\n"
                    f"{chunk}"
                )},
            ]

            content = self._call_api(messages, temperature=0.3, max_tokens=600, timeout=90)
            if content:
                notes_parts.append(f"## 片段 {i + 1}/{total_windows}\n\n{content}")

            print(f"[DeepRead] Window {i + 1}/{total_windows} done ({len(chunk)} chars)", flush=True)

        return "\n\n".join(notes_parts)

    def _chunked_read(
        self,
        full_text: str,
        title: str,
        chunk_size: int = _TIER2_MAX,
    ) -> str:
        """超长文本 (>60K) 切块独立分析后合成。

        每块独立走一次完整分析调用，块间无重叠。
        """
        text_len = len(full_text)
        chunks = []
        for i in range(0, text_len, chunk_size):
            chunks.append(full_text[i:i + chunk_size])

        all_notes: list[str] = []
        for i, chunk in enumerate(chunks):
            notes = self._rlm_window_read(
                chunk, title,
                window_size=_WINDOW_SIZE,
                overlap=_OVERLAP,
            )
            if notes:
                all_notes.append(f"## Chunk {i + 1}/{len(chunks)}\n\n{notes}")
            print(f"[DeepRead] Chunk {i + 1}/{len(chunks)} analyzed", flush=True)

        return "\n\n".join(all_notes)

    # ── Deep Read ──

    _DEEP_READ_SYSTEM = (
        "你是一位资深学术审稿人。请基于提供的阅读笔记对论文进行结构化精读分析。\n\n"
        "严格按以下 JSON 格式输出，不要额外文字：\n"
        "{\n"
        '  "core_contribution": "论文解决的核心问题（一句话）",\n'
        '  "method": "关键技术路线或研究方法（2-3句）",\n'
        '  "key_evidence": "支撑结论的核心实验或数据，引用原文具体内容",\n'
        '  "highlights": "创新点或论文最强方面（2-3句）",\n'
        '  "limitations": "明显局限或改进空间（1-2句）",\n'
        '  "scores": {"novelty": 7, "rigor": 6, "significance": 8}\n'
        "}\n\n"
        "scores 中 novelty/rigor/significance 各为 1-10 的整数。\n"
        "保持客观，基于笔记而非猜测。如果笔记中某项信息缺失，标注'未提及'而非编造。"
    )

    def deep_read(self, paper: dict, full_text: str | None = None) -> dict:
        """对单篇论文做结构化精读分析。

        Args:
            paper: paper dict，至少含 title
            full_text: 论文全文；为 None 时自动通过三级链路获取

        Returns:
            dict 含 core_contribution / method / key_evidence /
                 highlights / limitations / scores，
            失败时返回空 dict
        """
        if not self.is_available:
            return {}

        title = (paper.get("title") or "").strip()
        if not title:
            return {}

        # 获取全文
        source = "provided"
        if not full_text:
            full_text, source = get_full_text_for_paper(paper)
            if not full_text:
                # HTML 正文过短（仅摘要页），不浪费 token 做假精读
                if source == "html_truncated":
                    return {"_truncated": True, "_title": title, "_source": source}
                abstract = (paper.get("abstract") or "").strip()
                if abstract and len(abstract) >= 50:
                    full_text = f"（注意：仅获取到摘要，无全文）\n\n{abstract}"
                    source = "abstract_fallback"
                else:
                    return {}

        text_len = len(full_text)
        print(f"[DeepRead] Title: {title[:50]}... | {text_len} chars | source: {source}", flush=True)

        # RLM 分层处理
        if text_len <= _TIER1_MAX:
            # Tier 1: 直接注入
            print(f"[DeepRead] Tier 1: direct injection", flush=True)
            notes = full_text
        elif text_len <= _TIER2_MAX:
            # Tier 2: 滑动窗口
            print(f"[DeepRead] Tier 2: sliding window", flush=True)
            notes = self._rlm_window_read(full_text, title)
            if not notes:
                # 窗口阅读失败，截断回退到 Tier 1
                notes = full_text[:_TIER1_MAX]
        else:
            # Tier 3: 切块分析
            print(f"[DeepRead] Tier 3: chunked read", flush=True)
            notes = self._chunked_read(full_text, title)
            if not notes:
                notes = full_text[:_TIER1_MAX]

        # 最终合成
        messages = [
            {"role": "system", "content": self._DEEP_READ_SYSTEM},
            {"role": "user", "content": (
                f"论文标题：《{title}》\n"
                f"全文来源：{source}\n\n"
                f"—— 阅读笔记 ——\n\n{notes[:15000]}"
            )},
        ]

        content = self._call_api(messages, temperature=0.3, max_tokens=1500, timeout=120)
        result = self._parse_json_response(content)

        if not result:
            # 解析失败，返回原始回复作为 fallback
            result = {
                "core_contribution": "",
                "method": "",
                "key_evidence": "",
                "highlights": "",
                "limitations": "",
                "scores": {"novelty": 0, "rigor": 0, "significance": 0},
                "_raw": content[:500],
                "_parse_error": True,
            }

        # 附上元信息
        result["_title"] = title
        result["_source"] = source
        result["_text_chars"] = text_len
        return result


    # ── AI 精排 ──

    _SCORE_PAPERS_SYSTEM = (
        "你是一位严格的学术审稿人，需要快速评估一批论文与课题的相关性和质量。\n\n"
        "## 评分维度（每个维度 1-10 分）\n"
        "- relevance（课题相关性，权重 40%）：论文核心问题与课题描述的匹配程度\n"
        "- method（方法质量，权重 25%）：实验设计是否严谨、数据是否充分、方法论是否可靠\n"
        "- novelty（创新性，权重 20%）：方法/结论是否有新意，还是重复已有工作\n"
        "- recency（时效性，权重 15%）：近年发表加分（2020+），经典老文献不减分\n\n"
        "## 分数计算\n"
        "总分 = (relevance×0.4 + method×0.25 + novelty×0.2 + recency×0.15) × 10\n"
        "结果四舍五入到整数，范围 0-100。\n\n"
        "## 分档参考\n"
        "S 必读 85-100：课题核心问题直接命中，方法/结论可直接借鉴\n"
        "A 推荐 70-84：高度相关，但方法或场景有差异\n"
        "B 可浏览 55-69：部分相关，某个子方向有参考价值\n"
        "C 可选 40-54：弱相关，可能是背景或相关领域\n"
        "D 不推荐 0-39：基本无关或质量明显有问题\n\n"
        "## 理由写作要求\n"
        "每个维度的 reason 必须写 1-3 句中文，具体引用论文中提到的技术/方法/场景，"
        "解释为什么给这个分数。不要写空洞的套话如'本论文在该维度表现良好'。\n"
        "reason_overall 是所有维度中最关键的一个判断，用于决策是否值得阅读全文。\n\n"
        "## 无摘要处理\n"
        "如果论文没有摘要（abstract 为空或短于 80 字符），将 method、novelty 两项标为 0，"
        "仅基于标题评估 relevance 和 recency，tier 标注为 'no_abstract'，各 reason 写'仅标题，无法判断'。\n\n"
        "## 输出格式\n"
        "严格的 JSON 数组，按论文输入顺序，只输出 JSON 不要其他文字：\n"
        '[{"index": 0, "score": 85, "tier": "S", '
        '"relevance": 9, "method": 8, "novelty": 7, "recency": 9, '
        '"reason_relevance": "论文研究钙钛矿稳定性退化机制，与课题描述完全匹配。具体聚焦热致离子迁移，...", '
        '"reason_method": "采用原位PL光谱+ToF-SIMS联合表征，实验设计严谨，但样本量偏少（n=3），...", '
        '"reason_novelty": "首次定量建立离子迁移活化能与界面缺陷密度的关联，创新性突出。", '
        '"reason_overall": "该论文核心问题与课题高度一致，方法可靠且结论创新性强，建议优先阅读全文。", '
        '"reason_recency": "2023年发表，时效性好。"}, ...]\n\n'
        "注意：基于摘要内容判断，不要编造。"
    )

    def score_papers(self, topic_desc: str, papers: list[dict],
                     max_papers: int = 20) -> list[dict]:
        """AI 精排：基于摘要批量打分。

        Args:
            topic_desc: 课题描述
            papers: paper dict 列表（仅含摘要）
            max_papers: 最多评分篇数（默认 20，安全上限 100）

        Returns:
            [{index, ai_score, ai_reason: {relevance, method, novelty, overall}}, ...]
            按 ai_score 降序排列
        """
        if not self.is_available or not papers:
            return []

        max_papers = min(max_papers, 100)  # 安全上限

        # 筛选有摘要的论文
        candidates = []
        for i, p in enumerate(papers):
            abstract = (p.get("abstract") or "").strip()
            if abstract and len(abstract) >= 80:
                candidates.append((i, p))
            if len(candidates) >= max_papers:
                break

        if not candidates:
            return []

        # 构建输入
        lines = [f"课题描述：{topic_desc}\n\n—— 待评分论文 ——"]
        for idx, (orig_i, p) in enumerate(candidates):
            title = (p.get("title") or "无标题")[:120]
            abstract = (p.get("abstract") or "")[:800]
            lines.append(f"\n[{idx}] {title}\n摘要：{abstract}")

        messages = [
            {"role": "system", "content": self._SCORE_PAPERS_SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ]

        # 输出 token 按篇数动态分配（每篇 ~80 tokens）+ 200 余量
        dyn_tokens = max(3000, len(candidates) * 80 + 200)
        content = self._call_api(messages, temperature=0.2, max_tokens=dyn_tokens, timeout=120)
        raw = self._parse_json_response(content)

        # 解析结果
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict) and "papers" in raw:
            items = raw["papers"]
        elif isinstance(raw, dict) and "results" in raw:
            items = raw["results"]
        else:
            return []

        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", -1)
            if idx < 0 or idx >= len(candidates):
                continue
            orig_i, paper = candidates[idx]
            results.append({
                "index": orig_i,
                "ai_score": int(item.get("score", 0)),
                "tier": str(item.get("tier", "") or ""),
                "ai_reason": {
                    "relevance": int(item.get("relevance", 0)),
                    "method": int(item.get("method", 0)),
                    "novelty": int(item.get("novelty", 0)),
                    "recency": int(item.get("recency", 0)),
                    "reason_relevance": str(item.get("reason_relevance", "") or ""),
                    "reason_method": str(item.get("reason_method", "") or ""),
                    "reason_novelty": str(item.get("reason_novelty", "") or ""),
                    "overall": str(item.get("reason_overall", "") or ""),
                },
            })

        results.sort(key=lambda x: x["ai_score"], reverse=True)
        return results


# ── 持久化 ──

def save_deep_read_json(paper: dict, result: dict) -> str | None:
    """将精读结果保存为本地 JSON 文件。

    Args:
        paper: paper dict（需含 title）
        result: deep_read 返回的结果 dict

    Returns:
        保存的文件路径，失败返回 None
    """
    title = (paper.get("title") or "untitled").strip()
    slug = re.sub(r"[^\w\-]", "_", title[:60].lower())
    slug = re.sub(r"_+", "_", slug).strip("_") or hashlib.md5(title.encode()).hexdigest()[:12]

    _DEEP_READ_DIR.mkdir(parents=True, exist_ok=True)
    path = _DEEP_READ_DIR / f"{slug}.json"
    try:
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception as e:
        logger.warning(f"Failed to save deep read JSON: {e}")
        return None
