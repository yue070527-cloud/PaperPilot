"""关键词提取准确率评测脚本（随机抽样 + 限流保护）。"""
import json
import random
import re
import time
from paperpilot.core_extractor import extract_core_keywords, extract_regular_keywords


def parse_test_file(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    topics = []
    pattern = r"课题(\d+):\s*(.+?)\n详细介绍:\s*(.+?)(?=\n\n|\n课题|\Z)"
    for m in re.finditer(pattern, text, re.DOTALL):
        tid = int(m.group(1))
        title = m.group(2).strip()
        desc = m.group(3).strip().replace("\n", " ")
        topics.append({"id": tid, "title": title, "desc": desc})
    return topics


def parse_ans_file(path: str) -> dict[int, dict]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    blocks = re.split(r"\n\n+", text.strip())
    result = {}
    for block in blocks:
        id_m = re.match(r"课题(\d+):", block)
        if not id_m:
            continue
        tid = int(id_m.group(1))
        entry = {"primary": [], "secondary": [], "regular": []}
        for tier, key in [("主关键词", "primary"), ("副关键词", "secondary"), ("普通关键词", "regular")]:
            m = re.search(rf"{tier}:\s*(.+?)(?=\n(?:主关键词|副关键词|普通关键词)|\Z)", block, re.DOTALL)
            if m:
                kws = [k.strip() for k in re.split(r"[;；]", m.group(1)) if k.strip()]
                entry[key] = kws
        result[tid] = entry
    return result


def normalize(kw: str) -> str:
    """标准化"""
    return kw.lower().strip()


def match_keyword(ai_kw: str, ans_kws: set[str]) -> bool:
    """检查 AI 关键词是否命中答案集中的任一关键词。

    三级匹配策略：
    1. 精确匹配（大小写不敏感）
    2. 包含匹配（一方是另一方的子串）
    3. 去后缀匹配（去掉"问题""技术""方法""研究""分析"等常见后缀后比较）
    """
    ai_norm = normalize(ai_kw)
    # 去掉常见后缀
    ai_trimmed = ai_norm
    for suffix in ["问题", "技术", "方法", "研究", "分析", "模型", "理论", "系统", "材料"]:
        if ai_trimmed.endswith(suffix) and len(ai_trimmed) - len(suffix) >= 4:
            ai_trimmed = ai_trimmed[:-len(suffix)]
            break

    for ak in ans_kws:
        ak_norm = normalize(ak)
        # 1. 精确匹配
        if ai_norm == ak_norm:
            return True
        # 2. 包含匹配
        if len(ai_norm) >= 3 and (ai_norm in ak_norm or ak_norm in ai_norm):
            return True
        # 3. 去后缀匹配
        ak_trimmed = ak_norm
        for suffix in ["问题", "技术", "方法", "研究", "分析", "模型", "理论", "系统", "材料"]:
            if ak_trimmed.endswith(suffix) and len(ak_trimmed) - len(suffix) >= 4:
                ak_trimmed = ak_trimmed[:-len(suffix)]
                break
        if len(ai_trimmed) >= 3 and len(ak_trimmed) >= 3:
            if ai_trimmed == ak_trimmed or ai_trimmed in ak_trimmed or ak_trimmed in ai_trimmed:
                return True
    return False


def extract_with_retry(text: str, max_retries: int = 3) -> list[tuple[str, float]]:
    """带重试的关键词提取，API 失败 3 次后回退到 jieba。"""
    from paperpilot.keywords import extract_keywords

    for attempt in range(max_retries):
        try:
            # 核心关键词
            core = extract_core_keywords(text) if any("一" <= c <= "鿿" for c in text) else []
            # 普通关键词
            regular = extract_regular_keywords(text)
            if not regular:
                raise RuntimeError("Regular extraction returned empty")

            # 去重
            core_lower = {kw.lower() for kw in core}
            regular = [kw for kw in regular if kw.lower() not in core_lower]

            result = [(kw, 1.0) for kw in core] + [(kw, 0.75) for kw in regular]
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))  # 递增延迟
            else:
                # 最终回退到 jieba
                print(f"    [回退] API 3次失败，使用 jieba 提取", flush=True)
                kws = extract_keywords(text, top_n=8)
                return [(kw, 0.75) for kw in kws]
    return []


def evaluate_single(ai_weighted, expected):
    ai_core = [normalize(kw) for kw, w in ai_weighted if w >= 1.0]
    ai_regular = [normalize(kw) for kw, w in ai_weighted if 0 < w < 1.0]
    ai_all = ai_core + ai_regular

    ans_core = set(expected["primary"] + expected["secondary"])
    ans_regular = set(expected["regular"])
    ans_all = ans_core | ans_regular

    # 用模糊匹配计算命中数
    tp_all = sum(1 for ak in ai_all if match_keyword(ak, ans_all))
    precision_all = tp_all / len(ai_all) if ai_all else 0
    recall_all = sum(1 for ak in ans_all if any(match_keyword(normalize(ak), {k}) is False and match_keyword(k, {normalize(ak)}) for k in ai_all))
    # 简化recall: answer中有多少被AI命中
    ans_matched = 0
    for ak in ans_all:
        ak_norm = normalize(ak)
        found = False
        for ai_kw in ai_all:
            if match_keyword(ai_kw, {ak_norm}):
                found = True
                break
        if found:
            ans_matched += 1
    recall_all = ans_matched / len(ans_all) if ans_all else 0

    tp_core = sum(1 for ak in ai_core if match_keyword(ak, ans_core))
    precision_core = tp_core / len(ai_core) if ai_core else 0
    ans_core_matched = 0
    for ak in ans_core:
        ak_norm = normalize(ak)
        if any(match_keyword(ai_kw, {ak_norm}) for ai_kw in ai_core):
            ans_core_matched += 1
    recall_core = ans_core_matched / len(ans_core) if ans_core else 0

    tp_reg = sum(1 for ak in ai_regular if match_keyword(ak, ans_regular))
    precision_reg = tp_reg / len(ai_regular) if ai_regular else 0
    ans_reg_matched = 0
    for ak in ans_regular:
        ak_norm = normalize(ak)
        if any(match_keyword(ai_kw, {ak_norm}) for ai_kw in ai_regular):
            ans_reg_matched += 1
    recall_reg = ans_reg_matched / len(ans_regular) if ans_regular else 0

    return {
        "ai_core": list(set(ai_core)), "ai_regular": list(set(ai_regular)),
        "ans_core": [normalize(k) for k in ans_core], "ans_regular": [normalize(k) for k in ans_regular],
        "tp_all": tp_all, "precision_all": precision_all, "recall_all": recall_all,
        "tp_core": tp_core, "precision_core": precision_core, "recall_core": recall_core,
        "tp_reg": tp_reg, "precision_reg": precision_reg, "recall_reg": recall_reg,
    }


def main():
    random.seed(2026)

    print("加载数据...", flush=True)
    all_topics = parse_test_file("test.txt")
    answers = parse_ans_file("ans.txt")

    # 随机抽取 30 个
    sample = random.sample(all_topics, 15)
    sample.sort(key=lambda t: t["id"])
    print(f"随机抽样 15 个课题 (seed=42): {[t['id'] for t in sample]}", flush=True)

    sums = {"p_all": 0, "r_all": 0, "p_core": 0, "r_core": 0, "p_reg": 0, "r_reg": 0}
    count = 0
    failures = 0
    detail_results = {}

    print("开始评测（每个课题间隔 1.5s 防限流）...", flush=True)
    for i, t in enumerate(sample):
        tid = t["id"]
        input_text = f"{t['title']}\n{t['desc']}"

        weighted = extract_with_retry(input_text)

        if not weighted:
            print(f"  [{i+1}/15] 课题{tid}: 提取完全失败", flush=True)
            failures += 1
            continue

        r = evaluate_single(weighted, answers[tid])
        sums["p_all"] += r["precision_all"]
        sums["r_all"] += r["recall_all"]
        sums["p_core"] += r["precision_core"]
        sums["r_core"] += r["recall_core"]
        sums["p_reg"] += r["precision_reg"]
        sums["r_reg"] += r["recall_reg"]
        count += 1

        detail_results[tid] = {
            "ai_core": r["ai_core"],
            "ai_regular": r["ai_regular"],
            "ans_primary": answers[tid]["primary"],
            "ans_secondary": answers[tid]["secondary"],
            "ans_regular": answers[tid]["regular"],
            "precision_all": round(r["precision_all"], 4),
            "recall_all": round(r["recall_all"], 4),
        }

        # 分批输出
        if (i + 1) % 5 == 0:
            rolling_p = sums["p_all"] / count
            rolling_r = sums["r_all"] / count
            f1 = 2 * rolling_p * rolling_r / (rolling_p + rolling_r) if (rolling_p + rolling_r) > 0 else 0
            print(f"  [{i+1}/15] 当前累计 P={rolling_p:.1%} R={rolling_r:.1%} F1={f1:.1%}", flush=True)

        # 防限流延迟
        time.sleep(1.5)

    print(f"\n{'='*60}")
    print(f"评测完成: {count} 个课题 (失败 {failures})")
    c = max(count, 1)

    pa, ra = sums["p_all"] / c, sums["r_all"] / c
    f1_all = 2 * pa * ra / (pa + ra) if (pa + ra) > 0 else 0
    print(f"\n【整体匹配】P={pa:.1%}  R={ra:.1%}  F1={f1_all:.1%}")

    pc, rc = sums["p_core"] / c, sums["r_core"] / c
    f1_core = 2 * pc * rc / (pc + rc) if (pc + rc) > 0 else 0
    print(f"【核心匹配】P={pc:.1%}  R={rc:.1%}  F1={f1_core:.1%}")

    pr, r_reg = sums["p_reg"] / c, sums["r_reg"] / c
    f1_reg = 2 * pr * r_reg / (pr + r_reg) if (pr + r_reg) > 0 else 0
    print(f"【普通匹配】P={pr:.1%}  R={r_reg:.1%}  F1={f1_reg:.1%}")

    with open("test_results.json", "w", encoding="utf-8") as f:
        json.dump({"summary": {"p_all": pa, "r_all": ra, "f1_all": f1_all,
                               "p_core": pc, "r_core": rc, "f1_core": f1_core,
                               "p_reg": pr, "r_reg": r_reg, "f1_reg": f1_reg,
                               "sample_size": count, "failures": failures},
                   "details": detail_results}, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已写入 test_results.json")


if __name__ == "__main__":
    main()
