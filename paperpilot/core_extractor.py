"""核心关键词提取 — 通过 DeepSeek API 从科研课题中提取高区分度关键词。

提取原则：
1. 核心关键词 = 课题的研究对象/科学问题本身，而非使用的工具方法
2. 普通关键词 = 具体的技术、材料、表征手段、应用场景
3. 保留复合专有名词完整性，不可拆分
4. 删除修饰性、背景性词语
"""

import json
import urllib.request
import urllib.error
from paperpilot.config import load_config

# DeepSeek API endpoint (OpenAI-compatible)
_API_URL = "https://api.deepseek.com/v1/chat/completions"
_DEFAULT_MODEL = "deepseek-v4-flash"

# V4 Flash/V4 Pro 默认开启推理模式，会消耗 reasoning tokens 导致无实际输出
# 必须显式禁用 thinking 才能用于关键词提取等简单任务
_THINKING_DISABLED = {"type": "disabled"}


def _get_model():
    """从 config.yaml 读取用户选择的模型，未配置时用 V4 Flash。"""
    model = load_config().get("deepseek", {}).get("model", "").strip()
    return model or _DEFAULT_MODEL


def _is_v4_model(model: str) -> bool:
    return "v4" in model.lower()

_SYSTEM_PROMPT = (
    "你是一个科研文献检索专家。你的任务是从课题描述中提取2-4个核心关键词，"
    "用于论文数据库检索。\n\n"
    "【核心关键词定义】\n"
    "核心关键词 = 课题要研究的科学对象或要解决的科学问题本身。\n"
    "  对：单原子催化剂、CO2还原、黎曼假设、量子密钥分发、锂硫电池\n"
    "  错：原位XAS（表征工具）、密度泛函理论（计算工具）、参数优化（通用动作）\n"
    "特殊说明：如果课题本身就是研究某种方法/模型的改进（如\"新型图神经网络\"），"
    "则该方法本身也是核心关键词。\n"
    "如果课题天然属于某个学科的核心问题（如椭圆曲线密码、DNA甲基化），"
    "则该学科的具体分支也是合法核心词（\"密码学\"\"表观遗传学\"），"
    "但不要提取过于宽泛的大类（\"数学\"\"生物学\"）。\n\n"
    "严格遵循以下规则：\n"
    "1. 先判断课题研究的是什么对象或问题，再从中提取关键词——"
    "不要把研究工具当成研究对象\n"
    "2. 提取粒度适中：比具体材料高一层的催化剂类型，比具体算法高一层的模型类别"
    "（如课题用\"氮掺杂碳负载铁\"做催化剂，核心词应为\"单原子催化剂\"而非\"氮掺杂碳\"）\n"
    "3. 如果课题涉及某学科的经典问题（如椭圆曲线密码学、表观遗传学），"
    "将该学科分支也作为一个核心词\n"
    "4. 保留完整短语，不做压缩或概括"
    "（如\"高维数据降维\"不要压缩为\"降维\"或\"高维降维\"）\n"
    "5. 即使两个术语看似属于同一大类（如CAR-T和CAR-NK），只要它们指代不同的"
    "具体技术/靶点，都必须分别提取\n"
    "6. 删除修饰性背景词：基于、用于、研究、优化、性能、设计、运用等\n"
    "7. 输出3-5个核心关键词，用顿号（、）分隔，不要解释、不要编号、不要换行\n\n"
    "输出格式示例：\n"
    "椭圆曲线密码学、离散对数问题、抗量子攻击\n"
    "DNA甲基化、表观遗传学、结直肠癌早筛、抑癌基因\n"
    "图神经网络、分子性质预测、药物发现、先导物优化"
)

_REGULAR_SYSTEM_PROMPT = (
    "你是一个科研文献检索专家。你的任务是从课题描述中提取5-8个辅助检索词，"
    "用于补充核心关键词的文献检索覆盖面。\n\n"
    "【辅助关键词定义】\n"
    "辅助关键词 = 课题中使用的具体技术、材料、表征方法、应用场景、交叉领域。\n"
    "这些词比核心关键词更具体一层（但不能拆成单字词），有独立的论文搜索价值。\n"
    "  对：氮掺杂碳、原位XAS、纠缠光子对、大气湍流、CpG岛\n"
    "  错：数论（学科大类）、岩石学（学科大类）、计算（单字）、分析（无意义词）\n\n"
    "严格遵循以下规则：\n"
    "1. 提取具体的技术方法、材料体系、表征手段、应用场景——每个词都应有独立的检索价值\n"
    "2. 不要输出学科大类标签（如\"数论\"\"计算生物学\"\"岩石学\"\"地球化学\"）\n"
    "3. 保留完整短语，既不拆分成单字词也不过度合并"
    "（如\"大气湍流\"不要拆成\"大气\"和\"湍流\"；\"扫描隧道显微镜\"不要缩成\"STM\"）\n"
    "4. 即使两个术语看似属于同一大类（如CAR-T和CAR-NK、mRNA和siRNA），只要它们"
    "指代不同的具体技术/靶点/分子，都必须分别提取，绝不可用一个替代另一个\n"
    "5. 以中文输出为主，但学科通用英文缩写必须保留原始形式（如CAR-NK、CAR-T、TCR-T、"
    "mRNA、siRNA、PD-1、CTLA-4、CRISPR-Cas12a等不得翻译为中文）\n"
    "6. 输出5-8个关键词，用顿号（、）分隔，不要解释、不要编号、不要换行\n\n"
    "输出格式示例：\n"
    "特征值谱收缩、主成分分析、高维协方差估计、多元统计分析\n"
    "CpG岛、亚硫酸氢盐测序、启动子甲基化、抑癌基因沉默、甲基化标志物\n"
    "纠缠光子对、大气湍流、自由空间QKD、成码率、退相干机制"
)


def extract_core_keywords(topic: str) -> list[str]:
    """从科研课题描述中提取核心关键词。

    Args:
        topic: 科研课题描述

    Returns:
        核心关键词列表（2-4个），失败时返回空列表
    """
    config = load_config()
    api_key = config.get("deepseek", {}).get("api_key", "").strip()
    if not api_key:
        return []

    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": topic},
        ],
        "temperature": 0.3,
        "max_tokens": 50,
        "stream": False,
    }
    model = _get_model()
    payload["model"] = model
    if _is_v4_model(model):
        payload["thinking"] = _THINKING_DISABLED

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
    return _parse_response(content, max_results=5)


def extract_regular_keywords(topic: str) -> list[str]:
    """从科研课题描述中提取辅助关键词。

    与 core 关键词不同：regular 追求具体技术/材料/方法的覆盖面。

    Args:
        topic: 科研课题描述

    Returns:
        辅助关键词列表（5-8个），失败时返回空列表
    """
    config = load_config()
    api_key = config.get("deepseek", {}).get("api_key", "").strip()
    if not api_key:
        return []

    payload = {
        "messages": [
            {"role": "system", "content": _REGULAR_SYSTEM_PROMPT},
            {"role": "user", "content": topic},
        ],
        "temperature": 0.3,
        "max_tokens": 80,
        "stream": False,
    }
    model = _get_model()
    payload["model"] = model
    if _is_v4_model(model):
        payload["thinking"] = _THINKING_DISABLED

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
    return _parse_response(content, max_results=8)


def _parse_response(content: str, max_results: int = 10) -> list[str]:
    """Parse model output into a clean keyword list."""
    import re
    keywords = [k.strip() for k in re.split(r"[、,，/\n]", content) if k.strip()]
    result = []
    for k in keywords:
        has_cjk = any("一" <= c <= "鿿" for c in k)
        has_alpha = any(c.isascii() and c.isalpha() for c in k)
        if len(k) >= 2 and (has_cjk or has_alpha):
            k = k.rstrip("。，,、.!！?？;；：:")
            if len(k) >= 2:
                result.append(k)
    return result[:max_results]
