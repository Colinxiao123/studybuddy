#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错题本数据层（SQLite）。

分层原则（《本体规范》P6「静态图谱与动态数据分离」）：
    本文件里的所有表都是**学生侧动态数据**，存放于 app/data/db.sqlite，
    绝不写入 ontology/data/src/；图谱始终只读。

三张主表：
    questions      题目（题干 / 原图 / OCR 原文 / AI 答案与解析）
    question_kps   题目 ↔ 知识点（带角色、置信度、命中依据）
    wrong_records  错题记录（同一题可多次错，支持订正闭环）
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.config import ASSETS_DIR, DB_PATH, ensure_dirs
from app.mathkg.textutil import to_plain

# 逐题处理是多线程并发的（每题一次模型调用），所以写入需要保护：
#   · busy_timeout 让并发写等待而不是直接报 database is locked；
#   · 题号生成 + 插入必须原子，否则两个线程会算出同一个号撞主键。
_WRITE_LOCK = threading.Lock()

ERROR_TYPE_CN = {
    "concept": "概念不清",
    "method": "方法不会",
    "calc": "计算失误",
    "read": "审题失误",
    "unknown": "未归类",
}
QUESTION_TYPE_CN = {"choice": "选择题", "blank": "填空题", "solution": "解答题"}

# 热度分级：错题数 → (级别, 标签, 颜色)
# 配色用「冷→热」热力图色阶（蓝→琥珀→红），不用绿色：
# 错得多 = 热 = 红，一眼能看出该先补哪里。与前端 HEAT_COLORS 必须一致。
HEAT_LEVELS = [
    (0, 0, "无", "#c8cdd6"),
    (1, 1, "低", "#4a90d9"),
    (3, 2, "中", "#f0b429"),
    (6, 3, "高", "#ef6c3a"),
    (10, 4, "极高", "#c62828"),
]


def heat_level(count: int) -> dict:
    level, label, color = 0, "无", "#c8cdd6"
    for threshold, lv, lb, cl in HEAT_LEVELS:
        if count >= threshold:
            level, label, color = lv, lb, cl
    return {"level": level, "label": label, "color": color}


SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id             TEXT PRIMARY KEY,
    student_id     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    source_name    TEXT DEFAULT '',
    question_type  TEXT DEFAULT 'solution',
    stem_md        TEXT DEFAULT '',
    stem_plain     TEXT DEFAULT '',
    asset_path     TEXT DEFAULT '',
    ocr_engine     TEXT DEFAULT '',
    ocr_confidence REAL DEFAULT 0,
    student_answer TEXT DEFAULT '',
    ai_answer      TEXT DEFAULT '',
    ai_analysis    TEXT DEFAULT '',
    status         TEXT DEFAULT 'pending',
    raw_json       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_q_student ON questions(student_id, created_at DESC);

CREATE TABLE IF NOT EXISTS question_kps (
    question_id TEXT NOT NULL,
    kp_id       TEXT NOT NULL,
    confidence  REAL DEFAULT 0,
    evidence    TEXT DEFAULT '',
    role        TEXT DEFAULT 'primary',
    status      TEXT DEFAULT 'auto',
    PRIMARY KEY (question_id, kp_id, role)
);
CREATE INDEX IF NOT EXISTS idx_qk_kp ON question_kps(kp_id);

CREATE TABLE IF NOT EXISTS wrong_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    wrong_at    TEXT NOT NULL,
    error_type  TEXT DEFAULT 'unknown',
    analysis    TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    resolved    INTEGER DEFAULT 0,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wr_student ON wrong_records(student_id, wrong_at DESC);
CREATE INDEX IF NOT EXISTS idx_wr_question ON wrong_records(question_id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path) if db_path else DB_PATH
    ensure_dirs()
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def next_question_id(student_id: str, db_path: Path | None = None,
                     conn: sqlite3.Connection | None = None) -> str:
    """Q-UP-{yyyyMMdd}-{NNNNN}，按天递增（UP = user upload）。

    传入已有的 conn 时复用它的连接，保证「查号 + 插入」在同一事务里完成。
    """
    day = datetime.now().strftime("%Y%m%d")
    prefix = f"Q-UP-{day}-"
    sql = "SELECT id FROM questions WHERE id LIKE ? ORDER BY id DESC LIMIT 1"
    if conn is not None:
        row = conn.execute(sql, (prefix + "%",)).fetchone()
    else:
        with connect(db_path) as own:
            row = own.execute(sql, (prefix + "%",)).fetchone()
    seq = int(row["id"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{seq:05d}"


def asset_dir(student_id: str, question_id: str) -> Path:
    month = datetime.now().strftime("%Y-%m")
    path = ASSETS_DIR / (student_id or "default") / month / question_id
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------- 写入
def create_question(student_id: str, *, stem_md: str, stem_plain: str = "",
                    question_type: str = "solution", asset_path: str = "",
                    ocr_engine: str = "", ocr_confidence: float = 0.0,
                    student_answer: str = "", source_name: str = "",
                    raw: dict | None = None, status: str = "pending",
                    db_path: Path | None = None) -> str:
    # 加锁保证「取号 + 插入」原子：并发处理多道题时不能撞主键
    with _WRITE_LOCK, connect(db_path) as conn:
        qid = next_question_id(student_id, db_path, conn=conn)
        conn.execute(
            """INSERT INTO questions (id, student_id, created_at, source_name, question_type,
                   stem_md, stem_plain, asset_path, ocr_engine, ocr_confidence,
                   student_answer, status, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (qid, student_id, now(), source_name, question_type, stem_md, stem_plain,
             asset_path, ocr_engine, float(ocr_confidence or 0), student_answer, status,
             json.dumps(raw or {}, ensure_ascii=False)))
    return qid


def update_question_text(question_id: str, *, stem_md: str | None = None,
                         student_answer: str | None = None,
                         db_path: Path | None = None) -> bool:
    """订正题干/作答。改了 stem_md 就同步重算 stem_plain（检索用纯文本）。

    只动文本字段：知识点、解析不动 —— 要不要重跑解析由调用方决定。
    """
    sets: list[str] = []
    params: list[Any] = []
    if stem_md is not None:
        sets.extend(["stem_md = ?", "stem_plain = ?"])
        params.extend([stem_md, to_plain(stem_md)])
    if student_answer is not None:
        sets.append("student_answer = ?")
        params.append(student_answer)
    if not sets:
        return False
    params.append(question_id)
    with connect(db_path) as conn:
        cur = conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def save_question_kps(question_id: str, linked: list[dict],
                      db_path: Path | None = None) -> int:
    """写入题目↔知识点。同一 (题目, 知识点, 角色) 覆盖更新。"""
    count = 0
    with connect(db_path) as conn:
        for item in linked or []:
            kp_id = item.get("kp_id")
            if not kp_id:
                continue
            conn.execute(
                """INSERT INTO question_kps (question_id, kp_id, confidence, evidence, role, status)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(question_id, kp_id, role) DO UPDATE SET
                     confidence = excluded.confidence,
                     evidence   = excluded.evidence,
                     status     = excluded.status""",
                (question_id, kp_id, float(item.get("score") or 0),
                 " / ".join(item.get("evidence") or []) if isinstance(item.get("evidence"), list)
                 else str(item.get("evidence") or ""),
                 item.get("role") or "primary",
                 item.get("status") or "auto"))
            count += 1
    return count


def save_analysis(question_id: str, *, ai_answer: str = "", ai_analysis: str = "",
                  student_answer: str | None = None, status: str | None = None,
                  db_path: Path | None = None) -> None:
    sets, params = [], []
    if ai_answer:
        sets.append("ai_answer = ?")
        params.append(ai_answer)
    if ai_analysis:
        sets.append("ai_analysis = ?")
        params.append(ai_analysis)
    if student_answer is not None:
        sets.append("student_answer = ?")
        params.append(student_answer)
    if status:
        sets.append("status = ?")
        params.append(status)
    if not sets:
        return
    params.append(question_id)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE id = ?", params)


def add_wrong_record(question_id: str, student_id: str, *,
                     error_type: str = "unknown", analysis: str = "", note: str = "",
                     db_path: Path | None = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO wrong_records (question_id, student_id, wrong_at, error_type, analysis, note)
               VALUES (?,?,?,?,?,?)""",
            (question_id, student_id, now(), error_type, analysis, note))
        return int(cur.lastrowid or 0)


def resolve_wrong(question_id: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE wrong_records SET resolved = 1, resolved_at = ? WHERE question_id = ?",
            (now(), question_id))


def delete_question(question_id: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM question_kps WHERE question_id = ?", (question_id,))
        conn.execute("DELETE FROM wrong_records WHERE question_id = ?", (question_id,))
        conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))


# --------------------------------------------------------------------- 读取
def get_question(question_id: str, db_path: Path | None = None) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        if not row:
            return None
        kps = conn.execute(
            "SELECT * FROM question_kps WHERE question_id = ? ORDER BY confidence DESC",
            (question_id,)).fetchall()
    data = dict(row)
    data["kps"] = [dict(k) for k in kps]
    return data


# 排序方式 → SQL（白名单，拼进语句前必须先比对这个表，不能让调用方随意拼 SQL）
SORTS = {
    "newest": "q.created_at DESC",
    "oldest": "q.created_at ASC",
    "last_wrong": "w.last_wrong IS NULL, w.last_wrong DESC, q.created_at DESC",
    "most_wrong": "COALESCE(w.wrong_times, 0) DESC, q.created_at DESC",
}

# 每题附带的错题聚合（错几次、最近一次、涉及哪些错因），供列表展示与排序
_WRONG_AGG = """
    LEFT JOIN (
        SELECT question_id, COUNT(*) AS wrong_times, MAX(wrong_at) AS last_wrong,
               GROUP_CONCAT(DISTINCT error_type) AS error_types
        FROM wrong_records GROUP BY question_id
    ) w ON w.question_id = q.id
"""


def _question_filter(student_id: str, *, kp_id: str | None = None,
                     error_types: list[str] | None = None,
                     date_from: str | None = None,
                     date_to: str | None = None) -> tuple[str, list[Any]]:
    """拼出错题列表的筛选条件。list_questions 与 count_questions 共用一份，
    免得两处各写一遍、改一处漏一处（列表数了 30 条、统计说 12 条这种）。"""
    joins = ""
    conds = ["q.student_id = ?"]
    params: list[Any] = [student_id]
    if kp_id:
        conds.append("EXISTS (SELECT 1 FROM question_kps qk "
                     "WHERE qk.question_id = q.id AND qk.kp_id = ?)")
        params.append(kp_id)
    types = [t for t in (error_types or []) if t]
    if types:
        conds.append("EXISTS (SELECT 1 FROM wrong_records w2 WHERE w2.question_id = q.id "
                     f"AND w2.error_type IN ({','.join('?' * len(types))}))")
        params.extend(types)
    # created_at 是 ISO 文本（YYYY-MM-DDTHH:MM:SS），date() 取日期部分再比较
    if date_from:
        conds.append("date(q.created_at) >= date(?)")
        params.append(date_from)
    if date_to:
        conds.append("date(q.created_at) <= date(?)")
        params.append(date_to)
    return joins + " AND ".join(conds), params


def list_questions(student_id: str, *, kp_id: str | None = None,
                   module: str | None = None, error_type: str | None = None,
                   error_types: list[str] | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   sort: str = "newest", limit: int = 50, offset: int = 0,
                   db_path: Path | None = None) -> list[dict]:
    """按条件取错题。error_type 是单个值的旧写法，error_types 是多选。"""
    types = list(error_types or [])
    if error_type:
        types.append(error_type)
    where, params = _question_filter(student_id, kp_id=kp_id, error_types=types,
                                     date_from=date_from, date_to=date_to)
    order = SORTS.get(sort, SORTS["newest"])
    sql = (f"SELECT q.*, COALESCE(w.wrong_times, 0) AS wrong_times, "
           f"w.last_wrong AS last_wrong, w.error_types AS error_types "
           f"FROM questions q {_WRONG_AGG} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?")
    with connect(db_path) as conn:
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["error_types"] = (item.get("error_types") or "").split(",") if item.get("error_types") else []
            item["kps"] = [dict(k) for k in conn.execute(
                "SELECT * FROM question_kps WHERE question_id = ? ORDER BY confidence DESC",
                (row["id"],)).fetchall()]
            out.append(item)
    return out


def count_questions(student_id: str, *, kp_id: str | None = None,
                    error_types: list[str] | None = None,
                    date_from: str | None = None, date_to: str | None = None,
                    db_path: Path | None = None) -> int:
    """同样的筛选条件下一共有多少道（列表是分页的，这个数才是真数）。"""
    where, params = _question_filter(student_id, kp_id=kp_id, error_types=error_types,
                                     date_from=date_from, date_to=date_to)
    with connect(db_path) as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) c FROM questions q WHERE {where}", params).fetchone()["c"])


def attach_kp_names(kps: list[dict], kg: Any) -> None:
    """给知识点关联补上图谱里的名称与模块。

    数据库只存 kp_id：名称会随图谱修订而变，冗余存储容易不一致。
    因此凡是把题目送给前端的地方都得调用一次，否则界面只能回退成显示编号
    （表现就是知识点标签把同一个编号印了两遍）。
    """
    for kp in kps:
        node = kg.get(kp.get("kp_id")) or {}
        kp["name"] = node.get("name", kp.get("kp_id", ""))
        kp["module_name"] = node.get("module_name", "")


def kp_wrong_stats(student_id: str, db_path: Path | None = None) -> dict[str, dict]:
    """每个知识点关联的错题数（按题目去重）与最近错题时间。"""
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT qk.kp_id,
                      COUNT(DISTINCT q.id)                        AS question_count,
                      COUNT(DISTINCT CASE WHEN wr.id IS NOT NULL THEN q.id END) AS wrong_count,
                      MAX(wr.wrong_at)                            AS last_wrong_at,
                      SUM(CASE WHEN wr.error_type='concept' THEN 1 ELSE 0 END) AS err_concept,
                      SUM(CASE WHEN wr.error_type='method'  THEN 1 ELSE 0 END) AS err_method,
                      SUM(CASE WHEN wr.error_type='calc'    THEN 1 ELSE 0 END) AS err_calc,
                      SUM(CASE WHEN wr.error_type='read'    THEN 1 ELSE 0 END) AS err_read
               FROM question_kps qk
               JOIN questions q ON q.id = qk.question_id
               LEFT JOIN wrong_records wr ON wr.question_id = q.id
               WHERE q.student_id = ?
               GROUP BY qk.kp_id""", (student_id,)).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            item = dict(row)
            item["heat"] = heat_level(item["wrong_count"] or 0)
            out[row["kp_id"]] = item
        # 重复错题次数（同一题错多次）
        repeat = conn.execute(
            """SELECT qk.kp_id, COUNT(wr.id) AS times
               FROM question_kps qk
               JOIN questions q ON q.id = qk.question_id
               JOIN wrong_records wr ON wr.question_id = q.id
               WHERE q.student_id = ?
               GROUP BY qk.kp_id""", (student_id,)).fetchall()
    for row in repeat:
        if row["kp_id"] in out:
            out[row["kp_id"]]["wrong_times"] = row["times"]
    return out


def student_overview(student_id: str, db_path: Path | None = None) -> dict:
    with connect(db_path) as conn:
        total_q = conn.execute("SELECT COUNT(*) c FROM questions WHERE student_id = ?",
                               (student_id,)).fetchone()["c"]
        total_w = conn.execute("SELECT COUNT(*) c FROM wrong_records WHERE student_id = ?",
                               (student_id,)).fetchone()["c"]
        resolved = conn.execute(
            "SELECT COUNT(DISTINCT question_id) c FROM wrong_records WHERE student_id = ? AND resolved = 1",
            (student_id,)).fetchone()["c"]
        by_type = {row["error_type"]: row["c"] for row in conn.execute(
            "SELECT error_type, COUNT(*) c FROM wrong_records WHERE student_id = ? GROUP BY error_type",
            (student_id,)).fetchall()}
    return {"question_count": total_q, "wrong_times": total_w,
            "resolved_count": resolved, "error_by_type": by_type}


def list_students(db_path: Path | None = None) -> list[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT student_id FROM questions GROUP BY student_id ORDER BY MAX(created_at) DESC"
        ).fetchall()
    return [r["student_id"] for r in rows]
