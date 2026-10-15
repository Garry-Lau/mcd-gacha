"""稀有度分布实测脚本 —— 用来给 README / docstring 里的数字提供依据。

跑法::

    python tools/measure_rarity.py

两组口径
--------
1. **随机勾选**：从 41 个单品里随机抽 2~5 件。这是「乱点」的基线，
   大部分场次跟官方套餐无关，因此 SSR 应当稀少——否则稀有度就失去意义了。
2. **对齐勾选**：从某张官方套餐的组成里抽 2~N 件。这是「想凑套餐」的真实用法，
   SSR 应当高频出现。用户点名的场景（汉堡 + 可乐，套餐是汉堡+可乐+薯条）属于这一类。
"""
from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import coupon as coupon_mod  # noqa: E402
from services import nutrition  # noqa: E402
from services.health import MockHealthProvider  # noqa: E402
from services.profile import UserProfile, build_target  # noqa: E402
from services.recommender import recommend  # noqa: E402

RARITIES = ("SSR", "SR", "R", "N")

PROFILES = [
    UserProfile(),                                                  # 175/70/26 男 轻度
    UserProfile(gender="female", age=32, height_cm=163, weight_kg=54),
    UserProfile(gender="male", age=21, height_cm=183, weight_kg=88, activity="moderate"),
    UserProfile(gender="female", age=45, height_cm=158, weight_kg=68, activity="sedentary"),
]

SEED = 20261014
ROUNDS = 120


def _measure(label: str, picker) -> dict:
    """picker(rng) -> list[FoodItem]；返回该口径下的统计。"""
    catalog = nutrition.load_catalog()
    set_meals = nutrition.load_set_meals(catalog=catalog)
    coupons = coupon_mod.load_coupons()
    burns = MockHealthProvider().get_daily_burn(7)

    rng = random.Random(SEED)
    top_cards: Counter[str] = Counter()
    sessions = 0
    with_ssr = 0
    top_is_ssr = 0

    for prof in PROFILES:
        target = build_target(prof, burns).meal_target
        for _ in range(ROUNDS):
            pick = picker(rng, catalog, set_meals)
            if not pick or len(pick) < 2:
                continue
            try:
                res = recommend(pick, target, coupons, set_meals, limit=5)
            except Exception:
                continue
            if not res:
                continue
            sessions += 1
            for c in res:
                top_cards[c.rarity] += 1
            if any(c.rarity == "SSR" for c in res):
                with_ssr += 1
            if res[0].rarity == "SSR":
                top_is_ssr += 1

    total = sum(top_cards.values()) or 1
    print(f"\n=== {label} ===")
    print(f"有效场次 {sessions} / {len(PROFILES) * ROUNDS}")
    print("前 5 张结果卡稀有度分布：")
    for r in RARITIES:
        print(f"  {r:4} {top_cards[r]:5}  {top_cards[r] / total * 100:5.1f}%")
    rate = with_ssr / sessions * 100 if sessions else 0.0
    top_rate = top_is_ssr / sessions * 100 if sessions else 0.0
    print(f"至少抽到 1 张 SSR 的场次：{with_ssr} / {sessions} = {rate:.1f}%")
    print(f"TOP1 即 SSR 的场次：      {top_is_ssr} / {sessions} = {top_rate:.1f}%")
    return {"sessions": sessions, "cards": dict(top_cards), "session_ssr_pct": rate}


def _random_pick(rng, catalog, _set_meals):
    return rng.sample(catalog, rng.randint(2, 5))


def _aligned_pick(rng, catalog, set_meals):
    """从某张官方套餐里抽 2~size-1 件（留下缺件，正是"差一件达成"的场景）。"""
    by_id = {i.id: i for i in catalog}
    sm = rng.choice(set_meals)
    comps = [by_id[c.food_id] for c in sm.components if c.food_id]
    if len(comps) < 3:
        return []
    return rng.sample(comps, rng.randint(2, len(comps) - 1))


def main() -> int:
    _measure("口径 1 · 随机勾选 2~5 件（乱点基线）", _random_pick)
    _measure("口径 2 · 从官方套餐组成里抽 2~N-1 件（凑套餐用法）", _aligned_pick)
    print("\n注：稀有度需要「稀缺性」才有意义，因此口径 1 的 SSR 比例本就应该很低；")
    print("    真正体现玩法的是口径 2 —— 想凑套餐时 SSR 高频出现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
