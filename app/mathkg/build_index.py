#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app/mathkg/build_index.py —— 构建图谱快照（应用层唯一数据入口）

把分散在 12 个模块目录里的节点/边聚合为单文件快照，并补充：
  1. 结构名（模块名 / 章名 / 节名 / 小节名 / 册名），供界面直接展示；
  2. 教材分段映射（section_id → pipeline/data/segments 下的 Markdown 文件），
     供讲解时引用教材原文，避免模型自由发挥；
  3. 度数统计（in/out degree），供「知识点重要性」排序使用。

输入：ontology/data/structure.json
      ontology/data/src/{模块}/nodes.json + edges.json
      pipeline/data/segments/{册}/index.json
输出：app/data/kg_snapshot.json

用法：python -m app.mathkg.build_index
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import (BOOK_CN, DIFFICULTY_CN, IMPORTANCE_CN, KP_TYPE_CN,  # noqa: E402
                        MODULE_CN, SEGMENTS_DIR, SNAPSHOT_PATH, SRC_DIR,
                        STRUCTURE_PATH, ensure_dirs)


def load_structure() -> dict:
    """结构骨架 → 各类 id 的查询表。"""
    raw = json.loads(STRUCTURE_PATH.read_text(encoding="utf-8"))
    modules = {m["id"]: m for m in raw.get("modules", [])}
    chapters = {c["id"]: c for c in raw.get("chapters", [])}
    sections = {s["id"]: s for s in raw.get("sections", [])}
    subsections = {s["id"]: s for s in raw.get("subsections", [])}
    return {"modules": modules, "chapters": chapters,
            "sections": sections, "subsections": subsections, "meta": raw.get("meta", {})}


def load_segments() -> dict[str, dict]:
    """section_id → 教材 Markdown 分段信息（用于回链原文）。"""
    mapping: dict[str, dict] = {}
    if not SEGMENTS_DIR.exists():
        return mapping
    for book_dir in sorted(d for d in SEGMENTS_DIR.iterdir() if d.is_dir()):
        index_file = book_dir / "index.json"
        if not index_file.exists():
            continue
        index = json.loads(index_file.read_text(encoding="utf-8"))
        for sec in index.get("sections", []):
            mapping[sec["id"]] = {
                "book": index.get("book", book_dir.name),
                "file": str((book_dir / sec["file"]).relative_to(SEGMENTS_DIR.parent.parent.parent)),
                "no": sec.get("no", ""),
                "name": sec.get("name", ""),
                "chars": sec.get("chars", 0),
            }
    return mapping


def load_graph() -> tuple[list[dict], list[dict]]:
    """聚合全部模块的节点与边（与 scripts/build_graph.py 的 load_all 一致）。"""
    nodes: list[dict] = []
    edges: list[dict] = []
    for mod_dir in sorted(d for d in SRC_DIR.iterdir() if d.is_dir()):
        nodes_file = mod_dir / "nodes.json"
        if nodes_file.exists():
            nodes += json.loads(nodes_file.read_text(encoding="utf-8"))
        edges_file = mod_dir / "edges.json"
        if edges_file.exists():
            edges += json.loads(edges_file.read_text(encoding="utf-8"))
    return nodes, edges


def enrich(nodes: list[dict], edges: list[dict], struct: dict,
           segments: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """补全结构名、教材回链、度数统计。"""
    modules, chapters = struct["modules"], struct["chapters"]
    sections, subsections = struct["sections"], struct["subsections"]

    in_deg: Counter[str] = Counter(e["to"] for e in edges)
    out_deg: Counter[str] = Counter(e["from"] for e in edges)
    prereq_in: Counter[str] = Counter(e["to"] for e in edges if e.get("rel") == "PREREQUISITE_OF")

    out: list[dict] = []
    for n in nodes:
        kp = dict(n)
        module = kp.get("module", "")
        kp["module_name"] = MODULE_CN.get(module, module)
        kp["kp_type_cn"] = KP_TYPE_CN.get(kp.get("kp_type", ""), kp.get("kp_type", ""))
        kp["importance_cn"] = IMPORTANCE_CN.get(kp.get("importance", ""), kp.get("importance", ""))
        kp["difficulty_cn"] = DIFFICULTY_CN.get(kp.get("difficulty", 0), "")

        sec_id = kp.get("section_id") or kp.get("sub_section_id") or ""
        kp["sec_id"] = sec_id
        sec = sections.get(sec_id)
        sub = subsections.get(kp.get("sub_section_id") or "")
        if sec:
            kp["section_name"] = sec.get("name", "")
            kp["section_no"] = sec.get("section_no", "")
            kp["elective"] = bool(sec.get("elective", False))
            chapter = chapters.get(sec.get("chapter_id", ""))
            if chapter:
                kp["chapter_name"] = chapter.get("name", "")
                kp["chapter_no"] = chapter.get("chapter_no")
                kp["book"] = chapter.get("book", "")
            else:
                kp["chapter_name"], kp["chapter_no"], kp["book"] = "", None, ""
        else:
            kp["section_name"], kp["section_no"] = "", ""
            kp["chapter_name"], kp["chapter_no"] = "", None
            kp["book"] = (kp.get("source") or {}).get("book", "")
            kp["elective"] = False
        kp["sub_section_name"] = sub.get("name", "") if sub else ""
        kp["book_name"] = BOOK_CN.get(kp.get("book", ""), kp.get("book", ""))
        # 教材分段回链（讲解时的原文依据）
        seg = segments.get(sec_id)
        kp["segment_file"] = seg["file"] if seg else ""
        # 度数：in_degree 表示「有多少知识点依赖它」，是重要性的核心指标
        kp["in_degree"] = in_deg.get(kp["id"], 0)
        kp["out_degree"] = out_deg.get(kp["id"], 0)
        kp["prereq_in_degree"] = prereq_in.get(kp["id"], 0)
        out.append(kp)

    # 边也补上两端名字，界面/卡片不必再查表
    name_of = {n["id"]: n["name"] for n in out}
    edges_out: list[dict] = []
    for e in edges:
        edge = dict(e)
        edge["from_name"] = name_of.get(e.get("from", ""), "")
        edge["to_name"] = name_of.get(e.get("to", ""), "")
        edges_out.append(edge)
    return out, edges_out


def main() -> None:
    struct = load_structure()
    segments = load_segments()
    nodes, edges = load_graph()
    nodes, edges = enrich(nodes, edges, struct, segments)

    rel_counter = Counter(e.get("rel", "") for e in edges)
    status_counter = Counter(n.get("status", "") for n in nodes)
    module_counter = Counter(n.get("module", "") for n in nodes)

    snapshot = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "generated_by": "app/mathkg/build_index.py",
            "source": "ontology/data/src/**  +  ontology/data/structure.json",
            "counts": {
                "nodes": len(nodes),
                "edges": len(edges),
                "modules": len(module_counter),
                "with_segment": sum(1 for n in nodes if n.get("segment_file")),
            },
            "rel_distribution": dict(rel_counter),
            "status_distribution": dict(status_counter),
        },
        "modules": [
            {"code": code,
             "name": MODULE_CN.get(code, code),
             "kp_count": count,
             "order": (struct["modules"].get(f"MOD-{code}", {}) or {}).get("order", 99)}
            for code, count in module_counter.items()
        ],
        "kps": nodes,
        "edges": edges,
    }
    snapshot["modules"].sort(key=lambda m: m["order"])

    ensure_dirs()
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[OK] 快照已生成：{SNAPSHOT_PATH}")
    print(f"     知识点 {len(nodes)}  关系 {len(edges)}  模块 {len(module_counter)}")
    print(f"     教材回链覆盖：{snapshot['meta']['counts']['with_segment']}/{len(nodes)}")
    print(f"     关系分布：{dict(rel_counter)}")
    print(f"     状态分布：{dict(status_counter)}")
    print(f"     文件大小：{SNAPSHOT_PATH.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
