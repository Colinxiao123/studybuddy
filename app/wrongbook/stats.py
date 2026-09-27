#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热度统计与复习优先级。

综合重要性（对应方案 §五 的公式）：
    w = 0.55·错题数归一 + 0.25·课标权重 + 0.20·图中心性

图中心性取该知识点在 PREREQUISITE_OF 中的入度（被多少后继知识点依赖）——
「错得多 + 被依赖得多 + 课标要求高」的知识点最值得优先攻克，
比单纯看错题数量更有指导意义。
"""

from __future__ import annotations

from pathlib import Path

from app.mathkg.store import KG, get_kg
from app.wrongbook import store

IMPORTANCE_WEIGHT = {"know": 0.55, "understand": 0.75, "master": 1.0, "apply": 1.0}


def heat_map(student_id: str, *, module: str | None = None, only_wrong: bool = False,
             kg: KG | None = None, db_path: Path | None = None) -> dict:
    """知识点热度表。返回 {items, summary, max_wrong}。"""
    kg = kg or get_kg()
    stats = store.kp_wrong_stats(student_id, db_path=db_path)
    max_wrong = max([s["wrong_count"] or 0 for s in stats.values()] or [0]) or 1
    max_centrality = max([k.get("prereq_in_degree", 0) for k in kg.kps] or [1]) or 1

    items: list[dict] = []
    for kp in kg.kps:
        if module and kp.get("module") != module:
            continue
        stat = stats.get(kp["id"])
        wrong = int(stat["wrong_count"]) if stat else 0
        if only_wrong and wrong == 0:
            continue
        heat = store.heat_level(wrong)
        centrality = min(1.0, kp.get("prereq_in_degree", 0) / max_centrality)
        importance = IMPORTANCE_WEIGHT.get(kp.get("importance", ""), 0.6)
        priority = 0.55 * (wrong / max_wrong) + 0.25 * importance + 0.20 * centrality
        items.append({
            "kp_id": kp["id"],
            "name": kp.get("name", ""),
            "module": kp.get("module", ""),
            "module_name": kp.get("module_name", ""),
            "section_no": kp.get("section_no", ""),
            "section_name": kp.get("section_name", ""),
            "kp_type_cn": kp.get("kp_type_cn", ""),
            "difficulty": kp.get("difficulty"),
            "difficulty_cn": kp.get("difficulty_cn", ""),
            "importance_cn": kp.get("importance_cn", ""),
            "depended_by_count": kp.get("prereq_in_degree", 0),
            "wrong_count": wrong,
            "question_count": int(stat["question_count"]) if stat else 0,
            "wrong_times": int(stat.get("wrong_times") or 0) if stat else 0,
            "last_wrong_at": (stat or {}).get("last_wrong_at") or "",
            "error_by_type": {
                "concept": int((stat or {}).get("err_concept") or 0),
                "method": int((stat or {}).get("err_method") or 0),
                "calc": int((stat or {}).get("err_calc") or 0),
                "read": int((stat or {}).get("err_read") or 0),
            } if stat else {},
            "heat": heat,
            "priority": round(priority, 4),
        })

    items.sort(key=lambda x: (-x["wrong_count"], -x["priority"], x["kp_id"]))
    summary = {
        "total_kp": len(items),
        "wrong_kp": sum(1 for i in items if i["wrong_count"] > 0),
        "by_level": {str(lv): sum(1 for i in items if i["heat"]["level"] == lv)
                     for lv in range(5)},
        "max_wrong": max_wrong,
    }
    return {"items": items, "summary": summary}


def module_summary(student_id: str, *, kg: KG | None = None,
                   db_path: Path | None = None) -> list[dict]:
    """按模块汇总错题热度，用于「哪一块最薄弱」的概览。"""
    kg = kg or get_kg()
    data = heat_map(student_id, kg=kg, db_path=db_path)
    buckets: dict[str, dict] = {}
    for item in data["items"]:
        bucket = buckets.setdefault(item["module"], {
            "module": item["module"], "module_name": item["module_name"],
            "wrong_count": 0, "wrong_kp": 0, "kp_count": 0,
            "max_level": 0, "top_kp": None})
        bucket["kp_count"] += 1
        bucket["wrong_count"] += item["wrong_count"]
        if item["wrong_count"]:
            bucket["wrong_kp"] += 1
        if item["heat"]["level"] > bucket["max_level"]:
            bucket["max_level"] = item["heat"]["level"]
        if bucket["top_kp"] is None or item["wrong_count"] > bucket["top_kp"]["wrong_count"]:
            bucket["top_kp"] = {"kp_id": item["kp_id"], "name": item["name"],
                                "wrong_count": item["wrong_count"]}
    out = sorted(buckets.values(), key=lambda b: (-b["wrong_count"], b["module"]))
    for bucket in out:
        bucket["color"] = store.heat_level(
            bucket["top_kp"]["wrong_count"] if bucket["top_kp"] else 0)["color"]
    return out


def review_plan(student_id: str, limit: int = 10, *, kg: KG | None = None,
                db_path: Path | None = None) -> list[dict]:
    """复习优先级清单：只取有错题的知识点，按综合重要性排序，附建议动作。"""
    kg = kg or get_kg()
    data = heat_map(student_id, only_wrong=True, kg=kg, db_path=db_path)
    plan: list[dict] = []
    for index, item in enumerate(data["items"][:limit], 1):
        errors = item.get("error_by_type") or {}
        dominant = max(errors.items(), key=lambda kv: kv[1])[0] if errors else "unknown"
        if dominant == "concept":
            action = "先补前置概念：沿前置链回看更基础的知识点"
        elif dominant == "method":
            action = "练方法：找同类型题目集中训练解题步骤"
        elif dominant == "calc":
            action = "练熟练度：公式没问题，重复训练减少计算失误"
        elif dominant == "read":
            action = "审题训练：圈画条件，逐条对应问题"
        else:
            action = "重做错题，确认是否真的掌握"
        item = dict(item)
        item["order"] = index
        item["dominant_error"] = dominant
        item["dominant_error_cn"] = store.ERROR_TYPE_CN.get(dominant, "未归类")
        item["action"] = action
        plan.append(item)
    return plan


def kp_detail(student_id: str, kp_id: str, *, kg: KG | None = None,
              db_path: Path | None = None) -> dict:
    """单个知识点的详情：图谱信息 + 关联错题 + 直接前置/后继。"""
    kg = kg or get_kg()
    kp = kg.get(kp_id)
    if not kp:
        return {}
    questions = store.list_questions(student_id, kp_id=kp_id, limit=100, db_path=db_path)
    for q in questions:
        store.attach_kp_names(q.get("kps") or [], kg)
    stats = store.kp_wrong_stats(student_id, db_path=db_path).get(kp_id, {})
    wrong = int(stats.get("wrong_count") or 0)
    predecessors = [{"kp_id": e["from"], "name": e.get("from_name", ""),
                     "strength": e.get("strength"), "reason": e.get("reason", "")}
                    for e in kg.pre_in.get(kp_id, [])]
    successors = [{"kp_id": e["to"], "name": e.get("to_name", ""),
                   "strength": e.get("strength"), "reason": e.get("reason", "")}
                  for e in kg.pre_out.get(kp_id, [])]
    related = [{"kp_id": (e["to"] if e["from"] == kp_id else e["from"]),
                "name": (e.get("to_name") if e["from"] == kp_id else e.get("from_name")),
                "rel": e.get("rel"), "kind": e.get("kind", "")}
               for e in kg.related(kp_id)]
    return {
        "kp": kp,
        "wrong_count": wrong,
        "heat": store.heat_level(wrong),
        "error_by_type": {k: int(stats.get(f"err_{k}") or 0)
                          for k in ("concept", "method", "calc", "read")},
        "questions": questions,
        "prerequisites": predecessors,
        "successors": successors,
        "related": related,
    }
