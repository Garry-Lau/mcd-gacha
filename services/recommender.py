"""契合度算法与「组合技」推荐。

五维打分（严格按需求实现）
========================

================  ==========================================================
维度               公式
================  ==========================================================
热量匹配 H         ``clamp(1 − |T_C − T_target| / T_target, 0, 1)``
蛋白匹配 P         供能比 ``r = P_C·4 / T_C``；理想区间 ``[0.20, 0.35]`` 记 1；
                  ``r < 0.20`` 按 ``r / 0.20`` 线性衰减；
                  ``r > 0.35`` 按 ``clamp(1 − (r − 0.35)/0.35, 0, 1)`` 衰减
脂肪匹配 F         供能比 ``f = F_C·9 / T_C``；``f ≤ 0.35`` 记 1；
                  否则 ``clamp(1 − (f − 0.35)/0.35, 0, 1)``
优惠覆盖 D         组合技省下的钱 / 子集原价，clamp 到 [0, 1]
组合完整性 C       覆盖品类数（主食/蛋白/蔬果/饮品） / 4
================  ==========================================================

``总分 = 100 × (0.35H + 0.20P + 0.15F + 0.20D + 0.10C) + 套餐匹配加成``，
封顶 100；**总分 < 50 直接丢弃**；结果按总分倒序。

相对原始需求的三处**有意调整**（均由用户明确要求）
================================================

1. **勾选下限 3 → 2**。用户给出的场景是「单点了一个汉堡和一杯可乐」，
   2 件也要能出结果。
2. **「恰好命中官方套餐 +5」→ 连续化的「套餐匹配度」加成**。
   原规则只有 0 或 1（命中/未命中），现在改为：

   ``M = 子集命中套餐的件数 / 套餐件数``（0~1，连续）
   ``套餐匹配加成 = M × 40``

   例：套餐 = 汉堡 + 可乐 + 薯条，用户勾了汉堡 + 可乐 → ``M = 2/3 ≈ 66.7%``。
   ``M = 1`` 时加成 40 分（原规则只给 5 分）。这是让 SSR 在 demo 中可达的
   主要手段：无券、无套餐匹配时五维满分仅 80 分，达不到 SSR 线（≥90）。
   详见 :func:`test_score_ceiling_without_any_discount_is_80` 与
   :func:`test_setmeal_bonus_makes_ssr_reachable`。
3. **引入单餐预算 ``meal_target`` 作为 ``T_target``**。一次推荐产出的是一餐的量级
   （约 300~1500 kcal），若拿全天预算（约 2400 kcal）算 H，H 会趋近 0，
   35% 的权重被结构性浪费、高分永不可达。见
   :class:`services.profile.CalorieTarget`。

价格口径
========

- ``total_price``    用户所选单品的**原价之和**（"原 N 张卡的价格"）
- ``combo_price``    应用套餐按件折价 + 券免单后的**组合技价**

套餐折价按**件均优惠额**折算（``套餐优惠额 / 套餐件数 × 命中件数``），
而不是按件均售价折算 —— 后者在只命中 1 件时会算出"三分之一个套餐只要三分之一个价"
这种明显不成立的优惠。件均折算在 ``M = 1`` 时恰好收敛到套餐真实价。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations
from typing import Any, Iterable, Sequence

from services.coupon import Coupon
from services.nutrition import CATEGORIES, FoodItem, SetMeal, match_key

#: 金额精度：一律保留两位小数，避免前端出现 ¥26.666666...
MONEY = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)

# --------------------------------------------------------------------------- #
# 常量（算法权重与阈值，全部集中在此，便于评审）
# --------------------------------------------------------------------------- #
WEIGHTS: dict[str, float] = {
    "heat": 0.35,      # H
    "protein": 0.20,   # P
    "fat": 0.15,       # F
    "discount": 0.20,  # D
    "complete": 0.10,  # C
}

PROTEIN_IDEAL_LOW = 0.20
PROTEIN_IDEAL_HIGH = 0.35
FAT_IDEAL_HIGH = 0.35

#: 套餐匹配加成上限：M = 1（凑满整套）时加满。
#: 取 40 是与「热量完美匹配」的 35 分（0.35 × 100）同量级——"凑满套餐"
#: 和"热量达标"被并列为同等重要的信号，这也是本项目的核心玩法。
#:
#: 数值依据（可用 ``python tools/measure_rarity.py`` 复现，41 单品 / 7 张套餐 /
#: 4 组画像 × 120 场，随机种子 20261014）：
#:   · 随机乱点 2~5 件（乱点基线）：前 5 张卡 SSR 2.7%；4.5% 的场次出现 SSR
#:   · 从官方套餐里抽 2~N-1 件（"想凑套餐"的真实用法）：
#:     前 5 张卡 SSR 21.5%；**51.1% 的场次出现 SSR**
#: 乱点几乎抽不到、想凑套餐时高频出现——这正是稀有度该有的手感。
SETMEAL_BONUS = 40.0
SCORE_FLOOR = 50.0      # 低于此分直接丢弃
MAX_INPUT_ITEMS = 8     # 勾选上限
MIN_INPUT_ITEMS = 2     # 勾选下限（原需求为 3，按用户要求放宽到 2）

RARITY_TABLE: tuple[tuple[float, str], ...] = (
    (90.0, "SSR"),
    (75.0, "SR"),
    (60.0, "R"),
    (50.0, "N"),
)

RARITY_LABELS: dict[str, str] = {
    "SSR": "SSR · 传说",
    "SR": "SR · 史诗",
    "R": "R · 稀有",
    "N": "N · 普通",
}


class RecommendError(ValueError):
    """输入不合法（勾选数量越界等）。"""


# --------------------------------------------------------------------------- #
# 结果数据类
# --------------------------------------------------------------------------- #
@dataclass
class Combo:
    """一个「组合技」推荐结果。"""

    name: str
    skill_name: str
    items: list[FoodItem]
    total_calories: float
    total_protein: float
    total_fat: float
    total_carb: float
    total_price: Decimal          # 单品原价之和
    combo_price: Decimal          # 组合技价（套餐折价 + 券免单之后）
    score: float
    rarity: str
    reason: str
    breakdown: dict[str, float] = field(default_factory=dict)
    covered_categories: list[str] = field(default_factory=list)
    coupon_ids: list[str] = field(default_factory=list)

    # ---- 套餐匹配 ----
    setmeal_id: str | None = None
    setmeal_name: str | None = None
    setmeal_size: int = 0
    matched_count: int = 0
    setmeal_ratio: float = 0.0
    missing_items: list[str] = field(default_factory=list)
    setmeal_discount: Decimal = Decimal("0")
    coupon_discount: Decimal = Decimal("0")

    # ---- 补齐建议 ----
    is_upgrade: bool = False
    upgrade_note: str = ""

    @property
    def saved(self) -> Decimal:
        return self.total_price - self.combo_price

    @property
    def protein_ratio(self) -> float:
        return (self.total_protein * 4.0 / self.total_calories) if self.total_calories > 0 else 0.0

    @property
    def fat_ratio(self) -> float:
        return (self.total_fat * 9.0 / self.total_calories) if self.total_calories > 0 else 0.0

    @property
    def setmeal_ratio_pct(self) -> float:
        return self.setmeal_ratio * 100.0

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好输出（金额用字符串，避免浮点误差）。"""
        return {
            "name": self.name,
            "skill_name": self.skill_name,
            "items": [
                {
                    "id": it.id,
                    "name": it.name,
                    "category": it.category,
                    "price": str(it.price),
                    "calories": round(it.calories, 1),
                    "protein": round(it.protein, 1),
                    "fat": round(it.fat, 1),
                    "carb": round(it.carb, 1),
                }
                for it in self.items
            ],
            "item_ids": [it.id for it in self.items],
            "item_names": [it.name for it in self.items],
            "total_calories": round(self.total_calories, 1),
            "total_protein": round(self.total_protein, 1),
            "total_fat": round(self.total_fat, 1),
            "total_carb": round(self.total_carb, 1),
            # 价格对比：原价 vs 组合技价
            "original_price": str(self.total_price),
            "combo_price": str(self.combo_price),
            "saved": str(self.saved),
            "saved_pct": round(float(self.saved / self.total_price), 4) if self.total_price > 0 else 0.0,
            "score": round(self.score, 1),
            "score_pct": round(self.score, 1),
            "rarity": self.rarity,
            "rarity_label": RARITY_LABELS.get(self.rarity, self.rarity),
            "reason": self.reason,
            "breakdown": {k: round(v, 4) for k, v in self.breakdown.items()},
            "covered_categories": self.covered_categories,
            "coupon_ids": self.coupon_ids,
            # 套餐匹配度
            "setmeal_id": self.setmeal_id,
            "setmeal_name": self.setmeal_name,
            "setmeal_size": self.setmeal_size,
            "matched_count": self.matched_count,
            "setmeal_ratio": round(self.setmeal_ratio, 4),
            "setmeal_ratio_pct": round(self.setmeal_ratio * 100, 1),
            "missing_items": self.missing_items,
            "setmeal_discount": str(self.setmeal_discount),
            "coupon_discount": str(self.coupon_discount),
            "is_upgrade": self.is_upgrade,
            "upgrade_note": self.upgrade_note,
            "protein_ratio": round(self.protein_ratio, 4),
            "fat_ratio": round(self.fat_ratio, 4),
        }


# --------------------------------------------------------------------------- #
# 基础打分函数（全部纯函数，便于单测）
# --------------------------------------------------------------------------- #
def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def heat_score(total_calories: float, target: float) -> float:
    """H：热量匹配度。"""
    if target <= 0:
        return 1.0 if total_calories <= 0 else 0.0
    return clamp(1.0 - abs(total_calories - target) / target, 0.0, 1.0)


def protein_score(protein: float, calories: float) -> float:
    """P：蛋白供能比匹配度。"""
    if calories <= 0:
        return 0.0
    r = protein * 4.0 / calories
    if r < PROTEIN_IDEAL_LOW:
        return clamp(r / PROTEIN_IDEAL_LOW, 0.0, 1.0)
    if r <= PROTEIN_IDEAL_HIGH:
        return 1.0
    return clamp(1.0 - (r - PROTEIN_IDEAL_HIGH) / PROTEIN_IDEAL_HIGH, 0.0, 1.0)


def fat_score(fat: float, calories: float) -> float:
    """F：脂肪供能比匹配度。"""
    if calories <= 0:
        return 0.0
    f = fat * 9.0 / calories
    if f <= FAT_IDEAL_HIGH:
        return 1.0
    return clamp(1.0 - (f - FAT_IDEAL_HIGH) / FAT_IDEAL_HIGH, 0.0, 1.0)


def completeness(items: Sequence[FoodItem]) -> tuple[float, list[str]]:
    """C：组合完整性 = 覆盖品类数 / 4。"""
    cats = sorted({it.category for it in items if it.category in CATEGORIES})
    return clamp(len(cats) / len(CATEGORIES), 0.0, 1.0), cats


def coupon_free_value(
    items: Sequence[FoodItem],
    coupons: Iterable[Coupon],
) -> tuple[Decimal, list[str], set[str]]:
    """券免单金额（本位为单品级券，命中即整件免单）。

    返回 ``(免单金额, 命中的 couponId 列表, 被免单的商品 id 集合)``。
    """
    free = Decimal("0")
    hit_ids: list[str] = []
    freed_item_ids: set[str] = set()
    for it in items:
        for c in coupons:
            if c.has_code and c.covers(it.id):
                free += it.price
                hit_ids.append(c.id)
                freed_item_ids.add(it.id)
                break
    return free, hit_ids, freed_item_ids


def discount_coverage(
    items: Sequence[FoodItem],
    coupons: Iterable[Coupon],
) -> tuple[float, Decimal, list[str]]:
    """D 的**券**部分（需求原文口径）：被券覆盖的商品原价之和 / 总价。

    保留此函数是为了让「需求原文口径」可被独立验证；推荐主链路用的是
    :func:`score_subset` 里的**综合优惠率**（券 + 套餐折价）。
    """
    ordered = list(items)
    total = sum((it.price for it in ordered), Decimal("0"))
    if total <= 0:
        return 0.0, Decimal("0"), []
    free, hit_ids, _ = coupon_free_value(ordered, coupons)
    return clamp(float(free / total), 0.0, 1.0), free, hit_ids


def rarity_of(score: float) -> str:
    for threshold, label in RARITY_TABLE:
        if score >= threshold:
            return label
    return "N"


# --------------------------------------------------------------------------- #
# 套餐匹配
# --------------------------------------------------------------------------- #
@dataclass
class SetMealMatch:
    """子集与某个官方套餐的匹配结果。"""

    set_meal: SetMeal
    matched_items: list[FoodItem]
    matched_components: list[Any]      # list[SetMealComponent]
    missing_components: list[Any]      # list[SetMealComponent]

    @property
    def ratio(self) -> float:
        """套餐匹配度 M = 命中件数 / 套餐件数。"""
        if self.set_meal.size <= 0:
            return 0.0
        return clamp(len(self.matched_components) / self.set_meal.size, 0.0, 1.0)

    @property
    def missing_names(self) -> list[str]:
        """还差哪几件。

        必须回显**门店菜单名**（``menu_name``）而不是套餐详情里的原始组件名：
        套餐详情写的是「中薯条」，卡池里能点到的是「薯条」。若回显原名，用户会去
        卡池里找一个不存在的按钮。
        """
        return [
            getattr(c, "menu_name", None) or c.name for c in self.missing_components
        ]


def _component_hits(comp: Any, item: FoodItem) -> bool:
    if comp.food_id and comp.food_id == item.id:
        return True
    return match_key(item.name) == comp.key


def match_setmeal(items: Sequence[FoodItem], set_meals: Sequence[SetMeal] | None) -> SetMealMatch | None:
    """在若干官方套餐中挑出**匹配度最高**的一个。

    组成项与子集商品做 1-1 贪心匹配（同一件商品不会被两个组成项重复占用）。
    平手时优先「套餐优惠额更大」的那张。
    """
    if not set_meals:
        return None

    best: SetMealMatch | None = None
    best_key: tuple[float, Decimal] | None = None

    for sm in set_meals:
        taken: set[int] = set()
        matched_items: list[FoodItem] = []
        matched_comps: list[Any] = []
        for comp in sm.components:
            for idx, it in enumerate(items):
                if idx in taken:
                    continue
                if _component_hits(comp, it):
                    taken.add(idx)
                    matched_items.append(it)
                    matched_comps.append(comp)
                    break
        if not matched_comps:
            continue
        missing = [c for c in sm.components if c not in matched_comps]
        cand = SetMealMatch(sm, matched_items, matched_comps, missing)
        key = (cand.ratio, sm.saving)
        if best_key is None or key > best_key:
            best, best_key = cand, key
    return best


# --------------------------------------------------------------------------- #
# 组合技命名与推荐理由
# --------------------------------------------------------------------------- #
def make_skill_name(
    items: Sequence[FoodItem],
    *,
    protein_ratio: float = 0.0,
    categories: Sequence[str] | None = None,
    discount: float = 0.0,
    heat: float = 0.0,
    setmeal_ratio: float = 0.0,
    setmeal_name: str | None = None,
) -> str:
    """按组合的**构成 + 维度得分**生成「组合技」名称。

    接入维度得分是为了让名字有辨识度：纯看品类构成时，八件套的绝大多数子集
    都会落进同一个分支，抽卡结果会显得千篇一律。
    """
    cats = [it.category for it in items]
    n = len(items)
    n_main = cats.count("主食")
    n_protein = cats.count("蛋白")
    n_drink = cats.count("饮品")
    n_veg = cats.count("蔬果")
    n_cats = len(set(categories)) if categories else len(set(cats))

    if n == 1:
        return "单点保底·孤军奋战"
    if setmeal_ratio >= 0.999 and setmeal_name:
        return f"套餐共鸣·{setmeal_name}"
    if discount >= 0.99:
        return "薅券流·全单免费连击"
    if protein_ratio >= 0.35 and n_main <= 1:
        return "高蛋白轻食连击"
    if protein_ratio < 0.15 and n_main >= 2:
        return "碳水炸弹·双人份"
    if setmeal_ratio >= 0.66:
        return "套餐拼图·差一件达成"
    if n_cats >= 4 and heat >= 0.95:
        return "全品类均衡·满配终结技"
    if n_cats >= 4:
        return "四品齐全·均衡连击"
    if n_protein >= 2 and n_main <= 1:
        return "高蛋白轻食连击"
    if n_drink >= 2:
        return "冰爽续杯连击"
    if n_main >= 1 and n_protein >= 1 and n_drink >= 1 and n >= 3:
        return "续航能量三连击"
    if n_veg >= 1 and n >= 3:
        return "清爽均衡套餐"
    if n_main == n and n >= 2:
        return "纯碳水快乐套餐"
    return "黄金三角均衡餐"


def make_reason(m: dict[str, Any]) -> str:
    """一句话推荐理由，包含「原价 → 组合技价」的对比。"""
    parts = [
        f"总热量 {m['calories']:.0f} kcal（预算 {m['target']:.0f} kcal）",
        f"蛋白供能比 {m['protein_ratio']:.0%}",
    ]
    if m.get("setmeal_name"):
        parts.append(
            f"命中「{m['setmeal_name']}」{m['setmeal_ratio']:.0%}"
        )
        if m.get("missing_names"):
            parts.append(f"再点 {'、'.join(m['missing_names'])} 即可凑满套餐")
    if m["saved"] > 0:
        parts.append(f"原价 ¥{m['total_price']} → 组合技价 ¥{m['combo_price']}，省 ¥{m['saved']}")
    else:
        parts.append(f"共 ¥{m['total_price']}")
    if m.get("categories"):
        parts.append(f"覆盖 {len(m['categories'])} 个品类（{'/'.join(m['categories'])}）")
    return "，".join(parts) + "。"


# --------------------------------------------------------------------------- #
# 主打分
# --------------------------------------------------------------------------- #
def price_breakdown(
    items: Sequence[FoodItem],
    coupons: Sequence[Coupon] | None,
    match: SetMealMatch | None,
) -> tuple[Decimal, Decimal, Decimal, set[str], list[str]]:
    """算出 ``(组合技价, 套餐折价, 券免单额, 被券免单的商品 id, 命中的 couponId)``。

    套餐折价按**件均优惠额**折算，且**跳过已被券免单的匹配件**，避免重复优惠。
    """
    ordered = list(items)
    total = sum((it.price for it in ordered), Decimal("0"))

    coupon_free, coupon_ids, freed = coupon_free_value(ordered, coupons or [])

    setmeal_off = Decimal("0")
    if match is not None:
        per_item = match.set_meal.per_item_saving
        for it in match.matched_items:
            if it.id in freed:
                continue          # 已被券免单，不再叠加套餐折价
            setmeal_off += per_item

    combo_price = total - coupon_free - setmeal_off
    if combo_price < 0:
        combo_price = Decimal("0")
    return (
        money(combo_price),
        money(setmeal_off),
        money(coupon_free),
        freed,
        sorted(set(coupon_ids)),
    )


def score_subset(
    subset: Sequence[FoodItem],
    target_calories: float,
    coupons: Sequence[Coupon] | None = None,
    set_meals: Sequence[SetMeal] | None = None,
) -> Combo:
    """给单个子集打分并组装 :class:`Combo`。"""
    items = list(subset)
    total_cal = sum(it.calories for it in items)
    total_protein = sum(it.protein for it in items)
    total_fat = sum(it.fat for it in items)
    total_carb = sum(it.carb for it in items)
    total_price = sum((it.price for it in items), Decimal("0"))

    match = match_setmeal(items, set_meals)
    ratio = match.ratio if match else 0.0

    combo_price, setmeal_off, coupon_free, _freed, coupon_ids = price_breakdown(
        items, coupons, match
    )

    # D：综合优惠率（券 + 套餐折价）
    if total_price > 0:
        d = clamp(float((total_price - combo_price) / total_price), 0.0, 1.0)
    else:
        d = 0.0

    h = heat_score(total_cal, target_calories)
    p = protein_score(total_protein, total_cal)
    f = fat_score(total_fat, total_cal)
    c, cats = completeness(items)

    base = 100.0 * (
        WEIGHTS["heat"] * h
        + WEIGHTS["protein"] * p
        + WEIGHTS["fat"] * f
        + WEIGHTS["discount"] * d
        + WEIGHTS["complete"] * c
    )
    bonus = ratio * SETMEAL_BONUS
    score = min(base + bonus, 100.0)

    protein_ratio = (total_protein * 4.0 / total_cal) if total_cal > 0 else 0.0
    saved = total_price - combo_price

    metrics = {
        "calories": total_cal,
        "target": target_calories,
        "protein_ratio": protein_ratio,
        "categories": cats,
        "total_price": total_price,
        "combo_price": combo_price,
        "saved": saved,
        "setmeal_name": match.set_meal.name if match else None,
        "setmeal_ratio": ratio,
        "missing_names": match.missing_names if match else [],
    }

    skill_name = make_skill_name(
        items,
        protein_ratio=protein_ratio,
        categories=cats,
        discount=d,
        heat=h,
        setmeal_ratio=ratio,
        setmeal_name=match.set_meal.name if match else None,
    )

    return Combo(
        name=skill_name,
        skill_name=skill_name,
        items=items,
        total_calories=total_cal,
        total_protein=total_protein,
        total_fat=total_fat,
        total_carb=total_carb,
        total_price=total_price,
        combo_price=combo_price,
        score=score,
        rarity=rarity_of(score),
        reason=make_reason(metrics),
        breakdown={
            "heat": h,
            "protein": p,
            "fat": f,
            "discount": d,
            "complete": c,
            "setmeal": ratio,
        },
        covered_categories=cats,
        coupon_ids=coupon_ids,
        setmeal_id=match.set_meal.id if match else None,
        setmeal_name=match.set_meal.name if match else None,
        setmeal_size=match.set_meal.size if match else 0,
        matched_count=len(match.matched_components) if match else 0,
        setmeal_ratio=ratio,
        missing_items=match.missing_names if match else [],
        setmeal_discount=setmeal_off,
        coupon_discount=coupon_free,
        is_upgrade=False,
        upgrade_note="",
    )


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def enumerate_subsets(items: Sequence[FoodItem]) -> Iterable[tuple[FoodItem, ...]]:
    """所有非空子集。n=8 时共 255 个，性能无忧。"""
    n = len(items)
    for size in range(1, n + 1):
        yield from combinations(items, size)


def recommend(
    items: Sequence[FoodItem],
    target_calories: float,
    coupons: Sequence[Coupon] | None = None,
    set_meals: Sequence[SetMeal] | None = None,
    *,
    limit: int | None = None,
) -> list[Combo]:
    """主推荐入口。

    :param items: 用户勾选的单品集合，数量必须 2 ≤ n ≤ 8
    :param target_calories: 目标热量 T_target（kcal）
    :param coupons: 可用券
    :param set_meals: 官方套餐（提供「套餐匹配度」与套餐折价）
    :param limit: 截断结果数量
    :raises RecommendError: 勾选数量越界
    """
    items = list(items)
    n = len(items)
    if n < MIN_INPUT_ITEMS:
        raise RecommendError(f"至少勾选 {MIN_INPUT_ITEMS} 件单品（当前 {n} 件）")
    if n > MAX_INPUT_ITEMS:
        raise RecommendError(f"最多勾选 {MAX_INPUT_ITEMS} 件单品（当前 {n} 件）")
    if target_calories <= 0:
        raise RecommendError("目标热量必须大于 0")

    coupons = list(coupons or [])

    results: list[Combo] = []
    for subset in enumerate_subsets(items):
        combo = score_subset(subset, target_calories, coupons, set_meals)
        if combo.score < SCORE_FLOOR:      # 规则：低于 50 直接丢弃
            continue
        results.append(combo)

    results.sort(key=lambda c: (-c.score, -c.total_calories))
    return results[:limit] if limit is not None else results


def resolve_combo(
    items: Sequence[FoodItem],
    target_calories: float,
    coupons: Sequence[Coupon] | None = None,
    set_meals: Sequence[SetMeal] | None = None,
) -> Combo:
    """对**给定的完整集合**直接打分，不做子集枚举。

    给「下单确认页」用：结果卡上展示的商品就是用户要买的全部商品，
    所以直接对该集合算一次即可 —— 既不浪费（255 个子集），
    也**不受** :data:`MAX_INPUT_ITEMS` 限制（补齐建议可能超过 8 件，
    但它本身就是一个已确定的组合，没有"枚举"的语义）。
    """
    items = list(items)
    if not items:
        raise RecommendError("至少需要 1 件单品")
    if target_calories <= 0:
        raise RecommendError("目标热量必须大于 0")
    return score_subset(items, target_calories, coupons, set_meals)


def suggest_upgrades(
    selection: Sequence[FoodItem],
    set_meals: Sequence[SetMeal] | None,
    catalog_by_id: dict[str, FoodItem],
    target_calories: float,
    coupons: Sequence[Coupon] | None = None,
    *,
    limit: int | None = 3,
) -> list[Combo]:
    """「补齐建议」：把套餐缺的那几件补上，凑满整套拿满套餐优惠。

    只对**用户所选恰好全部属于该套餐**（``selection ⊆ 套餐组成``）且**缺失件都能
    在门店菜单里买到**的套餐生成建议——否则凑出来的组合会夹带套餐外的商品，
    「补齐」的语义就站不住脚了。

    结果 ``is_upgrade=True``，价格按 ``M = 1`` 的真实套餐价计算。
    """
    out: list[Combo] = []
    if not set_meals:
        return out

    for sm in set_meals:
        match = match_setmeal(selection, [sm])
        if match is None or match.ratio >= 1.0:
            continue
        # 用户选的每一件都必须落在这张套餐里，否则不是"补齐"而是"换餐"
        if len(match.matched_items) != len(selection):
            continue
        missing_foods: list[FoodItem] = []
        ok = True
        for comp in match.missing_components:
            food = catalog_by_id.get(comp.food_id) if comp.food_id else None
            if food is None:
                ok = False       # 有缺件在门店菜单里买不到 → 无法补齐
                break
            missing_foods.append(food)
        if not ok or not missing_foods:
            continue

        subset = list(match.matched_items) + missing_foods
        combo = score_subset(subset, target_calories, coupons, [sm])
        combo.is_upgrade = True
        combo.upgrade_note = (
            f"再加 {len(missing_foods)} 件（{'、'.join(f.name for f in missing_foods)}）"
            f"即可凑满「{sm.name}」"
        )
        out.append(combo)

    out.sort(key=lambda c: -c.score)
    return out[:limit] if limit is not None else out
