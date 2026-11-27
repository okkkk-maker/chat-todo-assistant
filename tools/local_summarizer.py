"""本地待办提炼与清单生成器 (Local To-Do Extractor & Markdown Generator)

「待办提炼与推送链路端到端闭环」：
当 外部协作引擎 未在线或离线执行时，本地内置基于规则语义的待办提取引擎，
自动分析 work/待办素材.json，提取带完整可验证溯源标签的待办清单，
输出为 output/todo-YYYYMMDD-HHmm.md，实现完全自治的端到端交付闭环。

设计原则（严格对齐 automation-prompt.md）：
1. 不臆造：无 msg_id 不出条，溯源字段原样保留。
2. 强制溯源：每条待办紧跟 [来源: ... | 会话: ... | 发送人: ... | 时间: ... | ID: ...] 标签。
3. 状态透明：头部呈现各通道状态表格，未覆盖通道在末尾清晰告警。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common

URGENT_KEYWORDS = re.compile(r"(紧急|加急|立即|马上|火速|尽快|务必|必须|重要|急件|截止今日|今晚之前)", re.IGNORECASE)
TODAY_KEYWORDS = re.compile(r"(今天|今日|明天|请|提交|安排|通知|提醒|审核|签字|汇报|确认|参会|开会|报送|落实|准备|批复|办理|协同|请阅)", re.IGNORECASE)
ACTION_KEYWORDS = re.compile(r"(抓紧|按时|下发|查收|反馈|沟通|跟进|注意|配合|组织|落实|请假|填报)", re.IGNORECASE)

# 噪音识别：微信/QQ 的占位语与营销推文。这类内容长度够、还带"请/确认"之类词，
# 只靠关键词分类会把广告塞进"今日待办"，必须在这里先剔掉。
PLACEHOLDER_NOISE = re.compile(r"(不支持展示该内容|请升级至最新版本|该消息类型暂不支持|已撤回一条消息)")
PROMO_HINTS = re.compile(
    r"(优惠|折扣|秒杀|红包|优惠券|领券|满减|特价|包邮|下单|退订|会员|点我|戳我|抢购|限时|"
    r"到店|外卖|好价|福利|返现|抽奖|清仓|快抢|折价|一折|直降|爆款|特卖|甩卖|团购|拼单|"
    r"运费险|现货|天猫|淘宝|京东|拼多多|代购|秒发)",
    re.IGNORECASE,
)
TOKEN_ONLY = re.compile(r"^[A-Za-z0-9_\-=+/]{12,}$")

# 强工作语汇：出现这些词的，即使带链接、即使命中两个营销词，也一律不当广告。
# 为什么需要它：「工会会员福利发放，请各部门统计名单 https://…」同时命中
# 会员/福利两个营销词并带链接，只按词频判就必然误杀；而这类通知恰恰是
# 体制内/企业群最主要的待办来源。宁可漏杀不可错杀。
WORK_HINT = re.compile(
    r"(各部门|各处室|各科室|各处|单位|统计|报送|提交|上报|名单|台账|材料|汇总|"
    r"会议|召开|参会|议程|审核|审批|签字|批复|落实|布置|部署|值班|考勤|报销|"
    r"截止|期限|公章|归档|整改|督查|考核)"
)


def _looks_like_noise(content: str) -> bool:
    """保守判定：只剔除明确的占位语与营销推文，宁可漏杀不可错杀。"""
    if PLACEHOLDER_NOISE.search(content):
        return True
    # 纯令牌/乱码（如 u_8V9zON7jwA5WXREgpARS9Q），既不是话也不是待办
    if TOKEN_ONLY.match(content.strip()):
        return True
    promo_hits = len(PROMO_HINTS.findall(content))
    has_link = ("http://" in content) or ("https://" in content) or ("👉" in content)
    # 3 个以上营销词：无论有没有链接都判推文（正常通知不会这么密集）
    if promo_hits >= 3:
        return True
    # 带链接 + 2 个营销词，且不含任何工作语汇 → 判推文
    if has_link and promo_hits >= 2 and not WORK_HINT.search(content):
        return True
    return False


def _audit_noise(items: list[dict]) -> None:
    """被噪音规则丢掉的条目留痕，方便事后核对"是不是误杀"。

    旧实现丢得无声无息：清单里没有、日志里没有、计数里也没有，用户根本
    不知道有一条待办被吃了。现在落 work/filtered-YYYYMMDD.jsonl，并在清单
    末尾给一行统计。
    """
    if not items:
        return
    try:
        path = common.WORK / f"filtered-{datetime.now():%Y%m%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for it in items:
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")
    except OSError:
        pass


def extract_action_items(messages: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """根据语义关键词将消息分类为：紧急重要、今日待办、近期关注。

    返回 (urgent, today, upcoming, noise_dropped)。noise_dropped 里是被
    噪音规则剔除的原始条目，调用方负责留痕与统计。
    """
    urgent = []
    today = []
    upcoming = []
    noise: list[dict] = []

    seen_ids = set()

    for m in messages:
        mid = str(m.get("msg_id") or "").strip()
        content = str(m.get("content") or "").strip()
        if not mid or not content or mid in seen_ids:
            continue
        seen_ids.add(mid)

        # 过滤纯表情、数字单字或无意义回复
        if len(content) < 3 or content.lower() in ("收到", "ok", "好的", "1", "赞", "[表情]", "[图片]"):
            continue

        # 过滤占位语与营销推文（先判噪音，再进关键词分类）
        if _looks_like_noise(content) and not URGENT_KEYWORDS.search(content):
            noise.append({
                "msg_id": mid,
                "source": m.get("source") or "",
                "chat_name": m.get("chat_name") or "",
                "sender": m.get("sender") or "",
                "timestamp": m.get("timestamp") or "",
                "content": content,
                "dropped_by": "noise_rule",
            })
            continue

        item = {
            "source": m.get("source") or "未知",
            "chat_name": m.get("chat_name") or "未知会话",
            "sender": m.get("sender") or "未知发言人",
            "timestamp": m.get("timestamp") or "未知时间",
            "msg_id": mid,
            "content": content,
        }

        if URGENT_KEYWORDS.search(content):
            urgent.append(item)
        elif TODAY_KEYWORDS.search(content):
            today.append(item)
        elif ACTION_KEYWORDS.search(content) or len(content) >= 15:
            upcoming.append(item)

    if noise:
        _audit_noise(noise)
        common.log(f"      [噪音过滤] 本轮按广告/占位规则忽略 {len(noise)} 条，"
                   f"明细见 work/filtered-{datetime.now():%Y%m%d}.jsonl")

    return urgent, today, upcoming, noise


def format_source_tag(m: dict) -> str:
    return f"[来源: {m['source']} | 会话: {m['chat_name']} | 发送人: {m['sender']} | 时间: {m['timestamp']} | ID: {m['msg_id']}]"


def generate_todo_markdown(material_path: Path | None = None, out_dir: Path | None = None) -> Path | None:
    """读取待办素材生成规范 Markdown 清单并落盘。"""
    common.ensure_dirs()
    mat_file = material_path or common.MATERIAL_PATH
    if not mat_file.exists():
        return None

    try:
        data = common.read_json(mat_file)
    except Exception:
        return None

    meta = data.get("meta", {})
    messages = data.get("messages", [])
    stats = meta.get("channel_stats", {})

    now_dt = datetime.now()
    cutoff_time = meta.get("cutoff_time") or now_dt.strftime("%Y-%m-%d %H:%M:%S")
    file_name = f"todo-{now_dt.strftime('%Y%m%d-%H%M')}.md"
    out_path = (out_dir or common.OUT) / file_name

    lines = []
    lines.append(f"# 待办汇总 · {cutoff_time}")
    lines.append("")
    lines.append("| 通道 | 状态 | 抓取条数 | 运行说明 |")
    lines.append("|---|---|---|---|")

    channel_names = {"WeCom": "企业微信", "QQ": "QQ", "WeChat": "个人微信", "Feishu": "飞书", "DingTalk": "钉钉"}
    all_chans = ["WeCom", "QQ", "WeChat", "Feishu", "DingTalk"]

    for ch in all_chans:
        cname = channel_names.get(ch, ch)
        cstat = stats.get(ch, {})
        st_code = cstat.get("status", "NOT_READY")
        cnt = cstat.get("count", 0)
        msg = cstat.get("message", "通道待装配")
        icon = "🟢" if st_code in ("OK", "USABLE") else ("🟡" if st_code in ("STALE", "EMPTY", "NO_NEW") else "🔴")
        lines.append(f"| {cname} | {icon} {st_code} | {cnt} | {msg} |")

    lines.append("")
    status_str = meta.get("status", "UNKNOWN")
    cnt_str = len(messages)
    time_range = meta.get("time_range", "最近")
    lines.append(f"> **本轮状态**：`{status_str}` ｜ **提取素材**：`{cnt_str}` 条 ｜ **时间窗口**：`{time_range}`")
    lines.append("")

    urgent, today, upcoming, noise_items = extract_action_items(messages)

    if not messages:
        lines.append("### ℹ️ 本轮暂无待办事项")
        lines.append("- 当前时间窗口内各监控会话未产生新待办消息或所有会话均无未决议题。")
        lines.append("")
    else:
        if urgent:
            lines.append("### 🔴 【紧急重要】")
            for u in urgent:
                lines.append(f"- **{u['content']}**")
                lines.append(f"  {format_source_tag(u)}")
            lines.append("")

        if today:
            lines.append("### 🟡 【今日待办】")
            for t in today:
                lines.append(f"- {t['content']}")
                lines.append(f"  {format_source_tag(t)}")
            lines.append("")

        if upcoming:
            lines.append("### 🔵 【近期关注】")
            for up in upcoming:
                lines.append(f"- {up['content']}")
                lines.append(f"  {format_source_tag(up)}")
            lines.append("")

    broken = meta.get("broken_channels") or []
    if broken:
        lines.append("---")
        for b in broken:
            cname = channel_names.get(b, b)
            bmsg = (stats.get(b) or {}).get("message") or "未连接"
            lines.append(f"⚠️ **本次未覆盖平台**：{cname}（{bmsg}），该平台可能有未收录的重要待办，请注意排查。")

    if noise_items:
        # 透明化：告诉用户"有内容被规则忽略了"，别让人以为只读到这么多
        lines.append("")
        lines.append(
            f"> ℹ️ 另有 {len(noise_items)} 条消息因命中广告/占位规则（含链接且有多个营销词）被忽略，"
            f"明细见 `work/filtered-{datetime.now():%Y%m%d}.jsonl`；"
            f"若确认是正常通知被误判，可调整 PROMO_HINTS 关键词。"
        )

    common.atomic_write_text(out_path, "\n".join(lines))
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="本地待办提炼生成器")
    ap.add_argument("--push", action="store_true", help="生成后尝试通过企微 Webhook 推送")
    args = ap.parse_args()

    p = generate_todo_markdown()
    if p:
        print(f"[OK] 待办清单已生成: {p}")
        if args.push:
            try:
                import push_wecom
                push_wecom.push_file(p)
            except Exception as e:
                print(f"[WARN] 推送失败: {e}")
        return 0
    print("[WARN] 未能生成清单（素材文件不存在或损坏）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
