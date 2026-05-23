"""中文关键词 → 英文术语翻译模块。

使用 Hunyuan-MT 1.5 (1.8B) 模型进行翻译，替代原有的三层映射架构。
模型从 ModelScope 下载，本地离线推理。

加载时机: 首次调用 translate_terms() 时懒加载，全局单例。
"""

import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

_MODEL_DIR = str(
    Path.home()
    / ".cache/modelscope/models/Tencent-Hunyuan/HY-MT1___5-1___8B"
)

# Chinese → English prompt: 告诉模型只输出翻译结果，不加额外解释
_TRANSLATE_PROMPT = (
    "将以下文本翻译为英文，注意只需要输出翻译后的结果，不要额外解释：\n\n"
    "{text}"
)

_model = None
_tokenizer = None


def _get_model_and_tokenizer():
    global _model, _tokenizer
    if _model is None:
        _tokenizer = AutoTokenizer.from_pretrained(
            _MODEL_DIR,
            trust_remote_code=True,
            local_files_only=True,
        )
        _model = AutoModelForCausalLM.from_pretrained(
            _MODEL_DIR,
            trust_remote_code=True,
            local_files_only=True,
            dtype=torch.float32,
        )
        _model.eval()
    return _model, _tokenizer


def translate_terms(chinese_terms: list[str]) -> list[str]:
    """将中文关键词列表翻译为英文术语列表。

    Args:
        chinese_terms: 中文关键词列表（如 ["钙钛矿太阳能电池", "基因治疗"]）

    Returns:
        英文术语列表（与输入一一对应），翻译失败的项返回空字符串
    """
    if not chinese_terms:
        return []

    model, tokenizer = _get_model_and_tokenizer()

    results = []
    for term in chinese_terms:
        # 如果输入已是纯 ASCII（英文字母/数字/符号），直接保留
        stripped = term.strip()
        if not stripped:
            results.append("")
            continue
        if all(ord(c) < 128 for c in stripped):
            results.append(stripped)
            continue

        prompt = _TRANSLATE_PROMPT.format(text=stripped)
        messages = [{"role": "user", "content": prompt}]

        encoded = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
        )
        input_ids = encoded['input_ids']
        with torch.no_grad():
            outputs = model.generate(
                input_ids,
                max_new_tokens=64,
                do_sample=False,
                temperature=None,
                top_p=None,
            )

        # 解码时跳过 prompt 部分，只取生成的新 token
        generated = outputs[0][input_ids.shape[1]:]
        translation = tokenizer.decode(generated, skip_special_tokens=True).strip()

        # 后处理：去除模型可能残留的填充标记
        translation = translation.replace("<|hy_place|>", "")
        translation = translation.replace("holder", "")
        translation = translation.replace("no_", "")
        translation = translation.strip()

        # 如果翻译失败（结果仍包含中文或空），返回空
        if not translation or any("一" <= c <= "鿿" for c in translation):
            results.append("")
        else:
            results.append(translation)

    return results
