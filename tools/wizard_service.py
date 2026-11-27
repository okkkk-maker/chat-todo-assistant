"""待办自动汇总助手 · 可视化新手引导向导服务层 (Wizard Service)

提供：
1. 向导进度与状态持久化 (config/wizard_state.json)
2. 环境、权限与核心聊天工具 (QQ / 微信 / 企微) 本地状态一键自动勘测
3. 真实脱敏示例素材初始化与即时提炼效果预览
4. 向导状态的重置与断点恢复
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

# 确保 tools 目录能正确引用同级模块
ROOT_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import common  # noqa: E402

WIZARD_FILE = common.CONFIG / "wizard_state.json"
SAMPLE_MATERIAL_FILE = common.WORK / "待办素材.sample.json"
SAMPLE_PREVIEW_FILE = common.OUT / "todo-demo-preview.md"


def get_wizard_state() -> dict[str, Any]:
    """读取向导状态。如果文件不存在，则初始化默认未完成状态。"""
    if WIZARD_FILE.exists():
        try:
            with open(WIZARD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            common.log(f"[Wizard] 读取 wizard_state.json 异常: {exc}")

    # 默认状态：新装状态下未完成，停留第 1 步
    default_state: dict[str, Any] = {
        "completed": False,
        "current_step": 1,
        "skipped": False,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": "首次启动待配置",
    }
    return default_state


def save_wizard_state(updates: dict[str, Any]) -> dict[str, Any]:
    """增量更新向导状态并原子落盘。"""
    state = get_wizard_state()
    state.update(updates)
    state["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        common.atomic_write_text(WIZARD_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        common.log(f"[Wizard] 写入 wizard_state.json 异常: {exc}")
    return state


def is_admin_user() -> bool:
    """检测当前进程是否具备管理员/特权权限。"""
    try:
        if os.name == "nt":
            import ctypes  # noqa: PLC0415
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        return os.geteuid() == 0
    except Exception:
        return False


def run_auto_detect() -> dict[str, Any]:
    """环境与核心数据目录一键勘测。"""
    env = common.load_env()

    # 1. 基础系统与 Python
    py_ver = sys.version.split()[0]
    os_name = f"{platform.system()} {platform.release()} ({platform.machine()})"
    admin_ok = is_admin_user()

    # 2. QQ 检测
    qq_info: dict[str, Any] = {
        "found": False,
        "accounts": [],
        "current_uin": env.get("QQ_TARGET_UIN", ""),
        "db_path": env.get("QQ_DB_PATH", ""),
        "has_key": bool(env.get("QQ_DBKEY")),
        "key_masked": common.mask(env.get("QQ_DBKEY", "")),
        "key_val": env.get("QQ_DBKEY", ""),
        "status_text": "未配置",
    }
    try:
        import admin_init  # noqa: PLC0415
        accs = admin_init.detect_qq_accounts()
        if accs:
            qq_info["found"] = True
            for a in accs:
                qq_info["accounts"].append({
                    "uin": a["uin"],
                    "size_mb": round(a["size"] / 1048576, 1),
                    "mtime": common.fmt(a["mtime"]),
                    "live": a["live"],
                    "db_path": str(a["db"]),
                })
            if not qq_info["current_uin"] and accs:
                qq_info["current_uin"] = accs[0]["uin"]
                qq_info["db_path"] = str(accs[0]["db"])
    except Exception as exc:  # noqa: BLE001
        qq_info["detect_error"] = str(exc)

    if qq_info["current_uin"] and qq_info["has_key"]:
        qq_info["status_text"] = "已就绪"
    elif qq_info["current_uin"]:
        qq_info["status_text"] = "待获取密钥"
    else:
        qq_info["status_text"] = "未检测到账号"

    # 3. 微信检测
    wx_info: dict[str, Any] = {
        "installed": False,
        "storage": "",
        "running": False,
        "has_key": bool(env.get("WX_DBKEY")),
        "key_masked": common.mask(env.get("WX_DBKEY", "")),
        "key_val": env.get("WX_DBKEY", ""),
        "status_text": "未配置",
    }
    try:
        import wxkey  # noqa: PLC0415
        diag = wxkey.diagnose(env)
        wx_info["installed"] = diag.get("wechat_installed", False)
        wx_info["storage"] = diag.get("storage", "")
        wx_info["running"] = diag.get("running", False)
        wx_info["has_key"] = diag.get("key_in_env", False)
        wx_info["key_state"] = diag.get("key_state", "")
        if wx_info["has_key"]:
            wx_info["status_text"] = "已就绪"
        elif wx_info["storage"]:
            wx_info["status_text"] = "待提取密钥"
        else:
            wx_info["status_text"] = "未检测到数据目录"
    except Exception as exc:  # noqa: BLE001
        wx_info["detect_error"] = str(exc)

    # 4. 企微检测（核查真实性，杜绝无条件假就绪）
    has_webhook = bool(env.get("WECOM_WEBHOOK_URL"))
    wb_exe = common.find_workbuddy_exe()
    if has_webhook:
        wecom_status = "已配置群机器人 Webhook"
        wecom_badge = "✔ Webhook已配置"
        wecom_ready = True
    elif wb_exe:
        wecom_status = f"已检测到 WorkBuddy ({wb_exe.name})，由其连接器推送"
        wecom_badge = "✔ WorkBuddy已安装"
        wecom_ready = True
    else:
        wecom_status = "未配置 Webhook 且未检测到 WorkBuddy (可选)"
        wecom_badge = "未配置 (可选)"
        wecom_ready = False

    wecom_info: dict[str, Any] = {
        "has_webhook": has_webhook,
        "connector_mode": bool(wb_exe),
        "status_text": wecom_status,
        "badge_text": wecom_badge,
        "is_ready": wecom_ready,
    }

    # 5. 高级通道检测 (飞书/钉钉)
    feishu_info: dict[str, Any] = {
        "has_app_id": bool(env.get("FEISHU_APP_ID")),
        "status_text": "已配置" if env.get("FEISHU_APP_ID") else "未配置 (可选)",
    }
    dingtalk_info: dict[str, Any] = {
        "has_client_id": bool(env.get("DINGTALK_CLIENT_ID")),
        "status_text": "已配置" if env.get("DINGTALK_CLIENT_ID") else "未配置 (可选)",
    }

    # 6. 激活模式与启用的平台
    active_cfg = {}
    try:
        cfg_file = common.CONFIG / "active_platforms.json"
        if cfg_file.exists():
            with open(cfg_file, "r", encoding="utf-8") as f:
                active_cfg = json.load(f)
    except Exception:
        pass

    return {
        "ok": True,
        "system": {
            "python_version": py_ver,
            "os": os_name,
            "is_admin": admin_ok,
            "wheels_ready": (ROOT_DIR / "vendor" / "wheels").exists(),
        },
        "channels": {
            "qq": qq_info,
            "wechat": wx_info,
            "wecom": wecom_info,
            "feishu": feishu_info,
            "dingtalk": dingtalk_info,
        },
        "active_config": active_cfg,
    }


def generate_demo_material() -> dict[str, Any]:
    """生成真实的脱敏示例待办素材与提炼清单效果预览。"""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sample_items = [
        {
            "id": "demo_item_01",
            "source": "qq",
            "channel_title": "QQ · 校行政办公综合群",
            "sender": "校办王主任",
            "timestamp": "今天 09:15",
            "raw_text": "【重要通知】请各学院及职能处室于本周五17:00前提交本学期工作总结及下周重点日程安排，电子版请直接发校办邮箱汇总，以便编印工作周报。",
            "todo": {
                "title": "提交本学期工作总结及下周工作日程安排",
                "deadline": "本周五 17:00",
                "priority": "高",
                "assignee": "各学院及各处室负责人",
                "source_ref": "校办王主任 @ 校行政办公综合群 (09:15)",
                "action": "撰写总结发校办邮箱",
            },
        },
        {
            "id": "demo_item_02",
            "source": "wechat",
            "channel_title": "微信 · 科研骨干与项目申报群",
            "sender": "科研处李处长",
            "timestamp": "今天 10:28",
            "raw_text": "2026年度省级高等职业教育教学成果奖申报工作正式启动，申报截止时间为11月15日。申报模版与支撑材料规范已上传群文件，请有意向的团队周三前将初审表交至科研处302室。",
            "todo": {
                "title": "省教学成果奖申报团队初审表提交与组织",
                "deadline": "本周三前 (总截止 11-15)",
                "priority": "高",
                "assignee": "申报团队骨干教师",
                "source_ref": "科研处李处长 @ 科研骨干与项目申报群 (10:28)",
                "action": "填报初审表并交科研处302",
            },
        },
        {
            "id": "demo_item_03",
            "source": "wecom",
            "channel_title": "企微 · 教务教学管理工作组",
            "sender": "教务处陈科长",
            "timestamp": "今天 11:40",
            "raw_text": "各位教学院长好，下周一上午9:00在行政楼三会议室召开教学质量月总结会，请各学院带齐期中试卷抽查与听课记录表备查。",
            "todo": {
                "title": "参加教学质量月总结会并携带听课备查表",
                "deadline": "下周一 09:00",
                "priority": "中",
                "assignee": "各二级学院教学院长",
                "source_ref": "教务处陈科长 @ 教务教学管理工作组 (11:40)",
                "action": "准备试卷抽查与听课表准时参会",
            },
        },
        {
            "id": "demo_item_04",
            "source": "qq",
            "channel_title": "QQ · 实训中心安全管理协同",
            "sender": "安全责任人张工",
            "timestamp": "今天 14:10",
            "raw_text": "各实训室管理员请注意：市特种设备年度复检定于本周四全天进行，请于明日下班前完成压力容器台账自查与消防应急通道清理。",
            "todo": {
                "title": "特种设备年度复检前台账自查与通道清理",
                "deadline": "明天下班前 (复检周四)",
                "priority": "高",
                "assignee": "各实训室管理员",
                "source_ref": "安全责任人张工 @ 实训中心安全管理协同 (14:10)",
                "action": "自查压力容器台账并清理通道",
            },
        },
    ]

    # 生成素材 JSON 结构
    material_payload = {
        "meta": {
            "generator": "待办自动汇总助手 · 示例生成引擎",
            "generated_at": now_str,
            "is_demo": True,
            "total_messages": len(sample_items),
        },
        "messages": [
            {
                "source": item["source"],
                "chat_id": f"chat_{item['id']}",
                "chat_name": item["channel_title"],
                "sender": item["sender"],
                "timestamp": item["timestamp"],
                "msg_id": f"msg_{item['id']}",
                "content": item["raw_text"],
            }
            for item in sample_items
        ],
    }

    # 生成提炼后的 Markdown 清单
    md_content = f"""# 📋 办公待办智能汇总清单 (示例演示)

> 提炼时间：{now_str}
> 数据范围：过去 4 小时多平台增量消息（演示模式）
> 安全校验：已完成来源核验，无臆造项

---

### 🚨 高优先级 / 今日及近期截止待办
1. **【校办】提交本学期工作总结及下周工作日程安排**
   - 截止时间：`本周五 17:00`
   - 责任对象：各学院及各处室负责人
   - 消息来源：`QQ` · 校行政办公综合群 · 校办王主任 (今天 09:15)
   - 行动建议：撰写电子版总结直接发校办邮箱。

2. **【科研】省教学成果奖申报团队初审表提交与组织**
   - 截止时间：`本周三前`（系统申报截止 11月15日）
   - 责任对象：申报团队骨干教师
   - 消息来源：`个人微信` · 科研骨干与项目申报群 · 科研处李处长 (今天 10:28)
   - 行动建议：下载群文件模板，初审表交科研处302室。

3. **【安全】特种设备年度复检前台账自查与通道清理**
   - 截止时间：`明天下班前`（复检本周四全天）
   - 责任对象：各实训室管理员
   - 消息来源：`QQ` · 实训中心安全管理协同 · 张工 (今天 14:10)
   - 行动建议：排查压力容器台账、清空消防通道。

---

### 📌 中优先级 / 常规推进待办
4. **【教务】参加教学质量月总结会并携带听课备查表**
   - 会议时间：`下周一 09:00` (行政楼三会议室)
   - 责任对象：各二级学院教学院长
   - 消息来源：`企业微信` · 教务教学管理工作组 · 陈科长 (今天 11:40)

---
*注：本数据为向导初始化演示样例，真实运行时将根据您已启用的通道自动生成真实待办。*
"""

    try:
        common.atomic_write_text(SAMPLE_MATERIAL_FILE, json.dumps(material_payload, ensure_ascii=False, indent=2))
        common.atomic_write_text(SAMPLE_PREVIEW_FILE, md_content)
    except Exception as exc:  # noqa: BLE001
        common.log(f"[Wizard] 写入示例素材文件异常: {exc}")

    return {
        "ok": True,
        "sample_count": len(sample_items),
        "items": sample_items,
        "markdown_preview": md_content,
        "sample_file": str(SAMPLE_MATERIAL_FILE),
        "preview_file": str(SAMPLE_PREVIEW_FILE),
    }


def reset_wizard() -> dict[str, Any]:
    """重置向导状态，允许重新进入。"""
    return save_wizard_state({
        "completed": False,
        "current_step": 1,
        "skipped": False,
        "notes": "用户主动重置向导",
    })


def complete_wizard() -> dict[str, Any]:
    """标记向导已顺利完成。"""
    return save_wizard_state({
        "completed": True,
        "current_step": 6,
        "skipped": False,
        "notes": "向导已顺利完成",
    })


def get_latest_artifacts() -> dict[str, Any]:
    """读取 work/ 下最新的待办素材与 output/ 下最新的待办清单与文件列表。"""
    mat_file = common.WORK / "待办素材.json"
    is_sample = False
    if not mat_file.exists():
        mat_file = common.WORK / "待办素材.sample.json"
        is_sample = True

    material_data: dict[str, Any] = {
        "exists": False,
        "is_sample": False,
        "message_count": 0,
        "messages": [],
        "updated_at": "",
    }
    if mat_file.exists():
        try:
            with open(mat_file, "r", encoding="utf-8") as f:
                content = json.load(f)
                msgs = content.get("messages", [])
                material_data = {
                    "exists": True,
                    "is_sample": is_sample,
                    "filename": mat_file.name,
                    "updated_at": datetime.datetime.fromtimestamp(mat_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "message_count": len(msgs),
                    "messages": msgs[:100],
                }
        except Exception as exc:  # noqa: BLE001
            material_data["error"] = str(exc)

    todo_files = list(common.OUT.glob("todo-*.md"))
    todo_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_todo: dict[str, Any] = {
        "exists": False,
        "filename": "",
        "content": "",
        "mtime": "",
    }
    if todo_files:
        p = todo_files[0]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            latest_todo = {
                "exists": True,
                "filename": p.name,
                "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "content": text,
            }
        except Exception as exc:  # noqa: BLE001
            latest_todo["error"] = str(exc)

    out_files: list[dict[str, Any]] = []
    if common.OUT.exists():
        for p in sorted(common.OUT.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file():
                st = p.stat()
                ftype = "todo" if p.name.startswith("todo-") else ("error" if "异常" in p.name else "other")
                out_files.append({
                    "name": p.name,
                    "size_kb": round(st.st_size / 1024, 1),
                    "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "type": ftype,
                })

    # 历史留存：当"本轮窗口"恰好没有消息时（data_state=no_new），归档页不该是一片空白，
    # 否则用户无法区分"链路坏了"和"这段时间确实没聊待办"。
    history: list[dict] = []
    try:
        import channel_collector  # noqa: PLC0415

        history = channel_collector.read_history(60)
    except Exception:  # noqa: BLE001
        history = []

    # 把"本轮为什么是 0 条"所需的证据一起给前端：游标、窗口、各通道 raw/count、data_state
    collect_state: dict[str, Any] = {}
    mat_meta: dict[str, Any] = {}
    try:
        if mat_file.exists():
            mat_meta = json.loads(mat_file.read_text(encoding="utf-8")).get("meta") or {}
    except (OSError, ValueError):
        mat_meta = {}
    try:
        cursor = common.read_cursor()
    except Exception:  # noqa: BLE001
        cursor = None
    try:
        since, until, why = common.window(None)
    except Exception:  # noqa: BLE001
        since = until = 0.0
        why = ""
    collect_state = {
        "data_state": mat_meta.get("data_state", ""),
        "status": mat_meta.get("status", ""),
        "time_range": mat_meta.get("time_range", ""),
        "cursor": common.fmt(cursor) if cursor else "",
        "window_since": common.fmt(since) if since else "",
        "window_until": common.fmt(until) if until else "",
        "window_reason": why,
        "channel_stats": {
            k: {
                "status": v.get("status"),
                "count": v.get("count"),
                "raw": v.get("raw"),
                "message": (v.get("message") or "")[:160],
            }
            for k, v in (mat_meta.get("channel_stats") or {}).items()
        },
    }

    return {
        "material": material_data,
        "latest_todo": latest_todo,
        "output_files": out_files[:30],
        "history": history,
        "collect_state": collect_state,
    }


def read_output_file(filename: str) -> dict[str, Any]:
    """读取 output/ 下的指定文件文本内容，防止路径穿越、符号链接逃逸与超大文件读取。"""
    raw_name = (filename or "").strip()
    if not raw_name or raw_name in (".", "..") or any(sep in raw_name for sep in ("/", "\\", "\x00")):
        return {"ok": False, "message": "非法文件名"}
    safe_name = Path(raw_name).name
    target = common.OUT / safe_name
    if target.is_symlink() or not target.exists() or not target.is_file():
        return {"ok": False, "message": "文件不存在或不允许读取"}
    try:
        out_root = common.OUT.resolve()
        resolved = target.resolve()
        try:
            in_out = resolved.is_relative_to(out_root)
        except AttributeError:
            in_out = str(resolved).startswith(str(out_root))
        if not in_out:
            return {"ok": False, "message": "拒绝访问目录外文件"}
        if resolved.stat().st_size > 2 * 1024 * 1024:
            return {"ok": False, "message": "文件超过 2MB 预览上限"}
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "filename": safe_name, "content": content}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
