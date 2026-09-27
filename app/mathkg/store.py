#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱快照加载 + 内存索引（图结构索引 + 检索索引）。"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from app.config import BUNDLE_DIR, SNAPSHOT_PATH
from app.mathkg.textutil import latex_tokens, ngrams, normalize, normalize_loose

# 各字段在检索打分中的权重
FIELD_WEIGHT = {"name": 3.0, "alias": 2.0, "tag": 1.5, "latex": 0.5, "statement": 0.35}


class KG:
    """知识图谱只读视图。全部索引在构造时一次性建好（544 节点，毫秒级）。"""

    def __init__(self, snapshot: dict):
        self.meta: dict = snapshot.get("meta", {})
        self.modules: list[dict] = snapshot.get("modules", [])
        self.kps: list[dict] = snapshot.get("kps", [])
        self.edges: list[dict] = snapshot.get("edges", [])
        self.by_id: dict[str, dict] = {k["id"]: k for k in self.kps}

        # ---- 图结构索引 ----
        self.out: dict[str, list[dict]] = defaultdict(list)    # id → 出边
        self.inc: dict[str, list[dict]] = defaultdict(list)    # id → 入边
        self.pre_out: dict[str, list[dict]] = defaultdict(list)  # 我作为前置 → 后继
        self.pre_in: dict[str, list[dict]] = defaultdict(list)   # 我的前置 → 我
        for e in self.edges:
            self.out[e["from"]].append(e)
            self.inc[e["to"]].append(e)
            if e.get("rel") == "PREREQUISITE_OF":
                self.pre_out[e["from"]].append(e)
                self.pre_in[e["to"]].append(e)

        # ---- 检索索引 ----
        self.profile: dict[str, dict[str, float]] = {}
        self.norm: dict[str, float] = {}
        self.df: dict[str, int] = defaultdict(int)
        self.name_index: dict[str, set[str]] = defaultdict(set)
        self.alias_index: dict[str, set[str]] = defaultdict(set)
        self.loose_index: dict[str, set[str]] = defaultdict(set)
        self.tag_index: dict[str, set[str]] = defaultdict(set)

        for kp in self.kps:
            self._add_kp(kp)
        for gram_map in (self.profile,):
            for kp_id, grams in gram_map.items():
                acc = 0.0
                for g, w in grams.items():
                    self.df[g] += 1
                    acc += w * w
                self.norm[kp_id] = math.sqrt(acc) or 1.0

        # ---- 主题标签（topical tags）----
        # 标签里混有两类东西：一类是知识主题（奇偶性、单调性），一类是题面用语
        # （取值范围、综合应用）。后者在任何题里都可能出现，用来做召回会大量误报。
        # 判别办法（数据驱动，无需人工维护停用词表）：把标签与「知识点名称的 n-gram」
        # 求交——知识主题必然会出现在某些知识点名称里，题面用语不会。
        name_grams: set[str] = set()
        for kp in self.kps:
            name_grams |= set(ngrams(normalize(kp.get("name", "")), 2, 8))
        self.name_grams = name_grams
        self.topical_tags = {t for t in self.tag_index if len(t) >= 3 and t in name_grams}

    # ------------------------------------------------------------------ 索引
    def _add_kp(self, kp: dict) -> None:
        kp_id = kp["id"]
        grams: dict[str, float] = {}

        def feed(text: str, weight: float, use_ngram: bool = True) -> None:
            if not text:
                return
            normalized = normalize(text)
            if use_ngram:
                for g in ngrams(normalized, 2, 4):
                    if grams.get(g, 0.0) < weight:
                        grams[g] = weight
            else:
                for t in latex_tokens(text):
                    if grams.get(t, 0.0) < weight:
                        grams[t] = weight

        feed(kp.get("name", ""), FIELD_WEIGHT["name"])
        for alias in kp.get("aliases") or []:
            feed(str(alias), FIELD_WEIGHT["alias"])
            if str(alias).strip():
                self.alias_index[normalize(str(alias))].add(kp_id)
                self.loose_index[normalize_loose(str(alias))].add(kp_id)
        for tag in kp.get("tags") or []:
            feed(str(tag), FIELD_WEIGHT["tag"])
            self.tag_index[normalize(str(tag))].add(kp_id)
        feed(kp.get("statement", ""), FIELD_WEIGHT["statement"])
        feed(kp.get("latex") or "", FIELD_WEIGHT["latex"], use_ngram=False)

        name_norm = normalize(kp.get("name", ""))
        if name_norm:
            self.name_index[name_norm].add(kp_id)
            self.loose_index[normalize_loose(kp.get("name", ""))].add(kp_id)

        self.profile[kp_id] = grams

    # ------------------------------------------------------------------ 查询
    def get(self, kp_id: str) -> dict | None:
        return self.by_id.get(kp_id)

    def of_module(self, code: str) -> list[dict]:
        return [k for k in self.kps if k.get("module") == code]

    def of_section(self, section_id: str) -> list[dict]:
        return [k for k in self.kps if k.get("sec_id") == section_id]

    def prereqs(self, kp_id: str, strength: str | None = "required") -> list[dict]:
        """直接前置。strength=None 表示不限强弱。"""
        return [e for e in self.pre_in.get(kp_id, [])
                if strength is None or e.get("strength") == strength]

    def successors(self, kp_id: str, strength: str | None = "required") -> list[dict]:
        return [e for e in self.pre_out.get(kp_id, [])
                if strength is None or e.get("strength") == strength]

    def related(self, kp_id: str) -> list[dict]:
        """横向关联 / 派生关系（不含前置）。"""
        return [e for e in self.out.get(kp_id, [])
                if e.get("rel") in ("RELATED_TO", "DERIVED_FROM")] + \
               [e for e in self.inc.get(kp_id, [])
                if e.get("rel") in ("RELATED_TO", "DERIVED_FROM")]

    # ------------------------------------------------------------- 教材原文
    def segment_excerpt(self, kp: dict, window: int = 1600) -> str:
        """返回该知识点在教材分段中的原文片段（优先以知识点名定位窗口）。"""
        rel = kp.get("segment_file") or ""
        if not rel:
            return ""
        # 片段属于只读资源：源码运行时 BUNDLE_DIR 就是项目根，打包后则是解包目录。
        # 不能用 ROOT —— 打包后 ROOT 是 exe 所在目录，那里只有可写数据。
        path = BUNDLE_DIR / rel
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        anchor = text.find(kp.get("name", ""))
        if anchor < 0:
            for alias in kp.get("aliases") or []:
                anchor = text.find(str(alias))
                if anchor >= 0:
                    break
        if anchor < 0:
            return text[:window]
        start = max(0, anchor - window // 3)
        return text[start:start + window]

    def stats(self) -> dict:
        return {
            "kp_count": len(self.kps),
            "edge_count": len(self.edges),
            "module_count": len(self.modules),
            "modules": self.modules,
            "generated_at": self.meta.get("generated_at", ""),
            "status_distribution": self.meta.get("status_distribution", {}),
        }


@lru_cache(maxsize=1)
def get_kg(path: str | None = None) -> KG:
    """加载快照（进程内缓存）。快照缺失时给出明确指引。"""
    snapshot_path = Path(path) if path else SNAPSHOT_PATH
    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"图谱快照不存在：{snapshot_path}\n"
            f"请先运行：python -m app.mathkg.build_index"
        )
    return KG(json.loads(snapshot_path.read_text(encoding="utf-8")))


def reload_kg(path: str | None = None) -> KG:
    get_kg.cache_clear()
    return get_kg(path)
