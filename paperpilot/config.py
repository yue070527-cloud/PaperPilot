"""配置管理，从 config.yaml 和环境变量读取。"""

import os
from pathlib import Path
import yaml


BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"


def _expand_env(value: str) -> str:
    """解析 ${ENV_VAR} 格式的环境变量引用。"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_var = value[2:-1]
        return os.environ.get(env_var, "")
    return value


def load_config(path: str | Path | None = None) -> dict:
    """加载配置文件，递归展开环境变量。

    所有代码统一调用此函数读取配置，不直接读 config.yaml。
    """
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return _expand_env(node)

    return walk(raw)


def save_config(updates: dict, path: str | Path | None = None) -> None:
    """保存配置到 config.yaml，保留已有结构，仅更新指定字段。"""
    path = Path(path or CONFIG_PATH)

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}
    else:
        current = {}

    def deep_merge(base: dict, updates: dict) -> None:
        for k, v in updates.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                deep_merge(base[k], v)
            else:
                base[k] = v

    deep_merge(current, updates)

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# 全局配置实例（进程启动时加载一次）
config = load_config()
