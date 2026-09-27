#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面版入口：双击运行，自动起服务并打开浏览器。

与 `python -m app.server` 的区别只有三点，都是为了「像个软件」：
  1. 端口被占用时自动换一个，不报错退出；
  2. 启动完成后自动打开系统浏览器；
  3. 把「配置文件在哪、怎么关掉它」这类信息打在窗口里。

打包成 exe 后（packaging/build_exe.py）用户看到的就是这个入口。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

if not getattr(sys, "frozen", False):
    # 直接 `python app/desktop.py` 时 sys.path[0] 是 app/ 而不是项目根，
    # 不补这一行就 import 不到 app.*（打包后由 PyInstaller 接管，无需处理）。
    _PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from app.config import ENV_PATH, FROZEN, ROOT, ensure_dirs  # noqa: E402

PORT_CANDIDATES = (8000, 8010, 8080, 8123, 9000)


def pick_port(preferred: int) -> int:
    """按候选顺序找一个能绑上的端口；全被占了就交给系统随机分配。"""
    for port in (preferred, *PORT_CANDIDATES):
        if port == 0:
            break
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))          # 让操作系统挑一个空闲端口
        return int(sock.getsockname()[1])


def banner(url: str) -> None:
    line = "─" * 56
    print(f"\n  {line}")
    print("   StudyBuddy · 知识图谱版")
    print(f"   已启动：{url}")
    print(f"   密钥与模型：打开网页右上角「⚙ 设置」填写")
    if FROZEN:
        print(f"   配置文件：{ENV_PATH}")
        print(f"   数据目录：{ROOT / 'data'}（错题库、图片都在这里）")
    print(f"   {line}")
    print("   关闭这个黑窗口即退出程序。\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="StudyBuddy 桌面版")
    parser.add_argument("--port", type=int, default=8000, help="首选端口（占用时自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    ensure_dirs()

    # 延迟导入：先把目录准备好，再让各模块完成初始化
    import uvicorn

    from app import server
    from app.wrongbook import store as wrongbook_store

    wrongbook_store.init_db()

    port = pick_port(args.port)
    url = f"http://127.0.0.1:{port}"
    banner(url)
    if not args.no_browser:
        # 稍等片刻再开浏览器，避免页面先于服务加载而报错
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # 冻结后不能用 "app.server:app" 这种字符串写法（没有可导入的文件路径），
    # 直接把 app 对象交给 uvicorn。
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
