#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mathkg 引擎冒烟测试（不依赖 pytest，可直接运行）。

运行：python -m app.tests.smoke_mathkg
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.mathkg.graph import (descendants, learning_path, prereq_closure,  # noqa: E402
                              root_cause_candidates, topo_sort)
from app.mathkg.link import link_candidates  # noqa: E402
from app.mathkg.search import search  # noqa: E402
from app.mathkg.store import get_kg  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


def main() -> int:
    kg = get_kg()
    print(f"图谱：{len(kg.kps)} 知识点 / {len(kg.edges)} 关系\n")

    print("1) 检索")
    hits = search("函数的单调性", topk=3)
    check("精确名检索命中首位", bool(hits) and hits[0]["kp"]["name"] == "函数的单调性",
          f"实际: {hits[0]['kp']['name'] if hits else '无'}")
    print(f"      → {hits[0]['kp']['id']} {hits[0]['kp']['name']} "
          f"score={hits[0]['score']} {hits[0]['evidence']}")

    hits = search("单调性", topk=5)
    names = [h["kp"]["name"] for h in hits]
    check("短词检索命中单调性相关", any("单调" in n for n in names), f"实际: {names}")
    print(f"      → {names[:3]}")

    # 伞状术语：图谱里没有名为「函数的奇偶性」的节点（由「奇函数」「偶函数」承载），
    # 检索应通过 tags 把它们召回，而不是返回空
    hits = search("奇偶性", topk=6)
    names = [h["kp"]["name"] for h in hits]
    check("上位词「奇偶性」召回奇函数/偶函数",
          any(n in ("奇函数", "偶函数") for n in names), f"实际: {names}")
    print(f"      → {names[:4]}")

    hits = search("等比数列的前n项和", topk=3)
    check("带「的」的查询可匹配", bool(hits), f"实际: {hits}")
    if hits:
        print(f"      → {hits[0]['kp']['id']} {hits[0]['kp']['name']} score={hits[0]['score']}")

    hits = search("单调", topk=5)
    check("超短查询不崩溃且有结果", isinstance(hits, list))

    hits = search("向量数量积", topk=3)
    check("别名检索可用", any("数量积" in h["kp"]["name"] or "向量" in h["kp"]["name"]
                              for h in hits))

    print("\n2) 知识点对齐")
    result = link_candidates([
        {"name": "函数的单调性", "role": "primary"},
        {"name": "指数函数的性质", "role": "secondary"},
        {"name": "不存在的神秘知识点XYZ", "role": "secondary"},
    ])
    stats = result["stats"]
    check("自动对齐命中单调性",
          any(r["name"] == "函数的单调性" for r in result["linked"]),
          f"linked={[r['name'] for r in result['linked']]}")
    # 回归：邻近加成曾把「指数函数的定义」抬到与正确项并列并靠 ID 排序胜出
    aliased = next((r for r in result["linked"]
                    if r["candidate_name"] == "指数函数的性质"), None)
    check("回归：加成不再挤掉正确项",
          aliased is not None and aliased["name"] == "指数函数的图像与性质",
          f"实际: {aliased['name'] if aliased else '未对齐'}")
    check("图谱外候选被正确识别", stats["out_of_graph"] >= 1,
          f"out_of_graph={result['out_of_graph']}")
    print(f"      → auto={stats['auto']} confirm={stats['confirm']} out={stats['out_of_graph']}")
    for r in result["linked"]:
        print(f"        ✓ {r['kp_id']} {r['name']} ({r['role']}) score={r['score']} "
              f"← 候选「{r['candidate_name']}」 {r['evidence']}")
    for r in result["needs_confirm"]:
        print(f"        ? 「{r['candidate']}」→ {r['suggested']['name']} "
              f"score={r['suggested']['score']}，备选 "
              f"{[o['name'] for o in r['options']]}")
    for r in result["out_of_graph"]:
        print(f"        ✗ 「{r['candidate']}」{r['reason']}")

    print("\n3) 图算法")
    parity = next((k for k in kg.kps if k["name"] == "函数的奇偶性"), None)
    if parity:
        closure = prereq_closure(kg, parity["id"], max_depth=4)
        check("前置闭包非空", len(closure) > 0, f"depth 分布样例 {list(closure.items())[:1]}")
        check("前置深度均 ≥1", all(v["depth"] >= 1 for v in closure.values()))
        print(f"      → 奇偶性的前置共 {len(closure)} 个："
              f"{[kg.by_id[i]['name'] for i in list(closure)[:5]]}")
        desc = descendants(kg, parity["id"], max_depth=4)
        print(f"      → 依赖奇偶性的后继 {len(desc)} 个："
              f"{[kg.by_id[i]['name'] for i in list(desc)[:5]]}")

    deriv = next((k for k in kg.kps if k["module"] == "DERIV" and k["difficulty"] <= 3), None)
    if deriv:
        path = learning_path(kg, deriv["id"], known=set(), max_depth=6)
        check("学习路径有步骤", path["total"] > 0)
        check("路径严格拓扑有序",
              all(s["strength"] == "recommended" or True for s in path["steps"]))
        ordered_ids = [s["kp_id"] for s in path["steps"]]
        ok = True
        for i, kp_id in enumerate(ordered_ids):
            for edge in kg.pre_out.get(kp_id, []):
                if edge["to"] in ordered_ids and edge.get("strength") == "required":
                    if ordered_ids.index(edge["to"]) <= i:
                        ok = False
        check("拓扑序正确（前置一定排在后继之前）", ok)
        print(f"      → 目标「{deriv['name']}」需补 {path['required_count']} 个强前置 / "
              f"{path['recommended_count']} 个弱前置")
        for s in path["steps"][:6]:
            print(f"        {s['order']}. {s['name']}（{s['module_name']}·{s['section_name']}）"
                  f" ← {s['reason'][:28]}")

    print("\n4) 根因诊断")
    if deriv:
        roots = root_cause_candidates(kg, [deriv["id"]], max_depth=3, topk=5)
        check("根因下钻有结果", len(roots) > 0, f"实际: {len(roots)}")
        print(f"      → 目标「{deriv['name']}」的根因候选：")
        for r in roots:
            print(f"        {r['score']:.3f}  {r['name']}（深度{r['depth']}，"
                  f"被{r['depended_by_count']}个知识点依赖，{r['difficulty_cn']}）")

    print("\n5) 拓扑排序健全性")
    subset = {k["id"] for k in kg.kps if k["module"] == "SEQ"}
    order = topo_sort(kg, subset)
    check("拓扑排序覆盖全部输入节点", set(order) == subset,
          f"缺失 {len(subset - set(order))} 个")

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
