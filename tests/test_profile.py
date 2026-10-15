"""画像与热量预算单测（Mifflin-St Jeor / BMI 分档 / 手表优先）。"""
from __future__ import annotations

import pytest

from services.health import DailyBurn
from services.profile import (
    ACTIVITY_FACTORS,
    DEFAULT_MEAL_SHARE,
    MEAL_SHARE_CHOICES,
    UserProfile,
    build_target,
    calc_bmi,
    calc_bmr,
    calc_tdee,
    goal_from_bmi,
    meal_budget,
)


# --------------------------------------------------------------------------- #
# BMR（Mifflin-St Jeor 手算校验）
# --------------------------------------------------------------------------- #
def test_bmr_male_formula():
    p = UserProfile(gender="male", age=30, height_cm=180, weight_kg=75)
    # 10*75 + 6.25*180 - 5*30 + 5 = 750 + 1125 - 150 + 5 = 1730
    assert calc_bmr(p) == pytest.approx(1730.0)


def test_bmr_female_formula():
    p = UserProfile(gender="female", age=30, height_cm=165, weight_kg=55)
    # 10*55 + 6.25*165 - 5*30 - 161 = 550 + 1031.25 - 150 - 161 = 1270.25
    assert calc_bmr(p) == pytest.approx(1270.25)


def test_tdee_applies_activity_factor():
    p = UserProfile(gender="male", age=30, height_cm=180, weight_kg=75, activity="moderate")
    assert calc_tdee(p) == pytest.approx(1730.0 * 1.55)


def test_activity_factor_table_matches_spec():
    assert ACTIVITY_FACTORS == {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "active": 1.725,
    }


# --------------------------------------------------------------------------- #
# BMI 与目标分档（需求原文：<18.5 增重 / <24 维持 / ≥24 减脂）
# --------------------------------------------------------------------------- #
def test_bmi_calculation():
    p = UserProfile(height_cm=180, weight_kg=75)
    assert calc_bmi(p) == pytest.approx(75 / 1.8 ** 2)


@pytest.mark.parametrize(
    "bmi, expected",
    [(15.0, "gain"), (18.49, "gain"), (18.5, "maintain"),
     (23.99, "maintain"), (24.0, "cut"), (30.0, "cut")],
)
def test_goal_from_bmi_boundaries(bmi, expected):
    assert goal_from_bmi(bmi) == expected


def test_target_uses_formula_when_no_watch_data():
    p = UserProfile(gender="male", age=30, height_cm=180, weight_kg=75, activity="light")
    result = build_target(p, burns=None)
    expected_tdee = 1730.0 * 1.375
    assert result.basis == "formula"
    assert result.tdee == pytest.approx(expected_tdee)
    # BMI = 75/3.24 = 23.15 → 维持 → ×1.0
    assert result.goal == "maintain"
    assert result.target == pytest.approx(expected_tdee)


def test_target_uses_watch_data_when_available():
    """手表有真实日均消耗 → 优先用它作为 TDEE。"""
    p = UserProfile(gender="male", age=30, height_cm=180, weight_kg=75, activity="light")
    burns = [
        DailyBurn(date="2026-10-08", calories=2400),
        DailyBurn(date="2026-10-09", calories=2600),
        DailyBurn(date="2026-10-10", calories=2500),
    ]
    result = build_target(p, burns=burns)
    assert result.basis == "watch"
    assert result.tdee == pytest.approx(2500.0)
    assert result.watch_average == pytest.approx(2500.0)
    assert result.days == 3
    assert result.target == pytest.approx(2500.0)   # 维持 → ×1.0


def test_target_multipliers_per_goal():
    # 增重 ×1.15
    thin = UserProfile(gender="female", age=25, height_cm=170, weight_kg=50, activity="sedentary")
    r_thin = build_target(thin)
    assert r_thin.goal == "gain"
    assert r_thin.target == pytest.approx(r_thin.tdee * 1.15)

    # 减脂 ×0.8
    heavy = UserProfile(gender="male", age=40, height_cm=175, weight_kg=95, activity="sedentary")
    r_heavy = build_target(heavy)
    assert r_heavy.goal == "cut"
    assert r_heavy.target == pytest.approx(r_heavy.tdee * 0.8)


def test_build_target_ignores_zero_watch_values():
    """手表返回全 0（未同步）时应回落到公式。"""
    p = UserProfile()
    burns = [DailyBurn(date="2026-10-08", calories=0), DailyBurn(date="2026-10-09", calories=0)]
    assert build_target(p, burns=burns).basis == "formula"


# --------------------------------------------------------------------------- #
# 单餐预算（推荐算法实际使用的 T_target）
# --------------------------------------------------------------------------- #
def test_meal_target_is_share_of_daily_target():
    p = UserProfile(gender="male", age=30, height_cm=180, weight_kg=75, activity="light")
    t = build_target(p)
    assert t.meal_share == pytest.approx(DEFAULT_MEAL_SHARE)
    assert t.meal_target == pytest.approx(t.target * DEFAULT_MEAL_SHARE)


def test_meal_share_choices_are_ordered():
    assert MEAL_SHARE_CHOICES["meal_small"] < MEAL_SHARE_CHOICES["meal_main"] < MEAL_SHARE_CHOICES["meal_big"]
    assert MEAL_SHARE_CHOICES["meal_main"] == DEFAULT_MEAL_SHARE


def test_profile_from_form_reads_meal_share():
    assert UserProfile.from_form({"meal_share": "meal_big"}).meal_share == pytest.approx(0.45)
    assert UserProfile.from_form({"meal_share": "meal_small"}).meal_share == pytest.approx(0.25)
    # 非法值回落主餐
    assert UserProfile.from_form({"meal_share": "???"}).meal_share == pytest.approx(DEFAULT_MEAL_SHARE)


def test_meal_budget_helper_clamps():
    assert meal_budget(2000, 0.35) == pytest.approx(700)
    assert meal_budget(2000, 5.0) == pytest.approx(2000)   # 上限 100%
    assert meal_budget(2000, 0.0) == pytest.approx(200)    # 下限 10%


def test_target_to_dict_exposes_meal_budget():
    d = build_target(UserProfile()).to_dict()
    assert d["meal_share"] == pytest.approx(DEFAULT_MEAL_SHARE)
    assert d["meal_target"] > 0 and d["meal_target"] < d["target"]


# --------------------------------------------------------------------------- #
# 表单解析
# --------------------------------------------------------------------------- #
def test_profile_from_form_parses_and_clamps():
    form = {"gender": "female", "age": "28", "height": "168", "weight": "58",
            "activity": "active", "city": "上海"}
    p = UserProfile.from_form(form)
    assert (p.gender, p.age, p.height_cm, p.weight_kg, p.activity, p.city) == (
        "female", 28, 168.0, 58.0, "active", "上海"
    )


def test_profile_from_form_falls_back_on_bad_values():
    p = UserProfile.from_form({"age": "abc", "height": "", "weight": "xx",
                               "gender": "??", "activity": "unknown"})
    assert p.age == 26 and p.height_cm == 175.0 and p.weight_kg == 70.0
    assert p.gender == "male" and p.activity == "light"   # 非法值 → 默认


def test_profile_to_dict_and_validity():
    p = UserProfile()
    d = p.to_dict()
    assert d["gender_label"] == "男" and d["activity_label"].startswith("轻度")
    assert p.is_valid() is True
    assert UserProfile(age=200).is_valid() is False
    assert UserProfile(height_cm=10).is_valid() is False
