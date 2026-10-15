"""阶段 3 验收脚本：定时任务是否真的落盘 ``data/cache/coupons.json``。

用法::

    python tools/accept_stage3.py            # 每 5 秒跑一次，观察 3 轮
    python tools/accept_stage3.py --sec 60   # 模拟「cron 改到 1 分钟后」

判断标准
--------
1. ``refresh_coupons()`` 首次调用即产出缓存文件；
2. 调度器按 interval 触发后，文件 mtime / refreshed_at 发生变化；
3. 一并打印近 7 天日均消耗，验证健康数据链路。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import mcp_client  # noqa: E402
from services import scheduler as sch  # noqa: E402
from services.health import MockHealthProvider  # noqa: E402


def _cache_state() -> tuple[bool, str | None, float | None]:
    if not sch.COUPON_CACHE.exists():
        return False, None, None
    payload = json.loads(sch.COUPON_CACHE.read_text(encoding="utf-8"))
    return True, payload.get("refreshed_at"), sch.COUPON_CACHE.stat().st_mtime


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=int, default=5, help="间隔秒数（默认 5）")
    ap.add_argument("--rounds", type=int, default=3, help="观察轮数（默认 3）")
    args = ap.parse_args()

    mcp_client.get_client(demo=True, reset=True)
    print(f"[验收] MCP 模式 = {mcp_client.get_client().mode}")

    # ---- 1. 单次刷新 ------------------------------------------------------ #
    print("\n[1/3] 手动调用 refresh_coupons() ...")
    payload = sch.refresh_coupons()
    exists, stamp, mtime = _cache_state()
    print(f"      文件存在 = {exists}   券数 = {payload['count']}   refreshed_at = {stamp}")
    if not exists:
        print("      ❌ 缓存未落盘")
        return 1

    # ---- 2. 调度器 interval 触发 ------------------------------------------ #
    print(f"\n[2/3] 启动调度器并改到每 {args.sec} 秒触发（模拟 cron 调到 1 分钟内）")
    sch.start_scheduler(force=True)
    sch.soon_every(args.sec)
    print(f"      调度器运行中 = {sch.is_running()}   下次执行 = {sch.next_run_time()}")

    prev_mtime = mtime
    hits = 0
    for i in range(args.rounds):
        time.sleep(args.sec + 0.8)
        _, stamp, mtime = _cache_state()
        changed = mtime is not None and prev_mtime is not None and mtime > prev_mtime
        hits += 1 if changed else 0
        print(f"      第 {i + 1} 轮：refreshed_at = {stamp}   "
              f"文件被重写 = {'✅ 是' if changed else '— 否'}")
        prev_mtime = mtime
    sch.stop_scheduler()

    # ---- 3. 健康数据 ------------------------------------------------------ #
    print("\n[3/3] 健康数据（Mock 手表）")
    provider = MockHealthProvider()
    burns = provider.get_daily_burn(7)
    avg = provider.average_burn(7)
    for b in burns:
        print(f"      {b.date}  {b.calories:7.1f} kcal   {b.steps:>6} 步   "
              f"{b.active_minutes:>3} 分钟活动   [{b.source}]")
    print(f"      近 7 天日均消耗 = {avg:.1f} kcal  （演示数据，非真实手表）")

    ok = hits > 0
    print(f"\n[结论] 定时任务触发并落盘 = {'✅ 通过' if ok else '❌ 失败'}"
          f"（{hits}/{args.rounds} 轮检测到文件重写）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
