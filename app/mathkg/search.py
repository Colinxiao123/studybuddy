#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识点检索：归一化精确匹配 + 字段化 IDF 打分。

打分分级（取最大值）：
    name 精确        1.00
    alias 精确       0.95 × 歧义惩罚
    name 宽松精确    0.92（忽略「的」等虚词）
    alias 宽松       0.88
    标签精确         0.72 × 轻度歧义惩罚   ← 让「奇偶性」这类上位词召回同标签知识点
    文本相似         ≤0.78（字段化 IDF 打分，见下）

为什么不用余弦相似：余弦要除以文档向量的整体模长，而 statement 长达 200 字、
贡献几百个 n-gram，会把「只命中一个标签」的短匹配稀释到 0.28 以下，
导致「奇偶性」这类上位词检索失败。改用字段化饱和归一化后长文本不再拖累短匹配：

    score = Σ_g idf(g)·w_kp(g) / (K + Σ_g idf(g)·W_max)

命中 name 的词能快速饱和（W_max=3.0），命中 statement 的词贡献极小，
符合「名称 > 别名 > 标签 > 陈述」的权重直觉。
"""

from __future__ import annotations

import math

from app.mathkg.store import KG, get_kg
from app.mathkg.textutil import ngrams, normalize, normalize_loose

EXACT_SCORES = {"name": 1.00, "alias": 0.95, "name_loose": 0.92,
                "alias_loose": 0.88, "tag": 0.72}
TEXT_GAIN = 1.6
TEXT_CAP = 0.78
W_MAX = 3.0     # name 字段权重
K_SAT = 15.0    # 饱和常数


def _idf(kg: KG, gram: str) -> float:
    n = len(kg.kps)
    return math.log(1.0 + n / (1.0 + kg.df.get(gram, 0)))


def _fielded_score(kg: KG, kp_id: str, query_grams: dict[str, float]) -> tuple[float, list[str]]:
    """字段化 IDF 打分，返回 (0–1 分, 贡献最大的命中词)。"""
    profile = kg.profile.get(kp_id) or {}
    num = 0.0
    den = K_SAT
    contrib: list[tuple[float, str]] = []
    for gram, qw in query_grams.items():
        gram_idf = _idf(kg, gram)
        den += gram_idf * W_MAX
        weight = profile.get(gram)
        if weight:
            value = gram_idf * weight * qw
            num += value
            contrib.append((value, gram))
    if num <= 0:
        return 0.0, []
    contrib.sort(reverse=True)
    return num / den, [g for _, g in contrib[:5]]


def _ambiguity_penalty(count: int, factor: float = 1.0) -> float:
    """同名/同标签指向多个知识点时降权。标签天然被共享，惩罚系数更小。"""
    if count <= 1:
        return 1.0
    return 1.0 / (1.0 + factor * math.log2(count))


def _search_core(query: str, topk: int, module: str | None,
                 kp_ids: list[str] | None, kg: KG) -> list[dict]:
    q_norm = normalize(query)
    if not q_norm:
        return []
    q_loose = normalize_loose(query)
    grams = ngrams(q_norm, 2, 4) or [q_norm]
    query_grams = {g: 1.0 for g in grams}

    if kp_ids is not None:
        candidates = [kg.by_id[i] for i in kp_ids if i in kg.by_id]
    elif module:
        candidates = kg.of_module(module)
    else:
        candidates = kg.kps

    name_hits = kg.name_index.get(q_norm, ())
    alias_hits = kg.alias_index.get(q_norm, ())
    tag_hits = kg.tag_index.get(q_norm, ())
    loose_hits = kg.loose_index.get(q_loose, ()) if q_loose else ()

    results: list[dict] = []
    for kp in candidates:
        kp_id = kp["id"]
        # kind 记录「这一票是怎么来的」：exact（图谱显式声明的名称/别名/标签）
        # 还是 text（模糊字面相似）。下游做上下文校验时要区别对待——
        # 图谱声明的别名要信任，模糊匹配才需要求证明出处。
        best, evidence, kind = 0.0, [], "text"

        if kp_id in name_hits:
            best, kind = EXACT_SCORES["name"], "name"
            evidence.append("名称精确匹配")
        elif kp_id in alias_hits:
            best, kind = EXACT_SCORES["alias"] * _ambiguity_penalty(len(alias_hits)), "alias"
            evidence.append("别名精确匹配" + (f"（{len(alias_hits)} 个知识点共享，已降权）"
                                          if len(alias_hits) > 1 else ""))
        elif kp_id in loose_hits:
            is_name = q_loose == normalize_loose(kp.get("name", ""))
            best = EXACT_SCORES["name_loose" if is_name else "alias_loose"]
            kind = "name_loose" if is_name else "alias_loose"
            evidence.append("名称宽松匹配（忽略虚词）" if is_name else "别名宽松匹配")
        elif kp_id in tag_hits:
            best, kind = EXACT_SCORES["tag"] * _ambiguity_penalty(len(tag_hits), factor=0.25), "tag"
            evidence.append(f"标签精确匹配（同标签 {len(tag_hits)} 个知识点）")

        raw, terms = _fielded_score(kg, kp_id, query_grams)
        text_score = min(TEXT_CAP, raw * TEXT_GAIN)
        if terms and text_score >= best:
            evidence = [f"文本相似：{' / '.join(terms[:3])}"]
            kind = "text"
        score = max(best, text_score)
        if score <= 0:
            continue

        tag_overlap = sum(1 for t in (kp.get("tags") or []) if normalize(str(t)) in q_norm)
        if tag_overlap and score < 1.0:
            score = min(1.0, score + min(0.08, 0.04 * tag_overlap))
            evidence.append(f"标签命中 {tag_overlap} 个")

        results.append({"kp": kp, "score": round(score, 4), "kind": kind,
                        "evidence": evidence, "matched_terms": terms})

    results.sort(key=lambda r: (-r["score"], r["kp"]["id"]))
    return results[:topk]


def search(query: str, topk: int = 10, module: str | None = None,
           kp_ids: list[str] | None = None, kg: KG | None = None) -> list[dict]:
    """检索知识点。返回 [{kp, score, evidence, matched_terms}]，按分数降序。"""
    return _search_core(query, topk, module, kp_ids, kg or get_kg())


def search_multi(queries: list[str], topk_each: int = 5, topk: int | None = None,
                 module: str | None = None, kg: KG | None = None) -> list[dict]:
    """多关键词检索并合并（同一知识点取最高分）。用于题干整体检索。"""
    kg = kg or get_kg()
    merged: dict[str, dict] = {}
    for q in queries:
        if not q or not str(q).strip():
            continue
        for hit in _search_core(str(q), topk_each, module, None, kg):
            kp_id = hit["kp"]["id"]
            prev = merged.get(kp_id)
            if prev is None or prev["score"] < hit["score"]:
                merged[kp_id] = hit
            elif hit["evidence"]:
                prev["evidence"] = list(dict.fromkeys(prev["evidence"] + hit["evidence"]))
    out = sorted(merged.values(), key=lambda r: (-r["score"], r["kp"]["id"]))
    return out[:topk] if topk else out
