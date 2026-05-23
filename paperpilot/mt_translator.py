"""中文关键词 → 英文术语翻译模块。

通过 DeepSeek API 进行学术关键词翻译，替代原有的 Hunyuan-MT 本地模型。
API Key 从 config.yaml 的 deepseek.api_key 读取，支持用户自有 Key。
"""

import json
import re
import urllib.request
import urllib.error
from paperpilot.config import load_config

_API_URL = "https://api.deepseek.com/v1/chat/completions"
_MODEL = "deepseek-chat"

_SYSTEM_PROMPT = (
    "You are a scientific translator. Translate Chinese academic keywords into "
    "precise English technical terms. For each input, output only the English "
    "translation. Use domain-appropriate terminology. Never add explanations."
)


def translate_terms(chinese_terms: list[str]) -> list[str]:
    """将中文关键词列表翻译为英文术语列表（批量调用 DeepSeek API）。

    Args:
        chinese_terms: 中文关键词列表（如 ["钙钛矿太阳能电池", "基因治疗"]）

    Returns:
        英文术语列表（与输入一一对应），翻译失败的项返回空字符串
    """
    if not chinese_terms:
        return []

    config = load_config()
    api_key = config.get("deepseek", {}).get("api_key", "").strip()
    if not api_key:
        return [""] * len(chinese_terms)

    # Separate: already-ASCII terms pass through, Chinese terms need translation
    to_translate = []
    indices = []
    results = [""] * len(chinese_terms)

    for i, term in enumerate(chinese_terms):
        stripped = term.strip()
        if not stripped:
            results[i] = ""
            continue
        if all(ord(c) < 128 for c in stripped):
            results[i] = stripped
            continue
        to_translate.append(stripped)
        indices.append(i)

    if not to_translate:
        return results

    # Build numbered list for reliable parsing
    numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(to_translate))
    user_msg = (
        "Translate these Chinese academic keywords to English. "
        "Output one translation per line in the format: number. English term\n\n"
        f"{numbered}"
    )

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.1,
        "max_tokens": len(to_translate) * 50,
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return results

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    translations = _parse_batch_response(content, len(to_translate))

    for j, translation in enumerate(translations):
        if translation:
            results[indices[j]] = translation

    return results


def _parse_batch_response(content: str, expected_count: int) -> list[str]:
    """Parse numbered translation output into ordered list."""
    lines = content.strip().split("\n")
    parsed = {}

    for line in lines:
        line = line.strip()
        # Match "1. English term" or "1 English term" or "1、English term"
        m = re.match(r"^(\d+)[\.\、\)\s]\s*(.+)", line)
        if m:
            idx = int(m.group(1)) - 1
            term = m.group(2).strip()
            # Remove trailing descriptions (模型偶尔会多话)
            term = re.sub(r"\s*[\(（].*[\)）]\s*$", "", term)
            term = term.rstrip(".,;:!。，；：！")
            if 0 <= idx < expected_count:
                parsed[idx] = term

    # Build result in order
    result = []
    for i in range(expected_count):
        t = parsed.get(i, "")
        # Reject if still contains Chinese
        if t and any("一" <= c <= "鿿" for c in t):
            t = ""
        result.append(t)
    return result
