"""解析层单测：伪表格营养表、菜单 join、券（JSON + Markdown）、MCP 客户端。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services import coupon as coupon_mod
from services import mcp_client, nutrition


# --------------------------------------------------------------------------- #
# 伪表格字符串解析
# --------------------------------------------------------------------------- #
def test_parse_nutrition_table_reads_all_rows():
    rows = nutrition.parse_nutrition_table(mcp_client.fetch_nutrition())
    assert len(rows) == 160, "阶段 0 抓到的营养表共 160 行"
    first = rows[0]
    assert first["productName"] == "猪柳麦满分"
    assert first["energyKcal"] == "308"
    assert first["protein"] == "16"


def test_parse_nutrition_table_is_column_name_based():
    """按列名取值，不依赖列顺序（阶段 0 建议）。"""
    shuffled = "[1]{protein,productName,energyKcal}:\n  16,猪柳麦满分,308"
    rows = nutrition.parse_nutrition_table(shuffled)
    assert rows == [{"protein": "16", "productName": "猪柳麦满分", "energyKcal": "308"}]


def test_parse_nutrition_table_tolerates_garbage():
    assert nutrition.parse_nutrition_table("") == []
    assert nutrition.parse_nutrition_table(None) == []
    assert nutrition.parse_nutrition_table({"data": "[1]{a,b}:\n  1"}) == []  # 列数不符 → 跳过


# --------------------------------------------------------------------------- #
# 名称归一化与别名
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected_substring",
    [("中薯条", "薯条"), ("大杯鲜萃咖啡", "鲜萃咖啡"), ("可乐中杯", "可乐"),
     ("小杯玉米杯", "玉米杯")],
)
def test_match_key_strips_size(raw, expected_substring):
    assert nutrition.match_key(raw) == expected_substring


def test_match_key_applies_alias():
    assert nutrition.match_key("朱古力新地") == nutrition.match_key("新地")
    assert nutrition.match_key("纯牛奶（盒装）") == nutrition.match_key("牛奶")


def test_normalize_name_handles_fullwidth_and_trademark():
    assert nutrition.normalize_name("麦咖啡™美式") == "麦咖啡美式"
    assert nutrition.normalize_name("可乐（中杯）") == "可乐"


# --------------------------------------------------------------------------- #
# 四分类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, menu_cat, expected",
    [
        ("可乐", "饮品", "饮品"),
        ("麦咖啡™美式", "麦咖啡™", "饮品"),
        ("浓浓黑巧（冰/热）", "麦咖啡™", "饮品"),
        ("苹果片", "小食甜品/其他", "蔬果"),
        ("玉米杯", "小食甜品/其他", "蔬果"),
        ("圆筒冰淇淋", "小食甜品/其他", "主食"),
        ("新地", "小食甜品/其他", "主食"),
        ("薯条", "小食甜品/其他", "主食"),
        ("巨无霸", "巨无霸\n牛鱼肉堡", "主食"),
        ("麦香鸡", "鸡肉汉堡\n/卷", "主食"),          # 名字无「堡」但菜单位于汉堡分类
        ("薄皮焦香V翅", "小食甜品/其他", "蛋白"),
        ("那么大鸡排（椒盐风味）", "小食甜品/其他", "蛋白"),
    ],
)
def test_classify(name, menu_cat, expected):
    assert nutrition.classify(name, menu_cat) == expected


# --------------------------------------------------------------------------- #
# join 成 FoodItem
# --------------------------------------------------------------------------- #
def test_build_catalog_joins_price_and_nutrition():
    catalog = nutrition.build_catalog(
        mcp_client.fetch_nutrition(), mcp_client.fetch_meals()
    )
    assert len(catalog) >= 10

    by_name = {i.name: i for i in catalog}
    assert "巨无霸" in by_name
    bun = by_name["巨无霸"]
    assert bun.id == "1100"
    assert bun.price == Decimal("25.5")
    assert bun.calories == 513          # 来自营养表
    assert bun.protein == 27
    assert bun.category == "主食"
    assert bun.fiber is None            # 营养表未提供膳食纤维


def test_build_catalog_deduplicates_by_code():
    """营养表有重复行（小杯玉米杯 / 纯牛奶（盒装））→ 同 code 只留一条。"""
    catalog = nutrition.build_catalog(
        mcp_client.fetch_nutrition(), mcp_client.fetch_meals()
    )
    codes = [i.id for i in catalog]
    assert len(codes) == len(set(codes))
    assert sum(1 for i in catalog if i.name == "玉米杯") == 1


def test_catalog_covers_all_four_categories():
    catalog = nutrition.load_catalog()
    cats = {i.category for i in catalog}
    assert cats == set(nutrition.CATEGORIES)


def test_parse_meals_flattens_code_map():
    meals = nutrition.parse_meals(mcp_client.fetch_meals())
    assert any(m["code"] == "1100" and m["name"] == "巨无霸" for m in meals)
    assert all(isinstance(m["price"], Decimal) for m in meals)
    assert all(m["official_meal"] == m["code"].startswith("99000") for m in meals)


def test_official_set_meals_have_price_and_composition():
    """套餐 = 「套餐优惠」载体：价格来自 query-meals，组成来自 query-meal-detail。"""
    sms = nutrition.load_set_meals()
    assert len(sms) >= 4

    giant = next(s for s in sms if s.name == "巨无霸三件套")
    assert giant.id == "9900005466"
    assert giant.price == Decimal("36.5")
    assert giant.original_price == Decimal("49")
    assert giant.saving == Decimal("12.5")
    assert giant.size == 3
    # 归一化名键：中薯条 -> 薯条，可乐中杯 -> 可乐
    assert giant.keys == {"巨无霸", "薯条", "可乐"}
    # 组成项都能解析到卡池商品
    assert all(c.food_id for c in giant.components)


def test_meal_detail_demo_routing_never_mismatches():
    """请求 A 套餐不能拿到 B 套餐的详情（张冠李戴会静默算错匹配度）。"""
    client = mcp_client.DemoMCPClient()
    detail = mcp_client.fetch_meal_detail("9900005456", client)
    assert detail["data"]["code"] == "9900005456"
    assert detail["data"]["name"] == "麦辣鸡腿汉堡三件套"

    # 未抓取详情的套餐必须报错，而不是回落成别的套餐
    with pytest.raises(mcp_client.MCPToolError):
        mcp_client.fetch_meal_detail("9900005462", client)


def test_parse_default_components_only_takes_isdefault():
    detail = {
        "data": {
            "rounds": [
                {"choices": [
                    {"code": "1", "name": "默认项", "isDefault": 1},
                    {"code": "2", "name": "备选项", "isDefault": 0},
                ]},
            ]
        }
    }
    comps = nutrition.parse_default_components(detail)
    assert comps == [{"code": "1", "name": "默认项"}]
    assert nutrition.parse_default_components({}) == []
    assert nutrition.parse_default_components(None) == []


# --------------------------------------------------------------------------- #
# 优惠券
# --------------------------------------------------------------------------- #
def test_parse_store_coupons_full_fields():
    coupons = coupon_mod.parse_store_coupons(mcp_client.fetch_store_coupons())
    assert len(coupons) == 1
    c = coupons[0]
    assert c.id == "8524308B846BBD05A1C5B9068AE52799"
    assert c.code == "MCD6WV10110800H9023P6"
    assert c.applicable_items == ["515837"]
    assert c.start_at == "2026-10-07 00:00:00"
    assert c.expire_at == "2026-11-06 23:59:59"
    assert c.has_code and c.usable and c.covers("515837")
    assert not c.covers("1100")


def test_parse_markdown_coupons_only_titles_and_unusable():
    coupons = coupon_mod.parse_markdown_coupons(
        mcp_client.fetch_available_coupons(), "available"
    )
    titles = [c.title for c in coupons]
    assert "中薯条买一送一" in titles
    assert "9.9元中杯冰美式" in titles
    assert len(coupons) == 6
    # Markdown 券拿不到 couponCode → 不能算价
    assert all(not c.usable and not c.has_code for c in coupons)


def test_parse_trade_datetime_splits_single_string():
    start, end = coupon_mod.parse_trade_datetime(
        "2026-10-07 00:00:00-2026-11-06 23:59:59"
    )
    assert start == "2026-10-07 00:00:00"
    assert end == "2026-11-06 23:59:59"
    assert coupon_mod.parse_trade_datetime("") == (None, None)


def test_load_coupons_merges_sources_and_flags_usable():
    coupons = coupon_mod.load_coupons()
    assert len(coupons) >= 7
    usable = coupon_mod.usable_coupons(coupons)
    assert len(usable) == 1
    assert usable[0].source == "store"


# --------------------------------------------------------------------------- #
# MCP 客户端
# --------------------------------------------------------------------------- #
def test_demo_client_reads_fixtures_and_reports_mode():
    client = mcp_client.get_client(demo=True, reset=True)
    assert client.mode == "demo"
    assert mcp_client.mcp_status(demo=True) == "demo"
    assert "data" in mcp_client.fetch_nutrition(client)


def test_demo_client_unknown_tool_raises():
    client = mcp_client.DemoMCPClient()
    with pytest.raises(mcp_client.MCPToolError):
        client.call_tool("not-a-real-tool", {})


def test_calculate_price_requires_coupon_pair():
    """§3 坑 3：couponId 与 couponCode 必须成对，否则整个算价请求失败。"""
    with pytest.raises(mcp_client.MCPToolError):
        mcp_client.calculate_price(
            [{"productCode": "515837", "quantity": 1, "couponId": "X"}]
        )
    # 成对时正常返回（fixture 为 price: 0 的成功样本）
    result = mcp_client.calculate_price(
        [{"productCode": "515837", "quantity": 1, "couponId": "X", "couponCode": "Y"}]
    )
    assert result["success"] is True
    assert result["data"]["price"] == 0


def test_default_mode_is_demo_without_env(monkeypatch):
    """无 --demo、无环境变量时也必须能跑（默认离线）。"""
    monkeypatch.delenv("MCD_MODE", raising=False)
    monkeypatch.delenv("MCD_MCP_COMMAND", raising=False)
    assert mcp_client.resolve_mode(None) == "demo"
    assert mcp_client.resolve_mode(True) == "demo"
    assert mcp_client.resolve_mode(False) == "live"


def test_live_client_without_command_raises_unavailable(monkeypatch):
    monkeypatch.delenv("MCD_MCP_COMMAND", raising=False)
    with pytest.raises(mcp_client.MCPUnavailable):
        mcp_client.LiveMCPClient()
