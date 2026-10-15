"""用户画像与热量预算。

算法约定（需求原样实现）：
- BMR：Mifflin-St Jeor 公式
    男：10·体重 + 6.25·身高 − 5·年龄 + 5
    女：10·体重 + 6.25·身高 − 5·年龄 − 161
- TDEE = BMR × 活动系数（久坐 1.2 / 轻度 1.375 / 中度 1.55 / 高强度 1.725）
- 目标热量（按 BMI 分档）：
    BMI < 18.5          → TDEE × 1.15（增重）
    18.5 ≤ BMI < 24     → TDEE（维持）
    BMI ≥ 24            → TDEE × 0.8（减脂）
- **若智能手表有真实日均消耗数据，优先用手表数据作为 TDEE**（formula → watch）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.health import DailyBurn

#: 活动系数表（前端表单用中文标签）
ACTIVITY_FACTORS: dict[str, float] = {
    "sedentary": 1.2,    # 久坐
    "light": 1.375,      # 轻度
    "moderate": 1.55,    # 中度
    "active": 1.725,     # 高强度
}

ACTIVITY_LABELS: dict[str, str] = {
    "sedentary": "久坐（几乎不运动）",
    "light": "轻度（每周 1-3 次）",
    "moderate": "中度（每周 3-5 次）",
    "active": "高强度（每周 6-7 次）",
}

GENDER_LABELS: dict[str, str] = {"male": "男", "female": "女"}

GOAL_LABELS: dict[str, str] = {"gain": "增重", "maintain": "维持", "cut": "减脂"}

#: TDEE 来源的中文标签
BASIS_LABELS: dict[str, str] = {"formula": "公式估算", "watch": "智能手表实测"}

#: 目标热量相对于 TDEE 的系数
GOAL_FACTORS: dict[str, float] = {"gain": 1.15, "maintain": 1.0, "cut": 0.8}

#: 单餐占日预算的默认比例（主餐约 35%，符合常规膳食分配）
DEFAULT_MEAL_SHARE = 0.35

MEAL_SHARE_CHOICES: dict[str, float] = {
    "meal_small": 0.25,   # 加餐/小食
    "meal_main": 0.35,    # 主餐（默认）
    "meal_big": 0.45,     # 大餐
}

MEAL_SHARE_LABELS: dict[str, str] = {
    "meal_small": "小食（日预算 25%）",
    "meal_main": "主餐（日预算 35%）",
    "meal_big": "大餐（日预算 45%）",
}


@dataclass
class UserProfile:
    """前端表单收集到的用户画像。

    默认值让 demo 不必填表也能跑（表单会预填这些）。
    """

    gender: str = "male"        # male / female
    age: int = 26
    height_cm: float = 175.0
    weight_kg: float = 70.0
    activity: str = "light"     # 见 ACTIVITY_FACTORS
    city: str = "北京"
    store_code: str = "1950970"
    meal_share: float = DEFAULT_MEAL_SHARE   # 单餐占日预算比例

    @property
    def activity_factor(self) -> float:
        return ACTIVITY_FACTORS.get(self.activity, ACTIVITY_FACTORS["light"])

    # -- 校验 -------------------------------------------------------------- #
    def is_valid(self) -> bool:
        return (
            10 <= self.age <= 100
            and 100 <= self.height_cm <= 250
            and 30 <= self.weight_kg <= 300
            and self.gender in GENDER_LABELS
        )

    @classmethod
    def from_form(cls, form: Any) -> "UserProfile":
        """从 Flask ``request.form`` 之类映射构造，带容错默认值。"""

        def _get(key: str, default: str = "") -> str:
            try:
                return str(form.get(key, default) or default).strip()
            except Exception:
                return default

        def _num(key: str, default: float) -> float:
            try:
                return float(_get(key) or default)
            except (TypeError, ValueError):
                return default

        gender = "female" if _get("gender", "male").lower().startswith("f") else "male"
        activity = _get("activity", "light").lower()
        if activity not in ACTIVITY_FACTORS:
            activity = "light"

        meal_key = _get("meal_share", "meal_main").lower()
        meal_share = MEAL_SHARE_CHOICES.get(meal_key, DEFAULT_MEAL_SHARE)

        return cls(
            gender=gender,
            age=int(_num("age", 26)),
            height_cm=_num("height", 175.0),
            weight_kg=_num("weight", 70.0),
            activity=activity,
            city=_get("city", "北京") or "北京",
            store_code=_get("store_code", "1950970") or "1950970",
            meal_share=meal_share,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gender": self.gender,
            "gender_label": GENDER_LABELS.get(self.gender, self.gender),
            "age": self.age,
            "height_cm": self.height_cm,
            "weight_kg": self.weight_kg,
            "activity": self.activity,
            "activity_label": ACTIVITY_LABELS.get(self.activity, self.activity),
            "city": self.city,
            "store_code": self.store_code,
            "meal_share": self.meal_share,
        }

    def to_dict_for_session(self) -> dict[str, Any]:
        """写入 Flask session 用的**纯数据**字典（不含展示用中文标签，便于往返反序列化）。"""
        return {
            "gender": self.gender,
            "age": self.age,
            "height_cm": self.height_cm,
            "weight_kg": self.weight_kg,
            "activity": self.activity,
            "city": self.city,
            "store_code": self.store_code,
            "meal_share": self.meal_share,
        }


@dataclass
class CalorieTarget:
    """热量预算结果。

    ``target`` 是**全天预算**（按需求原文由 TDEE 推导）；
    ``meal_target`` 是**单餐预算** = ``target × meal_share``。

    为什么要有单餐预算：推荐算法一次产出的是一份「套餐/单品组合」，
    也就是一餐的量级（约 300~1500 kcal）。若直接拿全天预算（约 2400 kcal）
    去算热量匹配度 H，任何单餐组合的 H 都会趋近 0，35% 的权重被结构性浪费，
    高分（SR/SSR）永远不可达。因此推荐链路用 ``meal_target`` 作为 T_target。
    """

    bmr: float
    bmi: float
    tdee: float
    target: float
    goal: str                  # gain / maintain / cut
    basis: str                 # formula / watch  —— TDEE 的来源
    watch_average: float | None = None
    days: int = 7
    meal_share: float = DEFAULT_MEAL_SHARE

    @property
    def goal_label(self) -> str:
        return GOAL_LABELS.get(self.goal, self.goal)

    @property
    def basis_label(self) -> str:
        """TDEE 来源的中文标签（模板直接取用，勿只在 to_dict 里给）。"""
        return BASIS_LABELS.get(self.basis, BASIS_LABELS["formula"])

    @property
    def meal_target(self) -> float:
        """单餐预算（推荐算法实际使用的 T_target）。"""
        return self.target * self.meal_share

    def to_dict(self) -> dict[str, Any]:
        return {
            "bmr": round(self.bmr, 1),
            "bmi": round(self.bmi, 1),
            "tdee": round(self.tdee, 1),
            "target": round(self.target, 1),
            "goal": self.goal,
            "goal_label": self.goal_label,
            "basis": self.basis,
            "basis_label": self.basis_label,
            "watch_average": round(self.watch_average, 1) if self.watch_average else None,
            "days": self.days,
            "meal_share": self.meal_share,
            "meal_target": round(self.meal_target, 1),
        }


# --------------------------------------------------------------------------- #
# 公式
# --------------------------------------------------------------------------- #
def calc_bmr(profile: UserProfile) -> float:
    """Mifflin-St Jeor。"""
    base = 10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * profile.age
    return base + 5 if profile.gender == "male" else base - 161


def calc_bmi(profile: UserProfile) -> float:
    h_m = profile.height_cm / 100.0
    if h_m <= 0:
        return 0.0
    return profile.weight_kg / (h_m * h_m)


def calc_tdee(profile: UserProfile, bmr: float | None = None) -> float:
    return (bmr if bmr is not None else calc_bmr(profile)) * profile.activity_factor


def goal_from_bmi(bmi: float) -> str:
    """BMI 分档（边界按需求原文：<18.5 增重；<24 维持；≥24 减脂）。"""
    if bmi < 18.5:
        return "gain"
    if bmi < 24:
        return "maintain"
    return "cut"


def average_daily_burn(burns: list[DailyBurn] | None) -> float | None:
    if not burns:
        return None
    values = [b.calories for b in burns if b.calories > 0]
    if not values:
        return None
    return sum(values) / len(values)


def build_target(
    profile: UserProfile,
    burns: list[DailyBurn] | None = None,
) -> CalorieTarget:
    """组装热量预算。

    ``burns``（手表数据）非空且平均值 > 0 时，**以手表数据作为 TDEE**；
    否则用手表公式估算。
    """
    bmr = calc_bmr(profile)
    bmi = calc_bmi(profile)
    goal = goal_from_bmi(bmi)

    watch_avg = average_daily_burn(burns)
    if watch_avg:
        tdee = watch_avg
        basis = "watch"
    else:
        tdee = calc_tdee(profile, bmr)
        basis = "formula"

    target = tdee * GOAL_FACTORS[goal]
    return CalorieTarget(
        bmr=bmr,
        bmi=bmi,
        tdee=tdee,
        target=target,
        goal=goal,
        basis=basis,
        watch_average=watch_avg,
        days=len(burns) if burns else 7,
        meal_share=profile.meal_share,
    )


def meal_budget(daily_target: float, share: float = DEFAULT_MEAL_SHARE) -> float:
    """单餐预算 = 日预算 × 餐次占比（夹在 10%~100%）。"""
    return daily_target * max(0.1, min(1.0, share))
