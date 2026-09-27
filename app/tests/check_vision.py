#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""识图通道连通性自检：不用真图片，直接验证配置与模型能力。

用法：
    python -m app.tests.check_vision                    # 纯文本连通性（最省 token）
    python -m app.tests.check_vision --image            # 用自动生成的数学题图片实测识图
    python -m app.tests.check_vision 我的错题.jpg       # 用你自己的图片/PDF 实测

它会依次回答四个问题：
  1. .env 配好了吗（provider/base/model/key 是否齐全）
  2. 网络与鉴权通不通（能否拿到模型回复）
  3. 这个模型接受图片输入吗
  4. 输出是不是我们要求的 JSON 结构、能拆出几道题
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.config import vision_config  # noqa: E402
from app.wrongbook.vision import (VisionError, chat_text,  # noqa: E402
                                  prepare_images, recognize_question)

PROBLEM = ("已知函数 f(x) = 2^x - 2^(-x)。\n"
           "(1) 判断 f(x) 的奇偶性，并说明理由；\n"
           "(2) 若 f(m-1) + f(2m) < 0，求实数 m 的取值范围。")


def mask(key: str) -> str:
    if not key:
        return "<未填>"
    return key[:6] + "*" * 6 + key[-4:] if len(key) > 14 else "***"


CJK_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def _load_font(size: int):
    from PIL import ImageFont
    for path in CJK_FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size), path
            except OSError:
                continue
    return None, ""


def make_image(path: Path) -> Path:
    """生成一张「试卷照片」风格的图。

    必须用真正的中文字体：PIL 默认位图字体画不出汉字（会渲染成空白或方块），
    那样测出来的「识别成功」其实是模型在猜，结论不可信。
    """
    from PIL import Image, ImageDraw
    font, font_path = _load_font(30)
    if font is None:
        print("   ⚠ 未找到中文字体，测试图中文会缺失，识别结果不可信。")
    else:
        print(f"   使用字体：{font_path}")
    img = Image.new("RGB", (1000, 300), (252, 250, 245))
    draw = ImageDraw.Draw(img)
    y = 34
    for line in PROBLEM.splitlines():
        draw.text((44, y), line, fill=(25, 25, 30), font=font)
        y += 58
    img.save(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="识图通道自检")
    parser.add_argument("path", nargs="?", default=None,
                        help="要识别的图片/PDF 路径；不填则用自动生成的测试图")
    parser.add_argument("--image", action="store_true", help="用自动生成的测试图实测识图")
    args = parser.parse_args()

    cfg = vision_config()
    print("=" * 60)
    print("1) 配置检查")
    print(f"   provider : {cfg['provider']}")
    print(f"   base     : {cfg['base']}")
    print(f"   model    : {cfg['model'] or '<未填>'}")
    print(f"   key      : {mask(cfg['key'])}")
    if (cfg["provider"] or "").lower() == "mock":
        print("\n   ⚠ provider 还是 mock（离线样例模式），不会真的调用模型。")
        print("     把 app/.env 里的 VISION_PROVIDER 改成 deepseek 再来测。")
        return 1
    if not cfg["key"]:
        print("\n   ✗ 没有填 VISION_API_KEY")
        return 1
    if not cfg["model"]:
        print("\n   ✗ 没有填 VISION_MODEL（模型名称）")
        return 1

    print("\n2) 纯文本连通性")
    try:
        reply = chat_text([{"role": "user", "content": "只回复两个字：通了"}], max_tokens=2048)
    except VisionError as exc:
        print(f"   ✗ 调用失败：{exc}")
        return 1
    print(f"   ✓ 模型回复：{reply.strip()[:60]}")

    if not args.image and not args.path:
        print("\n3) 跳过识图测试（加 --image 或直接给出图片路径可实测）")
        print("=" * 60)
        return 0

    print("\n3) 识图测试")
    generated = False
    if args.path:
        img_path = Path(args.path)
        if not img_path.exists():
            print(f"   ✗ 文件不存在：{img_path}")
            return 1
        print(f"   使用你自己的文件：{img_path}（{img_path.stat().st_size / 1024:.0f} KB）")
    else:
        img_path = Path(__file__).parent / "_vision_check.png"
        make_image(img_path)
        generated = True
        print(f"   已生成测试题图片：{img_path.name}")
    try:
        images = prepare_images([img_path])
        print(f"   图片预处理 OK（{len(images)} 张）")
        result = recognize_question([img_path], hint="")
    except VisionError as exc:
        print(f"   ✗ 识图失败：{exc}")
        print("\n   可能原因：")
        print("     · 该模型不支持图片输入（换支持视觉的模型）")
        print("     · 模型名称写错")
        print("     · 账号未开通该模型")
        return 1
    finally:
        if generated:
            img_path.unlink(missing_ok=True)

    problems = result["problems"]
    print(f"   ✓ 识别引擎：{result['engine']}")
    print(f"   ✓ 识别到 {len(problems)} 道题")
    if result.get("page_note"):
        print(f"   ✓ 页面说明：{result['page_note'][:70]}")
    for p in problems[:12]:
        stem = p["stem_md"].replace(chr(10), " ")
        print(f"      [{p['label'] or '?'}] {p['question_type']} "
              f"conf={p['confidence']:.2f} :: {stem[:56]}")
    if len(problems) > 12:
        print(f"      … 还有 {len(problems) - 12} 道")

    print("\n4) 输出结构校验")
    first = problems[0]
    missing = [k for k in ("stem_md", "question_type", "confidence") if not first.get(k)]
    if missing:
        print(f"   ⚠ 缺少字段：{missing}")
    else:
        print("   ✓ 字段齐全，可直接进入知识点对齐流程")
    latex_hit = "$" in first.get("stem_md", "")
    print(f"   {'✓' if latex_hit else '⚠'} LaTeX 公式："
          f"{'已识别为 $...$ 格式' if latex_hit else '未出现 $ 符号，公式可能是纯文本'}")
    if len(problems) > 1:
        print(f"   ✓ 已自动拆分为 {len(problems)} 道题"
              f"（网页录题时会让你勾选要记录哪几道）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
