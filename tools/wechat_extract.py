"""【已废弃 · 保留仅为兼容】个人微信通道的旧实现。

 本模块**不再被 channel_collector 调用**。微信的真实实现在 `channels/wechat.py`：
   读 vendor/wechat-key 解密出的明文镜像（db_storage/message/message_N.db），
   按 Msg_<md5(wxid)> 取消息，支持群/单聊识别与卡片过滤。

保留本文件的原因：某些旧部署可能还有脚本直接 `import wechat_extract`。
它现在只做「检测目录 + 报告未实现」，不会伪造任何成功状态。

历史说明：本文件曾经声称"扫描到 0 条有效消息"并把游标推到当前时间，
而实际上它从未打开任何 .db —— 那是最危险的一类假阴性，已在新实现中彻底移除。

原始微信（Windows 客户端）聊天记录增量抽取模块。

设计原则（与 QQ 抽取 extract_impl.py 完全对齐）：
    1. 只读保护：先拷贝 MSG*.db / MicroMsg.db 至临时副本 work/tmp_wx/，绝不碰原库。
    2. 严格溯源：输出格式包含 (来源, 会话, 发送人, 时间, msg_id)。
    3. 0条四态判定：offline / no_new / only_noise / ok。
    4. 密钥未就绪保护：未填 WX_DBKEY 时明确返回 DISABLED，若未启用微信则安全跳过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import msgparse

WX_SEARCH_ROOTS = [
    Path(os.environ.get("USERPROFILE", "C:\\Users\\Default")) / "Documents" / "WeChat Files",
    Path(r"C:\WeChat Files"),
    Path(r"D:\WeChat Files"),
    Path(r"E:\WeChat Files"),
]


@dataclass
class WxResult:
    level: int = 0                     # 0 正常 / 1 陈旧 / 2 失败
    status: str = "OK"                 # OK / STALE / FAIL / NO_KEY / DISABLED
    count: int = 0
    stale_warning: bool = False
    message: str = ""
    exception: Path | None = None
    messages: list[dict] = field(default_factory=list)
    raw_count: int = 0
    dropped_count: int = 0
    max_ts: float = 0.0
    until: float = 0.0
    data_state: str = "ok"             # ok / no_new / only_noise / offline / disabled

    @property
    def ok(self) -> bool:
        return self.level < 2


def find_wechat_dir(configured_path: str = "") -> Path | None:
    if configured_path and Path(configured_path).exists():
        return Path(configured_path)
    for root in WX_SEARCH_ROOTS:
        if root.is_dir():
            try:
                for child in root.iterdir():
                    if child.is_dir() and (child / "Msg").is_dir():
                        return child
            except OSError:
                pass
    return None


def run(hours: float | None = None, env_path: Path | None = None) -> WxResult:
    env = common.load_env(env_path)
    wx_key = (env.get("WX_DBKEY", "") or "").strip()
    wx_dir_str = (env.get("WX_DATA_PATH", "") or "").strip()

    if not wx_key:
        return WxResult(level=0, status="DISABLED", message="未配置 WX_DBKEY，微信通道已跳过", data_state="disabled")

    wx_dir = find_wechat_dir(wx_dir_str)
    if not wx_dir or not (wx_dir / "Msg").is_dir():
        path = common.write_exception(
            "异常说明-微信目录未找到.txt",
            "已配置 WX_DBKEY 但未在常见路径检测到 WeChat Files 消息目录。",
            f"扫描路径: {[str(r) for r in WX_SEARCH_ROOTS]}\n配置路径: {wx_dir_str}"
        )
        return WxResult(level=2, status="FAIL", message="找不到微信消息目录", exception=path, data_state="offline")

    since, until, _ = common.window(hours, overlap_sec=600)
    cur_file = common.WORK / "last_sync_wx.txt"
    cur = common.read_cursor(cur_file)
    if cur and (until - cur < 7 * 86400):
        since = cur

    tmp_wx = common.WORK / "tmp_wx"
    shutil.rmtree(tmp_wx, ignore_errors=True)
    tmp_wx.mkdir(parents=True, exist_ok=True)

    messages: list[dict] = []
    raw_count = 0
    dropped = 0
    max_ts = 0.0

    try:
        msg_dir = wx_dir / "Msg" / "Multi"
        if not msg_dir.is_dir():
            msg_dir = wx_dir / "Msg"
        db_files = sorted(msg_dir.glob("MSG*.db"))

        # 【确定缺陷修复】本版本没有实现微信消息库的结构解析与解密读取。
        #
        # 旧代码在这里是：
        #     messages, raw_count 恒为 [] / 0
        #     data_state = "ok" if messages else ("offline" if stale else "no_new")
        #     → data_state 恒为 no_new，status 恒为 OK，max_ts 恒为 until
        # 也就是说：只要填了 WX_DBKEY，不管读没读，都会向上报告
        # 「微信在线，本时段没有新消息」，并且把 last_sync_wx.txt 推到当前时间。
        # 这是对着「不臆造」红线最严重的一处违反（假阴性 + 游标永久跳过）。
        #
        # 现在改为：明确返回 NOT_IMPLEMENTED，不写游标、不报成功。
        # 真正的读取实现需要：MSG*.db 的 SQLCipher 参数探测 + 表结构固化，
        # 与 qqdb / probe 同一套思路，属于后续功能开发，不属于本次稳定性修复。
        return WxResult(
            level=0,
            status="NOT_IMPLEMENTED",
            count=0,
            stale_warning=False,
            message=(
                f"微信通道尚未实现本地消息读取（已探到 {len(db_files)} 个 MSG*.db 文件，"
                "但本版本不会解密/解析它们）；微信条数按 0 计，不代表微信没有待办"
            ),
            messages=[],
            raw_count=0,
            dropped_count=0,
            max_ts=0.0,
            until=until,
            data_state="unavailable",
        )
    except Exception as exc:
        path = common.write_exception(
            "异常说明-微信抽取异常.txt",
            f"微信聊天记录读取过程中发生异常: {exc}",
            f"WX_DIR={wx_dir}\nWX_KEY={wx_key[:8]}..."
        )
        return WxResult(level=2, status="FAIL", message=f"微信抽取失败: {exc}", exception=path, data_state="offline")
    finally:
        shutil.rmtree(tmp_wx, ignore_errors=True)
