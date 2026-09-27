#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉识别适配层：把错题图片/PDF 转成结构化题目。

Provider 走 OpenAI 兼容的 /chat/completions 协议，因此：
    DeepSeek / 通义 / 智谱 / 本地 vLLM / Ollama 都只需改 .env 里的三项配置，
    业务代码不变。

app/.env 配置项：
    VISION_PROVIDER=deepseek          # deepseek / openai / ollama / mock
    VISION_API_BASE=https://api.deepseek.com/v1
    VISION_API_KEY=sk-xxxx
    VISION_MODEL=<你在 DeepSeek 控制台看到的识图模型名>
    # 文本任务（抽取、分析）默认复用以上凭据，可单独覆盖：
    TEXT_API_BASE= / TEXT_API_KEY= / TEXT_MODEL=deepseek-chat

VISION_PROVIDER=mock 时不联网，返回可预测的样例结果，便于离线开发与联调。
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from app.config import DATA_DIR, ENV_PATH, ROOT, text_config, vision_config

MAX_IMAGE_SIDE = 1600
SUPPORTED_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
SUPPORTED_PDF = {".pdf"}

RECOGNIZE_PROMPT = r"""你是高中数学错题录入助手。请把图片中的题目转成结构化数据。

**最重要的一条**：一张图里可能有**多道互不相干的题目**（例如整页试卷、练习册照片）。
必须把它们**拆开**，一道题一个对象放进 `problems` 数组，
绝对不要把多道题拼进同一个 stem_md 里。

每道题的对象字段：
1. `label`：题号或位置标识，如 "1"、"第3题"、"左栏第2题"；没有就写空字符串。
2. `stem_md`：**这一道题**的完整题干。数学公式一律用 LaTeX 行内格式 `$...$`，
   例如 $f(x)=x^2-2x$、$\frac{a}{b}$、$\sqrt{3}$、$\overrightarrow{AB}$；
   多行公式用 `$$...$$`。保留所有条件与选项（A/B/C/D）。
   填空处保留横线：______
3. `question_type`：choice（选择）/ blank（填空）/ solution（解答）。
4. `student_answer`：这一题有学生手写作答就转录，没有留空字符串。
5. `figure_desc`：这一题有图形时用文字描述，无图留空。
6. `stem_plain`：该题不含 LaTeX 的纯文字版本，用于全文检索。
7. `confidence`：0–1，你对这一题识别准确度的判断；手写模糊、公式被遮挡要调低。

只输出 JSON，不要解释文字：
{"problems": [{"label": "", "stem_md": "", "stem_plain": "", "question_type": "solution",
               "student_answer": "", "figure_desc": "", "confidence": 0.9, "notes": ""}],
 "page_note": "对整页的补充说明，如页眉页码、与本页题目无关的印刷内容"}

注意：
- 只有一道题时，`problems` 数组里也**只有一个**元素。
- 题干必须**自足**：不要写"如上题"、"同第2题"，把条件完整抄录。
- 与本页题目无关的印刷内容（班级、姓名、页码、装订线文字）不要当成题目。
"""


class VisionError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """从模型回复里稳健地取出 JSON（容忍 ```json 围栏与前后废话）。"""
    if not text:
        raise VisionError("模型返回为空")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise VisionError(f"模型未返回合法 JSON：{exc}\n原文片段：{cleaned[:300]}") from exc
    raise VisionError(f"模型未返回 JSON：{cleaned[:300]}")


# ------------------------------------------------------------------ 图片预处理
def _image_bytes(path: Path) -> bytes:
    """读取图片；超大图片等比缩小以控制 token 与延迟。"""
    data = path.read_bytes()
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            if max(img.size) <= MAX_IMAGE_SIDE:
                return data
            ratio = MAX_IMAGE_SIDE / max(img.size)
            resized = img.convert("RGB").resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
    except Exception:
        return data


def _pdf_to_png(path: Path, max_pages: int = 0) -> tuple[list[bytes], int]:
    """PDF → 每页一张 PNG。返回 (图片列表, PDF 总页数)。

    max_pages <= 0 表示不限制。早先这里硬编码成 3 页，超过的页会被**静默丢弃**，
    表现就是「只扫描了第一页」，所以现在把总页数一并返回给调用方去告警。
    """
    try:
        # 新版包名是 pymupdf，fitz 只是兼容别名（会告警）
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError as exc:
            raise VisionError("需要 PyMuPDF 才能处理 PDF：pip install pymupdf") from exc
    out: list[bytes] = []
    with fitz.open(path) as doc:
        total = doc.page_count
        limit = total if max_pages <= 0 else min(total, max_pages)
        for page in doc[:limit]:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            out.append(pix.tobytes("png"))
    return out, total


def prepare_images(paths: list[Path], max_pages: int = 0) -> list[tuple[bytes, str]]:
    """统一成 [(字节, mime)]。支持图片与 PDF（PDF 每页一张）。"""
    out: list[tuple[bytes, str]] = []
    for path in paths:
        suffix = path.suffix.lower()
        if suffix in SUPPORTED_IMAGE:
            out.append((_image_bytes(path),
                        "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"))
        elif suffix in SUPPORTED_PDF:
            pages, _ = _pdf_to_png(path, max_pages)
            out.extend((buf, "image/png") for buf in pages)
        else:
            raise VisionError(f"不支持的文件类型：{path.name}（支持图片与 PDF）")
    if not out:
        raise VisionError("没有可识别的图片")
    return out


def _load_units(paths: list[Path], max_pages: int) -> tuple[list[dict], list[str]]:
    """把上传文件切成「识别单元」，每个单元一次模型请求。

    为什么要拆：PDF 所有页塞进一次请求有两个坑——
      1. 页数一多就超出上下文，模型会**忽略后面的页**（表现为「只扫描了第一页」）；
      2. 模型容易只汇报第一页的内容。
    所以 PDF 按页拆成独立单元，各自识别后再合并，这样还能给每道题标上页码。

    普通图片则合并成一个单元：同一道题的题干和解答常常分两张拍，
    拆开会丢失上下文。
    """
    units: list[dict] = []
    warnings: list[str] = []
    photo_group: list[tuple[bytes, str]] = []

    for path in paths:
        suffix = path.suffix.lower()
        if suffix in SUPPORTED_PDF:
            pages, total = _pdf_to_png(path, max_pages)
            if total > len(pages):
                warnings.append(
                    f"{path.name} 共 {total} 页，超过单次上限 {max_pages} 页，"
                    f"只处理了前 {len(pages)} 页（可在 app/.env 调大 VISION_MAX_PAGES）。")
            for index, data in enumerate(pages, 1):
                units.append({"label": f"{path.name} 第{index}页", "page": index,
                              "images": [(data, "image/png")]})
        elif suffix in SUPPORTED_IMAGE:
            photo_group.append((_image_bytes(path),
                                "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"))
        else:
            raise VisionError(f"不支持的文件类型：{path.name}（支持图片与 PDF）")

    if photo_group:
        units.insert(0, {"label": "上传的图片", "page": None, "images": photo_group})
    if not units:
        raise VisionError("没有可识别的图片")
    return units, warnings


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# ------------------------------------------------------------------ 模型调用
def _chat(cfg: dict, messages: list[dict], *, json_mode: bool = False,
          max_tokens: int | None = None) -> str:
    if not cfg.get("base"):
        raise VisionError("未配置 API 地址（VISION_API_BASE）")
    if not cfg.get("key"):
        raise VisionError(
            f"未配置 API Key。请在 {ENV_PATH} 中填写 VISION_API_KEY，"
            "或在网页右上角的「设置」里填写；也可以设置 VISION_PROVIDER=mock 离线体验。")
    budget = max_tokens or cfg.get("max_tokens") or 8192
    body: dict = {"model": cfg["model"], "messages": messages,
                  "temperature": 0.1, "max_tokens": budget}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    url = cfg["base"].rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url, headers={"Authorization": f"Bearer {cfg['key']}",
                          "Content-Type": "application/json"},
            json=body, timeout=cfg.get("timeout", 180))
    except requests.RequestException as exc:
        raise VisionError(f"调用模型失败：{exc}") from exc
    if resp.status_code != 200:
        raise VisionError(f"模型返回 HTTP {resp.status_code}：{resp.text[:400]}")
    try:
        payload = resp.json()
        choice = payload["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
    except (KeyError, IndexError, ValueError) as exc:
        raise VisionError(f"模型响应结构异常：{resp.text[:400]}") from exc

    if not content.strip():
        # 推理型模型（deepseek-flash / deepseek-reasoner 等）先把 token 花在
        # reasoning_content 上，预算不足时 content 会是空串、finish_reason=length。
        # 这种「假故障」必须给出明确指引，否则排查成本极高。
        finish = choice.get("finish_reason", "")
        usage = payload.get("usage") or {}
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if reasoning and finish == "length":
            raise VisionError(
                f"模型把 {budget} 个 token 全部用在了思考过程上，没来得及输出正文。"
                f"请在网页「设置」里调大 API_MAX_TOKENS（当前 {budget}，建议 8192 以上），"
                f"或换用非推理型模型作为识图/文本模型。"
                f"（finish_reason=length，reasoning_tokens={reasoning_tokens}）")
        raise VisionError(
            f"模型返回了空内容（finish_reason={finish}）"
            f"{'，且只有思考过程没有正文' if reasoning else ''}。"
            f"请检查模型名是否正确、是否支持当前输入类型。")
    return content


# ------------------------------------------------------------------ 诊断留痕
LOG_DIR = DATA_DIR / "logs"

# 模型可能用的其它字段名 / 中文题型，做一层兼容
STEM_KEYS = ("stem_md", "stem", "question", "question_md", "problem", "content", "text")
QUESTION_TYPE_MAP = {
    "选择": "choice", "选择题": "choice", "单选": "choice", "多选": "choice", "choice": "choice",
    "填空": "blank", "填空题": "blank", "blank": "blank",
    "解答": "solution", "解答题": "solution", "解答题（含证明）": "solution",
    "计算": "solution", "证明": "solution", "solution": "solution", "application": "solution",
}


def _pick_stem(data: dict) -> str:
    """从模型返回里找题干。容忍字段名不一致与嵌套一层。"""
    if not isinstance(data, dict):
        return ""
    for key in STEM_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in data.values():          # 形如 {"result": {...}} 的套一层
        if isinstance(value, dict):
            found = _pick_stem(value)
            if found:
                return found
    return ""


def _normalize_problem(item: dict, engine: str, images: int) -> dict:
    """把单道题的返回规整成内部统一结构。"""
    qtype = str(item.get("question_type") or "solution").strip()
    qtype = QUESTION_TYPE_MAP.get(qtype, QUESTION_TYPE_MAP.get(qtype.replace("题", ""), "solution"))
    answer = item.get("student_answer") or item.get("student_solution") or ""
    if isinstance(answer, (list, dict)):
        answer = json.dumps(answer, ensure_ascii=False)
    try:
        confidence = float(item.get("confidence") or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "label": str(item.get("label") or item.get("no") or "").strip(),
        "stem_md": _pick_stem(item) or _pick_stem({"stem_md": item.get("stem_md")}),
        "stem_plain": str(item.get("stem_plain") or ""),
        "question_type": qtype,
        "student_answer": str(answer),
        "figure_desc": str(item.get("figure_desc") or item.get("figure") or ""),
        "confidence": confidence,
        "notes": str(item.get("notes") or ""),
        "engine": engine,
        "image_count": images,
    }


def _extract_problems(data: dict) -> tuple[list[dict], str]:
    """从模型返回里取出题目列表。返回 (problems, page_note)。

    兼容三种形态：
      · {"problems": [...]}                    首选（新提示词）
      · {"questions": [...]} / {"items": [...]} 模型的同义写法
      · {"stem_md": "..."}                     单题老格式 / 模型没按新格式返回
    """
    if not isinstance(data, dict):
        return [], ""
    note = str(data.get("page_note") or "")
    for key in ("problems", "questions", "items", "list"):
        value = data.get(key)
        if isinstance(value, list) and value:
            items = [v for v in value if isinstance(v, dict)]
            if items:
                return items, note
    # 老格式：整个返回就是一道题
    if _pick_stem(data):
        return [data], note
    # 嵌套一层
    for value in data.values():
        if isinstance(value, dict):
            found, sub_note = _extract_problems(value)
            if found:
                return found, note or sub_note
    return [], note


def _log_failure(stage: str, payload: dict) -> Path | None:
    """把失败的原始响应落盘，便于事后排查（界面上的报错信息放不下这些）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{stage}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except OSError:
        return None


def _mock_recognize(stem_hint: str) -> dict:
    return {
        "stem_md": stem_hint or (
            "已知函数 $f(x)=2^{x}-2^{-x}$。\n"
            "(1) 判断 $f(x)$ 的奇偶性，并说明理由；\n"
            "(2) 若 $f(m-1)+f(2m)<0$，求实数 $m$ 的取值范围。"),
        "stem_plain": "已知函数 f(x)=2^x-2^-x，判断奇偶性并解不等式。",
        "question_type": "solution",
        "student_answer": "（1）f(-x)=2^x-2^-x=f(x)，所以是偶函数",
        "figure_desc": "",
        "confidence": 0.62,
        "notes": "离线样例模式（VISION_PROVIDER=mock），非真实识别结果。",
    }


def _stash_unit_images(units: list[dict], stash_dir: Path | None) -> None:
    """把每个识别单元的图片落盘，路径记在 unit["stash"]。

    为什么要落盘：一张试卷图里往往有多道题，学生勾选后才入库。
    如果不暂存，入库时只能重新上传并重新识别一遍 —— 慢，而且
    模型重跑可能给出与勾选时**不一样的题目**。
    """
    if not stash_dir:
        return
    stash_dir = Path(stash_dir)
    stash_dir.mkdir(parents=True, exist_ok=True)
    for index, unit in enumerate(units):
        saved: list[str] = []
        for seq, (data, mime) in enumerate(unit["images"], 1):
            base = f"page_{unit['page']}" if unit.get("page") else f"photo_{index + 1}"
            if len(unit["images"]) > 1:
                base += f"_{seq}"
            path = stash_dir / (base + (".jpg" if mime == "image/jpeg" else ".png"))
            path.write_bytes(data)
            saved.append(path.relative_to(ROOT).as_posix())
        unit["stash"] = saved


def _recognize_unit(cfg: dict, unit: dict, engine: str, hint: str) -> tuple[list[dict], str, str]:
    """识别一个单元（一页 PDF，或一组图片）。返回 (problems, 错误说明, 原始响应)。"""
    content: list[dict] = [{"type": "text", "text": RECOGNIZE_PROMPT}]
    if hint:
        content.append({"type": "text", "text": f"补充说明（来自学生）：{hint}"})
    if unit.get("multi"):
        content.append({"type": "text",
                        "text": f"这次要识别的是「{unit['label']}」，只需识别本页的内容。"})
    for data, mime in unit["images"]:
        content.append({"type": "image_url", "image_url": {"url": _data_url(data, mime)}})

    report: list[str] = []
    last_raw = ""
    for json_mode, label in ((True, "JSON 模式"), (False, "普通模式重试（去掉 response_format）")):
        try:
            text = _chat(cfg, [{"role": "user", "content": content}], json_mode=json_mode)
        except VisionError as exc:
            return [], str(exc), last_raw          # 调用层失败：无 key / 网络 / token 耗尽
        last_raw = text
        try:
            parsed = extract_json(text)
        except VisionError as exc:
            report.append(f"{label}：{exc}")
            continue
        raw_problems, page_note = _extract_problems(parsed)
        normalized = [_normalize_problem(p, engine, len(unit["images"]))
                      for p in raw_problems if isinstance(p, dict)]
        normalized = [p for p in normalized if p["stem_md"]]
        if normalized:
            for problem in normalized:
                problem["page"] = unit.get("page")
                problem["source"] = unit["label"]
                problem["page_note"] = page_note
            return normalized, "", text
        keys = sorted(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
        report.append(f"{label}：解析出的题目里没有题干（顶层字段：{keys}）")
    return [], "；".join(report), last_raw


def recognize_question(paths: list[Path], *, hint: str = "",
                       cfg: dict | None = None,
                       on_page_done=None,
                       stash_dir: Path | None = None) -> dict:
    """图片/PDF → 结构化题目列表。

    on_page_done(index, total, label, done_count) 用于上报「已识别到第几页」，
    方便界面上显示实时进度。

    stash_dir 给定时，把每页图片写到该目录并挂到对应题目的 `asset_path` 上。
    这样「多题页里让学生勾选后再入库」就不必重新上传、重新识别：
    勾选时看到的题目和最终入库的题目保证是同一批（重新识别是可能出不同结果的）。

    返回 {"problems": [{label, page, source, stem_md, stem_plain, question_type,
                        student_answer, figure_desc, confidence, notes, asset_path}],
          "engine", "warnings", "page_count", "image_count"}

    PDF **按页分别识别再合并**：把所有页塞进一次请求时，页数一多就会超出上下文，
    模型会忽略后面的页（表现为「只扫描了第一页」）。按页拆开后：
      · 每页结果互不干扰，个别页失败不影响其它页（会记进 warnings）；
      · 每道题都带上页码，界面能显示「第几页的第几题」。

    只有一道题时列表长度也是 1。调用方负责决定「记录哪几道」。
    """
    cfg = cfg or vision_config()
    provider = (cfg.get("provider") or "deepseek").lower()
    if provider == "mock":
        problem = _normalize_problem(_mock_recognize(hint), "mock", 0)
        problem.update({"page": 1, "source": "mock", "page_note": "", "asset_path": ""})
        return {"problems": [problem], "engine": "mock", "warnings": [],
                "page_count": 1, "image_count": 0}

    max_pages = int(cfg.get("max_pages") or 0)
    units, warnings = _load_units(paths, max_pages)
    multi = len(units) > 1
    engine = f"{provider}:{cfg['model']}"
    image_count = sum(len(u["images"]) for u in units)
    for unit in units:
        unit["multi"] = multi
    _stash_unit_images(units, stash_dir)

    # 按页识别是串行的，4 页就是 4 倍耗时（20 页的 PDF 会慢到不可用）。
    # 这些调用是纯 I/O 等待，用线程池并发发出去，结果按页序回收保证顺序稳定。
    workers = max(1, min(int(cfg.get("concurrency") or 1), len(units)))
    outcomes: list[tuple[list[dict], str, str]] = []
    if workers > 1 and len(units) > 1:
        finished: dict[int, tuple[list[dict], str, str]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_recognize_unit, cfg, unit, engine, hint): index
                       for index, unit in enumerate(units)}
            for done_count, future in enumerate(as_completed(futures), 1):
                index = futures[future]
                finished[index] = future.result()
                if on_page_done:
                    # 并发回来的顺序是乱的，报「第几页」而不是「第几个完成」更好懂
                    on_page_done(index, len(units), units[index]["label"], done_count)
        outcomes = [finished[i] for i in range(len(units))]
    else:
        for index, unit in enumerate(units):
            outcomes.append(_recognize_unit(cfg, unit, engine, hint))
            if on_page_done:
                on_page_done(index, len(units), unit["label"], index + 1)

    problems: list[dict] = []
    failures: list[str] = []
    logs: list[dict] = []
    for unit, (got, error, raw) in zip(units, outcomes):
        logs.append({"unit": unit["label"], "error": error, "raw_response": raw})
        if error:
            failures.append(f"{unit['label']}：{error}")
            continue
        for problem in got:                       # 挂上本页的图片，供后续入库直接用
            problem["asset_path"] = (unit.get("stash") or [""])[0]
        problems.extend(got)

    if failures:
        if problems:
            warnings.append("部分页面识别失败：" + "；".join(failures))
        else:
            log_path = _log_failure("vision", {
                "provider": provider, "model": cfg.get("model"),
                "files": [str(p) for p in paths], "units": len(units),
                "hint": hint, "details": logs,
            })
            tail = f"，原始响应已存到 {log_path}" if log_path else ""
            raise VisionError(
                "未能从图片中识别出题干。\n"
                + "\n".join(f"  · {f}" for f in failures)
                + tail
                + "\n可以改用「手动输入题干」，或换一张更清晰的图片。")

    return {"problems": problems, "engine": engine, "warnings": warnings,
            "page_count": len(units), "image_count": image_count}


def recognize_single(paths: list[Path], *, hint: str = "",
                     cfg: dict | None = None) -> dict:
    """只要第一道题的便捷入口（自检脚本等场景用）。"""
    result = recognize_question(paths, hint=hint, cfg=cfg)
    first = dict(result["problems"][0])
    first.update({"engine": result["engine"], "raw_text": "",
                  "problem_count": len(result["problems"]),
                  "page_count": result["page_count"],
                  "warnings": result["warnings"]})
    return first


def chat_text(messages: list[dict], *, json_mode: bool = False, cfg: dict | None = None,
              max_tokens: int | None = None) -> str:
    """纯文本模型调用（知识点抽取、错因分析共用）。max_tokens 留空则用配置值。"""
    cfg = cfg or text_config()
    if (cfg.get("key") or "") == "" and json_mode:
        raise VisionError("未配置文本模型 API Key（TEXT_API_KEY / VISION_API_KEY）")
    return _chat(cfg, messages, json_mode=json_mode, max_tokens=max_tokens)


def is_configured() -> tuple[bool, str]:
    """检查配置状态，供界面给出友好提示。"""
    cfg = vision_config()
    provider = (cfg.get("provider") or "").lower()
    if provider == "mock":
        return True, "离线样例模式（mock）：不联网，仅用于界面调试"
    if not cfg.get("key"):
        return False, "未配置 VISION_API_KEY（点右上角「设置」填写，或用 mock 模式离线体验）"
    return True, f"{provider} / {cfg['model']}"


def _models_from_payload(data: object) -> list[str]:
    """从 /models 的响应里取出模型 id。不同服务商的字段名不统一，逐个容错。"""
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    elif isinstance(data, list):
        items = data
    else:
        return []
    names: list[str] = []
    for m in items:
        if isinstance(m, dict):
            name = m.get("id") or m.get("name") or m.get("model") or ""
        else:
            name = str(m)
        if name:
            names.append(str(name))
    return sorted(set(names))


def list_models(cfg: dict | None = None) -> dict:
    """拉取服务商提供的模型列表（OpenAI 兼容协议的 GET /models）。

    不抛异常：失败原因放在 message 里，界面直接展示即可。
    可用 cfg 传入界面上还没保存的填写值，先拉再存。
    """
    cfg = dict(cfg or vision_config())
    cfg["timeout"] = min(int(cfg.get("timeout") or 20), 20)
    base, key = cfg.get("base", ""), cfg.get("key", "")

    if (cfg.get("provider") or "").lower() == "mock":
        return {"ok": False, "models": [],
                "message": "离线样例模式（mock）没有模型列表，它不联网。"}
    if not base:
        return {"ok": False, "models": [], "message": "还没有填 API 地址。"}
    if not key:
        return {"ok": False, "models": [], "message": "还没有填 API Key。"}

    url = base.rstrip("/") + "/models"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {key}"},
                            timeout=cfg["timeout"])
    except requests.RequestException as exc:
        return {"ok": False, "models": [], "message": f"请求失败：{exc}"}
    if resp.status_code != 200:
        return {"ok": False, "models": [],
                "message": f"服务端返回 HTTP {resp.status_code}：{resp.text[:200]}"}
    try:
        models = _models_from_payload(resp.json())
    except ValueError:
        return {"ok": False, "models": [], "message": f"响应不是合法 JSON：{resp.text[:200]}"}

    if not models:
        return {"ok": False, "models": [], "message": "接口没有返回任何模型名称。"}
    return {"ok": True, "models": models, "message": f"拉到 {len(models)} 个模型。"}


def test_connection(cfg: dict | None = None) -> dict:
    """用一次极小的对话请求验证「地址 + 密钥 + 模型名」三者是否对得上。

    不抛异常：失败原因放在 message 里，界面直接展示即可。
    可用 cfg 传入界面上还没保存的填写值，先试再存。
    """
    cfg = dict(cfg or vision_config())
    cfg["timeout"] = min(int(cfg.get("timeout") or 30), 30)
    model, base = cfg.get("model", ""), cfg.get("base", "")

    if (cfg.get("provider") or "").lower() == "mock":
        return {"ok": True, "model": model, "base": base,
                "message": "当前是离线样例模式（mock），不需要联网，也不会真的调用模型。"}

    if not base:
        return {"ok": False, "model": model, "base": base,
                "message": "还没有填 API 地址。"}
    if not cfg.get("key"):
        return {"ok": False, "model": model, "base": base,
                "message": "还没有填 API Key。"}
    if not model:
        return {"ok": False, "model": model, "base": base,
                "message": "还没有填模型名称。"}

    try:
        # 注意不能用很小的 max_tokens：推理型模型（deepseek-flash 等）光"思考"就会
        # 把预算花光，正文一个 token 都轮不到，测试会假装失败。max_tokens 只是上限，
        # 请求本身很短，给足预算不会多花钱。
        reply = _chat(cfg, [{"role": "user", "content": "回复 ok 两个字即可。"}],
                      max_tokens=int(cfg.get("max_tokens") or 8192))
    except VisionError as exc:
        return {"ok": False, "model": model, "base": base, "message": str(exc)}

    return {"ok": True, "model": model, "base": base,
            "message": f"连通正常，模型回复：{reply.strip()[:40]}"}
