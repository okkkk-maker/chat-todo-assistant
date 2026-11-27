"""多平台通道适配层。"""
from __future__ import annotations

from . import base, feishu, wechat

NATIVE_ADAPTERS = {
    "WeChat": wechat.fetch,
    "Feishu": feishu.fetch,
}

INBOX_CHANNELS = ("WeCom", "DingTalk")

CHANNEL_CN = {
    "QQ": "QQ",
    "WeChat": "个人微信",
    "Feishu": "飞书",
    "DingTalk": "钉钉",
    "WeCom": "企业微信",
}

__all__ = ["base", "feishu", "wechat", "NATIVE_ADAPTERS", "INBOX_CHANNELS", "CHANNEL_CN"]
