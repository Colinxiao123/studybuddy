#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 服务（FastAPI）：错题本 + 图谱查询。

启动：
    python -m app.server                 # http://127.0.0.1:8000
    python -m app.server --port 9000
"""

from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import progress as jobs
from app.config import (ASSETS_DIR, DATA_DIR, ROOT, STATIC_DIR, save_env,
                        settings_snapshot, vision_config)

PENDING_DIR = DATA_DIR / "pending"      # 识别阶段暂存的页面图片，入库后清理
from app.mathkg.graph import impact_rank, learning_path, root_cause_candidates
from app.mathkg.link import link_text
from app.mathkg.search import search
from app.mathkg.store import get_kg
from app.wrongbook import chat, pipeline, store, stats
from app.wrongbook.vision import VisionError, list_models, test_connection

app = FastAPI(title="StudyBuddy", version="0.1.0")

KP_FIELDS = ("id", "name", "kp_type", "kp_type_cn", "module", "module_name", "section_id",
             "sec_id", "section_no", "section_name", "chapter_name", "book", "book_name",
             "difficulty", "difficulty_cn", "importance", "importance_cn", "statement",
             "latex", "tags", "aliases", "status", "in_degree", "prereq_in_degree",
             "out_degree", "segment_file", "source", "notes")


def kp_brief(kp: dict) -> dict:
    return {k: kp.get(k) for k in KP_FIELDS}


@app.exception_handler(VisionError)
async def vision_error_handler(_request, exc: VisionError):
    return JSONResponse(status_code=400, content={"error": str(exc), "kind": "vision"})


# ------------------------------------------------------------------ 静态资源
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    # 204 响应不能带 body：JSONResponse(content=None) 会发出 b"null"，
    # 新版 uvicorn 做 Content-Length 校验时会报 "Response content longer than Content-Length"。
    return Response(status_code=204)


# ------------------------------------------------------------------ 基础信息
@app.get("/api/health")
async def health() -> dict:
    kg = get_kg()
    return {"ok": True, "kg": kg.stats(), "environment": pipeline.check_environment()}


# ------------------------------------------------------------------ 设置
# 界面上的字段名 → 写入 .env 的键名
_SETTINGS_FIELDS = {
    "provider": "VISION_PROVIDER",
    "base": "VISION_API_BASE",
    "model": "VISION_MODEL",
    "text_model": "TEXT_MODEL",
    "max_tokens": "API_MAX_TOKENS",
    "max_pages": "VISION_MAX_PAGES",
    "concurrency": "VISION_CONCURRENCY",
}


def _cfg_from_payload(payload: dict) -> dict:
    """把界面上填的值叠到已保存的配置上（未填的字段保持原值）。"""
    cfg = dict(vision_config())
    for field in ("provider", "base", "model"):
        value = str(payload.get(field, "")).strip()
        if value:
            cfg[field] = value
    api_key = str(payload.get("api_key", "")).strip()
    if api_key and api_key != "__clear__":
        cfg["key"] = api_key
    for field in ("max_tokens", "timeout"):
        if str(payload.get(field, "")).strip().isdigit():
            cfg[field] = int(payload[field])
    return cfg


@app.get("/api/settings")
async def api_settings() -> dict:
    """当前模型配置（密钥打码），供网页「设置」面板回显。"""
    return settings_snapshot()


@app.post("/api/settings")
async def api_settings_save(payload: dict) -> dict:
    """保存模型配置并立即生效，不需要重启程序。"""
    updates = {env_key: str(payload[field]).strip()
               for field, env_key in _SETTINGS_FIELDS.items() if field in payload}

    # api_key 留空表示「不改动」；传 __clear__ 表示清除、回落到默认。
    # 这样界面上不用把真密钥回显出来，也不会把打码串误存回去。
    api_key = str(payload.get("api_key", "")).strip()
    if api_key == "__clear__":
        updates["VISION_API_KEY"] = ""
    elif api_key:
        updates["VISION_API_KEY"] = api_key

    if not updates:
        raise HTTPException(400, "没有收到任何要保存的字段")

    path = save_env(updates)
    ok, note = pipeline.is_configured()
    return {"ok": True, "saved_to": str(path), "settings": settings_snapshot(),
            "vision_ready": ok, "vision_note": note}


@app.post("/api/settings/test")
async def api_settings_test(payload: dict | None = None) -> dict:
    """用界面上填的值先试一次连通性（不落盘），避免存错了才发现。"""
    return test_connection(_cfg_from_payload(payload or {}))


@app.post("/api/settings/models")
async def api_settings_models(payload: dict | None = None) -> dict:
    """按界面上的地址与密钥拉取可用模型列表（不落盘），供下拉选择。"""
    return list_models(_cfg_from_payload(payload or {}))


@app.get("/api/modules")
async def modules() -> dict:
    kg = get_kg()
    return {"modules": kg.modules}


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1), module: str | None = None,
                     topk: int = 12) -> dict:
    hits = search(q, topk=topk, module=module or None)
    return {"query": q, "results": [{"kp": kp_brief(h["kp"]), "score": h["score"],
                                     "evidence": h["evidence"]} for h in hits]}


@app.get("/api/kp/{kp_id}")
async def api_kp(kp_id: str, student_id: str = "default") -> dict:
    detail = stats.kp_detail(student_id, kp_id)
    if not detail:
        raise HTTPException(404, f"知识点不存在：{kp_id}")
    detail["kp"] = kp_brief(detail["kp"])
    return detail


@app.get("/api/impact")
async def api_impact(topk: int = 15) -> dict:
    return {"items": impact_rank(get_kg(), topk)}


@app.get("/api/books")
async def api_books() -> dict:
    from app.config import BOOK_CN
    kg = get_kg()
    counter: dict[str, int] = {}
    for kp in kg.kps:
        book = kp.get("book") or ""
        if book:
            counter[book] = counter.get(book, 0) + 1
    order = ["BX1", "BX2", "BX3", "XB1", "XB2"]
    return {"books": [{"code": b, "name": BOOK_CN.get(b, b), "kp_count": counter[b]}
                      for b in order if b in counter]}


@app.get("/api/graph")
async def api_graph(book: str | None = None, module: str | None = None,
                    focus: str | None = None, depth: int = 2,
                    student_id: str = "default") -> dict:
    """可视化用子图：图谱结构 + 学生错题热度叠加。

    返回的每个节点带 heat（热度分级）与 wrong_count，
    前端据此给节点着色（错得多越红）、按被依赖数调整大小（越枢纽越大）。
    """
    from app.mathkg.graph import subgraph
    try:
        data = subgraph(get_kg(), book=book or None, module=module or None,
                        focus=focus or None, depth=depth)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    wrong = store.kp_wrong_stats(student_id)
    max_wrong = max([v["wrong_count"] or 0 for v in wrong.values()] or [0]) or 1
    hot = 0
    for node in data["nodes"]:
        stat = wrong.get(node["id"])
        count = int(stat["wrong_count"]) if stat else 0
        node["wrong_count"] = count
        node["wrong_times"] = int(stat.get("wrong_times") or 0) if stat else 0
        node["last_wrong_at"] = (stat or {}).get("last_wrong_at") or ""
        node["heat"] = store.heat_level(count)
        if count:
            hot += 1
    data["stats"].update({"wrong_kp": hot, "max_wrong": max_wrong,
                          "student_id": student_id})
    return data


# ------------------------------------------------------------------ 学习路径
@app.get("/api/path")
async def api_path(target: str, known: str = "", max_depth: int = 6) -> dict:
    known_set = {k.strip() for k in known.split(",") if k.strip()}
    if target not in get_kg().by_id:
        raise HTTPException(404, f"知识点不存在：{target}")
    return learning_path(get_kg(), target, known=known_set, max_depth=max_depth)


@app.get("/api/root-cause")
async def api_root_cause(kp_ids: str, student_id: str = "default",
                         max_depth: int = 3) -> dict:
    ids = [k.strip() for k in kp_ids.split(",") if k.strip()]
    weakness = {k: v["wrong_count"] for k, v in
                store.kp_wrong_stats(student_id).items() if v.get("wrong_count")}
    return {"items": root_cause_candidates(get_kg(), ids, max_depth=max_depth,
                                           weakness=weakness)}


# ------------------------------------------------------------------ 错题本
def _parse_csv(raw: str) -> list[str]:
    """把 "concept,calc" 拆成 ['concept', 'calc']（去空白、去空项）。"""
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _parse_date(raw: str) -> str:
    """只接受 YYYY-MM-DD，其它一律忽略。

    这个值会被拼进 SQL 的 date(?)，不校验的话调用方能塞进任意字符串。
    """
    raw = (raw or "").strip()
    return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else ""


def _parse_picks(picks: str) -> list[int] | str | None:
    """空 = 未指定；"all" = 全部；"0,2" = 只取第 1、3 道。"""
    raw = (picks or "").strip()
    if not raw:
        return None
    if raw == "all":
        return "all"
    parsed = [int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()]
    if not parsed:
        raise HTTPException(400, f"picks 参数无法解析：{picks}")
    return parsed


async def _save_uploads(files: list[UploadFile], tmpdir: Path) -> list[Path]:
    saved: list[Path] = []
    for upload in files or []:
        if not upload.filename:
            continue
        dest = tmpdir / Path(upload.filename).name
        with dest.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        saved.append(dest)
    return saved


@app.post("/api/ingest")
async def api_ingest(
    student_id: str = Form("default"),
    stem_text: str = Form(""),
    hint: str = Form(""),
    student_answer: str = Form(""),
    error_type: str = Form("unknown"),
    auto_analyze: bool = Form(True),
    # 一张图里有多道题时用它挑：空 = 先返回题目清单让学生勾选；
    # "all" = 全部入库；"0,2" = 只入库第 1、3 道
    picks: str = Form(""),
    files: list[UploadFile] = File(default=[]),  # noqa: B008
) -> dict:
    """同步录入（会一直等到完成，适合脚本与测试）。界面请用 /api/ingest_async。"""
    parsed_picks = _parse_picks(picks)
    tmpdir = Path(tempfile.mkdtemp(prefix="upload_"))
    try:
        saved = await _save_uploads(files, tmpdir)
        return pipeline.ingest(student_id, files=saved, stem_text=stem_text, hint=hint,
                               student_answer=student_answer, error_type=error_type,
                               auto_analyze=auto_analyze, picks=parsed_picks)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_ingest_job(job, payload: dict, tmpdir: Path) -> None:
    """后台线程：跑完整个识别流程，把进度与结果写回 job。"""
    reporter = jobs.Progress(job)
    try:
        result = pipeline.ingest(
            payload["student_id"], files=payload["files"], stem_text=payload["stem_text"],
            hint=payload["hint"], student_answer=payload["student_answer"],
            error_type=payload["error_type"], auto_analyze=payload["auto_analyze"],
            picks=payload["picks"], progress=reporter,
            stash_dir=payload.get("stash_dir"))
        # 完整题目（含图片路径）存在服务端，供 /api/ingest_commit 复用。
        # 不进轮询响应：那会让前端每轮都传输几十道题的全文。
        if result.get("problems_full"):
            job.payload["problems_full"] = result.pop("problems_full")
        reporter.finish(result)
    except Exception as exc:                     # noqa: BLE001
        # 后台线程里抛异常没人接，必须自己吞下并写进任务状态，
        # 否则前端会一直轮询一个永远 running 的任务
        reporter.fail(str(exc))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_commit_job(job, source, args: dict) -> None:
    """后台线程：把「已识别但未入库」的题目按勾选入库。"""
    reporter = jobs.Progress(job)
    try:
        result = pipeline.commit_picks(
            args["student_id"], source.payload["problems_full"], args["picks"],
            error_type=args["error_type"], auto_analyze=args["auto_analyze"],
            hint=args["hint"], edits=args.get("edits"), progress=reporter)
        reporter.finish(result)
    except Exception as exc:                     # noqa: BLE001
        reporter.fail(str(exc))
    finally:
        # 图片已经复制成每道题自己的资源，暂存目录可以清掉了
        stash = (source.payload or {}).get("stash_dir")
        if stash:
            shutil.rmtree(str(stash), ignore_errors=True)
        source.payload.pop("problems_full", None)


@app.post("/api/ingest_async")
async def api_ingest_async(
    student_id: str = Form("default"),
    stem_text: str = Form(""),
    hint: str = Form(""),
    student_answer: str = Form(""),
    error_type: str = Form("unknown"),
    auto_analyze: bool = Form(True),
    picks: str = Form(""),
    files: list[UploadFile] = File(default=[]),  # noqa: B008
) -> dict:
    """异步录入：立即返回 job_id，用 GET /api/jobs/{job_id} 轮询进度。"""
    parsed_picks = _parse_picks(picks)
    tmpdir = Path(tempfile.mkdtemp(prefix="upload_"))
    try:
        saved = await _save_uploads(files, tmpdir)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    job = jobs.create_job("ingest")
    stash_dir = PENDING_DIR / job.id
    job.payload["stash_dir"] = stash_dir
    payload = {"student_id": student_id, "files": saved, "stem_text": stem_text,
               "hint": hint, "student_answer": student_answer,
               "error_type": error_type, "auto_analyze": auto_analyze,
               "picks": parsed_picks, "stash_dir": stash_dir}
    threading.Thread(target=_run_ingest_job, args=(job, payload, tmpdir),
                     daemon=True).start()
    return {"job_id": job.id, "state": job.state, "percent": job.percent,
            "message": job.message}


@app.post("/api/ingest_commit")
async def api_ingest_commit(payload: dict) -> dict:
    """把上一轮识别出的题目按勾选入库。

    故意不接收文件：识别结果已在服务端缓存，重新上传 + 重新识别不仅慢，
    更麻烦的是模型重跑可能给出与勾选时**不一样的题目**。
    """
    source = jobs.get_job(str(payload.get("job_id") or ""))
    if not source or not source.payload.get("problems_full"):
        raise HTTPException(404, "找不到待入库的识别结果（任务可能已过期，请重新上传）")

    raw_picks = payload.get("picks")
    if raw_picks is None:
        raise HTTPException(400, "缺少 picks 参数")
    if raw_picks == "all":
        picks: list[int] | str = "all"
    else:
        try:
            picks = [int(x) for x in raw_picks]
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"picks 参数格式错误：{raw_picks}") from exc
        if not picks:
            raise HTTPException(400, "请至少勾选一道题")

    job = jobs.create_job("commit")
    edits = payload.get("edits")
    if edits is not None and not isinstance(edits, dict):
        raise HTTPException(400, "edits 参数格式错误：应为 {题号: {字段: 新值}}")
    args = {
        "student_id": str(payload.get("student_id") or source.payload.get("student_id") or "default"),
        "picks": picks,
        "error_type": str(payload.get("error_type") or "unknown"),
        "auto_analyze": bool(payload.get("auto_analyze", True)),
        "hint": str(payload.get("hint") or ""),
        "edits": edits,
    }
    threading.Thread(target=_run_commit_job, args=(job, source, args),
                     daemon=True).start()
    return {"job_id": job.id, "state": job.state, "percent": job.percent,
            "message": "准备入库…", "reused_problems": len(source.payload["problems_full"])}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str) -> dict:
    """查询后台任务进度。state: running / done / error。"""
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, f"任务不存在或已过期：{job_id}")
    return job.snapshot()


@app.get("/api/wrongbook")
async def api_wrongbook(student_id: str = "default", kp_id: str | None = None,
                        error_type: str | None = None, error_types: str = "",
                        date_from: str = "", date_to: str = "", sort: str = "newest",
                        limit: int = 200, offset: int = 0) -> dict:
    """错题列表。error_types 用逗号分隔多选（如 concept,calc）。"""
    kg = get_kg()
    types = _parse_csv(error_types)
    if error_type:
        types.append(error_type)          # 兼容早期的单值参数
    frm = _parse_date(date_from)
    to = _parse_date(date_to)
    order = sort if sort in store.SORTS else "newest"
    items = store.list_questions(student_id, kp_id=kp_id or None, error_types=types,
                                 date_from=frm, date_to=to, sort=order,
                                 limit=limit, offset=offset)
    for item in items:
        store.attach_kp_names(item["kps"], kg)
    return {"items": items,
            "total": store.student_overview(student_id)["question_count"],
            "matched": store.count_questions(student_id, kp_id=kp_id or None,
                                             error_types=types,
                                             date_from=frm, date_to=to),
            "filters": {"error_types": types, "date_from": frm, "date_to": to,
                        "sort": order},
            "overview": store.student_overview(student_id)}


@app.get("/api/wrongbook/{question_id}")
async def api_wrongbook_detail(question_id: str) -> dict:
    item = store.get_question(question_id)
    if not item:
        raise HTTPException(404, f"题目不存在：{question_id}")
    kg = get_kg()
    store.attach_kp_names(item["kps"], kg)
    for kp in item["kps"]:
        node = kg.get(kp["kp_id"]) or {}
        kp["section_name"] = node.get("section_name", "")
        kp["statement"] = node.get("statement", "")
    primary = [k["kp_id"] for k in item["kps"] if k["role"] in ("primary", "secondary")]
    item["root_causes"] = root_cause_candidates(
        kg, primary, max_depth=3,
        weakness={k: v["wrong_count"] for k, v in store.kp_wrong_stats(
            item["student_id"]).items() if v.get("wrong_count")},
        topk=4) if primary else []
    return item


@app.delete("/api/wrongbook/{question_id}")
async def api_delete(question_id: str) -> dict:
    store.delete_question(question_id)
    return {"ok": True, "deleted": question_id}


@app.post("/api/wrongbook/{question_id}/confirm")
async def api_confirm(question_id: str, payload: dict) -> dict:
    kps = pipeline.apply_confirmations(
        question_id, payload.get("chosen") or [],
        drop_kp_ids=payload.get("drop") or [])
    return {"ok": True, "kps": kps}


@app.post("/api/wrongbook/{question_id}/analyze")
async def api_analyze(question_id: str) -> dict:
    data, error = pipeline.analyze_and_store(question_id)
    if error:
        raise HTTPException(400, error)
    return {"ok": True, "analysis": data}


@app.post("/api/wrongbook/{question_id}/stem")
async def api_update_stem(question_id: str, payload: dict) -> dict:
    """订正题干/作答（把识别错了的地方手工改回来）。

    只改文本：知识点与已有解析原地不动 —— 解析是围绕原题干生成的，
    改完题干是否重跑交给前端再调 /analyze，免得每次小改都白等半分钟。
    """
    if not store.get_question(question_id):
        raise HTTPException(404, f"题目不存在：{question_id}")

    stem = payload.get("stem_md")
    answer = payload.get("student_answer")
    if stem is None and answer is None:
        raise HTTPException(400, "没有要修改的内容：请提供 stem_md 或 student_answer")
    if stem is not None and not str(stem).strip():
        raise HTTPException(400, "题干不能改成空的")

    store.update_question_text(
        question_id,
        stem_md=str(stem).strip()[:8000] if stem is not None else None,
        student_answer=str(answer or "").strip()[:8000] if answer is not None else None)
    return {"ok": True, "question": store.get_question(question_id)}


@app.post("/api/wrongbook/{question_id}/resolve")
async def api_resolve(question_id: str) -> dict:
    store.resolve_wrong(question_id)
    return {"ok": True}


@app.get("/api/link")
async def api_link(q: str, module: str | None = None, topk: int = 8) -> dict:
    return {"results": [{"kp": kp_brief(h["kp"]), "score": h["score"],
                         "evidence": h["evidence"]}
                        for h in link_text(q, topk=topk, module=module or None)]}


# ------------------------------------------------------------------ AI 对话
def _chat_input(payload: dict) -> tuple[str, str, dict, list[dict], list[dict]]:
    """抽出对话接口的公共入参并校验。"""
    message = str(payload.get("message") or "").strip()[:4000]
    question_id = str(payload.get("question_id") or "").strip()
    if not message and not question_id:
        raise HTTPException(400, "请先输入问题，或附上一道错题")
    if not message:
        message = "这道题我不会，请讲一下怎么想。"      # 只附题不写字时的默认问法
    context = chat.gather_context(question_id=question_id or None, message=message)
    history = payload.get("history") or []
    return message, question_id, context, history, [
        {k: v for k, v in item.items() if k in ("kp_id", "name", "role", "statement",
                                                "module_name", "section_name", "score",
                                                "confidence")}
        for item in context["kps"]]


@app.post("/api/chat/draft")
async def api_chat_draft(payload: dict) -> dict:
    """第一遍：给出解答（带图谱依据与高中范围约束）。"""
    message, question_id, context, history, kps = _chat_input(payload)
    data, error = chat.draft_answer(message, context, history)
    if error:
        raise HTTPException(400, error)
    return {"message": message, "question_id": question_id or None,
            "kps": kps, "kp_source": context["kp_source"],
            "draft": data, "scope_hits": data.get("scope_hits") or []}


@app.post("/api/chat/verify")
async def api_chat_verify(payload: dict) -> dict:
    """第二遍：复核上一版解答并给最终版。"""
    draft = payload.get("draft") or {}
    if not isinstance(draft, dict) or not str(draft.get("answer_md") or "").strip():
        raise HTTPException(400, "缺少待复核的解答（draft.answer_md）")
    message, question_id, context, history, kps = _chat_input(payload)
    data, error = chat.review_answer(message, draft, context, history)
    if error:
        raise HTTPException(400, error)
    return {"question_id": question_id or None, "kps": kps,
            "review": data, "scope_hits": data.get("scope_hits") or []}


# ------------------------------------------------------------------ 统计
@app.get("/api/heatmap")
async def api_heatmap(student_id: str = "default", module: str | None = None,
                      only_wrong: bool = False) -> dict:
    return stats.heat_map(student_id, module=module or None, only_wrong=only_wrong)


@app.get("/api/module-summary")
async def api_module_summary(student_id: str = "default") -> dict:
    return {"items": stats.module_summary(student_id)}


@app.get("/api/review-plan")
async def api_review_plan(student_id: str = "default", limit: int = 10) -> dict:
    return {"items": stats.review_plan(student_id, limit=limit)}


@app.get("/api/students")
async def api_students() -> dict:
    return {"students": store.list_students() or ["default"]}


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description="StudyBuddy Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()
    store.init_db()
    print(f"\n  StudyBuddy  →  http://{args.host}:{args.port}\n")
    uvicorn.run("app.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
