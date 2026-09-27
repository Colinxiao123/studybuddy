#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示数据：往错题本里灌入一组样例，方便查看界面效果。

用法：
    python -m app.tests.seed_demo                       # 写入 student_id=demo
    python -m app.tests.seed_demo --clear               # 仅清空演示数据
    python -m app.tests.seed_demo --student 小明        # 指定学生

注意：这里用「文本题干」路径录入，不需要 API Key；
若配置了识图模型，也可以在网页里直接拍照上传。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.wrongbook import pipeline, store  # noqa: E402

SAMPLES = [
    (r"已知等差数列 $\{a_n\}$ 的首项为 2，公差为 3，求它的前 $n$ 项和公式。", "calc", ""),
    (r"在 $\triangle ABC$ 中，已知 $a=3$，$b=4$，$C=60^\circ$，求边 $c$ 的长。", "method", ""),
    (r"已知函数 $f(x)=2^{x}-2^{-x}$。(1) 判断 $f(x)$ 的奇偶性并证明；"
     r"(2) 若 $f(m-1)+f(2m)<0$，求实数 $m$ 的取值范围。", "concept",
     r"$f(-x)=2^{-x}-2^{x}=f(x)$，所以是偶函数"),
    (r"求过点 $(1,2)$ 且与直线 $2x-y+1=0$ 垂直的直线方程。", "concept", ""),
    (r"从 5 名男生和 4 名女生中选 3 人参加比赛，要求至少有 1 名女生，共有多少种不同的选法？",
     "read", ""),
    (r"已知 $\sin\alpha=\frac{3}{5}$，且 $\alpha$ 是第二象限角，求 $\cos 2\alpha$ 的值。",
     "method", r"$\cos 2\alpha=1-2\sin^2\alpha$"),
    (r"已知椭圆 $\frac{x^2}{25}+\frac{y^2}{9}=1$，求它的离心率与焦点坐标。", "concept", ""),

    # ---- 下面几道都围绕「函数的单调性」这一薄弱点，
    #      让演示数据里至少有一个知识点能积累到「中/高」热度，
    #      这样才能看出图谱视图里节点颜色的分级效果 ----
    (r"用定义证明函数 $f(x)=x+\frac{1}{x}$ 在区间 $(1,+\infty)$ 上是增函数。", "method", ""),
    (r"求函数 $f(x)=x^{2}-4x+3$ 的单调递增区间。", "method", ""),
    (r"已知 $f(x)$ 在 $\mathbf{R}$ 上是增函数，且 $f(2a-1)>f(a+3)$，求 $a$ 的取值范围。",
     "concept", ""),
    (r"判断函数 $f(x)=\frac{1}{x}$ 在 $(-\infty,0)$ 和 $(0,+\infty)$ 上的单调性。", "concept", ""),
    (r"已知函数 $f(x)=x^{2}-2ax+3$ 在区间 $[1,2]$ 上单调递增，求实数 $a$ 的取值范围。",
     "method", ""),
    (r"设函数 $f(x)=x^3-3x$，求 $f(x)$ 的单调区间与极值。", "method", ""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="写入演示错题数据")
    parser.add_argument("--student", default="demo")
    parser.add_argument("--clear", action="store_true", help="清空该学生的全部数据后退出")
    parser.add_argument("--no-confirm", action="store_true",
                        help="不自动确认「待确认知识点」（默认自动采纳首选，模拟学生点确认）")
    args = parser.parse_args()

    store.init_db()
    existing = store.list_questions(args.student, limit=500)
    for item in existing:
        store.delete_question(item["id"])
    print(f"[清理] 删除 {len(existing)} 条已有记录（学生 {args.student}）")
    if args.clear:
        return 0

    for stem, error_type, student_answer in SAMPLES:
        result = pipeline.ingest(args.student, stem_text=stem, student_answer=student_answer,
                                 error_type=error_type, auto_analyze=False)
        qid = result["question_id"]

        # 模拟学生点开「待确认」并采纳系统首选（真实使用时由人来勾选）
        confirmed = 0
        if not args.no_confirm:
            chosen = [{"kp_id": item["options"][0]["kp_id"], "role": item["role"],
                       "candidate_name": item["candidate"],
                       "score": item["options"][0]["score"]}
                      for item in result["needs_confirm"] if item.get("options")]
            if chosen:
                pipeline.apply_confirmations(qid, chosen)
                confirmed = len(chosen)

        auto = [i["name"] for i in result["linked"]]
        print(f"  {qid}  错因={error_type:8s} 自动 {len(auto)} + 确认 {confirmed} 个知识点")
        if auto:
            print(f"      自动：{'、'.join(auto)}")
        if confirmed:
            print(f"      确认：{'、'.join(item['options'][0]['name'] for item in result['needs_confirm'] if item.get('options'))}")

    heat = store.kp_wrong_stats(args.student)
    hot = sorted(heat.items(), key=lambda kv: -kv[1]["wrong_count"])[:8]
    print(f"\n[完成] {len(heat)} 个知识点有错题记录，热度 top：")
    for kp_id, stat in hot:
        print(f"  {kp_id}  {stat['wrong_count']} 道  {stat['heat']['label']}")
    print(f"\n打开网页 → 右上角把「学生」改成 {args.student} 即可查看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
