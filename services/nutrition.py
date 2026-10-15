"""营养数据 + 菜单价格 -> :class:`FoodItem` 归一化。

阶段 0 发现的两个结构陷阱直接决定了本模块的设计：

1. ``list-nutrition-foods`` 返回的 ``data`` **不是 JSON 数组**，而是一段自定义
   表格字符串： ``[160]{列名,...}:\\n  行1\\n  行2``。→ 需要 :func:`parse_nutrition_table`
2. 营养表**既没有商品编码也没有价格**，必须与 ``query-meals`` 按商品名 join。

join 的难点是两边命名不一致（营养表「中薯条」vs 菜单「薯条」）。本模块用
**归一化 + 尺寸词剥离 + 别名表** 三级兜底，而不是硬编码下标。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from services import mcp_client

# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #
#: 契合度算法只需要 4 个品类（主食 / 蛋白 / 蔬果 / 饮品）
CATEGORIES = ("主食", "蛋白", "蔬果", "饮品")


@dataclass(frozen=True)
class FoodItem:
    """归一化后的可选单品/套餐。"""

    id: str                      # 商品编码（来自 query-meals）
    name: str
    category: str                # 四分类，用于「组合完整性」计算
    price: Decimal               # 单位：元
    calories: float              # kcal
    protein: float               # g
    fat: float                   # g
    carb: float                  # g
    fiber: float | None = None   # g（营养表未提供，故为 None）
    sodium: float = 0.0          # mg
    calcium: float | None = None # mg
    energy_kj: float | None = None
    image: str = ""
    menu_category: str = ""      # query-meals 里的原始分类名，仅用于 UI 分组展示
    official_meal: bool = False  # 是否官方套餐（code 以 99000 开头）

    @property
    def protein_ratio(self) -> float:
        """蛋白供能比（0~1）。总热量为 0 时返回 0，避免除零。"""
        if self.calories <= 0:
            return 0.0
        return self.protein * 4.0 / self.calories

    @property
    def fat_ratio(self) -> float:
        """脂肪供能比（0~1）。"""
        if self.calories <= 0:
            return 0.0
        return self.fat * 9.0 / self.calories


# --------------------------------------------------------------------------- #
# 名称归一化
# --------------------------------------------------------------------------- #
#: 营养表 -> 菜单表 的别名（键为归一化并剥离尺寸词后的名字）
_ALIAS: dict[str, str] = {
    "朱古力新地": "新地",
    "草莓新地": "新地",
    "香芋派": "派",
    "菠萝派": "派",
    "纯牛奶": "牛奶",
    "冰牛奶": "牛奶",
    "热牛奶": "牛奶",
    "麦旋酷阳光橙": "阳光橙麦炫酷",
    "冰美式": "麦咖啡美式",
    "热美式": "麦咖啡美式",
    "冰奶铁": "麦咖啡奶铁",
    "热奶铁": "麦咖啡奶铁",
    "冰燕麦奶铁": "燕麦奶铁",
    "热燕麦奶铁": "燕麦奶铁",
    "冰浓浓黑巧": "浓浓黑巧",
    "热浓浓黑巧": "浓浓黑巧",
    "热浓浓燕麦黑巧": "浓浓燕麦黑巧",
    "冰浓浓燕麦黑巧": "浓浓燕麦黑巧",
    "热燕麦奶": "燕麦奶",
    "冰燕麦奶": "燕麦奶",
    "苹果片一袋": "苹果片",
}

_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """NFKC（全角→半角、™→TM）+ 去空白 + 去括号补充说明 + 去商标符号。"""
    if not name:
        return ""
    n = unicodedata.normalize("NFKC", str(name))
    n = _BRACKET_RE.sub("", n)
    n = n.replace("™", "").replace("TM", "")
    n = _WS_RE.sub("", n)
    return n.strip().lower()


_SIZE_RE = re.compile(r"[小中大]?杯|[小中大]薯条|[小中大]?(?:号)?$")


def strip_size(name: str) -> str:
    """剥离尺寸词，让「中薯条」/「薯条」、「可乐中杯」/「可乐」对齐。

    注意：只在 join 时作为**兜底 key** 使用，不改变展示名。
    """
    n = name.replace("迷你", "")
    n = n.replace("小杯", "").replace("中杯", "").replace("大杯", "")
    n = n.replace("小薯条", "薯条").replace("中薯条", "薯条").replace("大薯条", "薯条")
    n = n.replace("小号", "").replace("中号", "").replace("大号", "")
    return n.strip()


def match_key(name: str) -> str:
    """join 用主键：归一化 + 剥尺寸词 + 别名。"""
    k = strip_size(normalize_name(name))
    return _ALIAS.get(k, k)


# --------------------------------------------------------------------------- #
# 四分类判定
# --------------------------------------------------------------------------- #
_DRINK_HINTS = (
    "可乐", "雪碧", "芬达", "怡泉", "咖啡", "美式", "奶铁", "卡布奇诺", "拿铁",
    "雪冰", "牛奶", "燕麦奶", "豆浆", "果汁", "苹果汁", "麦炫酷", "阿芙佳朵",
    "红茶", "绿茶", "鲜萃", "朱古力", "摩卡", "玛奇朵", "奶昔", "柠", "橙",
    "豆浆", "水", "气泡",
)
_DESSERT_HINTS = ("冰淇淋", "新地", "麦旋风", "圆筒", "派", "松饼", "拉明顿", "饼干")
_STAPLE_HINTS = (
    "堡", "汉堡", "麦满分", "卷", "薯条", "薯饼", "薯角", "玉米杯", "粥", "油条",
    "三件套", "四件套", "套餐", "随心选", "单品精选", "饭",
)
_PROTEIN_HINTS = (
    "麦乐鸡", "翅", "鸡腿", "鸡排", "鸡块", "鸡球", "脆汁鸡", "鸡柳", "香肠",
    "图林根", "鳕鱼", "牛堡", "蛋", "肉", "鸡",
)
#: 蔬果**必须先判**，否则「玉米杯」「苹果片」会被主食/饮品规则抢走
_VEG_HINTS = ("苹果片", "玉米杯", "沙拉", "蔬菜", "鲜蔬", "蔬果")
#: 菜单侧分类名 -> 主食（用来修正「麦香鸡」这类纯靠名字判不出的汉堡）
_MENU_STAPLE_HINTS = ("汉堡", "卷", "套餐", "单人餐", "大卡")
_MENU_DRINK_HINTS = ("麦咖啡", "饮品")


def classify(name: str, menu_category: str = "") -> str:
    """把商品名映射到四分类之一（主食 / 蛋白 / 蔬果 / 饮品）。

    判定顺序（顺序即优先级，每一级都有明确的现实理由）：

    1. 蔬果（玉米杯 / 苹果片 / 沙拉）—— 量少且名字独特，最优先且不会被误抢；
    2. 饮品 —— 菜单原始分类含「饮品/麦咖啡」或名字命中饮品词；
    3. 甜品（冰淇淋 / 新地 / 麦旋风 / 派）—— 并入主食桶（四分类无「甜品」）；
    4. 菜单原始分类含「汉堡/卷/套餐/单人餐/大卡」→ 主食。
       这一级专门用来修正「麦香鸡」这种**名字里没有"堡"却是汉堡**的商品；
    5. 名字命中主食词（堡 / 麦满分 / 薯条 / 粥 / 三件套 …）→ 主食；
    6. 名字命中蛋白词（翅 / 鸡排 / 麦乐鸡 / 香肠 …）→ 蛋白；
    7. 兜底主食。
    """
    n = normalize_name(name)
    cat = normalize_name(menu_category)

    if any(h in n for h in _VEG_HINTS):
        return "蔬果"
    if any(h in cat for h in _MENU_DRINK_HINTS) or any(h in n for h in _DRINK_HINTS):
        return "饮品"
    if any(h in n for h in _DESSERT_HINTS):
        return "主食"
    if any(h in cat for h in _MENU_STAPLE_HINTS):
        return "主食"
    if any(h in n for h in _STAPLE_HINTS):
        return "主食"
    if any(h in n for h in _PROTEIN_HINTS):
        return "蛋白"
    return "主食"


# --------------------------------------------------------------------------- #
# 解析：伪表格字符串
# --------------------------------------------------------------------------- #
def parse_nutrition_table(data: Any) -> list[dict[str, Any]]:
    """解析 ``list-nutrition-foods`` 的自定义表格字符串。

    输入可以是：
    - 原始字符串 ``"[160]{a,b,c}:\\n  1,2,3..."``
    - MCP 包裹体 ``{"success": True, "data": "<字符串>"}``
    - ``{"_markdown": "..."}``（不可能，但容错）

    返回： ``[{"productName": ..., "energyKcal": ...}, ...]``，**按列名取值**，
    不依赖列顺序（阶段 0 建议）。
    """
    if isinstance(data, dict):
        data = data.get("data") or data.get("_markdown") or ""
    if not isinstance(data, str):
        return []

    text = data.strip()
    if not text:
        return []

    # 表头与表体：第一个 ':' 之前是 "[160]{col,col}" ，之后是行数据
    if ":" in text and text.lstrip().startswith("["):
        head, body = text.split(":", 1)
        cols = re.findall(r"\{([^}]*)\}", head)
        columns = [c.strip() for c in cols[0].split(",")] if cols else []
    else:  # 容错：直接是 CSV 行
        body = text
        columns = []

    rows: list[dict[str, Any]] = []
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split(",")]
        if not columns:
            columns = [f"col{i}" for i in range(len(cells))]
        if len(cells) != len(columns):
            # 行内逗号异常/列数不符：跳过并保持健壮，不抛异常
            continue
        rows.append(dict(zip(columns, cells)))
    return rows


# --------------------------------------------------------------------------- #
# 解析：query-meals
# --------------------------------------------------------------------------- #
def parse_meals(envelope: Any) -> list[dict[str, Any]]:
    """把 ``query-meals`` 的返回拍平成 ``[{code,name,price,original_price,...}]``。

    结构（§2.2）：``data.categories[].meals[].code`` + ``data.meals`` 是**以 code 为
    key 的映射对象**（不是数组）。价格是字符串、单位元。
    """
    if isinstance(envelope, dict) and "data" in envelope:
        envelope = envelope["data"]
    if not isinstance(envelope, dict):
        return []

    meals_map = envelope.get("meals") or {}
    if not isinstance(meals_map, dict):
        return []

    # code -> 原始菜单分类名
    cat_of: dict[str, str] = {}
    for cat in envelope.get("categories", []) or []:
        cat_name = (cat.get("name") or "").replace("\n", "").strip()
        for m in cat.get("meals", []) or []:
            code = str(m.get("code") or "")
            if code and code not in cat_of:
                cat_of[code] = cat_name

    out: list[dict[str, Any]] = []
    for code, info in meals_map.items():
        if not isinstance(info, dict):
            continue
        name = (info.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "code": str(code),
                "name": name,
                "price": to_decimal(info.get("currentPrice")),
                "original_price": to_decimal(info.get("originalPrice")),
                "image": info.get("image") or "",
                "menu_category": cat_of.get(str(code), ""),
                "official_meal": is_official_meal(str(code)),
            }
        )
    return out


def is_official_meal(code: str) -> bool:
    """官方套餐判定：编码以 99000 开头（阶段 0 观察到的编码规律）。"""
    return str(code).startswith("99000")


def to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """数值字段普遍是字符串（§4.6），统一在这里兜住。"""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "" or value == "null":
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# join 成 FoodItem
# --------------------------------------------------------------------------- #
def build_catalog(
    nutrition_envelope: Any,
    meals_envelope: Any,
) -> list[FoodItem]:
    """把营养表与菜单价格按商品名 join 成 :class:`FoodItem` 列表。

    - 只保留**能匹配到价格**的单品（没有价格就无法被勾选）；
    - 同一 code 只保留一条（营养表存在重复行，如「小杯玉米杯」「纯牛奶（盒装）」）。
    """
    meals = parse_meals(meals_envelope)
    by_key: dict[str, dict[str, Any]] = {}
    for m in meals:
        by_key.setdefault(match_key(m["name"]), m)

    rows = parse_nutrition_table(nutrition_envelope)

    catalog: list[FoodItem] = []
    seen_codes: set[str] = set()

    for row in rows:
        raw_name = (row.get("productName") or "").strip()
        if not raw_name:
            continue
        key = match_key(raw_name)
        meal = by_key.get(key)
        if meal is None:
            continue  # 无价格 -> 不进卡池（已在 MCP_INTEGRATION.md 记录别名缺口）
        code = meal["code"]
        if code in seen_codes:
            continue
        seen_codes.add(code)

        catalog.append(
            FoodItem(
                id=code,
                name=meal["name"],           # 展示名用菜单名（有价格的那一边）
                category=classify(meal["name"], meal.get("menu_category", "")),
                price=meal["price"],
                calories=to_float(row.get("energyKcal")),
                protein=to_float(row.get("protein")),
                fat=to_float(row.get("fat")),
                carb=to_float(row.get("carbohydrate")),
                fiber=None,                  # 营养表未提供膳食纤维
                sodium=to_float(row.get("sodium")),
                calcium=to_float(row.get("calcium")) or None,
                energy_kj=to_float(row.get("energyKj")) or None,
                image=meal.get("image") or "",
                menu_category=meal.get("menu_category") or "",
                official_meal=meal.get("official_meal", False),
            )
        )

    return catalog


def load_catalog(client: mcp_client.BaseMCPClient | None = None) -> list[FoodItem]:
    """从 MCP（demo=fixture）加载完整卡池。"""
    client = client or mcp_client.get_client()
    return build_catalog(
        mcp_client.fetch_nutrition(client),
        mcp_client.fetch_meals(client),
    )


# --------------------------------------------------------------------------- #
# 官方套餐（=「套餐优惠」载体）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SetMealComponent:
    """套餐的一个组成项。"""

    code: str            # 详情接口里的商品编码（如 4810）
    name: str            # 详情接口里的名字（如 中薯条）
    key: str             # 归一化 join 键（中薯条 -> 薯条）
    food_id: str | None  # 解析到卡池的 FoodItem.id；None = 该商品不在门店菜单
    menu_name: str | None = None
    #: 解析到的**门店菜单名**（中薯条 -> 薯条）。
    #: 提示用户「再点 X 即可凑满套餐」时必须用这个名字，否则用户在卡池里找不到按钮。


@dataclass(frozen=True)
class SetMeal:
    """官方套餐。价格来自 ``query-meals``（元），组成来自 ``query-meal-detail``。

    这是本项目「套餐优惠」的载体：套餐价 ``price`` 天然低于 ``original_price``，
    差额 ``saving`` 就是套餐带来的优惠。
    """

    id: str
    name: str
    price: Decimal
    original_price: Decimal
    components: tuple[SetMealComponent, ...]

    @property
    def size(self) -> int:
        return len(self.components)

    @property
    def keys(self) -> set[str]:
        return {c.key for c in self.components}

    @property
    def resolvable_ids(self) -> list[str]:
        """能被解析为卡池商品的组成项 id（用于「补齐建议」）。"""
        return [c.food_id for c in self.components if c.food_id]

    @property
    def saving(self) -> Decimal:
        s = self.original_price - self.price
        return s if s > 0 else Decimal("0")

    @property
    def per_item_saving(self) -> Decimal:
        """件均优惠额 = 总优惠 / 件数。用于按匹配件数折算优惠。"""
        if self.size <= 0:
            return Decimal("0")
        return self.saving / self.size


def parse_default_components(detail_envelope: Any) -> list[dict[str, str]]:
    """从 ``query-meal-detail`` 取**默认组成**（``isDefault == 1``）。

    阶段 0 note：只有 ``isDefault=1`` 的 choice 才是默认搭配。
    """
    if not isinstance(detail_envelope, dict) or "data" not in detail_envelope:
        return []
    data = detail_envelope["data"]
    if not isinstance(data, dict):
        return []
    out: list[dict[str, str]] = []
    for rnd in data.get("rounds", []) or []:
        for ch in rnd.get("choices", []) or []:
            if str(ch.get("isDefault")) not in ("1", "True", "true"):
                continue
            code = str(ch.get("code") or "").strip()
            name = (ch.get("name") or "").strip()
            if code and name:
                out.append({"code": code, "name": name})
    return out


def load_set_meals(
    client: mcp_client.BaseMCPClient | None = None,
    catalog: list[FoodItem] | None = None,
) -> list[SetMeal]:
    """加载全部官方套餐（含价格与默认组成）。

    组成项解析到卡池商品时**先按编码、再按归一化名**：
    - ``4810`` 在门店菜单里就是「薯条」→ 编码直接命中；
    - ``3050 可乐中杯`` 不在门店菜单，但归一化后是「可乐」→ 名称命中。
    """
    client = client or mcp_client.get_client()
    catalog = catalog if catalog is not None else load_catalog(client)

    by_id = {f.id: f for f in catalog}
    by_key: dict[str, FoodItem] = {}
    for f in catalog:
        by_key.setdefault(match_key(f.name), f)

    meals = parse_meals(mcp_client.fetch_meals(client))
    result: list[SetMeal] = []
    for m in meals:
        if not m["official_meal"]:
            continue
        try:
            detail = mcp_client.fetch_meal_detail(m["code"], client)
        except Exception:
            continue  # 未抓取详情的套餐跳过（demo 只覆盖 7 张，见 MCP_INTEGRATION.md）
        raw_components = parse_default_components(detail)
        if not raw_components:
            continue

        components: list[SetMealComponent] = []
        for rc in raw_components:
            key = match_key(rc["name"])
            food = by_id.get(rc["code"]) or by_key.get(key)
            components.append(
                SetMealComponent(
                    code=rc["code"],
                    name=rc["name"],
                    key=key,
                    food_id=food.id if food else None,
                    menu_name=food.name if food else None,
                )
            )
        result.append(
            SetMeal(
                id=m["code"],
                name=m["name"],
                price=m["price"],
                original_price=m["original_price"] or m["price"],
                components=tuple(components),
            )
        )
    return result
