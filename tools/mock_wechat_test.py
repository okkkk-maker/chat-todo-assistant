# -*- coding: utf-8 -*-
"""微信链路联调入口（保留旧文件名，实际执行真加密 / 真解密 / 真读消息）。

历史说明
旧版本这个脚本是「假跑」：它把一个明文 sqlite 直接拷进镜像目录，再写一个
4096 字节的全零文件冒充加密库，于是解密这一步根本没被执行，密钥也写成假值，
却打印「100% 跑通」。这种测试比没有测试更糟 —— 它会把真 bug 藏起来。

现在直接委托给 tools/wx_selftest.py：
    真 SQLCipher4 加样本 → 真取密钥（含内存扫描）→ 真解密 → 真读消息 → 逐条核对。

用法:
    python tools\\mock_wechat_test.py            # 完整流程（推荐）
    python tools\\mock_wechat_test.py --no-scan  # 没有管理员权限时跳过内存扫描
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import wx_selftest  # noqa: E402

if __name__ == "__main__":
    sys.exit(wx_selftest.main(sys.argv[1:]))
