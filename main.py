#!/usr/bin/env python3
"""道路标志生成器 —— 程序入口：双击运行或 python main.py。

作者：Kcx36
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    try:
        from ui import main as gui_main
    except ImportError as exc:
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        return 2
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
