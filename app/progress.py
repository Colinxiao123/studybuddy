#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台任务与进度上报。

为什么要这个：录入一道错题要经过「按页识别 → 逐题抽取知识点 → 图谱对齐 → AI 解析」，
整页试卷动辄十几道题、耗时以分钟计。如果只用一个阻塞请求，界面只能干等着转圈，
用户既不知道在干什么、也不知道是不是卡住了。

做法：
    POST /api/ingest_async  → 立刻返回 job_id，实际工作丢到后台线程
    GET  /api/jobs/{job_id} → 查询 state / percent / message / result

局限：任务表在进程内存里，多 worker（如 uvicorn --workers 4）之间不共享。
本应用是单进程本地服务，够用；真要横向扩展再换 Redis 之类。
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field

JOB_TTL_SECONDS = 3600       # 完成后保留 1 小时，方便前端偶尔晚到的轮询
MAX_JOBS = 200


@dataclass
class Job:
    id: str
    kind: str
    state: str = "running"          # running / done / error
    percent: float = 0.0
    message: str = "准备中…"
    result: dict | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 服务端私有：不进 snapshot，用来在两次请求之间携带中间产物
    # 例如「已识别但还没入库的题目」与暂存图片目录
    payload: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "percent": round(self.percent, 1),
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "elapsed": round(time.time() - self.created_at, 1),
        }


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def create_job(kind: str) -> Job:
    _cleanup()
    with _LOCK:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        _JOBS[job.id] = job
        return job


def get_job(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def _cleanup() -> None:
    """清掉过期任务，避免长跑进程里无限堆积（含它们暂存的上传文件）。"""
    now = time.time()
    with _LOCK:
        stale = [jid for jid, job in _JOBS.items()
                 if job.state != "running" and now - job.updated_at > JOB_TTL_SECONDS]
        for jid in stale:
            _purge_payload(_JOBS.pop(jid, None))
        if len(_JOBS) > MAX_JOBS:
            for jid, _ in sorted(_JOBS.items(), key=lambda kv: kv[1].created_at)[:len(_JOBS) - MAX_JOBS]:
                if _JOBS[jid].state != "running":
                    _purge_payload(_JOBS.pop(jid, None))


def _purge_payload(job: Job | None) -> None:
    """删掉任务暂存的图片目录，避免临时文件越积越多。"""
    if not job:
        return
    stash = (job.payload or {}).get("stash_dir")
    if stash:
        shutil.rmtree(str(stash), ignore_errors=True)


class Progress:
    """进度上报器。任务不跑在后台时传 None 即可，所有方法都安全空转。"""

    def __init__(self, job: Job | None = None):
        self.job = job

    # ---------------------------------------------------------------- 上报
    def set(self, percent: float, message: str) -> None:
        if not self.job:
            return
        with _LOCK:
            # 进度只增不减：并发分页回来时顺序是乱的，回退会让进度条来回跳
            self.job.percent = max(self.job.percent, min(99.0, float(percent)))
            self.job.message = message
            self.job.updated_at = time.time()

    def span(self, low: float, high: float, done: int, total: int, message: str) -> None:
        """把 [low, high] 区间按 done/total 映射成绝对百分比。"""
        ratio = 1.0 if total <= 0 else min(1.0, done / total)
        self.set(low + (high - low) * ratio, message)

    def note(self, message: str) -> None:
        """只更新文案，不动百分比。

        并发跑多个子任务时用：各子任务算出的百分比是跨区间的，
        誰先把条推到后面的区间，前面的进展就永远显示不出来了（条会卡住）。
        所以并发场景的百分比只由「已完成几个」驱动，子任务的细节走 note()。
        """
        if not self.job:
            return
        with _LOCK:
            self.job.message = message
            self.job.updated_at = time.time()

    def finish(self, result: dict | None = None) -> None:
        if not self.job:
            return
        with _LOCK:
            self.job.state = "done"
            self.job.percent = 100.0
            self.job.message = "完成"
            self.job.result = result
            self.job.updated_at = time.time()

    def fail(self, error: str) -> None:
        if not self.job:
            return
        with _LOCK:
            self.job.state = "error"
            self.job.message = "失败"
            self.job.error = error
            self.job.updated_at = time.time()
