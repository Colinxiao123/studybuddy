#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 对话答疑：先答一遍，再复核一遍。

需求里的三条约束落在这一层：
  1. **结合图谱**：讲解的事实依据只能是「这道题在图谱里对齐到的知识点」及其教材原文，
     并让模型回填实际用到的知识点编号（kp_used），界面据此展示依据；
  2. **不超纲**：提示词里写明只允许高中范围的方法，服务端再按关键词兜一道 ——
     模型说漏嘴时宁可提示，也不能让学生照着超纲方法背；
  3. **二次验证**：第一遍出解答，第二遍专门挑错（超纲 / 与图谱不符 / 计算失误 / 漏答），
     给出 verdict 与修订后的解答。

两遍拆成两个函数、两个接口，界面上能看见「解答 → 复核」的过程，而不是等一个黑盒。
"""

from __future__ import annotations

from app.mathkg.search import search
from app.mathkg.store import get_kg
from app.wrongbook import store
from app.wrongbook.analyze import _textbook_context
from app.wrongbook.vision import VisionError, chat_text, extract_json

# 高中范围外的典型方法/名词：只作「提示级」兜底。
# 真正的判断交给第二遍复核；这里只负责把「确实在用」的抬到界面上提醒一句。
OUT_OF_SCOPE = [
    "洛必达", "泰勒展开", "泰勒公式", "麦克劳林", "拉格朗日中值", "罗尔定理", "柯西中值",
    "中值定理", "ε-δ", "epsilon-delta", "夹逼定理", "矩阵", "行列式", "特征值",
    "微分方程", "偏导数", "重积分", "级数收敛", "傅里叶", "欧拉公式", "隐函数求导",
]

# 句子附近出现这些词，说明是在「讲为什么不能用」，而不是在用
_NEGATION = ("不能", "不可以", "不建议", "不应", "不宜", "超纲", "不超", "不许", "禁止",
             "不得", "大学", "考研", "考纲", "高中不", "范围外", "中值定理的证明")

DRAFT_PROMPT = """你是高中数学老师，正在跟学生对话答疑。

你会收到：学生的问题、可能附带的错题（题干与学生的作答）、以及这道题在知识图谱里
对应的**知识点清单**（含编号与教材陈述）、这些知识点在教材中的原文片段。

输出 JSON：
{
  "answer_md": "解答（Markdown，公式用 $...$）。先给结论，再给关键步骤；学生问概念就讲定义与来龙去脉",
  "kp_used": ["KP-XXXX-XXXX"],
  "scope_note": "若学生的问题超出高中范围，在这里说明并给出高中范围内的替代思路；正常情况留空",
  "follow_up": "一句追问，引导学生自己往下想（可为空）"
}

铁律：
1. **只讲高中范围内的方法**：导数只用高中教材的求导公式与单调性/极值判定；不许出现洛必达、
   泰勒展开、中值定理、矩阵、ε-δ 定义等超纲内容。若题目必须用超纲工具才能做，就在
   scope_note 里说明「这题超出高中范围」，而不要硬用超纲方法给出解答；
2. **讲解必须与图谱清单一致**：结论与公式以清单里的教材陈述为准，不要引入清单之外的知识点；
   清单里缺但确实需要的结论，先在 scope_note 里讲明，再给高中范围内的做法；
3. `kp_used` 只能填清单里出现过的编号，不要编造；用不到任何知识点时给空数组；
4. 学生只是闲聊或提问与数学无关时，正常回答，kp_used 给空数组；
5. 只输出 JSON。"""

VERIFY_PROMPT = """你是**复核老师**，任务是给上一版解答挑错。宁可挑得严，也不要放过问题。

你会收到：学生的问题、题目与图谱知识点清单（含教材陈述）、上一版解答。

逐项检查，然后输出 JSON：
{
  "verdict": "ok|fixed|reject",
  "issues": [{"type": "超纲|与图谱不符|计算错误|逻辑跳跃|漏答|表述不清", "detail": "问题在哪", "fix": "怎么改"}],
  "answer_md": "修订后的最终解答（verdict=ok 时原样返回上一版，不要重写措辞）",
  "confidence": 0.0
}

检查清单：
1. **超纲**：有没有用到高中范围外的方法/结论（洛必达、泰勒、中值定理、矩阵、ε-δ…）？
   有就换成高中做法；
2. **与图谱不符**：结论与给定的知识点陈述、教材片段是否矛盾？引用的知识点编号是否存在、是否真的用到了？
3. **计算**：每一步是否可复核？有没有算错、跳步导致学生跟不上？
4. **完整**：有没有漏答、答非所问、结论与过程矛盾？
5. verdict 取值：`ok` = 没有实质问题；`fixed` = 有问题但已在 answer_md 里改好；
   `reject` = 这题在高中范围内确实讲不了，此时在 issues 里说明原因，answer_md 给出现有条件下最合适的说明；
6. 只输出 JSON。"""


def scan_scope(text: str) -> dict:
    """扫高中范围外的关键词，区分「在用」与「只是提到」。

    知道「不能用洛必达」是好讲解，不该被当成超纲误报 —— 实测模型正是这么答的，
    所以要看命中词附近有没有否定/限定语。
    """
    text = text or ""
    used: set[str] = set()
    mentioned: set[str] = set()
    for term in OUT_OF_SCOPE:
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            window = text[max(0, idx - 40): idx + len(term) + 20]
            (mentioned if any(n in window for n in _NEGATION) else used).add(term)
            start = idx + len(term)
    return {"used": sorted(used), "mentioned": sorted(mentioned)}


def scope_warning(text: str) -> list[str]:
    """需要提醒学生复核的超纲词（只含「在用」的那类）。"""
    return scan_scope(text)["used"]


def _brief_kp(kg, kp_id: str, role: str = "", extra: dict | None = None) -> dict:
    node = kg.get(kp_id) or {}
    item = {
        "kp_id": kp_id,
        "name": node.get("name", kp_id),
        "role": role,
        "statement": node.get("statement", ""),
        "module_name": node.get("module_name", ""),
        "section_name": node.get("section_name", ""),
        "book_name": node.get("book_name", ""),
    }
    if extra:
        item.update(extra)
    return item


def gather_context(*, question_id: str | None = None, message: str = "",
                   topk: int = 3) -> dict:
    """攒出讲解的事实依据：题目 + 图谱知识点 + 教材原文片段。

    题目的知识点优先用识别阶段对齐好的；**没有对齐过就用题干现查** ——
    学生手输的题干、或对齐失败/没确认的题，不能让讲解失去图谱依据。
    """
    kg = get_kg()
    question = store.get_question(question_id) if question_id else None
    linked: list[dict] = []
    if question:
        for k in question.get("kps") or []:
            if k.get("kp_id"):
                linked.append(_brief_kp(kg, k["kp_id"], k.get("role") or "primary",
                                        {"confidence": k.get("confidence")}))
    source = "识别时对齐"
    if not linked:
        text = (question or {}).get("stem_md") or message
        if text.strip():
            for hit in search(text, topk=topk):
                linked.append(_brief_kp(kg, hit["kp"]["id"], "candidate",
                                        {"score": round(float(hit.get("score") or 0), 3)}))
            source = "按题干现查" if linked else "无"
    return {
        "question": question,
        "kps": linked,
        "kp_source": source,
        "textbook": _textbook_context([{"kp_id": i["kp_id"]} for i in linked], kg),
    }


def _context_prompt(context: dict, message: str, draft: str = "") -> str:
    question = context.get("question") or {}
    kp_lines = "\n".join(
        f"- {i['kp_id']} {i['name']}（{i.get('role') or 'primary'}）：{i.get('statement') or '（无陈述）'}"
        for i in context.get("kps") or []) or "（无：这道题还没对齐到图谱知识点）"
    parts = []
    if question:
        parts.append(f"错题题干：\n{question.get('stem_md') or ''}")
        if question.get("student_answer"):
            parts.append(f"学生的作答：\n{question['student_answer']}")
    else:
        parts.append("（本次没有附带错题，学生直接提问）")
    parts.append(f"学生的问题：\n{message}")
    parts.append(f"图谱知识点清单（只能引用这里面的编号）：\n{kp_lines}")
    parts.append(f"教材原文依据：\n{context.get('textbook') or '（无）'}")
    if draft:
        parts.append(f"上一版解答（请复核）：\n{draft}")
    return "\n\n".join(parts)


def _history_messages(history: list[dict] | None, limit: int = 6) -> list[dict]:
    """把界面上最近几轮对话带上，让学生能追问「那这一步呢？」。

    只保留 role/content，并且截断长度 —— 历史是前端存了发过来的，不能全信。
    """
    out: list[dict] = []
    for item in (history or [])[-limit:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content[:2000]})
    return out


def _clean(data: dict, *, draft: str = "") -> dict:
    """字段兜底：模型偶尔会漏字段或给错类型，别让界面拿到 undefined。"""
    answer = str(data.get("answer_md") or "").strip()
    kp_used = data.get("kp_used")
    if not isinstance(kp_used, list):
        kp_used = []
    issues = data.get("issues")
    if not isinstance(issues, list):
        issues = []
    cleaned_issues = []
    for item in issues:
        if isinstance(item, dict):
            cleaned_issues.append({"type": str(item.get("type") or "其它"),
                                   "detail": str(item.get("detail") or ""),
                                   "fix": str(item.get("fix") or "")})
        elif item:
            cleaned_issues.append({"type": "其它", "detail": str(item), "fix": ""})
    return {
        "answer_md": answer or draft,
        "kp_used": [str(x) for x in kp_used if x],
        "scope_note": str(data.get("scope_note") or ""),
        "follow_up": str(data.get("follow_up") or ""),
        "verdict": str(data.get("verdict") or ""),
        "issues": cleaned_issues,
        "confidence": float(data.get("confidence") or 0) if data.get("confidence") else 0.0,
    }


def draft_answer(message: str, context: dict,
                 history: list[dict] | None = None) -> tuple[dict, str]:
    """第一遍：解答。返回 (结果, 失败原因)。"""
    msgs = [{"role": "system", "content": DRAFT_PROMPT}]
    msgs += _history_messages(history)
    msgs.append({"role": "user", "content": _context_prompt(context, message)})
    try:
        raw = chat_text(msgs, json_mode=True)
        data = _clean(extract_json(raw))
    except VisionError as exc:
        return {}, str(exc)
    if not data["answer_md"]:
        return {}, "模型没有给出解答内容"
    data["scope_hits"] = scope_warning(data["answer_md"])
    data["scope_mentioned"] = scan_scope(data["answer_md"])["mentioned"]
    return data, ""


def review_answer(message: str, draft: dict, context: dict,
                  history: list[dict] | None = None) -> tuple[dict, str]:
    """第二遍：复核并给最终解答。返回 (结果, 失败原因)。"""
    # 复核这一遍不带聊天历史：只针对「问题 + 图谱依据 + 上一版解答」来判断，
    # 免得被前几轮的措辞带偏。
    msgs = [{"role": "system", "content": VERIFY_PROMPT},
            {"role": "user", "content": _context_prompt(context, message,
                                                        draft=draft.get("answer_md", ""))}]
    try:
        raw = chat_text(msgs, json_mode=True)
        data = _clean(extract_json(raw), draft=draft.get("answer_md", ""))
    except VisionError as exc:
        return {}, str(exc)
    if data["verdict"] not in ("ok", "fixed", "reject"):
        # 没有明确结论时按「改过」处理，让界面把修订版放在前面
        data["verdict"] = "fixed" if data["answer_md"] != draft.get("answer_md") else "ok"
    data["scope_hits"] = scope_warning(data["answer_md"])
    data["scope_mentioned"] = scan_scope(data["answer_md"])["mentioned"]
    return data, ""
