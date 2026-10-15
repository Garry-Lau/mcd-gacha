"""智能手表热量消耗 —— 可插拔 Provider。

现实约束：智能手表（Apple Health / Google Fit / 华为运动健康…）都没有公开的
稳定接口可供本 demo 直接读取。因此本模块把它抽象为 :class:`HealthProvider`，
默认使用 :class:`MockHealthProvider`：

- 读 ``data/health.json``；
- 文件不存在时**生成过去 7 天模拟数据并落盘**（日均 1800–2600 kcal）；
- 生成用「日期 + 固定盐」做随机种子 → 同一天多次运行结果**稳定可复现**。

UI 上必须明确标注「演示数据」（见 app.py / templates）。
真实接入只需要实现一个 :class:`HealthProvider` 子类，其余代码零改动。
"""
from __future__ import annotations

import json
import random
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEALTH_PATH = ROOT / "data" / "health.json"

#: 模拟数据的日均消耗区间（kcal）
MOCK_MIN_BURN = 1800
MOCK_MAX_BURN = 2600
#: 落在「合理」区间的步数与活跃分钟
MOCK_STEP_RANGE = (6000, 14000)
MOCK_ACTIVE_RANGE = (25, 90)
#: 落盘文件的保留窗口。UI 只要最近 7 天，这里留一个季度余量；
#: 超出的老记录会被丢弃，避免 ``data/health.json`` 随时间无限增长。
MOCK_RETENTION_DAYS = 90


@dataclass
class DailyBurn:
    """某一天的热量消耗。"""

    date: str            # ISO 日期，如 "2026-10-14"
    calories: float      # 消耗 kcal
    steps: int = 0       # 步数（可选，用于 UI 展示）
    active_minutes: int = 0
    source: str = "mock"  # mock / apple_health / google_fit ...

    def to_dict(self) -> dict:
        return asdict(self)


class HealthProvider(ABC):
    """健康数据提供方抽象基类。

    实现一个子类即可接入真实手表：
        class MyWatchProvider(HealthProvider):
            name = "my_watch"
            def get_daily_burn(self, days=7): ...
    """

    name: str = "base"

    @abstractmethod
    def get_daily_burn(self, days: int = 7) -> list[DailyBurn]:
        """返回最近 ``days`` 天（含今天）的每日消耗，按日期**升序**。"""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Mock
# --------------------------------------------------------------------------- #
def _mock_daily(d: date) -> dict:
    rng = random.Random(f"{d.isoformat()}::mcd-gacha-demo")
    calories = rng.randint(MOCK_MIN_BURN, MOCK_MAX_BURN)
    steps = rng.randint(*MOCK_STEP_RANGE)
    active = rng.randint(*MOCK_ACTIVE_RANGE)
    return {
        "date": d.isoformat(),
        "calories": float(calories),
        "steps": steps,
        "active_minutes": active,
        "source": "mock",
    }


class MockHealthProvider(HealthProvider):
    """演示用 Provider。读 ``data/health.json``，缺失时自动生成并落盘。"""

    name = "mock"

    def __init__(self, path: Path | str | None = None, days: int = 7) -> None:
        self.path = Path(path or DEFAULT_HEALTH_PATH)
        self.days = days
        self.ensure_data()

    # -- 数据准备 ---------------------------------------------------------- #
    def ensure_data(self) -> None:
        """确保文件覆盖最近 ``days`` 天。

        规则：**只补缺、只淘汰过期**，已有日期的数值绝不改写。
        淘汰窗口取 ``max(days, MOCK_RETENTION_DAYS)``，保证 UI 想读多少天都够。
        """
        payload = self._read_payload()
        records = [r for r in (payload.get("records") or []) if isinstance(r, dict)]
        existing = {r.get("date") for r in records}

        today = date.today()
        window = max(self.days, MOCK_RETENTION_DAYS)
        changed = False

        # 1) 补齐窗口内缺失的日期
        for i in range(self.days - 1, -1, -1):
            d = today - timedelta(days=i)
            if d.isoformat() not in existing:
                records.append(_mock_daily(d))
                changed = True

        # 2) 丢弃超出保留窗口的老记录（仅丢弃，不覆盖）
        cutoff = (today - timedelta(days=window - 1)).isoformat()
        kept = [r for r in records if str(r.get("date") or "") >= cutoff]
        if len(kept) != len(records):
            records = kept
            changed = True

        # 3) 元信息与当前配置不一致时也重写一次，让文件自我修复
        meta = payload.get("_meta") or {}
        if meta.get("retention_days") != window or meta.get("generated_days") != self.days:
            changed = True

        if changed or not self.path.exists():
            records.sort(key=lambda r: r.get("date", ""))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "_meta": {
                            "provider": "mock",
                            "note": "演示数据（非真实手表数据）。真实接入见 services/health.py。",
                            "generated_days": self.days,
                            "retention_days": window,
                        },
                        "records": records,
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )

    def _read_payload(self) -> dict:
        """读原始 JSON。损坏 / 缺失 / 形状不对时一律返回空 dict。"""
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):        # 容忍裸数组格式
            return {"records": payload}
        return {}

    def _read_raw(self) -> list[dict]:
        records = self._read_payload().get("records")
        return records if isinstance(records, list) else []

    # -- 读取 -------------------------------------------------------------- #
    def get_daily_burn(self, days: int = 7) -> list[DailyBurn]:
        self.days = days
        self.ensure_data()
        records = [r for r in self._read_raw() if isinstance(r, dict) and r.get("date")]
        records.sort(key=lambda r: r["date"])
        records = records[-days:]
        return [
            DailyBurn(
                date=str(r.get("date")),
                calories=float(r.get("calories") or 0),
                steps=int(r.get("steps") or 0),
                active_minutes=int(r.get("active_minutes") or 0),
                source=str(r.get("source") or "mock"),
            )
            for r in records
        ]

    def average_burn(self, days: int = 7) -> float:
        data = self.get_daily_burn(days)
        if not data:
            return 0.0
        return sum(d.calories for d in data) / len(data)


# --------------------------------------------------------------------------- #
# 真实接入桩（保留接口，触发清晰的 NotImplementedError）
# --------------------------------------------------------------------------- #
class AppleHealthProvider(HealthProvider):
    """Apple Health 接入桩 —— 未实现。

    接入思路（iOS 侧）：
    1. 在 iOS App 里用 HealthKit 读取 ``HKQuantityTypeIdentifierActiveEnergyBurned``
       （或 ``BasalEnergyBurned`` + ``ActiveEnergyBurned`` 求和）；
    2. 通过自建后端把每日汇总 POST 到本服务；
    3. 本 Provider 改成读取该后端接口。

    参考：https://developer.apple.com/documentation/healthkit
    """

    name = "apple_health"

    def get_daily_burn(self, days: int = 7) -> list[DailyBurn]:  # pragma: no cover
        raise NotImplementedError(
            "AppleHealthProvider 尚未实现。需要先打通 HealthKit 导出链路，"
            "详见 README.md『真实手表接入』一节。"
        )


class GoogleFitProvider(HealthProvider):
    """Google Fit 接入桩 —— 未实现。

    接入思路：
    1. 在 Google Cloud Console 启用 Fitness API，配置 OAuth 2.0 凭据；
    2. 用 ``users.dataset.aggregate`` 聚合
       ``com.google.calories.expended`` 的日汇总；
    3. 把 daily total 喂给本 Provider。

    参考：https://developers.google.com/fit/rest
    """

    name = "google_fit"

    def get_daily_burn(self, days: int = 7) -> list[DailyBurn]:  # pragma: no cover
        raise NotImplementedError(
            "GoogleFitProvider 尚未实现。需要先完成 OAuth 授权与 Fitness API 聚合，"
            "详见 README.md『真实手表接入』一节。"
        )


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[HealthProvider]] = {
    "mock": MockHealthProvider,
    "apple": AppleHealthProvider,
    "apple_health": AppleHealthProvider,
    "google": GoogleFitProvider,
    "google_fit": GoogleFitProvider,
}


def get_provider(name: str = "mock", **kwargs) -> HealthProvider:
    """按名字取 Provider。未知名字回落到 Mock，保证 demo 永不崩。

    只有 :class:`MockHealthProvider` 接受 ``path`` / ``days`` 参数；真实接入桩
    不吃参数，因此分开构造。
    """
    cls = _REGISTRY.get((name or "mock").strip().lower(), MockHealthProvider)
    if cls is MockHealthProvider:
        return MockHealthProvider(**kwargs)
    return cls()
