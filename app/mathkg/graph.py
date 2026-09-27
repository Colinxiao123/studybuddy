#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图算法：前置子图遍历、拓扑排序、学习路径、根因下钻。

关系语义（见《本体规范》§4.3）：
    A -PREREQUISITE_OF-> B   表示「先掌握 A，才能学 B」
    strength=required        强前置（缺之无法理解）
    strength=recommended     弱前置（有助理解）
"""

from __future__ import annotations

from collections import deque

from app.mathkg.store import KG, get_kg

IMPORTANCE_WEIGHT = {"know": 0.55, "understand": 0.75, "master": 1.0, "apply": 1.0}


# ------------------------------------------------------------------ 子图遍历
def prereq_closure(kg: KG, kp_id: str, max_depth: int = 6,
                   strength: str | None = "required") -> dict[str, dict]:
    """反向遍历 PREREQUISITE_OF，返回 {前置id: {depth, via}}（含间接前置）。"""
    seen: dict[str, dict] = {}
    queue: deque[tuple[str, int]] = deque([(kp_id, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in kg.pre_in.get(current, []):
            if strength and edge.get("strength") != strength:
                continue
            src = edge["from"]
            if src in seen:
                continue
            seen[src] = {"depth": depth + 1, "via": edge}
            queue.append((src, depth + 1))
    return seen


def descendants(kg: KG, kp_id: str, max_depth: int = 6,
                strength: str | None = "required") -> dict[str, int]:
    """正向遍历：该知识点是哪些知识点的前置（返回 {id: depth}）。"""
    seen: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque([(kp_id, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in kg.pre_out.get(current, []):
            if strength and edge.get("strength") != strength:
                continue
            dst = edge["to"]
            if dst in seen:
                continue
            seen[dst] = depth + 1
            queue.append((dst, depth + 1))
    return seen


def topo_sort(kg: KG, subset: set[str]) -> list[str]:
    """对子集内的 PREREQUISITE_OF 边做拓扑排序（Kahn）。同层按模块顺序、难度排序。"""
    indeg = {n: 0 for n in subset}
    adjacency: dict[str, list[str]] = {n: [] for n in subset}
    for n in subset:
        for edge in kg.pre_out.get(n, []):
            if edge["to"] in subset:
                adjacency[n].append(edge["to"])
                indeg[edge["to"]] += 1

    def sort_key(n: str):
        kp = kg.by_id.get(n, {})
        return (kp.get("module", ""), kp.get("sec_id", ""), kp.get("difficulty", 0), n)

    ready = sorted([n for n, d in indeg.items() if d == 0], key=sort_key)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(adjacency[node], key=sort_key):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=sort_key)
    # 理论上有环才会漏，兜底追加
    order += [n for n in sorted(subset, key=sort_key) if n not in order]
    return order


# ------------------------------------------------------------------ 学习路径
def learning_path(kg: KG, target: str, known: set[str] | None = None,
                  max_depth: int = 6, include_recommended: bool = True,
                  kg_limit: int = 40) -> dict:
    """返回「从零到目标」的补课路径：缺失前置的拓扑序 + 每步理由。"""
    if target not in kg.by_id:
        raise KeyError(f"知识点不存在：{target}")
    known = set(known or ())
    known.add(target)

    required = prereq_closure(kg, target, max_depth, "required")
    missing = {i for i in required if i not in known}
    optional: set[str] = set()
    if include_recommended:
        recommended = prereq_closure(kg, target, max_depth, "recommended")
        optional = {i for i in recommended if i not in known and i not in missing}

    if not missing and not optional:
        return {"target": kg.by_id[target], "steps": [], "total": 0,
                "note": "目标知识点没有未掌握的前置，可以直接开始学习。"}

    missing_order = topo_sort(kg, missing)[:kg_limit]
    ordered = missing_order + topo_sort(kg, optional)[:kg_limit]

    steps: list[dict] = []
    for idx, kp_id in enumerate(ordered):
        kp = kg.by_id[kp_id]
        # 理由：优先取「它 → 路径中后继」的前置理由
        reason, unlocks = "", []
        for edge in kg.pre_out.get(kp_id, []):
            if edge["to"] == target:
                reason, unlocks = edge.get("reason", ""), unlocks + [edge["to"]]
            elif edge["to"] in missing or edge["to"] in optional:
                if not reason:
                    reason = edge.get("reason", "")
                unlocks.append(edge["to"])
        steps.append({
            "order": idx + 1,
            "kp_id": kp_id,
            "name": kp.get("name", ""),
            "module": kp.get("module", ""),
            "module_name": kp.get("module_name", ""),
            "section_name": kp.get("section_name", ""),
            "difficulty": kp.get("difficulty"),
            "difficulty_cn": kp.get("difficulty_cn", ""),
            "importance_cn": kp.get("importance_cn", ""),
            "statement": kp.get("statement", ""),
            "strength": "required" if kp_id in missing else "recommended",
            "reason": reason,
            "unlocks": [u for u in dict.fromkeys(unlocks)][:4],
            "unlocks_names": [kg.by_id[u]["name"] for u in dict.fromkeys(unlocks) if u in kg.by_id][:4],
        })

    return {
        "target": kg.by_id[target],
        "steps": steps,
        "required_count": len(missing),
        "recommended_count": len(optional),
        "total": len(steps),
        "note": "",
    }


# ------------------------------------------------------------------ 根因诊断
def root_cause_candidates(kg: KG, kp_ids: list[str], max_depth: int = 3,
                          known: set[str] | None = None,
                          weakness: dict[str, int] | None = None,
                          topk: int = 5) -> list[dict]:
    """由错题涉及的知识点，沿强前置链上溯，排序出「最可能的根因」。

    打分 = 0.40·深度衰减 + 0.25·图中心性 + 0.20·基础度 + 0.15·课标权重 (+ 历史薄弱加成)
    """
    known = set(known or ())
    weakness = weakness or {}
    max_centrality = max([k.get("prereq_in_degree", 0) for k in kg.kps] or [1]) or 1
    max_weak = max(weakness.values()) if weakness else 1

    acc: dict[str, dict] = {}
    for kp_id in kp_ids:
        for ancestor_id, info in prereq_closure(kg, kp_id, max_depth, "required").items():
            if ancestor_id in known:
                continue
            item = acc.setdefault(ancestor_id, {"depth": info["depth"], "from": [],
                                               "via": info["via"]})
            item["depth"] = min(item["depth"], info["depth"])
            if kp_id not in item["from"]:
                item["from"].append(kp_id)

    ranked: list[dict] = []
    for kp_id, info in acc.items():
        kp = kg.by_id.get(kp_id)
        if not kp:
            continue
        depth_factor = 1.0 / (1.0 + info["depth"])
        centrality = min(1.0, kp.get("prereq_in_degree", 0) / max_centrality)
        base_factor = 1.0 - (int(kp.get("difficulty") or 3) - 1) / 4.0
        importance = IMPORTANCE_WEIGHT.get(kp.get("importance", ""), 0.6)
        weak = weakness.get(kp_id, 0) / max_weak if max_weak else 0.0

        score = (0.40 * depth_factor + 0.25 * centrality +
                 0.20 * base_factor + 0.15 * importance)
        score = min(1.0, score + 0.25 * weak)
        ranked.append({
            "kp_id": kp_id,
            "name": kp.get("name", ""),
            "module_name": kp.get("module_name", ""),
            "section_name": kp.get("section_name", ""),
            "difficulty": kp.get("difficulty"),
            "difficulty_cn": kp.get("difficulty_cn", ""),
            "importance_cn": kp.get("importance_cn", ""),
            "statement": kp.get("statement", ""),
            "score": round(score, 4),
            "depth": info["depth"],
            "centrality": round(centrality, 3),
            "depended_by_count": kp.get("prereq_in_degree", 0),
            "historical_wrong": weakness.get(kp_id, 0),
            "because_of": [{"kp_id": f, "name": kg.by_id[f]["name"] if f in kg.by_id else ""}
                           for f in info["from"]],
            "reason": info["via"].get("reason", ""),
        })

    ranked.sort(key=lambda r: (-r["score"], r["depth"], r["kp_id"]))
    return ranked[:topk]


def impact_rank(kg: KG, topk: int = 15) -> list[dict]:
    """全图「被依赖最多」的知识点排行（表结构中的关键枢纽）。"""
    rows = [{"kp_id": k["id"], "name": k.get("name", ""),
             "module_name": k.get("module_name", ""),
             "depended_by_count": k.get("prereq_in_degree", 0)}
            for k in kg.kps if k.get("prereq_in_degree", 0) > 0]
    rows.sort(key=lambda r: -r["depended_by_count"])
    return rows[:topk]


# ------------------------------------------------------------------ 子图提取
EDGE_KINDS = ("PREREQUISITE_OF", "RELATED_TO", "DERIVED_FROM")


def subgraph(kg: KG, *, book: str | None = None, module: str | None = None,
             focus: str | None = None, depth: int = 2,
             max_nodes: int = 700) -> dict:
    """提取可视化用的子图。

    三种取法（优先级从高到低）：
      focus  以某知识点为中心，前后各展开 depth 层（看「它依赖谁、谁依赖它」）
      book   按教材册过滤（对应学段进度，节点量适中）
      module 按模块过滤

    不传任何参数则返回全图（544 节点，力导向布局会比较吃力）。
    """
    if focus:
        if focus not in kg.by_id:
            raise KeyError(f"知识点不存在：{focus}")
        ids: set[str] = {focus}
        frontier = {focus}
        for _ in range(max(0, depth)):
            nxt: set[str] = set()
            for node in frontier:
                for edge in kg.pre_in.get(node, []) + kg.pre_out.get(node, []):
                    other = edge["from"] if edge["to"] == node else edge["to"]
                    if other not in ids:
                        nxt.add(other)
            ids |= nxt
            frontier = nxt
            if len(ids) >= max_nodes:
                break
    else:
        pool = kg.kps
        if book:
            pool = [k for k in pool if k.get("book") == book]
        elif module:
            pool = [k for k in pool if k.get("module") == module]
        ids = {k["id"] for k in pool}
        if len(ids) > max_nodes:
            # 超量时优先保留「被依赖多」的枢纽，保证图仍有结构可看
            ranked = sorted(ids,
                            key=lambda i: -kg.by_id[i].get("prereq_in_degree", 0))
            ids = set(ranked[:max_nodes])

    edges = [e for e in kg.edges
             if e.get("rel") in EDGE_KINDS
             and e.get("from") in ids and e.get("to") in ids]

    nodes = []
    for kp_id in sorted(ids):
        kp = kg.by_id[kp_id]
        nodes.append({
            "id": kp_id, "name": kp.get("name", ""),
            "module": kp.get("module", ""), "module_name": kp.get("module_name", ""),
            "kp_type": kp.get("kp_type", ""), "kp_type_cn": kp.get("kp_type_cn", ""),
            "difficulty": kp.get("difficulty"),
            "difficulty_cn": kp.get("difficulty_cn", ""),
            "importance_cn": kp.get("importance_cn", ""),
            "section_no": kp.get("section_no", ""),
            "section_name": kp.get("section_name", ""),
            "book": kp.get("book", ""),
            "statement": kp.get("statement", ""),
            "depended_by_count": kp.get("prereq_in_degree", 0),
        })
    return {
        "nodes": nodes,
        "edges": [{"source": e["from"], "target": e["to"], "rel": e.get("rel", ""),
                   "strength": e.get("strength", ""), "reason": e.get("reason", ""),
                   "kind": e.get("kind", ""), "method": e.get("method", "")}
                  for e in edges],
        "stats": {"node_count": len(nodes), "edge_count": len(edges),
                  "focus": focus, "book": book, "module": module, "depth": depth},
    }
