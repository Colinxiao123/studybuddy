#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 端到端冒烟测试（需要服务已启动）。

先启动服务：python -m app.server --port 8010
再运行：    python -m app.tests.smoke_web [base_url]
"""

from __future__ import annotations

import io
import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


def make_png() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (420, 200), "white")
    return io.BytesIO(_save(img))


def make_multi_png() -> bytes:
    """生成一张「两道题」的图，用于验证多题拆分与勾选入库。

    必须用中文字体：PIL 默认位图字体画不出汉字，会渲染成空白，
    那样测出来的「识别成功」其实是模型在猜。
    """
    from PIL import Image, ImageDraw, ImageFont
    font = None
    for path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
        try:
            font = ImageFont.truetype(path, 26)
            break
        except OSError:
            continue
    img = Image.new("RGB", (1000, 240), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 32), "1. 求函数 f(x)=x^2-4x+3 的单调递增区间。", fill=(20, 20, 25), font=font)
    draw.text((40, 90), "2. 求过点 (1,2) 且与直线 2x-y+1=0 垂直的直线方程。", fill=(20, 20, 25), font=font)
    draw.text((40, 150), "3. 已知 sin a = 3/5，求 cos 2a 的值。", fill=(20, 20, 25), font=font)
    return _save(img)


def _save(img) -> bytes:
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    student = "smoke_test"

    print("1) 健康检查")
    r = requests.get(f"{BASE}/api/health", timeout=30)
    check("GET /api/health", r.status_code == 200, r.text[:200])
    health = r.json()
    print(f"      → 图谱 {health['kg']['kp_count']} 知识点 / {health['kg']['edge_count']} 关系")
    print(f"      → 识别环境 {health['environment']['vision_note']}")

    print("\n2) 录入错题（纯文字，走降级链路）")
    r = requests.post(f"{BASE}/api/ingest", timeout=120, data={
        "student_id": student,
        "stem_text": "已知函数 $f(x)=2^{x}-2^{-x}$，判断 $f(x)$ 的奇偶性并证明；"
                     "若 $f(m-1)+f(2m)<0$，求 $m$ 的取值范围。",
        "student_answer": "f(-x)=2^{-x}-2^{x}=f(x)，所以是偶函数",
        "error_type": "concept",
        "auto_analyze": "false",
    })
    check("POST /api/ingest（纯文字）", r.status_code == 200, r.text[:300])
    q1 = r.json() if r.status_code == 200 else {}
    if q1:
        print(f"      → 题目 {q1['question_id']}，对齐 {q1['stats']['auto']} 个知识点")
        for item in q1["linked"]:
            print(f"        ✓ {item['kp_id']} {item['name']} ({item['role']}) "
                  f"score={item['score']} {item['evidence']}")
        for warn in q1["warnings"]:
            print(f"        ⚠ {warn}")
        matched = q1["stats"]["auto"] + q1["stats"]["confirm"]
        check("无模型时降级链路仍能匹配知识点", matched >= 1,
              f"匹配到 {matched} 个（题干含「奇偶性」，应由标签召回奇函数/偶函数）")

    print("\n3) 录入错题（图片通道）")
    provider = (health.get("environment", {}) or {}).get("vision_provider", "mock")
    if provider == "mock":
        # mock 模式：空白图也能返回预置内容，用来验证「图片 → 归档 → 入库」链路
        png = make_png()
        r = requests.post(f"{BASE}/api/ingest", timeout=180,
                          data={"student_id": student, "error_type": "method",
                                "auto_analyze": "false"},
                          files=[("files", ("shot.png", png, "image/png"))])
        check("POST /api/ingest（图片）", r.status_code == 200, r.text[:300])
        q2 = r.json() if r.status_code == 200 else {}
        if q2:
            print(f"      → 题目 {q2['question_id']}，识别引擎 {q2['ocr_engine']}，"
                  f"置信度 {q2['ocr_confidence']}")
            print(f"      → 原图归档 {q2['asset_path']}")
            print(f"      → 对齐知识点 {[i['name'] for i in q2['linked']]}")
    else:
        # 真实模型：空白图本来就该被拒（这正是我们想要的守门行为），
        # 所以这里验证「拒绝」而不是「成功」，避免每次跑测试都烧图片 token。
        png = make_png()
        r = requests.post(f"{BASE}/api/ingest", timeout=180,
                          data={"student_id": student, "auto_analyze": "false"},
                          files=[("files", ("blank.png", png, "image/png"))])
        check("POST /api/ingest（空白图应被拒绝）", r.status_code == 400,
              f"期望 400，实际 {r.status_code} {r.text[:200]}")
        print(f"      → 识别引擎 {provider}（真实模型）")
        print(f"      → 空白图被正确拒绝：{r.json().get('error', '')[:60] if r.status_code == 400 else ''}")
        print("      → 真实识图能力请用：python -m app.tests.check_vision --image")

        print("\n3b) 多题拆分与勾选入库（一页多题不能一股脑全录）")
        multi = make_multi_png()
        r = requests.post(f"{BASE}/api/ingest_async", timeout=120,
                          data={"student_id": student, "auto_analyze": "false"},
                          files=[("files", ("page.png", multi, "image/png"))])
        check("POST /api/ingest_async（多题页）", r.status_code == 200, r.text[:300])
        rec_job = r.json().get("job_id", "") if r.status_code == 200 else ""
        job, deadline = {}, time.time() + 600
        while rec_job and time.time() < deadline:
            job = requests.get(f"{BASE}/api/jobs/{rec_job}", timeout=30).json()
            if job.get("state") != "running":
                break
            time.sleep(0.5)
        check("识别任务完成", job.get("state") == "done",
              f"{job.get('state')}: {job.get('error', '')[:120]}")
        pick = job.get("result") or {}
        if pick.get("needs_pick"):
            check("多题时先不入库", not pick.get("saved", False), "saved 应为 False")
            check("拆分出 ≥2 道题", pick.get("problem_count", 0) >= 2,
                  f"实际 {pick.get('problem_count')}")
            print(f"      → 识别到 {pick['problem_count']} 道题，尚未入库：")
            for item in pick["problems"][:4]:
                print(f"        [{item['index']}] {item['label']} :: {item['stem_md'][:46]}")

            # 关键：勾选入库必须复用识别结果，不能再传文件
            r = requests.post(f"{BASE}/api/ingest_commit", timeout=120, json={
                "job_id": rec_job, "picks": [0],
                "student_id": student, "auto_analyze": False, "error_type": "method",
            })
            check("POST /api/ingest_commit（不重传文件）", r.status_code == 200, r.text[:300])
            commit_job = (r.json() or {}).get("job_id", "") if r.status_code == 200 else ""
            check("复用了已识别的题目", (r.json() or {}).get("reused_problems", 0) >= 2,
                  f"reused_problems={(r.json() or {}).get('reused_problems')}")
            job, deadline = {}, time.time() + 600
            while commit_job and time.time() < deadline:
                job = requests.get(f"{BASE}/api/jobs/{commit_job}", timeout=30).json()
                if job.get("state") != "running":
                    break
                time.sleep(0.5)
            saved = job.get("result") or {}
            check("入库任务完成", job.get("state") == "done",
                  f"{job.get('state')}: {job.get('error', '')[:120]}")
            check("只入库勾选的那一道", saved.get("created_count", 1) == 1,
                  f"created_count={saved.get('created_count')}")
            check("题目拿到了本页图片", bool(saved.get("asset_path")),
                  f"asset_path={saved.get('asset_path')!r}")
            print(f"      → 勾选第 1 道后入库：{saved.get('question_id')} "
                  f"{saved.get('stem_md', '')[:36]}")
            if saved.get("question_id"):
                requests.delete(f"{BASE}/api/wrongbook/{saved['question_id']}", timeout=30)
        else:
            check("应返回 needs_pick（模型未拆分多题）", False,
                  f"实际 problem_count={pick.get('problem_count')}")
        # 改用文字路径验证后续链路
        r = requests.post(f"{BASE}/api/ingest", timeout=180, data={
            "student_id": student, "error_type": "method", "auto_analyze": "false",
            "stem_text": "在 $\\triangle ABC$ 中，已知 $a=3$，$b=4$，$C=60^\\circ$，求边 $c$。",
        })
        check("POST /api/ingest（文字，真实模型）", r.status_code == 200, r.text[:300])
        q2 = r.json() if r.status_code == 200 else {}
        if q2:
            print(f"      → 题目 {q2['question_id']}，对齐 "
                  f"{[i['name'] for i in q2['linked']]}"
                  f"{'，待确认 ' + str(len(q2['needs_confirm'])) + ' 个' if q2['needs_confirm'] else ''}")

    print("\n4) 错题本列表")
    r = requests.get(f"{BASE}/api/wrongbook", params={"student_id": student}, timeout=30)
    check("GET /api/wrongbook", r.status_code == 200, r.text[:200])
    book = r.json() if r.status_code == 200 else {"items": []}
    check("列表含刚才录入的题目", len(book.get("items", [])) >= 1,
          f"实际 {len(book.get('items', []))} 条")
    print(f"      → 共 {book.get('total')} 条，overview={book.get('overview')}")

    if book.get("items"):
        qid = book["items"][0]["id"]
        r = requests.get(f"{BASE}/api/wrongbook/{qid}", timeout=30)
        check("GET /api/wrongbook/{id}", r.status_code == 200, r.text[:200])
        detail = r.json()
        print(f"      → 根因候选 {[x['name'] for x in detail.get('root_causes', [])]}")

    # 回归：/api/kp/{id} 返回的相关错题必须带知识点名称。
    # 此前只有 /api/wrongbook 补了名称，/api/kp/{id} 漏了，前端只好回退成显示编号，
    # 界面上看起来就是同一个编号被印了两遍。
    linked = [k["kp_id"] for it in book.get("items", []) for k in (it.get("kps") or [])]
    if linked:
        kp_doc = requests.get(f"{BASE}/api/kp/{linked[0]}",
                              params={"student_id": student}, timeout=30).json()
        qs = kp_doc.get("questions") or []
        missing = [k["kp_id"] for q in qs for k in (q.get("kps") or []) if not k.get("name")]
        check("回归：/api/kp/{id} 的相关错题带知识点名称",
              bool(qs) and not missing, f"相关错题 {len(qs)} 道，缺名称 {missing}")
        print(f"      → {linked[0]} 关联 {len(qs)} 道错题，知识点名称均已补齐")

    print("\n5) 热度统计")
    r = requests.get(f"{BASE}/api/heatmap", params={"student_id": student}, timeout=30)
    check("GET /api/heatmap", r.status_code == 200, r.text[:200])
    heat = r.json()
    print(f"      → {heat['summary']}")
    hot = [i for i in heat["items"] if i["wrong_count"]][:5]
    for item in hot:
        print(f"        {item['name']}：{item['wrong_count']} 道，"
              f"热度「{item['heat']['label']}」{item['heat']['color']}，"
              f"优先级 {item['priority']}")

    r = requests.get(f"{BASE}/api/module-summary", params={"student_id": student}, timeout=30)
    check("GET /api/module-summary", r.status_code == 200, r.text[:200])
    rows = [m for m in r.json()["items"] if m["wrong_count"]]
    print(f"      → 有错题的模块：{[m['module_name'] for m in rows]}")

    r = requests.get(f"{BASE}/api/review-plan", params={"student_id": student}, timeout=30)
    check("GET /api/review-plan", r.status_code == 200, r.text[:200])
    for p in r.json()["items"][:3]:
        print(f"        {p['order']}. {p['name']}（{p['dominant_error_cn']}）→ {p['action']}")

    print("\n6) 图谱检索与学习路径")
    r = requests.get(f"{BASE}/api/search", params={"q": "函数的奇偶性"}, timeout=30)
    check("GET /api/search", r.status_code == 200, r.text[:200])
    results = r.json()["results"]
    print(f"      → {[x['kp']['name'] for x in results[:4]]}")

    kp_id = results[0]["kp"]["id"] if results else "KP-FUNC-0041"
    r = requests.get(f"{BASE}/api/kp/{kp_id}", params={"student_id": student}, timeout=30)
    check("GET /api/kp/{id}", r.status_code == 200, r.text[:200])
    kp = r.json()
    print(f"      → {kp['kp']['name']}：前置 {len(kp['prerequisites'])} 个，"
          f"后继 {len(kp['successors'])} 个，关联错题 {kp['wrong_count']} 道")

    r = requests.get(f"{BASE}/api/path", params={"target": kp_id}, timeout=30)
    check("GET /api/path", r.status_code == 200, r.text[:200])
    path = r.json()
    print(f"      → 学习路径 {path['total']} 步："
          f"{[s['name'] for s in path['steps'][:4]]}")

    print("\n7b) 异步录入与进度查询")
    r = requests.post(f"{BASE}/api/ingest_async", timeout=60, data={
        "student_id": student, "auto_analyze": "false",
        "stem_text": "已知函数 $f(x)=x^2-4x+3$，求它的单调递增区间。",
    })
    check("POST /api/ingest_async 立即返回 job_id",
          r.status_code == 200 and r.json().get("job_id"), r.text[:200])
    job_id = r.json().get("job_id") if r.status_code == 200 else ""
    if job_id:
        check("初始状态为 running",
              requests.get(f"{BASE}/api/jobs/{job_id}", timeout=30).json()["state"] == "running")
        progress_seen: list[float] = []
        deadline = time.time() + 300
        state = "running"
        while time.time() < deadline:
            job = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=30).json()
            progress_seen.append(job["percent"])
            state = job["state"]
            if state != "running":
                break
            time.sleep(0.5)
        check("任务能跑到终态", state == "done", f"最终状态 {state}：{job.get('error', '')[:120]}")
        check("进度单调不回退", all(b >= a for a, b in zip(progress_seen, progress_seen[1:])),
              f"采样：{progress_seen[:12]}")
        check("完成时进度为 100", job["percent"] == 100, f"实际 {job['percent']}")
        check("结果已回写到任务", bool(job.get("result")), "result 为空")
        created = (job.get("result") or {}).get("question_id", "")
        print(f"      → 任务 {job_id} 完成，耗时 {job['elapsed']}s，"
              f"进度采样 {len(progress_seen)} 次，入库 {created}")
        if created:
            requests.delete(f"{BASE}/api/wrongbook/{created}", timeout=30)
    r = requests.get(f"{BASE}/api/jobs/nonexistent-job", timeout=30)
    check("查询不存在的任务返回 404", r.status_code == 404, f"实际 {r.status_code}")

    print("\n7) 图谱视图接口")
    r = requests.get(f"{BASE}/api/books", timeout=30)
    check("GET /api/books", r.status_code == 200, r.text[:200])
    books = r.json()["books"]
    print(f"      → {[(b['code'], b['kp_count']) for b in books]}")

    for params, label, expect_nodes in [
        ({"book": "BX1"}, "按册 BX1", 113),
        ({"module": "FUNC"}, "按模块 FUNC", 56),
        ({"focus": "KP-FUNC-0043", "depth": 2}, "焦点展开", 0),
    ]:
        r = requests.get(f"{BASE}/api/graph", params={**params, "student_id": student}, timeout=60)
        ok = r.status_code == 200
        check(f"GET /api/graph（{label}）", ok, r.text[:200])
        if not ok:
            continue
        data = r.json()
        got = data["stats"]["node_count"]
        if expect_nodes:
            check(f"  {label} 节点数正确", got == expect_nodes, f"期望 {expect_nodes}，实际 {got}")
        else:
            check(f"  {label} 至少含目标节点", any(n["id"] == params["focus"] for n in data["nodes"]))
        check(f"  {label} 边两端都在节点集内",
              all(e["source"] in {n["id"] for n in data["nodes"]}
                  and e["target"] in {n["id"] for n in data["nodes"]} for e in data["edges"]))
        hot = [n for n in data["nodes"] if n["wrong_count"]]
        hot_text = "、".join(f"{n['name']}({n['wrong_count']})" for n in hot[:3])
        print(f"      → {label}：{got} 节点 / {data['stats']['edge_count']} 边，"
              f"{len(hot)} 个有错题" + (f"：{hot_text}" if hot_text else ""))

    r = requests.get(f"{BASE}/api/graph", params={"focus": "KP-NOT-EXIST"}, timeout=30)
    check("不存在的焦点返回 404", r.status_code == 404, f"实际 {r.status_code}")

    print("\n8) 清理")
    for item in book.get("items", []):
        requests.delete(f"{BASE}/api/wrongbook/{item['id']}", timeout=30)
    r = requests.get(f"{BASE}/api/wrongbook", params={"student_id": student}, timeout=30)
    check("删除后列表为空", len(r.json()["items"]) == 0, f"剩余 {len(r.json()['items'])}")

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
