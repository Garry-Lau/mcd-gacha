"""健康数据 Provider 单测。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from services.health import (
    MOCK_MAX_BURN,
    MOCK_MIN_BURN,
    MOCK_RETENTION_DAYS,
    AppleHealthProvider,
    GoogleFitProvider,
    MockHealthProvider,
    get_provider,
)


def test_mock_provider_generates_seven_days(tmp_path):
    path = tmp_path / "health.json"
    provider = MockHealthProvider(path=path, days=7)
    burns = provider.get_daily_burn(7)

    assert len(burns) == 7
    assert all(MOCK_MIN_BURN <= b.calories <= MOCK_MAX_BURN for b in burns)
    assert all(b.source == "mock" for b in burns)
    # 日期升序
    assert [b.date for b in burns] == sorted(b.date for b in burns)
    # 文件已落盘
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["_meta"]["provider"] == "mock"
    assert len(payload["records"]) == 7


def test_mock_provider_is_deterministic(tmp_path):
    """同一天多次运行结果稳定（种子 = 日期 + 固定盐）。"""
    a = MockHealthProvider(path=tmp_path / "a.json", days=7).get_daily_burn(7)
    b = MockHealthProvider(path=tmp_path / "b.json", days=7).get_daily_burn(7)
    assert [x.calories for x in a] == [x.calories for x in b]


def test_mock_provider_reuses_existing_records(tmp_path):
    """已有记录不被覆盖：今天已有数据时应原样返回，不重新生成。

    注意：日期必须用 ``date.today()`` **动态**生成。写死日期会让本用例在
    跨天后失效——"今天" 变成昨天，``ensure_data`` 会合理地补一条新记录，
    ``records[-1]`` 就不再是断言的那个值。
    """
    path = tmp_path / "health.json"
    today = date.today().isoformat()
    path.write_text(
        json.dumps({"records": [{"date": today, "calories": 2222, "source": "mock"}]}),
        encoding="utf-8",
    )
    provider = MockHealthProvider(path=path, days=1)
    burns = provider.get_daily_burn(1)
    assert len(burns) == 1
    assert burns[0].calories == 2222.0


def test_mock_provider_backfills_gaps_without_overwriting(tmp_path):
    """补齐缺失的日期，但已存在日期的数值必须原样保留。"""
    path = tmp_path / "health.json"
    keep = date.today() - timedelta(days=2)
    path.write_text(
        json.dumps(
            {"records": [{"date": keep.isoformat(), "calories": 2222, "source": "mock"}]}
        ),
        encoding="utf-8",
    )

    burns = MockHealthProvider(path=path, days=3).get_daily_burn(3)

    assert len(burns) == 3
    assert burns[0].date == keep.isoformat()
    assert burns[0].calories == 2222.0                                   # 老记录未被动过
    assert [b.date for b in burns] == sorted(b.date for b in burns)      # 仍按日期升序
    assert len({b.date for b in burns}) == 3                             # 无重复日期


def test_mock_provider_prunes_records_outside_retention_window(tmp_path):
    """超出保留窗口的老记录被丢弃，避免文件随时间无限增长。"""
    path = tmp_path / "health.json"
    ancient = (date.today() - timedelta(days=MOCK_RETENTION_DAYS + 5)).isoformat()
    path.write_text(
        json.dumps({"records": [{"date": ancient, "calories": 1.0, "source": "mock"}]}),
        encoding="utf-8",
    )

    MockHealthProvider(path=path, days=3).get_daily_burn(3)

    payload = json.loads(path.read_text(encoding="utf-8"))
    dates = [r["date"] for r in payload["records"]]
    assert ancient not in dates, "过期记录应被淘汰"
    assert len(dates) == 3, "窗口内只有近 3 天"
    assert payload["_meta"]["retention_days"] == MOCK_RETENTION_DAYS


def test_mock_provider_average_burn(tmp_path):
    provider = MockHealthProvider(path=tmp_path / "h.json", days=7)
    avg = provider.average_burn(7)
    assert MOCK_MIN_BURN <= avg <= MOCK_MAX_BURN


def test_mock_provider_handles_corrupt_file(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{ not json", encoding="utf-8")
    burns = MockHealthProvider(path=path, days=3).get_daily_burn(3)
    assert len(burns) == 3   # 损坏文件 → 视为空，重新生成


def test_real_providers_are_explicitly_not_implemented():
    with pytest.raises(NotImplementedError):
        AppleHealthProvider().get_daily_burn(7)
    with pytest.raises(NotImplementedError):
        GoogleFitProvider().get_daily_burn(7)


@pytest.mark.parametrize("name", ["mock", "apple", "apple_health", "google", "google_fit", "unknown-xyz"])
def test_get_provider_factory(name, tmp_path):
    provider = get_provider(name, path=tmp_path / "h.json", days=7)
    assert provider.name in {"mock", "apple_health", "google_fit"}


def test_get_provider_unknown_falls_back_to_mock(tmp_path):
    provider = get_provider("nope", path=tmp_path / "h.json", days=7)
    assert isinstance(provider, MockHealthProvider)


def test_daily_burn_to_dict(tmp_path):
    burn = MockHealthProvider(path=tmp_path / "h.json", days=1).get_daily_burn(1)[0]
    d = burn.to_dict()
    assert set(d) == {"date", "calories", "steps", "active_minutes", "source"}
