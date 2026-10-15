"""契合度算法单测。

覆盖需求点名项：热量完美匹配得高分、低于 50 被过滤、排序正确、稀有度边界值；
外加本次新增的「套餐匹配度」「原价 vs 组合技价」「补齐建议」。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.coupon import Coupon
from services.nutrition import FoodItem, SetMeal, SetMealComponent
from services.recommender import (
    MAX_INPUT_ITEMS,
    MIN_INPUT_ITEMS,
    SCORE_FLOOR,
    SETMEAL_BONUS,
    WEIGHTS,
    RecommendError,
    clamp,
    completeness,
    discount_coverage,
    enumerate_subsets,
    fat_score,
    heat_score,
    match_setmeal,
    money,
    protein_score,
    rarity_of,
    recommend,
    score_subset,
    suggest_upgrades,
)


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
def make_item(
    code: str,
    name: str,
    category: str,
    calories: float,
    protein: float,
    fat: float,
    carb: float,
    price: str = "10",
) -> FoodItem:
    return FoodItem(
        id=code,
        name=name,
        category=category,
        price=Decimal(price),
        calories=calories,
        protein=protein,
        fat=fat,
        carb=carb,
    )


def balanced_items() -> list[FoodItem]:
    """宏量营养较均衡的一组单品，用于「能出结果」的正向用例。"""
    return [
        make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
        make_item("M2", "板烧鸡腿堡", "主食", 391, 23, 17, 35, "23.5"),
        make_item("P1", "麦乐鸡", "蛋白", 213, 12, 12, 13, "11.9"),
        make_item("V1", "苹果片", "蔬果", 32, 0, 0, 7, "7"),
        make_item("V2", "玉米杯", "蔬果", 87, 4, 1, 12, "13"),
        make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5"),
    ]


def fake_setmeal(
    price: str = "36.5",
    original: str = "49",
    components: tuple[SetMealComponent, ...] | None = None,
) -> SetMeal:
    """模拟「巨无霸三件套」：汉堡 + 薯条 + 可乐，原价 49 套餐价 36.5。"""
    comps = components or (
        SetMealComponent(code="1100", name="巨无霸", key="巨无霸", food_id="M1"),
        SetMealComponent(code="4810", name="中薯条", key="薯条", food_id="F1"),
        SetMealComponent(code="3050", name="可乐中杯", key="可乐", food_id="D1"),
    )
    return SetMeal(
        id="9900005466",
        name="巨无霸三件套",
        price=Decimal(price),
        original_price=Decimal(original),
        components=comps,
    )


# --------------------------------------------------------------------------- #
# H：热量匹配
# --------------------------------------------------------------------------- #
def test_heat_perfect_match_is_one():
    assert heat_score(700, 700) == 1.0


def test_heat_decays_linearly_with_distance():
    assert heat_score(770, 700) == pytest.approx(0.9)
    assert heat_score(630, 700) == pytest.approx(0.9)
    assert heat_score(1400, 700) == pytest.approx(0.0)
    assert heat_score(0, 700) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# P / F：供能比曲线
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ratio, expected",
    [
        (0.20, 1.0), (0.27, 1.0), (0.35, 1.0),
        (0.10, 0.5), (0.00, 0.0),
        (0.525, 0.5), (0.70, 0.0), (1.00, 0.0),
    ],
)
def test_protein_score_curve(ratio, expected):
    calories = 1000.0
    assert protein_score(ratio * calories / 4.0, calories) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    "ratio, expected",
    [(0.00, 1.0), (0.35, 1.0), (0.525, 0.5), (0.70, 0.0), (1.00, 0.0)],
)
def test_fat_score_curve(ratio, expected):
    calories = 1000.0
    assert fat_score(ratio * calories / 9.0, calories) == pytest.approx(expected, abs=1e-6)


def test_zero_calorie_subset_scores_zero_for_ratios():
    assert protein_score(10, 0) == 0.0
    assert fat_score(10, 0) == 0.0


# --------------------------------------------------------------------------- #
# C / D
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cats, expected",
    [(["主食", "蛋白", "蔬果", "饮品"], 1.0), (["主食", "蛋白"], 0.5), (["主食"], 0.25)],
)
def test_completeness(cats, expected):
    items = [make_item(str(i), f"n{i}", c, 100, 5, 5, 10) for i, c in enumerate(cats)]
    score, covered = completeness(items)
    assert score == pytest.approx(expected)
    assert sorted(covered) == sorted(set(cats))


def test_discount_coverage_coupon_only_keeps_spec_semantics():
    """保留需求原文口径：被券覆盖的商品原价之和 / 总价。"""
    items = [
        make_item("A", "巨无霸", "主食", 500, 27, 26, 42, "20"),
        make_item("B", "薯条", "主食", 289, 4, 12, 38, "10"),
    ]
    c = Coupon(id="CP1", title="薯条券", code="CODE1", applicable_items=["B"])
    ratio, covered, ids = discount_coverage(items, [c])
    assert ratio == pytest.approx(10 / 30)
    assert covered == Decimal("10")
    assert ids == ["CP1"]


def test_coupon_without_code_does_not_count():
    items = [make_item("A", "圆筒", "主食", 93, 2, 3, 14, "5")]
    c = Coupon(id="X", title="无码券", code=None, applicable_items=["A"])
    assert discount_coverage(items, [c])[0] == 0.0


# --------------------------------------------------------------------------- #
# 稀有度边界（需求点名：SSR ≥ 90 / SR 75–89 / R 60–74 / N 50–59）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "score, expected",
    [
        (100.0, "SSR"), (90.0, "SSR"), (89.99, "SR"), (75.0, "SR"),
        (74.99, "R"), (60.0, "R"), (59.99, "N"), (50.0, "N"),
    ],
)
def test_rarity_boundaries(score, expected):
    assert rarity_of(score) == expected


def test_score_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_clamp_bounds():
    assert clamp(-1) == 0.0 and clamp(2) == 1.0 and clamp(0.4) == 0.4


def test_money_quantizes_to_two_decimals():
    assert money(Decimal("26.666666666")) == Decimal("26.67")
    assert money(Decimal("8.333333")) == Decimal("8.33")
    assert money(Decimal("12.5")) == Decimal("12.50")


# --------------------------------------------------------------------------- #
# 子集枚举
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n, expected", [(3, 7), (4, 15), (8, 255)])
def test_enumerate_subsets_count(n, expected):
    items = [make_item(str(i), f"n{i}", "主食", 100, 5, 5, 10) for i in range(n)]
    assert len(list(enumerate_subsets(items))) == expected


# --------------------------------------------------------------------------- #
# 套餐匹配度（本次新增核心）
# --------------------------------------------------------------------------- #
def test_setmeal_match_ratio_is_two_thirds():
    """用户场景：勾了 汉堡 + 可乐，套餐是 汉堡 + 可乐 + 薯条 → 66.7%。"""
    items = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
             make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5")]
    m = match_setmeal(items, [fake_setmeal()])
    assert m is not None
    assert m.set_meal.name == "巨无霸三件套"
    assert len(m.matched_components) == 2
    assert m.set_meal.size == 3
    assert m.ratio == pytest.approx(2 / 3)
    assert m.missing_names == ["中薯条"]


def test_setmeal_match_full_and_none():
    full = [
        make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
        make_item("F1", "薯条", "主食", 289, 4, 12, 38, "13.5"),
        make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5"),
    ]
    assert match_setmeal(full, [fake_setmeal()]).ratio == pytest.approx(1.0)

    unrelated = [make_item("X1", "培根安格斯厚牛堡", "主食", 707, 34, 44, 43, "34.5")]
    assert match_setmeal(unrelated, [fake_setmeal()]) is None
    assert match_setmeal(full, None) is None
    assert match_setmeal(full, []) is None


def test_setmeal_match_matches_by_normalized_name_when_code_differs():
    """详情用 code 3050 可乐中杯，卡池是另一编码的同名商品 → 靠归一化名命中。"""
    menu_cola = make_item("9900008751", "可乐", "饮品", 147, 0, 0, 36, "9.5")
    m = match_setmeal(
        [menu_cola],
        [fake_setmeal(components=(
            SetMealComponent(code="3050", name="可乐中杯", key="可乐", food_id="OTHER"),
        ))],
    )
    assert m is not None and m.ratio == pytest.approx(1.0)


def test_setmeal_bonus_component(monkeypatch):
    """加成 = M × SETMEAL_BONUS，且封顶 100。

    基线必须**带上套餐折价带来的 D**，否则会把 D 的增益算进加成里。
    """
    import services.recommender as rec

    items = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
             make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5")]
    sm = fake_setmeal()

    monkeypatch.setattr(rec, "SETMEAL_BONUS", 0.0)
    no_bonus = rec.score_subset(items, 660, [], [sm])
    assert no_bonus.breakdown["setmeal"] == pytest.approx(2 / 3)
    assert no_bonus.breakdown["discount"] > 0        # 套餐折价已经体现在 D 上

    monkeypatch.setattr(rec, "SETMEAL_BONUS", 40.0)
    with_bonus = rec.score_subset(items, 660, [], [sm])

    assert with_bonus.score == pytest.approx(
        min(no_bonus.score + (2 / 3) * 40.0, 100.0)
    )
    assert with_bonus.score > no_bonus.score
    assert with_bonus.setmeal_name == "巨无霸三件套"
    assert with_bonus.missing_items == ["中薯条"]
    assert with_bonus.setmeal_ratio_pct == pytest.approx(66.7, abs=0.1)


def test_setmeal_bonus_makes_ssr_reachable():
    """「凑满套餐」是 SSR 的主要路径 —— 这是让 demo 里 SSR 可达的关键机制。"""
    items = [
        make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
        make_item("F1", "薯条", "主食", 289, 4, 12, 38, "13.5"),
        make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5"),
    ]
    target = 949.0
    without = score_subset(items, target, [], None)
    with_sm = score_subset(items, target, [], [fake_setmeal()])

    assert without.rarity != "SSR"          # 没有套餐匹配时够不到 SSR
    assert with_sm.setmeal_ratio == pytest.approx(1.0)
    assert with_sm.rarity == "SSR"


def test_score_ceiling_without_any_discount_is_80():
    """无券、无套餐匹配时五维满分只有 80 → 最多 SR，SSR 必须靠优惠或套餐达成。"""
    items = [
        make_item("A", "巨无霸", "主食", 600, 45, 18, 60, "25"),
        make_item("B", "苹果片", "蔬果", 100, 2, 1, 20, "7"),
        make_item("C", "可乐", "饮品", 100, 0, 0, 25, "9.5"),
        make_item("D", "麦乐鸡", "蛋白", 100, 12, 6, 8, "11.9"),
    ]
    combo = score_subset(items, 900, [], None)
    assert combo.breakdown["discount"] == 0.0
    assert combo.breakdown["complete"] == pytest.approx(1.0)
    assert combo.score == pytest.approx(80.0)


# --------------------------------------------------------------------------- #
# 价格：原价 vs 组合技价
# --------------------------------------------------------------------------- #
def test_combo_price_uses_per_item_setmeal_discount():
    """件均折价，且 M=1 时收敛到套餐真实价。"""
    items = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
             make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5")]
    sm = fake_setmeal()                       # 49 -> 36.5，件均省 12.5/3
    combo = score_subset(items, 660, [], [sm])

    assert combo.total_price == Decimal("35.0")          # 原 2 张卡价格
    assert combo.setmeal_discount == Decimal("8.33")     # 4.1667 × 2
    assert combo.combo_price == Decimal("26.67")         # 35 - 8.33
    assert combo.saved == Decimal("8.33")

    # M = 1 时收敛到真实套餐价
    full = items + [make_item("F1", "薯条", "主食", 289, 4, 12, 38, "13.5")]
    full_combo = score_subset(full, 949, [], [sm])
    assert full_combo.combo_price == Decimal("36.00")
    assert full_combo.setmeal_discount == Decimal("12.50")


def test_per_item_discount_beats_naive_pro_rata():
    """只命中 1 件时不会被算出「三分之一个套餐只要三分之一个价」。"""
    only_burger = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5")]
    combo = score_subset(only_burger, 500, [], [fake_setmeal()])
    # 件均折价 4.17 => 25.5 - 4.17 = 21.33（而按售价折算会得到 12.17）
    assert combo.combo_price == Decimal("21.33")


def test_coupon_discount_does_not_stack_on_same_item():
    """同一件商品已被券免单时，不再叠加套餐折价（避免重复优惠）。"""
    items = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5")]
    coupon = Coupon(id="C1", title="巨无霸券", code="CODE", applicable_items=["M1"])
    combo = score_subset(items, 500, [coupon], [fake_setmeal()])
    assert combo.coupon_discount == Decimal("25.50")
    assert combo.setmeal_discount == Decimal("0.00")
    assert combo.combo_price == Decimal("0.00")
    assert combo.coupon_ids == ["C1"]


def test_recommend_uses_coupon_to_reduce_price():
    items = balanced_items()
    cheap = items[3]  # 苹果片 ¥7
    c = Coupon(id="C1", title="苹果片券", code="CODE", applicable_items=[cheap.id])
    results = recommend(items, target_calories=800, coupons=[c])
    with_coupon = [r for r in results if cheap.id in [i.id for i in r.items] and r.coupon_ids]
    assert with_coupon, "应存在使用了券的组合"
    assert all(r.combo_price < r.total_price for r in with_coupon)


# --------------------------------------------------------------------------- #
# 补齐建议
# --------------------------------------------------------------------------- #
def test_suggest_upgrades_completes_the_setmeal():
    selection = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
                 make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5")]
    fries = make_item("F1", "薯条", "主食", 289, 4, 12, 38, "13.5")
    catalog = {i.id: i for i in [*selection, fries]}

    ups = suggest_upgrades(selection, [fake_setmeal()], catalog, 846)
    assert len(ups) == 1
    up = ups[0]
    assert up.is_upgrade is True
    assert up.setmeal_ratio == pytest.approx(1.0)
    assert "薯条" in up.upgrade_note
    assert {i.id for i in up.items} == {"M1", "D1", "F1"}
    # 凑满后价格 = 真实套餐价，比单点省
    assert up.total_price == Decimal("48.5")
    assert up.combo_price == Decimal("36.00")
    assert up.saved == Decimal("12.50")


def test_suggest_upgrades_skips_when_selection_has_outsiders():
    """勾选里含套餐外的商品时不算「补齐」（否则夹带商品、语义不成立）。"""
    selection = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
                 make_item("V1", "苹果片", "蔬果", 32, 0, 0, 7, "7")]
    catalog = {i.id: i for i in selection}
    assert suggest_upgrades(selection, [fake_setmeal()], catalog, 846) == []


def test_suggest_upgrades_skips_when_missing_item_not_on_menu():
    """缺件在门店菜单里买不到 → 无法补齐。"""
    selection = [make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
                 make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5")]
    catalog = {i.id: i for i in selection}   # 没有薯条
    assert suggest_upgrades(selection, [fake_setmeal()], catalog, 846) == []


def test_suggest_upgrades_skips_already_full():
    full = [
        make_item("M1", "巨无霸", "主食", 513, 27, 26, 42, "25.5"),
        make_item("F1", "薯条", "主食", 289, 4, 12, 38, "13.5"),
        make_item("D1", "可乐", "饮品", 147, 0, 0, 36, "9.5"),
    ]
    catalog = {i.id: i for i in full}
    assert suggest_upgrades(full, [fake_setmeal()], catalog, 949) == []


# --------------------------------------------------------------------------- #
# 主入口：过滤 / 排序 / 输入校验
# --------------------------------------------------------------------------- #
def test_recommend_filters_below_50():
    heavy = [
        make_item("H1", "巨无霸", "主食", 2000, 10, 10, 100, "20"),
        make_item("H2", "大薯条", "主食", 2000, 10, 10, 100, "20"),
        make_item("H3", "大可乐", "饮品", 2000, 10, 10, 100, "20"),
    ]
    assert recommend(heavy, target_calories=600) == []


def test_recommend_results_sorted_desc_and_all_above_floor():
    results = recommend(balanced_items(), target_calories=800)
    assert results, "均衡卡池应该至少产出一个组合"
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert all(s >= SCORE_FLOOR for s in scores)
    assert all(r.rarity in {"SSR", "SR", "R", "N"} for r in results)


def test_recommend_perfect_heat_no_setmeal_scores_77_5():
    items = [
        make_item("A", "巨无霸", "主食", 600, 45, 18, 60, "25"),
        make_item("B", "苹果片", "蔬果", 100, 2, 1, 20, "7"),
        make_item("C", "可乐", "饮品", 100, 0, 0, 25, "9.5"),
    ]
    results = recommend(items, target_calories=800)
    top = results[0]
    assert top.total_calories == 800
    assert top.breakdown["heat"] == pytest.approx(1.0)
    assert top.breakdown["protein"] == pytest.approx(1.0)
    assert top.breakdown["fat"] == pytest.approx(1.0)
    # 无券无套餐：100 × (0.35 + 0.20 + 0.15 + 0 + 0.10×0.75) = 77.5
    assert top.score == pytest.approx(77.5)
    assert top.rarity == "SR"


def test_recommend_limit():
    results = recommend(balanced_items(), target_calories=800, limit=3)
    assert len(results) == 3


@pytest.mark.parametrize("n", [0, 1])
def test_recommend_rejects_too_few_items(n):
    with pytest.raises(RecommendError):
        recommend(balanced_items()[:n], target_calories=800)


def test_recommend_rejects_too_many_items():
    items = [
        make_item(str(i), f"n{i}", "主食", 200, 10, 8, 20, "10")
        for i in range(MAX_INPUT_ITEMS + 1)
    ]
    with pytest.raises(RecommendError):
        recommend(items, target_calories=800)


def test_recommend_rejects_non_positive_target():
    with pytest.raises(RecommendError):
        recommend(balanced_items(), target_calories=0)


def test_min_items_lowered_to_two():
    """需求原为 3~8；按用户「单点一个汉堡和一杯可乐」的场景放宽到 2。"""
    assert MIN_INPUT_ITEMS == 2 and MAX_INPUT_ITEMS == 8
    result = recommend(balanced_items()[:2], target_calories=660)
    assert isinstance(result, list)


# --------------------------------------------------------------------------- #
# 端到端：真实 demo 数据集
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def demo_data():
    from services import coupon as coupon_mod
    from services import nutrition

    catalog = nutrition.load_catalog()
    return {
        "catalog": catalog,
        "by_id": {i.id: i for i in catalog},
        "by_name": {i.name: i for i in catalog},
        "set_meals": nutrition.load_set_meals(catalog=catalog),
        "coupons": coupon_mod.load_coupons(),
    }


def test_real_demo_has_set_meals_with_composition(demo_data):
    sms = demo_data["set_meals"]
    assert len(sms) >= 4
    giant = next(s for s in sms if s.name == "巨无霸三件套")
    assert giant.size == 3
    assert giant.price == Decimal("36.5") and giant.original_price == Decimal("49")
    assert {c.key for c in giant.components} == {"巨无霸", "薯条", "可乐"}
    # 组成项都能解析到卡池商品（可乐中杯 -> 可乐）
    assert all(c.food_id for c in giant.components)


def test_real_demo_burger_plus_cola_scenario(demo_data):
    """用户给的完整场景：单点 汉堡 + 可乐，套餐是 汉堡 + 可乐 + 薯条。"""
    by_name = demo_data["by_name"]
    selection = [by_name["巨无霸"], by_name["可乐"]]

    combo = score_subset(
        selection, 846, demo_data["coupons"], demo_data["set_meals"]
    )
    assert combo.setmeal_name == "巨无霸三件套"
    assert combo.setmeal_ratio_pct == pytest.approx(66.7, abs=0.1)
    # 原 2 张卡的价格 vs 组合技价
    assert combo.total_price == Decimal("35.0")
    assert combo.combo_price < combo.total_price
    assert combo.rarity == "SSR"

    ups = suggest_upgrades(
        selection, demo_data["set_meals"], demo_data["by_id"], 846, demo_data["coupons"]
    )
    assert ups, "应给出「补薯条凑满套餐」的建议"
    assert ups[0].is_upgrade and ups[0].setmeal_ratio == pytest.approx(1.0)
    assert ups[0].combo_price == Decimal("36.00")


def test_missing_items_use_menu_names_not_setmeal_names(demo_data):
    """「还差 X」必须写卡池里能点到的名字。

    套餐详情把薯条写作「中薯条」、可乐写作「可乐中杯」，而卡池里叫「薯条」「可乐」。
    若回显详情原名，用户会去卡池里找一个根本不存在的按钮。
    """
    by_name = demo_data["by_name"]
    selection = [by_name["巨无霸"], by_name["可乐"]]

    combo = score_subset(
        selection, 846, demo_data["coupons"], demo_data["set_meals"]
    )
    assert combo.setmeal_ratio_pct == pytest.approx(66.7, abs=0.1)
    assert combo.missing_items == ["薯条"]        # 不是「中薯条」
    assert combo.missing_items[0] in by_name      # 该名字确实能在卡池里点到


def test_setmeal_components_expose_menu_names(demo_data):
    giant = next(s for s in demo_data["set_meals"] if s.name == "巨无霸三件套")
    mapping = {c.name: c.menu_name for c in giant.components}
    assert mapping["中薯条"] == "薯条"
    assert mapping["可乐中杯"] == "可乐"
    assert mapping["巨无霸"] == "巨无霸"


def test_recommend_with_real_demo_catalog(demo_data):
    catalog, sms = demo_data["catalog"], demo_data["set_meals"]
    picked: list[FoodItem] = []
    for cat in ("主食", "蛋白", "蔬果", "饮品"):
        picked += [i for i in catalog if i.category == cat][:2]
    picked = picked[:8]
    assert MIN_INPUT_ITEMS <= len(picked) <= MAX_INPUT_ITEMS

    results = recommend(
        picked, target_calories=846, coupons=demo_data["coupons"],
        set_meals=sms, limit=5,
    )
    assert results
    assert len(results) <= 5
    for r in results:
        assert r.score >= SCORE_FLOOR
        assert r.skill_name and r.reason
        d = r.to_dict()
        assert d["item_ids"] and d["original_price"] and d["combo_price"]


def test_real_demo_ssr_is_reachable(demo_data):
    """在真实数据集上，SSR 必须真的抽得出来（用户明确要求）。

    固定随机种子 → 结果确定，可当作「SSR 可达性」的回归闸门。
    调整 SETMEAL_BONUS / 权重 / MEAL_SHARE 时若跌破 5%，这个用例会失败。
    """
    import random

    catalog, sms, coupons = (
        demo_data["catalog"], demo_data["set_meals"], demo_data["coupons"]
    )
    rng = random.Random(20261014)

    sessions = 0
    with_ssr = 0
    for _ in range(80):
        n = rng.choice([2, 3, 4, 5, 6])
        selection = rng.sample(catalog, n)
        target = rng.choice([700, 846, 950, 1100])
        results = recommend(selection, target, coupons, sms, limit=5)
        if not results:
            continue
        sessions += 1
        if any(r.rarity == "SSR" for r in results):
            with_ssr += 1

    assert sessions > 20, "样本太少，测试本身失效"
    assert with_ssr / sessions >= 0.05, (
        f"SSR 仅 {with_ssr}/{sessions}，稀有度梯度太陡，demo 里抽不到 SSR"
    )


def test_ssr_is_common_when_user_tries_to_complete_a_setmeal(demo_data):
    """用户真实用法下的 SSR 手感闸门。

    口径：从某张官方套餐的组成里抽 2~N-1 件（即"只想凑套餐、还差几件"），
    目标热量取真实画像的单餐预算。这是用户点名的场景类型
    （汉堡 + 可乐，套餐是汉堡 + 可乐 + 薯条）。

    与 ``tools/measure_rarity.py`` 完全同口径，实测 51.1% 的场次能出 SSR；
    这里留足余量断言 ≥40%。若后续调低 SETMEAL_BONUS 或改动权重导致跌破，本用例会失败。
    """
    import random

    from services.health import MockHealthProvider
    from services.profile import UserProfile, build_target

    catalog, sms, coupons = (
        demo_data["catalog"], demo_data["set_meals"], demo_data["coupons"]
    )
    by_id = demo_data["by_id"]
    burns = MockHealthProvider().get_daily_burn(7)
    profiles = [
        UserProfile(),
        UserProfile(gender="female", age=32, height_cm=163, weight_kg=54),
        UserProfile(gender="male", age=21, height_cm=183, weight_kg=88, activity="moderate"),
        UserProfile(gender="female", age=45, height_cm=158, weight_kg=68, activity="sedentary"),
    ]
    rng = random.Random(20261014)

    sessions = 0
    with_ssr = 0
    for prof in profiles:
        target = build_target(prof, burns).meal_target
        for _ in range(40):
            sm = rng.choice(sms)
            comps = [by_id[c.food_id] for c in sm.components if c.food_id]
            if len(comps) < 3:
                continue
            selection = rng.sample(comps, rng.randint(2, len(comps) - 1))
            results = recommend(selection, target, coupons, sms, limit=5)
            if not results:
                continue
            sessions += 1
            if any(r.rarity == "SSR" for r in results):
                with_ssr += 1

    assert sessions > 40, "样本太少，测试本身失效"
    assert with_ssr / sessions >= 0.40, (
        f"想凑套餐时 SSR 仅 {with_ssr}/{sessions}，"
        f"与 README 承诺的『一半以上场次能出 SSR』不符"
    )


def test_full_setmeal_selection_is_ssr(demo_data):
    """凑满「巨无霸三件套」的组合必须能评到 SSR（确定性的可达性保证）。"""
    by_name = demo_data["by_name"]
    full = [by_name["巨无霸"], by_name["薯条"], by_name["可乐"]]
    combo = score_subset(full, 949, demo_data["coupons"], demo_data["set_meals"])
    assert combo.setmeal_ratio == pytest.approx(1.0)
    assert combo.rarity == "SSR"
    assert combo.combo_price == Decimal("36.00")     # 真实套餐价
    assert combo.saved == Decimal("12.50")
