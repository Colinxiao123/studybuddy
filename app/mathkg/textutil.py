#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文/数学文本归一化与 n-gram 工具。

设计说明：不依赖任何分词库，用「归一化 + 中文 n-gram + IDF 加权」实现检索，
这样既没有外部依赖，也不必处理 FTS5 对 CJK 不切词的坑（544 个知识点规模下
内存倒排索引的构建耗时在百毫秒级）。
"""

from __future__ import annotations

import re
import unicodedata

# 需要在匹配时忽略的噪声字符（LaTeX 标记、数学分隔符、常见虚词标点）
_NOISE = str.maketrans({c: "" for c in "$\\{}_^~（）()【】[]，。、；：！？“”‘’\"'`·—…《》<>|"})
_SPACE = re.compile(r"\s+")

_MATH_DELIM = re.compile(r"\$+([^$]*)\$+")
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\s*")
_MD_MARK = re.compile(r"(\*\*|__|\*|_|`|^#{1,6}\s*|^>\s*)", re.M)


def to_plain(text: str) -> str:
    """把 stem_md 压成「检索用纯文本」：去 $ 定界符、LaTeX 命令名与 markdown 记号。

    只服务于检索上下文（stem_plain），不用于展示 ——
    展示一律走 stem_md + KaTeX 渲染，两者不能混。
    例：`已知 $f(x)=x^2-4x+3$，求 \\frac{a}{b} 的范围`
        → `已知 f(x)=x^2-4x+3，求 a b 的范围`
    """
    if not text:
        return ""
    t = _MATH_DELIM.sub(r"\1", text)
    t = _LATEX_CMD.sub(" ", t)
    t = t.translate(str.maketrans({"{": "", "}": ""}))
    t = _MD_MARK.sub("", t)
    return _SPACE.sub(" ", t).strip()


def normalize(text: str) -> str:
    """NFKC（全角→半角）+ 小写 + 去空白 + 去噪声字符。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower()
    t = t.translate(_NOISE)
    return _SPACE.sub("", t)


def normalize_loose(text: str) -> str:
    """在 normalize 基础上再去掉「的/之/其」等虚词，用于同义匹配。

    例：「函数的单调性」与「函数单调性」归一后相同。
    """
    t = normalize(text)
    for ch in "的之其与和及等":
        t = t.replace(ch, "")
    return t


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def ngrams(text: str, nmin: int = 2, nmax: int = 4) -> list[str]:
    """滑窗生成 n-gram。对无空格的连续中文串尤其有效。"""
    if not text:
        return []
    out: list[str] = []
    length = len(text)
    for n in range(nmin, min(nmax, length) + 1):
        for i in range(length - n + 1):
            out.append(text[i:i + n])
    return out


_TERM_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def terms(text: str) -> list[str]:
    """按非字母数字汉字切分为词条，并过滤单字（单字噪声太大）。"""
    return [t for t in _TERM_SPLIT.split(normalize(text)) if len(t) >= 2]


def latex_tokens(text: str) -> list[str]:
    """从 LaTeX 中提取有意义的结构化记号，如 \\frac、\\overrightarrow、sin、delta。"""
    if not text:
        return []
    out: list[str] = []
    for m in re.finditer(r"\\([a-zA-Z]+)", text):
        out.append("\\" + m.group(1).lower())
    for m in re.finditer(r"[a-zA-Z]{2,}", text):
        out.append(m.group(0).lower())
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
