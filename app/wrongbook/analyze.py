#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 任务：知识点候选抽取 +。AI 解答与错因分析。

两条纪律（对应方案 §三 与 §六）：
  1. LLM 只给**候选知识点名**，最终 ID 由 link.py 的确定性算法决定；
  2. LLM 的解答必须引用的知识点、必须来自图谱（我们会把已对齐的 KP 列表喂给它），
     讲解内容尽量贴紧教材原文片段，不自由发挥。
模型不可用时一律优雅降级，返回空结果并附 reason，界面据此提示。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.wrongbook.vision import VisionError, chat_text, extract_json

KP_EXTRACT_PROMPT = """你是高中数学教研员。请从下面这道题中，找出它考查的知识点。

只输出 JSON：
{"kps": [{"name": "知识点名称", "role": "primary|secondary", "reason": "为什么这道题会用到它"}]}

规则：
1. `name` 用教材标准叫法，例如"函数的单调性""余弦定理""等比数列的前n项和公式"；
   不要写"计算能力""数学思维"这类非知识点。
2. `role`：primary = 这道题主要考查的；secondary = 解题过程中会涉及但不是考点的。
3. 最多 6 个，按重要性排序。宁少勿滥。
4. 若题目涉及多个知识点，都要列出（例如既考奇偶性又考指数运算）。
5. 只输出 JSON，不要解释。"""

ANALYZE_PROMPT = """你是高中数学老师，正在帮学生订正一道错题。

你会收到：题目、学生作答、题目涉及的**图谱知识点清单**（含编号）、以及这些知识点在教材中的原文片段。

请输出 JSON：
{
  "ai_answer": "标准答案（含关键步骤，LaTeX 用 $...$）",
  "ai_analysis": "分步解析（Markdown，公式用 $...$）",
  "error_type": "concept|method|calc|read|unknown",
  "error_detail": "具体错在哪一步、为什么错（若学生作答为空则写"未提供学生作答"）",
  "hint": "一句启发式提示，不要直接给答案",
  "textbook_ref": "用到的教材结论，注明来自哪个知识点编号"
}

判定 error_type 的标准：
  concept 概念不清——定义/条件记错，如把 f(-x) 算成了 f(x) 的相反数
  method  方法不会——思路方向错误或完全不会下手
  calc    计算失误——思路正确但某步运算出错
  read    审题失误——漏看条件、看错问法

铁律：
1. 讲解必须基于给出的知识点清单与教材原文，不要引入清单之外的知识点；
2. 必须引用知识点编号（如 KP-FUNC-0043）作为依据；
3. 只输出 JSON。"""


def _textbook_context(linked: list[dict], kg, max_kp: int = 3, max_chars: int = 1400) -> str:
    """取已对齐知识点的教材原文片段，作为讲解的事实依据。"""
    chunks: list[str] = []
    for item in (linked or [])[:max_kp]:
        kp = kg.get(item.get("kp_id") or "")
        if not kp:
            continue
        excerpt = kg.segment_excerpt(kp, window=max_chars)
        head = f"【{kp['id']} {kp['name']}（{kp.get('book_name','')}·{kp.get('section_no','')} {kp.get('section_name','')}）】"
        body = f"陈述：{kp.get('statement','')}"
        if kp.get("latex"):
            body += f"\n公式：{kp['latex']}"
        if excerpt:
            body += f"\n教材原文片段：{excerpt}"
        chunks.append(f"{head}\n{body}")
    return "\n\n".join(chunks)


def extract_kp_candidates(stem_md: str, *, figure_desc: str = "",
                          student_answer: str = "", extra_hint: str = "") -> tuple[list[dict], str]:
    """返回 (候选知识点列表, 失败原因)。失败时列表为空并给出原因。"""
    parts = [f"题目：\n{stem_md}"]
    if figure_desc:
        parts.append(f"图形说明：{figure_desc}")
    if student_answer:
        parts.append(f"学生作答：\n{student_answer}")
    if extra_hint:
        parts.append(f"补充：{extra_hint}")
    prompt = "\n\n".join(parts)

    try:
        raw = chat_text([{"role": "system", "content": KP_EXTRACT_PROMPT},
                         {"role": "user", "content": prompt}], json_mode=True)
        data = extract_json(raw)
        items = data.get("kps") or data.get("knowledge_points") or []
        out: list[dict] = []
        for item in items:
            if isinstance(item, str):
                out.append({"name": item, "role": "primary", "reason": ""})
            elif isinstance(item, dict) and item.get("name"):
                role = item.get("role") if item.get("role") in ("primary", "secondary") else "primary"
                out.append({"name": str(item["name"]).strip(), "role": role,
                            "reason": str(item.get("reason") or "")})
        return out, "" if out else "模型没有给出任何知识点候选"
    except VisionError as exc:
        return [], str(exc)


def solve_and_analyze(*, stem_md: str, linked: list[dict], kg,
                      figure_desc: str = "", student_answer: str = "") -> tuple[dict, str]:
    """返回 (分析结果 dict, 失败原因)。"""
    kp_lines = "\n".join(
        f"- {i.get('kp_id')} {i.get('name')}（{i.get('role')}）：{i.get('statement','')}"
        for i in (linked or []))
    context = _textbook_context(linked, kg)
    prompt = (f"题目：\n{stem_md}\n\n"
              f"{'图形说明：' + figure_desc if figure_desc else ''}\n\n"
              f"学生作答：\n{student_answer or '（空）'}\n\n"
              f"题目涉及的图谱知识点清单：\n{kp_lines or '（无）'}\n\n"
              f"教材原文依据：\n{context or '（无）'}")
    try:
        raw = chat_text([{"role": "system", "content": ANALYZE_PROMPT},
                         {"role": "user", "content": prompt}], json_mode=True)
        data = extract_json(raw)
        for key in ("ai_answer", "ai_analysis", "error_type", "error_detail", "hint", "textbook_ref"):
            data.setdefault(key, "")
        if data.get("error_type") not in ("concept", "method", "calc", "read", "unknown"):
            data["error_type"] = "unknown"
        return data, ""
    except VisionError as exc:
        return {}, str(exc)


def kp_list_text(linked: list[dict]) -> str:
    return "、".join(f"{i.get('name')}（{i.get('kp_id')}）" for i in (linked or []))


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def load_prompt_file(path: Path) -> str:
    """预留：允许把提示词外置成文件，便于老师自行调整。"""
    return path.read_text(encoding="utf-8") if path.exists() else ""
