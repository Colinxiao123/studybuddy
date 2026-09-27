#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用层配置：路径常量 + .env 加载。

配置来源（后者覆盖前者）：
  pipeline/.env   —— 已有的 MinerU 凭据（复用，不重复配置）
  app/.env        —— 应用层新增配置（视觉模型、文本模型等）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 打包成 exe（PyInstaller）后路径分两处：
#   BUNDLE_DIR —— 只读资源（static、图谱快照、教材片段…），在解包目录里
#   ROOT       —— 可写数据（数据库、题目图、.env），放在 exe 旁边，方便备份与迁移
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    ROOT = Path(sys.executable).resolve().parent
    APP_DIR = BUNDLE_DIR / "app"
    DATA_DIR = ROOT / "data"
    STATIC_DIR = APP_DIR / "static"
    SNAPSHOT_PATH = APP_DIR / "data" / "kg_snapshot.json"
else:
    APP_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = APP_DIR.parent
    ROOT = BUNDLE_DIR
    DATA_DIR = APP_DIR / "data"
    STATIC_DIR = APP_DIR / "static"
    SNAPSHOT_PATH = DATA_DIR / "kg_snapshot.json"

ASSETS_DIR = DATA_DIR / "assets"
DB_PATH = DATA_DIR / "db.sqlite"

ONTOLOGY_DIR = BUNDLE_DIR / "ontology"
STRUCTURE_PATH = ONTOLOGY_DIR / "data" / "structure.json"
SRC_DIR = ONTOLOGY_DIR / "data" / "src"
SEGMENTS_DIR = BUNDLE_DIR / "pipeline" / "data" / "segments"

# 用户配置文件：源码运行时是 app/.env，打包后是 exe 旁边的 .env。
# 网页端「设置」面板写的就是它。
ENV_PATH = ROOT / ".env" if FROZEN else APP_DIR / ".env"

# 图谱来源契约（与本体现范一致）
KP_TYPE_CN = {"concept": "概念", "theorem": "定理", "formula": "公式",
              "property": "性质", "rule": "法则", "method": "方法"}
IMPORTANCE_CN = {"know": "了解", "understand": "理解", "master": "掌握", "apply": "运用"}
DIFFICULTY_CN = {1: "识记", 2: "理解", 3: "应用", 4: "综合", 5: "拓展"}
MODULE_CN = {
    "SET": "集合与逻辑", "INEQ": "不等式", "FUNC": "函数", "TRIG": "三角函数",
    "VEC": "向量", "CPLX": "复数", "GEO": "立体几何", "PROB": "概率统计",
    "ANAG": "解析几何", "SEQ": "数列", "DERIV": "导数", "COMB": "计数原理",
}
BOOK_CN = {"BX1": "必修第一册", "BX2": "必修第二册", "BX3": "必修第三册",
           "XB1": "选择性必修第一册", "XB2": "选择性必修第二册"}


def _read_env(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


def load_env() -> dict[str, str]:
    """pipeline/.env ← 用户 .env（后者优先），并叠加真实环境变量。"""
    cfg = _read_env(ROOT / "pipeline" / ".env")
    cfg.update(_read_env(ENV_PATH))
    for key, value in os.environ.items():
        if value and key not in cfg:
            cfg[key] = value
    return cfg


_ENV: dict[str, str] | None = None


def env(key: str, default: str = "") -> str:
    global _ENV
    if _ENV is None:
        _ENV = load_env()
    return _ENV.get(key, default)


def reload_env() -> dict[str, str]:
    """丢弃缓存重新读取 .env。网页端保存设置后调用，无需重启进程。"""
    global _ENV
    _ENV = None
    return load_env()


def vision_config() -> dict:
    """视觉（识图）模型配置：默认 DeepSeek，走 OpenAI 兼容协议。"""
    return {
        "provider": env("VISION_PROVIDER", "deepseek"),
        "base": env("VISION_API_BASE", env("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")),
        "key": env("VISION_API_KEY", env("DEEPSEEK_API_KEY", "")),
        "model": env("VISION_MODEL", env("DEEPSEEK_VISION_MODEL", "deepseek-vl")),
        "timeout": int(env("VISION_TIMEOUT", "180")),
        # 推理型模型（如 deepseek-flash / deepseek-reasoner）会先花大量 token 思考，
        # 预算给小了会出现「content 为空、finish_reason=length」的假故障。
        "max_tokens": int(env("API_MAX_TOKENS", "8192")),
        # PDF 单次处理页数上限（<=0 表示不限）。PDF 是**按页分别识别**的，
        # 所以这个上限只影响耗时，不会影响上下文长度。
        "max_pages": int(env("VISION_MAX_PAGES", "50")),
        # 多页并发识别的线程数。这些调用是纯 I/O 等待，并发能成倍缩短总耗时。
        "concurrency": int(env("VISION_CONCURRENCY", "4")),
    }


def text_config() -> dict:
    """文本模型配置：缺省复用视觉模型的凭据。"""
    vision = vision_config()
    return {
        "base": env("TEXT_API_BASE", vision["base"]),
        "key": env("TEXT_API_KEY", vision["key"]),
        "model": env("TEXT_MODEL", env("DEEPSEEK_TEXT_MODEL", "deepseek-chat")),
        "timeout": int(env("TEXT_TIMEOUT", "180")),
        "max_tokens": int(env("API_MAX_TOKENS", "8192")),
    }


def task_config() -> dict:
    """逐题处理的并发度。每题一次模型调用，纯 I/O 等待，并发能成倍缩短总耗时。"""
    return {"concurrency": int(env("TASK_CONCURRENCY", "4"))}


def mineru_config() -> dict:
    return {
        "base": env("MINERU_API_BASE", "https://mineru.net/api/v4"),
        "token": env("MINERU_TOKEN", ""),
    }


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)


# 网页端「设置」面板允许写入的键。其余键（如 MinerU 凭据）不暴露在界面上，
# 但保存时会原样保留。
SETTINGS_KEYS = (
    "VISION_PROVIDER", "VISION_API_BASE", "VISION_API_KEY", "VISION_MODEL",
    "TEXT_MODEL", "API_MAX_TOKENS", "VISION_MAX_PAGES", "VISION_CONCURRENCY",
)


def mask_key(key: str) -> str:
    """密钥打码，只留头尾各四位，供界面回显。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "****"
    return f"{key[:4]}****{key[-4:]}"


def save_env(updates: dict[str, str]) -> Path:
    """把设置写回用户 .env。

    只覆盖 updates 里出现的键；值为空串表示「删除该键、回落到默认值」。
    文件里原有的注释与其它键原样保留，写完立即热重载。
    """
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    written: set[str] = set()
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key not in updates:
            out.append(line)
            continue
        value = updates[key].strip()
        if value:
            out.append(f"{key}={value}")
            written.add(key)
        # 值为空 -> 整行丢掉，等价于恢复默认

    for key, value in updates.items():
        value = value.strip()
        if key not in written and value:
            out.append(f"{key}={value}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    reload_env()
    return ENV_PATH


def settings_snapshot() -> dict:
    """网页端「设置」面板所需的当前配置（密钥打码）。"""
    v, t = vision_config(), text_config()
    return {
        "frozen": FROZEN,
        "env_path": str(ENV_PATH),
        "data_dir": str(DATA_DIR),
        "vision": {
            "provider": v["provider"],
            "base": v["base"],
            "model": v["model"],
            "key_masked": mask_key(v["key"]),
            "has_key": bool(v["key"]),
        },
        "text_model": t["model"],
        "text_key_masked": mask_key(t["key"]),
        "max_tokens": env("API_MAX_TOKENS", "8192"),
        "max_pages": env("VISION_MAX_PAGES", "50"),
        "concurrency": env("VISION_CONCURRENCY", "4"),
    }
