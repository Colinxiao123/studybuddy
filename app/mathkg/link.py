#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识点对齐（Entity Linking）：把 LLM 给出的自由文本候选落到图谱真实 ID。

核心原则：**LLM 只负责提候选名，落地 ID 一律由确定性检索算法决定**，
避免模型编造不存在的 KP 编号。

三级分流：
    score ≥ 0.80        自动采用 auto
    0.45 ≤ score < 0.80 需确认 confirm（返回 top3 备选）
    score < 0.45        图谱外 out_of_graph（保留原名，不落 ID）

二次校正：已高置信对齐的结果会给「同节知识点」和「前置链邻近知识点」带来加成，
用于消解单点歧义（如「性质」这类泛称）。
"""

from __future__ import annotations

from app.mathkg.search import search
from app.mathkg.store import KG, get_kg
from app.mathkg.textutil import ngrams, normalize

AUTO_THRESHOLD = 0.80
CONFIRM_THRESHOLD = 0.45
AMBIGUITY_MARGIN = 0.05   # 第一名与第二名差距小于此值 → 不自动采用，转人工确认
SECTION_BONUS = 0.06
PREREQ_BONUS = 0.04

ROLE_CN = {"primary": "主要考查", "secondary": "涉及", "root_cause": "根因候选"}


def _normalize_candidates(candidates) -> list[dict]:
    """允许传入 ['函数的奇偶性', {...}] 两种形式。"""
    out: list[dict] = []
    for item in candidates or []:
        if isinstance(item, str):
            if item.strip():
                out.append({"name": item.strip(), "role": "primary", "reason": ""})
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("kp_name") or "").strip()
            if not name:
                continue
            role = item.get("role") or "primary"
            if role not in ROLE_CN:
                role = "secondary"
            out.append({"name": name, "role": role,
                        "reason": str(item.get("reason") or ""),
                        "confidence": item.get("confidence"),
                        # 允许调用方给置信度设上限：词典匹配一类「弱证据」不该报满分
                        "score_cap": item.get("score_cap")})
    return out


def _name_coverage(kp_name: str, context_norm: str) -> float:
    """知识点名称有多大比例真正出现在题干/候选中。

    解决的问题：候选「函数的奇偶性」既像「判断函数的奇偶性」，也像
    「正弦函数的奇偶性」——两者都包含这个子串，字符串得分几乎打平，
    只能靠 ID 排序瞎猜（实测就猜错成了正弦）。而题干里根本没有「正弦」二字。

    办法：把名称切成 n-gram，统计多少比例能在上下文里找到。
    「判断函数的奇偶性」在题干「判断 $f(x)$ 的奇偶性」中覆盖率 100%；
    「正弦函数的奇偶性」只有 75%（正弦二字无出处），据此降权。
    """
    name = normalize(kp_name)
    if not name or not context_norm:
        return 1.0
    grams = ngrams(name, 2, 3)
    if not grams:
        return 1.0
    return sum(1 for g in grams if g in context_norm) / len(grams)


def _apply_context_penalty(option: dict, context_norm: str) -> None:
    """就地按名称覆盖率降权。

    只作用于**模糊文本匹配**（kind="text"）：那种情况我们只是「字面对上了」，
    自然该要求名字在题干里有出处。
    反过来，图谱把「指数函数性质」显式写成了「指数函数的图像与性质」的别名，
    这是图谱自己声明的等价关系，不该因为多出「图像」二字就降权
    （曾经这么干过一版，结果把正确的别名匹配也误伤了）。
    """
    if not context_norm or option.get("kind") != "text":
        return
    coverage = _name_coverage(option["kp"].get("name", ""), context_norm)
    if coverage >= 1.0:
        return
    # 覆盖率 0.75 → 系数 0.89；覆盖率 0.50 → 系数 0.78
    option["score"] = round(option["score"] * (0.55 + 0.45 * coverage), 4)
    option["evidence"] = option["evidence"] + [
        f"名称有 {1 - coverage:.0%} 未在题干中出现，已降权"]


def _append_tag_siblings(options: list[dict], candidate_name: str, kg: KG,
                         score: float = 0.50) -> None:
    """标记/追加「主题标签兄弟」——与候选共享主题标签、但字面无重叠的知识点。

    场景：「函数的奇偶性」在图谱里没有同名节点，概念由「奇函数」「偶函数」承载。
    这两个名字与候选没有任何字面重叠，只能靠共享的 tags 找到。

    两种情况都统一到同一个分数：
      · 已在检索结果里 → 标记为兄弟，并把分数抬到兄弟基准分；
      · 不在结果里 → 补进去。
    为什么要抬分：实测「偶函数」检索分 0.29、「奇函数」根本进不了前 6，
    同一概念的两个承载点差了近一倍，纯粹是字面巧合造成的，不该影响判断。
    """
    cand_norm = normalize(candidate_name)
    if not cand_norm:
        return
    existing = {o["kp"]["id"]: o for o in options}
    for term in kg.topical_tags:
        if len(term) < 3 or term not in cand_norm:
            continue
        for kp_id in kg.tag_index.get(term) or []:
            kp = kg.by_id.get(kp_id)
            if not kp:
                continue
            if kp_id in existing:
                opt = existing[kp_id]
                if not opt.get("is_sibling"):
                    opt["is_sibling"] = True
                    opt["evidence"] = opt["evidence"] + [f"与候选共享主题标签「{term}」"]
                    opt["score"] = max(opt["score"], score)
                continue
            entry = {"kp": kp, "score": score, "kind": "tag", "is_sibling": True,
                     "evidence": [f"与候选共享主题标签「{term}」"],
                     "matched_terms": [term]}
            options.append(entry)
            existing[kp_id] = entry


def link_candidates(candidates, kg: KG | None = None, module: str | None = None,
                    context_text: str = "", topk_options: int = 4) -> dict:
    """主入口。返回 {linked, needs_confirm, out_of_graph, stats}。"""
    kg = kg or get_kg()
    cands = _normalize_candidates(candidates)
    if not cands:
        return {"linked": [], "needs_confirm": [], "out_of_graph": [],
                "stats": {"total": 0, "auto": 0, "confirm": 0, "out_of_graph": 0}}

    # 上下文 = 题干 + 全部候选名：用来判断知识点名称是否有出处
    context_norm = normalize(" ".join([context_text] + [c["name"] for c in cands]))

    # ---------- 第一轮：独立检索 ----------
    rounds: list[dict] = []
    for cand in cands:
        hits = search(cand["name"], topk=max(topk_options + 2, 5), module=module, kg=kg)
        if not hits:
            rounds.append({"cand": cand, "options": []})
            continue
        options = list(hits)
        # 用 LLM 给的 reason + 题干上下文做弱加权（只微调，不主导）
        weak_text = f"{cand.get('reason', '')} {context_text}".strip()
        if weak_text:
            extra = {r["kp"]["id"]: r for r in
                     search(weak_text[:120], topk=12, module=module, kg=kg)}
            for opt in options:
                extra_hit = extra.get(opt["kp"]["id"])
                if extra_hit:
                    opt["score"] = round(min(1.0, opt["score"] + 0.04 * extra_hit["score"]), 4)
                    opt["evidence"] = opt["evidence"] + ["题干上下文相关"]
        options.sort(key=lambda r: (-r["score"], r["kp"]["id"]))
        cap = cand.get("score_cap")
        if cap is not None:
            for opt in options:
                if opt["score"] > cap:
                    opt["score"] = round(float(cap), 4)
                    opt["evidence"] = opt["evidence"] + [f"弱证据，置信度上限 {cap}"]
        # 名称覆盖率降权，剔除「字面对得上、但题干里查无此名」的假匹配。
        # 词典匹配（有 cap）本身就是扫描题干得到的，不需要再降权。
        if cap is None:
            for opt in options:
                _apply_context_penalty(opt, context_norm)
        # 主题标签兄弟：图谱里有些概念被拆成多个知识点承载，没有同名节点。
        # 例如「奇偶性」拆成「奇函数」「偶函数」，只靠字面匹配永远抓不到，
        # 但它们与候选共享主题标签，应当出现在备选里供学生挑选。
        _append_tag_siblings(options, cand["name"], kg)
        options.sort(key=lambda r: (-r["score"], r["kp"]["id"]))
        # 保留前 N 名 + 全部标签兄弟。注意不能直接切片了事：兄弟知识点分数天然
        # 偏低（0.5 上下），会被截断丢掉，而它们往往正是该选的那个
        # （实测「偶函数」就这样被切掉了，学生答案里写的偏偏就是它）。
        head = list(options[:topk_options + 2])
        head_ids = {o["kp"]["id"] for o in head}
        head += [o for o in options
                 if o.get("is_sibling") and o["kp"]["id"] not in head_ids]
        rounds.append({"cand": cand, "options": head})

    # ---------- 第二轮：仅对「本身不确定」的候选做邻近加成 ----------
    # 重要：如果某候选已经拿到高置信答案（≥ AUTO_THRESHOLD），就不再施加任何加成。
    # 否则「同节 +0.06」「有直接关系 +0.04」会叠加，把原本正确的第一名挤掉
    # （实测：正确项 0.92 被加成后的 0.82+0.04+0.06=0.92 追平，再按 ID 排序落败）。
    anchors = [r["options"][0]["kp"]["id"] for r in rounds
               if r["options"] and r["options"][0]["score"] >= AUTO_THRESHOLD]
    anchor_sections = {kg.by_id[a].get("sec_id") for a in anchors if a in kg.by_id}
    anchor_neighbors: set[str] = set()
    for a in anchors:
        for edge in kg.pre_in.get(a, []) + kg.pre_out.get(a, []):
            anchor_neighbors.add(edge["from"])
            anchor_neighbors.add(edge["to"])

    if anchors:
        for r in rounds:
            if not r["options"] or r["options"][0]["score"] >= AUTO_THRESHOLD:
                continue  # 已有高置信答案，不做任何加权重排
            for opt in r["options"]:
                opt["base_score"] = opt["score"]
            for opt in r["options"]:
                kp_id = opt["kp"]["id"]
                if kg.by_id.get(kp_id, {}).get("sec_id") in anchor_sections:
                    opt["score"] = round(min(1.0, opt["score"] + SECTION_BONUS), 4)
                    opt["evidence"] = opt["evidence"] + ["与已确认知识点同节"]
                if kp_id in anchor_neighbors:
                    opt["score"] = round(min(1.0, opt["score"] + PREREQ_BONUS), 4)
                    opt["evidence"] = opt["evidence"] + ["与已确认知识点有直接关系"]
            r["options"].sort(key=lambda x: (-x["score"], x["kp"]["id"]))

    # ---------- 分流 ----------
    best_by_kp: dict[str, dict] = {}
    needs_confirm: list[dict] = []
    out_of_graph: list[dict] = []

    for r in rounds:
        cand, options = r["cand"], r["options"]
        if not options:
            out_of_graph.append({"candidate": cand["name"], "role": cand["role"],
                                 "reason": "图谱中无任何匹配", "best_guess": None})
            continue
        top = options[0]
        score, kp = top["score"], top["kp"]
        # 歧义保护：第一名与第二名咬得很近时，不替学生做决定。
        # 这类「两个知识点名字都像候选」的情况（如「正弦函数的奇偶性」vs
        # 「判断函数的奇偶性」），算法没有依据判断，交给确认环节最诚实。
        ambiguous = (len(options) > 1 and score < 0.95
                     and score - options[1]["score"] <= AMBIGUITY_MARGIN)
        if ambiguous:
            top["evidence"] = top["evidence"] + [
                f"与备选「{options[1]['kp'].get('name', '')}」得分仅差 "
                f"{score - options[1]['score']:.2f}，需人工确认"]
        record = {
            "kp_id": kp["id"], "name": kp.get("name", ""),
            "module_name": kp.get("module_name", ""),
            "section_name": kp.get("section_name", ""),
            "section_no": kp.get("section_no", ""),
            "kp_type_cn": kp.get("kp_type_cn", ""),
            "difficulty": kp.get("difficulty"),
            "importance_cn": kp.get("importance_cn", ""),
            "statement": kp.get("statement", ""),
            "score": score, "base_score": top.get("base_score", score),
            "evidence": top["evidence"],
            "candidate_name": cand["name"], "role": cand["role"],
            "depended_by_count": kp.get("prereq_in_degree", 0),
        }
        if score >= AUTO_THRESHOLD and not ambiguous:
            record["status"] = "auto"
            prev = best_by_kp.get(kp["id"])
            if prev is None or prev["score"] < score:
                best_by_kp[kp["id"]] = record
            elif prev["role"] != "primary" and cand["role"] == "primary":
                prev["role"] = "primary"
        elif ambiguous or score >= CONFIRM_THRESHOLD:
            record["status"] = "confirm"
            # 展示的备选 = 按分数取前 N + 最多 3 个「主题标签兄弟」。
            # 兄弟知识点（如奇函数/偶函数）分数天然偏低，容易被截断，
            # 但它们往往正是学生需要选的那个（图谱里的概念就是它们承载的）。
            # options 此时已按分数排序，所以取前 3 个兄弟即得分最高的 3 个。
            shown = list(options[:topk_options])
            shown_ids = {o["kp"]["id"] for o in shown}
            extra = 0
            for opt in options:
                if extra >= 3:
                    break
                if opt.get("is_sibling") and opt["kp"]["id"] not in shown_ids:
                    shown.append(opt)
                    shown_ids.add(opt["kp"]["id"])
                    extra += 1
            needs_confirm.append({
                "candidate": cand["name"], "role": cand["role"],
                "reason": cand.get("reason", ""),
                "suggested": {k: record[k] for k in
                              ("kp_id", "name", "score", "evidence")},
                "options": [{"kp_id": o["kp"]["id"], "name": o["kp"].get("name", ""),
                             "module_name": o["kp"].get("module_name", ""),
                             "section_name": o["kp"].get("section_name", ""),
                             "score": o["score"], "evidence": o["evidence"],
                             "is_sibling": bool(o.get("is_sibling"))}
                            for o in shown],
            })
        else:
            out_of_graph.append({
                "candidate": cand["name"], "role": cand["role"],
                "reason": "最高分低于阈值，视为图谱外知识点",
                "best_guess": {"kp_id": kp["id"], "name": kp.get("name", ""), "score": score},
            })

    linked = sorted(best_by_kp.values(), key=lambda r: (-r["score"], r["kp_id"]))
    return {
        "linked": linked,
        "needs_confirm": needs_confirm,
        "out_of_graph": out_of_graph,
        "stats": {"total": len(cands), "auto": len(linked),
                  "confirm": len(needs_confirm), "out_of_graph": len(out_of_graph)},
    }


def resolve_confirm(candidates: list[dict], kg: KG | None = None) -> list[dict]:
    """把前端确认过的候选（含用户选定的 kp_id）转成 linked 结构。"""
    kg = kg or get_kg()
    out: list[dict] = []
    for item in candidates or []:
        kp_id = item.get("kp_id")
        kp = kg.by_id.get(kp_id) if kp_id else None
        if not kp:
            continue
        out.append({
            "kp_id": kp_id, "name": kp.get("name", ""),
            "module_name": kp.get("module_name", ""),
            "section_name": kp.get("section_name", ""),
            "kp_type_cn": kp.get("kp_type_cn", ""),
            "difficulty": kp.get("difficulty"),
            "statement": kp.get("statement", ""),
            "score": float(item.get("score") or 1.0),
            "evidence": ["用户确认"],
            "candidate_name": item.get("candidate_name", ""),
            "role": item.get("role") or "primary",
            "status": "confirmed",
            "depended_by_count": kp.get("prereq_in_degree", 0),
        })
    return out


def link_text(text: str, topk: int = 8, module: str | None = None,
              kg: KG | None = None) -> list[dict]:
    """纯文本直接检索（用于「搜索知识点」入口）。"""
    return search(text, topk=topk, module=module, kg=kg)


def dedupe_names(names: list[str]) -> list[str]:
    """候选名去重（归一化后比较，保留首次出现）。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = normalize(n)
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out
