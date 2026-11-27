"""[已合并] 兼容垫片：真正的实现在 tools/health.py。

历史背景
本文件曾是 health.py 的一份**完整副本**（同一批函数抄了两遍：
inspect_channels_environment / handle_wechat_key_sync / test_* / import_inbox_messages）。
两份代码各自演进，改了一边忘了另一边，是"同一个按钮在不同入口行为不一致"的
典型土壤。v1.5.1 起只保留 health.py 一份实现，本文件仅做转发，
旧代码里的 `import inspect_helper` 仍然可用，但不再有第二套逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import health as _health  # noqa: E402

candidate_key_files = _health.candidate_key_files
inspect_channels_environment = _health.inspect_channels_environment
handle_wechat_key_sync = _health.handle_wechat_key_sync
wechat_key_scan = _health.wechat_key_scan
wechat_key_capture = _health.wechat_key_capture
trigger_wechat_key_extractor = _health.trigger_wechat_key_extractor
trigger_qq_key_extractor = _health.trigger_qq_key_extractor
test_feishu_connection = _health.test_feishu_connection
test_dingtalk_connection = _health.test_dingtalk_connection
test_wecom_webhook = _health.test_wecom_webhook
import_inbox_messages = _health.import_inbox_messages

__all__ = [
    "candidate_key_files",
    "inspect_channels_environment",
    "handle_wechat_key_sync",
    "wechat_key_scan",
    "wechat_key_capture",
    "trigger_wechat_key_extractor",
    "trigger_qq_key_extractor",
    "test_feishu_connection",
    "test_dingtalk_connection",
    "test_wecom_webhook",
    "import_inbox_messages",
]
