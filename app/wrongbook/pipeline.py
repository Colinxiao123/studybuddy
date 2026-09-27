#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错题录入编排：上传 → 识别 → 抽知识点 → 图谱对齐 → 落库 → AI 分析。

对应方案里的 M1–M6。每一步都可单独调用，编排函数只负责串起来并收集告警，
任何一步失败都不会丢掉已识别的内容（降级而非中断）。
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import ROOT, task_config, vision_config
from app.mathkg.graph import root_cause_candidates
from app.mathkg.link import link_candidates, resolve_confirm
from app.mathkg.store import get_kg
from app.mathkg.textutil import normalize, to_plain
from app.wrongbook import store
from app.wrongbook.analyze import extract_kp_candidates, solve_and_analyze
from app.wrongbook.vision import VisionError, is_configured, recognize_question


def _fallback_candidates(stem_md: str, kg, topk: int = 8) -> list[dict]:
    """模型不可用时的兜底：用图谱词典在题干中做名称/别名匹配。

    为什么不直接把整段题干丢进检索：题干上百字会产生几百个 n-gram，
    打分分母被撑大，所有知识点得分都被压到 0.05 以下。改成「词典扫描」
    （题干里出现了哪些知识点名/别名）后，精确得多，且完全确定性。
    结果统一标为 secondary，并在界面提示需人工确认。
    """
    stem_norm = normalize(stem_md)
    if not stem_norm:
        return []
    hits: list[tuple[float, int, str]] = []   # (优先级, 词长, kp_id)
    for term, ids in kg.name_index.items():
        if len(term) >= 2 and term in stem_norm:
            hits.extend((3.0, len(term), kp_id) for kp_id in ids)
    for term, ids in kg.alias_index.items():
        # 别名过滤：太短或指向过多知识点（如「函数」）的别名噪声太大
        if len(term) >= 3 and len(ids) <= 8 and term in stem_norm:
            hits.extend((2.0, len(term), kp_id) for kp_id in ids)
    # 标签扫描很关键：题干常只说上位词（如「奇偶性」），而图谱里没有同名知识点，
    # 概念由「奇函数」「偶函数」承载，只能靠 tags 召回。
    # 但只认「主题标签」（出现在某些知识点名称里的标签），否则「取值范围」
    # 这种题面用语会把毫不相关的知识点拉进来。
    for term in kg.topical_tags:
        ids = kg.tag_index.get(term) or []
        if len(ids) <= 12 and term in stem_norm:
            hits.extend((1.0, len(term), kp_id) for kp_id in ids)

    hits.sort(reverse=True)
    seen: set[str] = set()
    out: list[dict] = []
    for priority, length, kp_id in hits:
        if kp_id in seen:
            continue
        seen.add(kp_id)
        kp = kg.by_id.get(kp_id)
        if not kp:
            continue
        how = {3.0: "名称", 2.0: "别名", 1.0: "标签"}[priority]
        out.append({"name": kp["name"], "role": "secondary",
                    "reason": f"题干中直接出现该知识点的{how}（词典匹配，{length} 字）",
                    # 词典匹配的置信度分级：题干里出现**完整知识点名称**算强证据（可自动采用）；
                    # 只出现别名或标签则属弱证据，封顶到确认区间，交给学生勾选，不冒充高置信。
                    "score_cap": {3.0: 0.82, 2.0: 0.62, 1.0: 0.52}[priority]})
        if len(out) >= topk:
            break
    return out


def _save_assets(student_id: str, question_id: str, files: list[Path]) -> str:
    """把上传的原图归档，返回首张图相对项目根目录的路径（便于直接静态访问）。"""
    if not files:
        return ""
    target = store.asset_dir(student_id, question_id)
    first = ""
    for idx, path in enumerate(files, 1):
        suffix = path.suffix.lower() or ".png"
        dest = target / f"original_{idx}{suffix}"
        shutil.copyfile(path, dest)
        if not first:
            first = dest.relative_to(ROOT).as_posix()
    return first


def _copy_asset(student_id: str, question_id: str, source_rel: str) -> str:
    """把暂存的页面图片复制成这道题自己的资源。

    必须复制而不是直接引用：暂存目录会在入库流程结束后清理掉。
    """
    src = ROOT / source_rel
    if not src.exists():
        return ""
    target = store.asset_dir(student_id, question_id)
    dest = target / f"question{src.suffix or '.png'}"
    shutil.copyfile(src, dest)
    return dest.relative_to(ROOT).as_posix()


def _process_problem(kg, student_id: str, problem: dict, *, files: list[Path],
                     hint: str, student_answer: str, error_type: str,
                     auto_analyze: bool, warnings: list[str], source_name: str,
                     db_path: Path | None, progress=None,
                     low: float = 0.0, high: float = 100.0) -> dict:
    """把一道（已识别的）题走完「抽知识点 → 图谱对齐 → 落库 → AI 解析」。"""
    stem_md = problem["stem_md"].strip()
    stem_plain = problem.get("stem_plain") or stem_md
    figure_desc = problem.get("figure_desc", "")
    ocr_engine = problem.get("engine", "")
    ocr_confidence = float(problem.get("confidence") or 0)
    answer = student_answer or problem.get("student_answer", "") or ""
    if problem.get("answer_edited"):
        # 在勾选页里亲手改过的作答应压过左侧表单里填的总作答，
        # 否则改了等于没改（优先级反过来会静默丢掉学生的修改）
        answer = problem.get("student_answer", "")
    if problem.get("notes"):
        warnings.append(str(problem["notes"]))
    tag = problem.get("label") or problem.get("source") or "这道题"
    width = max(0.0, high - low)

    def stage(ratio: float, message: str) -> None:
        if progress:
            progress.set(low + width * ratio, message)

    # ---------- M3 抽取候选知识点 ----------
    stage(0.10, f"{tag}：抽取知识点…")
    candidates, extract_error = extract_kp_candidates(
        stem_md, figure_desc=figure_desc, student_answer=answer, extra_hint=hint)
    if extract_error:
        candidates = _fallback_candidates(stem_md, kg)
        if candidates:
            warnings.append(f"AI 抽取不可用（{extract_error}），已改用题干直接检索，请人工确认知识点。")
        else:
            warnings.append(f"未能抽取知识点：{extract_error}")

    # ---------- M4 图谱对齐 ----------
    stage(0.55, f"{tag}：对齐图谱知识点…")
    link_result = link_candidates(candidates, kg=kg, context_text=stem_plain)

    # ---------- M5/M6 落库 ----------
    stage(0.70, f"{tag}：写入错题本…")
    question_id = store.create_question(
        student_id, stem_md=stem_md, stem_plain=stem_plain,
        question_type=problem.get("question_type", "solution"),
        ocr_engine=ocr_engine, ocr_confidence=ocr_confidence, student_answer=answer,
        source_name=source_name, raw=problem, status="pending", db_path=db_path)
    # 识别阶段已经把每页图片暂存下来并挂在题目上了（多题页抢先下），
    # 优先用它；没有就退回拷贝本次上传的文件
    asset_path = _copy_asset(student_id, question_id, problem["asset_path"]) \
        if problem.get("asset_path") else ""
    if not asset_path:
        asset_path = _save_assets(student_id, question_id, files)
    if asset_path:
        with store.connect(db_path) as conn:
            conn.execute("UPDATE questions SET asset_path = ? WHERE id = ?",
                         (asset_path, question_id))

    store.save_question_kps(question_id, link_result["linked"], db_path=db_path)
    wrong_id = store.add_wrong_record(question_id, student_id, error_type=error_type,
                                     db_path=db_path)

    result = {
        "question_id": question_id,
        "wrong_record_id": wrong_id,
        "student_id": student_id,
        "label": problem.get("label", ""),
        "stem_md": stem_md,
        "question_type": problem.get("question_type", "solution"),
        "asset_path": asset_path,
        "ocr_engine": ocr_engine,
        "ocr_confidence": ocr_confidence,
        "figure_desc": figure_desc,
        "student_answer": answer,
        "kp_candidates": candidates,
        "linked": link_result["linked"],
        "needs_confirm": link_result["needs_confirm"],
        "out_of_graph": link_result["out_of_graph"],
        "stats": link_result["stats"],
        "warnings": warnings,
        "analysis": None,
    }

    # ---------- M5 AI 解答与错因分析 ----------
    if auto_analyze:
        stage(0.82, f"{tag}：生成 AI 解析…")
        analysis, analyze_error = analyze_and_store(question_id, kg=kg, db_path=db_path)
        result["analysis"] = analysis
        if analyze_error:
            warnings.append(f"AI 解析未生成：{analyze_error}")
    stage(1.0, f"{tag}：完成")
    return result


def ingest(student_id: str, *, files: list[Path] | None = None, stem_text: str = "",
           hint: str = "", student_answer: str = "", error_type: str = "unknown",
           auto_analyze: bool = True, picks: list[int] | str | None = None,
           db_path: Path | None = None, progress=None,
           stash_dir: Path | None = None) -> dict:
    """端到端录入错题。

    picks 用来处理「一张图里有多道题」：
      None     只有一道题就直接入库；多道题则**先不入库**，返回 needs_pick 让学生勾选
      [0, 2]   只入库第 1、3 道
      "all"    全部入库

    progress   传 app.progress.Progress 实例时会上报实时进度
    stash_dir  给定时把每页图片暂存下来，返回的 problems_full 里会带图片路径，
               让后续 commit_picks 无需重新上传与重新识别
    """
    kg = get_kg()
    warnings: list[str] = []
    files = [Path(f) for f in (files or [])]
    problems: list[dict] = []
    recognized: dict = {}
    ocr_engine = ""

    def report(percent: float, message: str) -> None:
        if progress:
            progress.set(percent, message)

    # ---------- M1/M2 识别（占总进度 5%–55%）----------
    report(1.0, "准备文件…")
    if files:
        ok, note = is_configured()
        if not ok:
            raise VisionError(note)
        try:
            recognized = recognize_question(
                files, hint=hint,
                # 并发完成的顺序是乱的，所以文案说「已完成 N/M 页」而不是「正在识别某页」，
                # 否则看起来像是进度在往回跳
                on_page_done=lambda index, total, label, done: progress and progress.span(
                    5, 55, done, total, f"识别中…（已完成 {done}/{total} 页，{label}）"),
                stash_dir=stash_dir)
            problems = recognized["problems"]
            ocr_engine = recognized.get("engine", "")
            page_count = recognized.get("page_count", 1)
            warnings.extend(recognized.get("warnings") or [])
            if page_count > 1:
                warnings.append(f"已按页识别 {page_count} 页。")
        except VisionError as exc:
            if not stem_text.strip():
                raise
            warnings.append(f"图片识别失败，已改用你输入的题干：{exc}")

    if not problems and stem_text.strip():
        problems = [{"label": "", "stem_md": stem_text.strip(), "stem_plain": "",
                     "question_type": "solution", "student_answer": student_answer,
                     "figure_desc": "", "confidence": 0.0, "notes": "",
                     "engine": ocr_engine}]

    if not problems:
        raise VisionError("没有可用的题干：请上传图片或直接输入题干文字。")

    # ---------- 多题：先让学生勾选要记录哪几道 ----------
    if len(problems) > 1 and picks is None:
        report(100.0, f"识别到 {len(problems)} 道题，等待勾选")
        return {
            "needs_pick": True,
            "saved": False,
            "problem_count": len(problems),
            "ocr_engine": ocr_engine,
            "student_id": student_id,
            "warnings": warnings,
            # 完整题目（含图片路径）交给调用方缓存，供 commit_picks 复用
            "problems_full": problems,
            "problems": [{
                "index": i,
                "label": p.get("label", ""),
                "page": p.get("page"),
                "source": p.get("source", ""),
                "stem_md": p["stem_md"],
                "question_type": p.get("question_type", "solution"),
                "confidence": float(p.get("confidence") or 0),
                "student_answer": p.get("student_answer", ""),
                "figure_desc": p.get("figure_desc", ""),
            } for i, p in enumerate(problems)],
        }

    if picks is None:
        selected = [(0, problems[0])]
    elif picks == "all":
        selected = list(enumerate(problems))
    else:
        selected = [(i, problems[i]) for i in picks if 0 <= i < len(problems)]
        if not selected:
            raise VisionError(f"勾选的题号无效：{picks}（共 {len(problems)} 道）")

    # ---------- 逐题处理（占总进度 55%–100%）----------
    source_name = files[0].name if files else "手动输入"
    results = _run_problems(kg, student_id, selected, files=files, hint=hint,
                            student_answer=student_answer, error_type=error_type,
                            auto_analyze=auto_analyze, warnings=warnings,
                            source_name=source_name, db_path=db_path,
                            progress=progress, low=55.0, high=100.0)

    result = results[0]
    result["needs_pick"] = False
    result["saved"] = True
    result["created_count"] = len(results)
    if len(results) > 1:
        result["extra"] = results[1:]
    return result


class _NoteOnly:
    """只转发文案、不动百分比的进度适配器（并发场景用）。"""

    def __init__(self, progress, tag: str):
        self.progress = progress
        self.tag = tag

    def set(self, percent: float, message: str) -> None:   # noqa: ARG002
        if self.progress:
            self.progress.note(f"{self.tag}：{message.split('：', 1)[-1]}")


def _run_problems(kg, student_id: str, selected: list[tuple[int, dict]], *,
                  files: list[Path], hint: str, student_answer: str, error_type: str,
                  auto_analyze: bool, warnings: list[str], source_name: str,
                  db_path: Path | None, progress=None,
                  low: float = 0.0, high: float = 100.0) -> list[dict]:
    """并发处理多道题（结果按传入顺序返回）。

    每题要跑一次模型调用抽取知识点、再跟一次生成解析，基本都是等待；
    串行时 2 道题就要 40 秒以上，所以并发发出去。
    写库的并发安全由 app/wrongbook/store.py 的写锁 + busy_timeout 保证。

    进度约定：并发时**百分比只由「已完成几道」驱动**，逐题的阶段细节只更新文案。
    否则后一道题的起始百分比（比如第二道从 55% 开始）会把进度条直接拉到那里，
    前面那道题的进展就再也显示不出来了（实测条卡在 55% 不动）。
    """
    if not selected:
        return []
    total = len(selected)
    solo = total == 1
    width = (high - low) / total
    counter_lock = threading.Lock()
    done_count = 0

    def run(order: int, problem: dict) -> dict:
        nonlocal done_count
        tag = problem.get("label") or problem.get("source") or f"第{order + 1}道"
        reporter = progress
        if progress and not solo:
            reporter = _NoteOnly(progress, tag)
        result = _process_problem(kg, student_id, problem, files=files, hint=hint,
                                  student_answer=student_answer, error_type=error_type,
                                  auto_analyze=auto_analyze, warnings=list(warnings),
                                  source_name=source_name, db_path=db_path,
                                  progress=reporter,
                                  low=low + order * width,
                                  high=low + (order + 1) * width)
        if progress and not solo:
            with counter_lock:
                done_count += 1
                snapshot = done_count
            progress.span(low, high, snapshot, total,
                          f"已完成 {snapshot}/{total} 道（{tag}）")
        return result

    workers = max(1, min(int(task_config()["concurrency"]), total))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, order, problem)
                       for order, (_, problem) in enumerate(selected)]
            return [f.result() for f in futures]
    return [run(order, problem) for order, (_, problem) in enumerate(selected)]


def apply_problem_edits(problems: list[dict], edits: dict | None) -> list[str]:
    """把学生在前端「原文」栏里改过的题干/作答写回题目。

    edits 形如 {"0": {"stem_md": "...", "student_answer": "..."}}（key 是题号）。
    只允许改这两个字段：题号越界、空题干一律忽略并记一条警告，不能因为
    一个错索引就把整批入库搞挂。
    改了 stem_md 必须重算 stem_plain —— 它是检索用纯文本，不能直接拿带 $ 的原样去匹配。
    """
    warnings: list[str] = []
    if not isinstance(edits, dict):
        return warnings
    for key, patch in edits.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            warnings.append(f"忽略了一条无效的修改（题号 {key} 不是数字）。")
            continue
        if not (0 <= index < len(problems)) or not isinstance(patch, dict):
            warnings.append(f"忽略了一条越界的修改（第 {index + 1} 题）。")
            continue
        label = problems[index].get("label") or f"第{index + 1}道"
        if "stem_md" in patch:
            text = str(patch["stem_md"] or "").strip()[:8000]
            if not text:
                warnings.append(f"{label}：题干不能改成空的，已沿用识别结果。")
            elif text != (problems[index].get("stem_md") or "").strip():
                problems[index]["stem_md"] = text
                problems[index]["stem_plain"] = to_plain(text)
                warnings.append(f"{label}：已按你的修改更新题干。")
        if "student_answer" in patch:
            answer = str(patch["student_answer"] or "").strip()[:8000]
            if answer != (problems[index].get("student_answer") or "").strip():
                problems[index]["student_answer"] = answer
                # 打上标记：入库时这份手改的要优先于左侧表单里的总作答
                problems[index]["answer_edited"] = True
                warnings.append(f"{label}：已按你的修改更新作答。")
    return warnings


def commit_picks(student_id: str, problems: list[dict], picks: list[int] | str, *,
                 error_type: str = "unknown", auto_analyze: bool = True,
                 hint: str = "",
                 edits: dict | None = None,
                 source_name: str = "页面入库", db_path: Path | None = None,
                 progress=None) -> dict:
    """把**已经识别好**的题目按勾选入库：不重新上传、不重新识别。

    problems 直接取自 ingest 返回的 problems_full（其中 asset_path 已指向暂存的页面图片）。
    edits 是学生在「原文」栏手改的题干/作答，入库前先应用。

    为什么必须复用而不是重跑识别：模型每次输出可能不同，
    重跑会出现「勾选的是第 5 题，入库的却是另一道题」这种对不上的情况。

    注意这里**不收**上传表单里的 student_answer：那是给单题（不走勾选页）用的，
    套到整页每道题头上会出现「13 道题的作答全是同一句话」，而且预览里看不到。
    勾选流程的作答只认每题自己的（识别结果，或学生在原文栏里写的）。
    """
    kg = get_kg()
    warnings: list[str] = apply_problem_edits(problems, edits)
    if picks == "all":
        selected = list(enumerate(problems))
    else:
        selected = [(i, problems[i]) for i in picks if 0 <= i < len(problems)]
    if not selected:
        raise VisionError(f"勾选的题号无效：{picks}（共 {len(problems)} 道）")

    results = _run_problems(kg, student_id, selected, files=[], hint=hint,
                            student_answer="", error_type=error_type,
                            auto_analyze=auto_analyze, warnings=warnings,
                            source_name=source_name, db_path=db_path,
                            progress=progress)
    result = results[0]
    result.update({"needs_pick": False, "saved": True,
                   "created_count": len(results), "warnings": warnings})
    if len(results) > 1:
        result["extra"] = results[1:]
    return result


def analyze_and_store(question_id: str, *, kg=None, db_path: Path | None = None) -> tuple[dict, str]:
    """对已入库的题目生成 AI 解答、错因归类与根因候选，并写回数据库。"""
    kg = kg or get_kg()
    question = store.get_question(question_id, db_path=db_path)
    if not question:
        return {}, f"题目不存在：{question_id}"
    linked = [{"kp_id": k["kp_id"], "name": k["kp_id"], "role": k["role"],
               "statement": ""} for k in question["kps"]]
    for item in linked:
        kp = kg.get(item["kp_id"])
        if kp:
            item["name"] = kp["name"]
            item["statement"] = kp.get("statement", "")

    data, error = solve_and_analyze(
        stem_md=question["stem_md"], linked=linked, kg=kg,
        student_answer=question.get("student_answer") or "")
    if error:
        return {}, error

    # 根因下钻：把「考查的知识点」沿强前置链上溯，给出更基础的薄弱点候选
    primary = [k["kp_id"] for k in question["kps"] if k["role"] in ("primary", "secondary")]
    roots = root_cause_candidates(kg, primary, max_depth=3, topk=4) if primary else []
    data["root_causes"] = roots

    store.save_analysis(question_id,
                        ai_answer=data.get("ai_answer", ""),
                        ai_analysis=data.get("ai_analysis", ""),
                        status="analyzed", db_path=db_path)
    with store.connect(db_path) as conn:
        conn.execute("UPDATE wrong_records SET error_type = ?, analysis = ? "
                     "WHERE question_id = ? AND error_type = 'unknown'",
                     (data.get("error_type", "unknown"), data.get("error_detail", ""),
                      question_id))
    return data, ""


def apply_confirmations(question_id: str, chosen: list[dict], *,
                        drop_kp_ids: list[str] | None = None,
                        db_path: Path | None = None) -> list[dict]:
    """应用学生对「待确认知识点」的选择：新增选中的、删除被否掉的。"""
    kg = get_kg()
    resolved = resolve_confirm(chosen, kg=kg)
    if resolved:
        store.save_question_kps(question_id, resolved, db_path=db_path)
    for kp_id in drop_kp_ids or []:
        with store.connect(db_path) as conn:
            conn.execute("DELETE FROM question_kps WHERE question_id = ? AND kp_id = ?",
                         (question_id, kp_id))
    question = store.get_question(question_id, db_path=db_path)
    return question["kps"] if question else []


def check_environment() -> dict:
    """启动时自检，把可用的/缺失的能力一次性告诉界面。"""
    ok, note = is_configured()
    cfg = vision_config()
    return {
        "vision_ready": ok,
        "vision_note": note,
        "vision_provider": cfg.get("provider", ""),
        "vision_model": cfg.get("model", ""),
    }
