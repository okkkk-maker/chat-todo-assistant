"""检查 QQ 本地消息库的新鲜度（判断“电脑上的 QQ 是不是还活着”）。

为什么单独做：
    nt_msg.db 只在 QQ 运行、收到消息时才写，而且 NTQQ 用 WAL 模式，
    新消息可能先落在 -wal 里还没 checkpoint 进主库。只看主库 mtime 会误判
    “没有新消息”，所以必须同时看 -wal / -shm。

用法:
    python tools/freshness.py                  # 彩一份快照（读 .env 的 QQ_DB_PATH）
    python tools/freshness.py --db "E:\\...\\nt_msg.db"
    python tools/freshness.py --watch 60 --# 采样观察写入行为
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import extract_impl  # noqa: E402

RELATED = ["nt_msg.db", "nt_msg.db-wal", "nt_msg.db-shm", "nt_msg.db-first.material", "nt_msg.db-last.material"]


def snapshot(db_path: Path) -> dict[str, tuple[float, int] | None]:
    out: dict[str, tuple[float, int] | None] = {}
    for name in RELATED:
        p = db_path.parent / name
        out[name] = (p.stat().st_mtime, p.stat().st_size) if p.exists() else None
    return out


def report(db_path: Path) -> dict:
    info = extract_impl.freshness(db_path)
    now = time.time()
    common.log(f"{'=' * 62}")
    common.log(f"  库: {db_path}")
    common.log(f"  采样时间: {common.fmt(now)}")
    common.log("-" * 62)
    for name, val in snapshot(db_path).items():
        if val is None:
            common.log(f"  {name:<26} 不存在")
            continue
        mtime, size = val
        common.log(f"  {name:<26} {size:>10,} B   {common.fmt(mtime)}  ({common.human_age(now - mtime)})")
    common.log("-" * 62)
    icon = {0: "🟢", 1: "🟡"}.get(info["level"], "🔴")
    common.log(f"  {icon} {info['reason']}")
    if info["level"] == 1:
        common.log("     说明: 这段期间手机端新消息没有同步到电脑，清单可能偏空。")
        common.log("     处理: 双击登录电脑 QQ 即可。")
    return info


def watch(db_path: Path, seconds: int, step: int) -> None:
    common.log(f"\n采样 {seconds} 秒，间隔 {step} 秒。请让 QQ 保持登录并收发几条消息。")
    first = snapshot(db_path)
    changed: list[str] = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(step)
        cur = snapshot(db_path)
        for name in RELATED:
            a, b = first.get(name), cur.get(name)
            if a != b and b is not None:
                mark = f"{name}: {(a[1] if a else 0):,} -> {b[1]:,} B"
                if mark not in changed:
                    changed.append(mark)
                    common.log(f"  [{time.time() - t0:4.0f}s] 变化 {mark}")
    common.log("\n--- 采样结束 ---")
    if changed:
        common.log("检测到写入，说明运行态下本地库确实随消息更新:")
        for c in changed:
            common.log("  " + c)
    else:
        common.log("未检测到任何变化：这期间没有新消息，或 QQ 未登录。")


def main() -> int:
    ap = argparse.ArgumentParser(description="QQ 消息库新鲜度检查")
    ap.add_argument("--db", help="nt_msg.db 路径（默认读 .env）")
    ap.add_argument("--watch", type=int, default=0, help="采样秒数，0 表示只快照一次")
    ap.add_argument("--step", type=int, default=5, help="采样间隔秒")
    args = ap.parse_args()

    db = Path(args.db) if args.db else Path(common.load_env().get("QQ_DB_PATH", "") or "")
    if not db or str(db) in ("", "."):
        common.log("没有配置 QQ_DB_PATH，先运行 Admin_Init.bat 或 --db 指定路径。")
        return 2
    if not db.exists():
        common.log(f"库文件不存在: {db}")
        return 3
    info = report(db)
    if args.watch:
        watch(db, args.watch, args.step)
    return 0 if info["level"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
