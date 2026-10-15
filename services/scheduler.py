"""定时任务：每天 08:00 拉取一次优惠券并落地到 ``data/cache/coupons.json``。

⚠️ Flask 开发服务器（Werkzeug）默认开启热重载，模块会被执行两次，
   导致 ``BackgroundScheduler`` **重复启动**（每天拉两次券、甚至报
   "maximum number of running instances reached"）。

   本项目用三重守卫：
   1. :func:`should_own_scheduler` —— 关热重载时主进程持有；开热重载时只有
      工作子进程（``WERKZEUG_RUN_MAIN == "true"``）持有；
   2. 模块级 ``_scheduler`` 单例 —— 重复调用 ``start_scheduler()`` 无副作用；
   3. ``app.py --no-reload`` —— 显式关掉热重载的逃生口。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from services import coupon as coupon_mod
from services import mcp_client

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
COUPON_CACHE = CACHE_DIR / "coupons.json"

#: 默认 cron：每天 08:00
DEFAULT_CRON_HOUR = 8
DEFAULT_CRON_MINUTE = 0

_scheduler = None


# --------------------------------------------------------------------------- #
# 业务动作
# --------------------------------------------------------------------------- #
def refresh_coupons() -> dict:
    """拉券并写入 ``data/cache/coupons.json``。返回写入的摘要。"""
    client = mcp_client.get_client()
    coupons = coupon_mod.load_coupons(client)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "refreshed_at": datetime.now().isoformat(timespec="seconds"),
        "mode": client.mode,
        "count": len(coupons),
        "coupons": [c.to_dict() for c in coupons],
    }
    with COUPON_CACHE.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def read_cached_coupons() -> dict | None:
    """读取缓存的券快照（不存在或损坏返回 None）。"""
    if not COUPON_CACHE.exists():
        return None
    try:
        with COUPON_CACHE.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------- #
# 调度器
# --------------------------------------------------------------------------- #
def should_own_scheduler(use_reloader: bool = False) -> bool:
    """守卫 1：判断**当前进程**是否应该持有调度器。

    - 关闭热重载（``--no-reload``）→ 只有一个进程，直接持有；
    - 开启热重载 → 父进程只负责拉起子进程，真正的服务在子进程里跑
      （``WERKZEUG_RUN_MAIN == "true"``），父进程必须放弃。
    - 逃生口 ``MCD_SCHEDULER_ALWAYS=1`` 强制持有（部署到 gunicorn 等
      不经过 Werkzeug 的 WSGI 服务器时使用）。
    """
    if os.environ.get("MCD_SCHEDULER_ALWAYS") == "1":
        return True
    if not use_reloader:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def start_scheduler(
    hour: int = DEFAULT_CRON_HOUR,
    minute: int = DEFAULT_CRON_MINUTE,
    *,
    force: bool = False,
    use_reloader: bool = False,
):
    """启动每天 ``hour:minute`` 的拉券任务。重复调用返回既有实例。

    :param force: 跳过 :func:`should_own_scheduler` 守卫（测试/验收脚本用）。
    :param use_reloader: Flask 是否开启了热重载；由 ``app.py`` 传入。
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler                      # 守卫 2：单例
    if not force and not should_own_scheduler(use_reloader):
        return None

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:                          # pragma: no cover - 依赖缺失
        return None

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        refresh_coupons,
        "cron",
        hour=hour,
        minute=minute,
        id="refresh-coupons",
        replace_existing=True,
        max_instances=1,                       # 防止叠加
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


def is_running() -> bool:
    return _scheduler is not None and getattr(_scheduler, "running", False)


def next_run_time() -> str | None:
    if not is_running():
        return None
    job = _scheduler.get_job("refresh-coupons")
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def soon_every(seconds: int = 60) -> None:
    """调试用：把拉券任务改成每 ``seconds`` 秒跑一次（阶段 3 验收用）。"""
    if _scheduler is None:
        return
    _scheduler.reschedule_job(
        "refresh-coupons", trigger="interval", seconds=seconds
    )


def next_run_within(minutes: int = 1) -> str:
    """调试用：返回一个 ``minutes`` 分钟后执行的时间串（阶段 3 验收用）。"""
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
