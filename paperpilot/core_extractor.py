"""核心关键词提取 — 通过 DeepSeek API 从科研课题中提取高区分度关键词。

提取原则（来自中文科研课题关键词提取指南）：
1. 优先提取具体技术/材料/方法，而非上层泛化概念
2. 提取课题真正解决的关键科学问题或应用场景
3. 跳过领域内过度饱和的热词，向下挖掘一层
4. 保留复合专有名词完整性，不可拆分
5. 删除修饰性、背景性词语
6. 输出 1-3 个核心关键词
"""

import json
import urllib.request
import urllib.error
from paperpilot.config import load_config

# DeepSeek API endpoint (OpenAI-compatible)
_API_URL = "https://api.deepseek.com/v1/chat/completions"
_MODEL = "deepseek-v4-flash"

_SYSTEM_PROMPT = (
    "你是一个科研关键词提取专家。你的任务是从科研课题标题中提取1-3个最核心、"
    "最具区分度的关键词。\n\n"
    "严格遵循以下规则：\n"
    "1. 优先提取具体的核心技术/材料/方法，而非上层泛化概念"
    "（如提取 Transformer 而非多模态大语言模型）\n"
    "2. 提取课题真正解决的关键科学问题或应用场景\n"
    "3. 跳过领域内过度饱和的热词，向下挖掘一层更具区分度的概念"
    "（如课题做LNP/mRNA但创新点在器官靶向性，则提取器官靶向性而非mRNA）\n"
    "4. 保留复合专有名词的完整性，不可拆分"
    "（如钙钛矿量子点、CRISPR-Cas12a、非阿贝尔编织）\n"
    "5. 删除基于、用于、研究、优化、性能等修饰性背景词\n"
    "6. 输出1-3个核心关键词，用顿号（、）分隔，不要解释、不要编号、不要换行\n\n"
    "输出格式示例：\n"
    "钙钛矿量子点、柔性显示器件\n"
    "CRISPR-Cas12a、遗传性视网膜病变\n"
    "数字孪生、自愈控制"
)


def extract_core_keywords(topic: str) -> list[str]:
    """从科研课题标题中提取核心关键词。

    Args:
        topic: 科研课题标题（中文）

    Returns:
        核心关键词列表（1-3个），失败时返回空列表
    """
    config = load_config()
    api_key = config.get("deepseek", {}).get("api_key", "").strip()
    if not api_key:
        return []

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": topic},
        ],
        "temperature": 0.3,
        "max_tokens": 30,
        "stream": False,
    }

    try:
        req = urllib.request.Request(
            _API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_response(content)


def _parse_response(content: str) -> list[str]:
    """Parse model output into a clean keyword list."""
    import re
    # Split by common delimiters
    keywords = [k.strip() for k in re.split(r"[、,，/\n]", content) if k.strip()]
    # Filter: must have Chinese or meaningful ASCII, at least 2 chars
    result = []
    for k in keywords:
        has_cjk = any("一" <= c <= "鿿" for c in k)
        has_alpha = any(c.isascii() and c.isalpha() for c in k)
        if len(k) >= 2 and (has_cjk or has_alpha):
            # Remove common trailing punctuation
            k = k.rstrip("。，,、.!！?？;；：:")
            if len(k) >= 2:
                result.append(k)
    return result[:3]
