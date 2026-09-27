#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错题本启动器：一个文件搞定「找 Python → 找依赖 → 起服务 → 开浏览器」。

    python run.py                 # 起服务并自动打开浏览器（端口被占用会自动顺延）
    python run.py --port 9000     # 指定端口
    python run.py --no-browser    # 不自动打开浏览器
    python run.py --install       # 先建 .venv 装依赖再启动（新机器上第一次跑就用它）
    python run.py --server ...    # 不开浏览器，参数透传给 app.server（开发用，支持 --reload）

为什么要有这个文件：Windows / macOS / Linux 上 `python` 指向的环境五花八门，
最常见的坑是「装了 Python，但那个环境里没有 fastapi」。这里按优先级找一个**真能跑起来**的
解释器；找不到就打印可以直接复制的安装命令，或者用 --install 自动装，而不是甩一个
ModuleNotFoundError 让人自己猜。

跨平台：只用标准库，路径一律用 pathlib；.venv 的 Windows / macOS / Linux 布局都认。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
# 只探这几个：够判断「这个环境能不能跑本项目」
PROBE = "import fastapi, uvicorn, multipart, requests, PIL, pymupdf"
DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"   # 国内快；--pip-index 可换


def venv_python(root: Path) -> Path | None:
    """项目内虚拟环境的解释器。Windows 与 macOS/Linux 的目录布局都认。"""
    for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
        path = root / ".venv" / rel
        if path.exists():
            return path
    return None


def candidates() -> list[Path]:
    """候选解释器，按「最可能能跑」排序。"""
    out: list[Path] = []
    venv = venv_python(ROOT)
    if venv:
        out.append(venv)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    # 本机常用的 conda 环境（有就顺手用，没有不影响）
    exe = "python.exe" if os.name == "nt" else "bin/python"
    for base in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
        path = Path.home() / base / "envs" / "accountant" / exe
        if path.exists():
            out.append(path)
    return out


def has_deps(python: str | Path) -> bool:
    """这个解释器能不能 import 起本项目要用的库。"""
    try:
        result = subprocess.run([str(python), "-c", PROBE],
                                capture_output=True, timeout=90)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pick_python() -> Path | None:
    """先看当前解释器，再挨个试候选。"""
    if has_deps(sys.executable):
        return Path(sys.executable)
    for candidate in candidates():
        try:
            if Path(candidate).resolve() == Path(sys.executable).resolve():
                continue
        except OSError:
            pass
        if has_deps(candidate):
            return candidate
    return None


def install_deps(index: str) -> int:
    """建 .venv 并装依赖（第一次在新机器上跑时用）。"""
    if not REQUIREMENTS.exists():
        print(f"✗ 找不到依赖清单：{REQUIREMENTS}")
        return 1
    python = venv_python(ROOT)
    if not python:
        print(f"[安装] 创建虚拟环境：{ROOT / '.venv'}")
        code = subprocess.call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
        if code:
            print("✗ 创建虚拟环境失败。Ubuntu/Debian 上可能需要先装：sudo apt install python3-venv")
            return code
        python = venv_python(ROOT)
    if not python:
        print("✗ 虚拟环境建好了但找不到解释器，请手动执行：")
        print(f"    {sys.executable} -m venv .venv")
        return 1
    print("[安装] 正在装依赖（用国内镜像，稍等一两分钟）…")
    subprocess.call([str(python), "-m", "pip", "install", "-U", "pip", "-i", index, "-q"])
    code = subprocess.call([str(python), "-m", "pip", "install", "-i", index,
                            "-r", str(REQUIREMENTS)])
    if code:
        print("✗ 依赖安装失败，可以手动重试：")
        print(f"    {python} -m pip install -r requirements.txt")
        return code
    print("[安装] 完成")
    return 0


def print_help() -> None:
    line = "=" * 68
    print(line)
    print("没有找到装有依赖的 Python 环境。")
    print(f"  当前解释器：{sys.executable}")
    print("  缺少：fastapi / uvicorn 等（见 requirements.txt）")
    print()
    print("三条路，任选一条：")
    print()
    print("【1】让脚本自己装（推荐：会建 .venv，不动你的系统环境）")
    print("    python run.py --install")
    print()
    print("【2】手动装依赖再启动")
    print("    python -m pip install -r requirements.txt")
    print("    python run.py")
    print()
    print("【3】已经有装好依赖的虚拟环境")
    print("    .venv/bin/python run.py          # macOS / Linux")
    print(r"    .venv\Scripts\python.exe run.py  # Windows")
    print(line)


def main() -> int:
    args = sys.argv[1:]
    index = DEFAULT_INDEX
    if "--pip-index" in args:
        pos = args.index("--pip-index")
        if pos + 1 < len(args):
            index = args[pos + 1]
            del args[pos:pos + 2]

    if "--install" in args:
        args.remove("--install")
        code = install_deps(index)
        if code:
            return code

    python = pick_python()
    if python is None:
        print_help()
        return 1

    # 当前解释器不行、但找到别的能用的 → 换过去重跑本脚本
    try:
        same = Path(python).resolve() == Path(sys.executable).resolve()
    except OSError:
        same = False
    if not same:
        print(f"[启动] 当前 Python 缺依赖，自动切换到：{python}")
        try:
            return subprocess.call([str(python), str(ROOT / "run.py"), *args], cwd=str(ROOT))
        except KeyboardInterrupt:
            return 0

    if "--server" in args:
        args.remove("--server")
        from app.server import main as serve          # noqa: PLC0415
        sys.argv = ["app.server", *args]
        serve()
        return 0

    # 默认走桌面入口：挑空闲端口 + 自动开浏览器（学生一条命令就能用）
    from app.desktop import main as desktop           # noqa: PLC0415
    sys.argv = ["desktop", *args]
    desktop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
