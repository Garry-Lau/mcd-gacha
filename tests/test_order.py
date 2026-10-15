"""订单层测试：持久化、归一化、demo 下单、live 安全闸。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from services import mcp_client
from services import nutrition, order as order_mod
from services.order import Order, OrderLine, OrderStore, order_from_combo, place_order
from services.recommender import resolve_combo


@pytest.fixture
def demo_client():
    """返回明确 demo 模式的 MCP 客户端。"""
    return mcp_client.get_client(demo=True, reset=True)


@pytest.fixture
def test_store(tmp_path):
    """每次测试独立的订单文件。"""
    return OrderStore(path=tmp_path / "orders.json")


@pytest.fixture
def big_mac_combo(demo_client):
    """巨无霸 + 可乐的组合（66.7% 套餐匹配度 SSR）。"""
    catalog = nutrition.load_catalog()
    by_id = {i.id: i for i in catalog}
    items = [by_id["1100"], by_id["9900008751"]]
    set_meals = nutrition.load_set_meals(catalog=catalog)
    return resolve_combo(items, 850.0, [], set_meals)


# --------------------------------------------------------------------------- #
# OrderLine / Order 归一化
# --------------------------------------------------------------------------- #
def test_order_line_to_dict_roundtrip():
    line = OrderLine(product_code="1100", name="巨无霸", price=Decimal("25.50"), quantity=1, calories=513.0)
    d = line.to_dict()
    restored = OrderLine.from_dict(d)
    assert restored.name == "巨无霸"
    assert restored.price == Decimal("25.50")


def test_order_saved_and_saved_pct():
    order = Order(
        order_id="DEMO001",
        created_at="2026-10-15 10:00:00",
        lines=[],
        original_price=Decimal("35.00"),
        pay_amount=Decimal("26.67"),
    )
    assert order.saved == Decimal("8.33")
    assert order.saved_pct == pytest.approx(8.33 / 35.0, abs=0.01)


# --------------------------------------------------------------------------- #
# OrderStore 持久化
# --------------------------------------------------------------------------- #
def test_store_roundtrip_and_sorting(test_store):
    """写入、读回、列表倒序，含同一秒多笔订单的排序。"""
    ts = "2026-10-15 10:00:00"
    for i in range(1, 4):
        test_store.add(
            Order(
                order_id=f"DEMO20261015100000{i:03d}",
                created_at=ts,
                lines=[OrderLine("1100", "巨无霸", Decimal("25.5"), 1, 513.0)],
                original_price=Decimal("35.00"),
                pay_amount=Decimal("26.67"),
            )
        )
    all_orders = test_store.list(limit=None)
    assert len(all_orders) == 3
    assert [o.order_id for o in all_orders] == ["DEMO20261015100000003", "DEMO20261015100000002", "DEMO20261015100000001"]


def test_store_rejects_duplicate_order_id(test_store):
    order = Order(order_id="SAME", created_at="2026-10-15 10:00:00", lines=[], original_price=Decimal("0"), pay_amount=Decimal("0"))
    test_store.add(order)
    with pytest.raises(order_mod.OrderError):
        test_store.add(order)


def test_store_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text("{ not json", encoding="utf-8")
    assert OrderStore(path=path).list() == []


def test_store_file_is_json_readable(test_store):
    order = Order(
        order_id="DEMO001",
        created_at="2026-10-15 10:00:00",
        lines=[OrderLine("1100", "巨无霸", Decimal("25.5"), 1, 513.0)],
        original_price=Decimal("35.00"),
        pay_amount=Decimal("26.67"),
        rarity="SSR",
    )
    test_store.add(order)
    payload = json.loads(test_store.path.read_text(encoding="utf-8"))
    assert payload["_meta"]["count"] == 1
    assert payload["orders"][0]["order_id"] == "DEMO001"


# --------------------------------------------------------------------------- #
# 下单
# --------------------------------------------------------------------------- #
def test_place_order_demo_writes_local(big_mac_combo, test_store, demo_client):
    order = place_order(big_mac_combo, "take-in-store", store=test_store, client=demo_client)
    assert order.order_id.startswith("DEMO")
    assert order.is_demo is True
    assert order.source == "demo"
    assert order.store_name == mcp_client.DEMO_STORE_NAME
    assert order.take_way_label == "外带"
    assert order.pay_amount == big_mac_combo.combo_price
    assert order.saved == big_mac_combo.saved
    assert order.rarity == big_mac_combo.rarity

    # 确实落盘
    assert test_store.get(order.order_id) is not None


def test_place_order_remembers_take_way(big_mac_combo, test_store, demo_client):
    order = place_order(big_mac_combo, "eat-in", store=test_store, client=demo_client)
    assert order.take_way_code == "eat-in"
    assert order.take_way_label == "堂食"


def test_place_order_preserves_combo_lines(big_mac_combo, test_store, demo_client):
    order = place_order(big_mac_combo, "take-in-store", store=test_store, client=demo_client)
    names = [l.name for l in order.lines]
    assert names == ["巨无霸", "可乐"]
    assert order.total_calories == pytest.approx(660.0, abs=1.0)


def test_place_order_empty_items_blocked(big_mac_combo, test_store, demo_client):
    combo = big_mac_combo
    combo.items = []
    with pytest.raises(mcp_client.MCPToolError, match="至少"):
        place_order(combo, "take-in-store", store=test_store, client=demo_client)


def test_place_order_missing_take_way_blocked(big_mac_combo, test_store, demo_client):
    with pytest.raises(mcp_client.MCPToolError, match="takeWayCode"):
        place_order(big_mac_combo, "", store=test_store, client=demo_client)


def test_place_order_invalid_take_way_blocked(big_mac_combo, test_store, demo_client):
    """非法取餐码必须被拦下，而不是静默落库成一条标签为乱码的订单。

    真实 ``create-order`` 只认 ``takeWayList`` 里的取值；若不前置校验，
    订单会被写进本地库且 take_way_label 直接回显成非法字符串。
    """
    with pytest.raises(order_mod.OrderError, match="取餐方式不合法"):
        place_order(big_mac_combo, "bogus-way", store=test_store, client=demo_client)
    assert test_store.list() == [], "非法请求不应产生任何本地订单记录"


# --------------------------------------------------------------------------- #
# live 安全闸
# --------------------------------------------------------------------------- #
class FakeLiveClient:
    """live 假客户端：只读工具放行，**任何写入工具都不允许被调用**。"""

    mode = "live"

    #: 只读工具白名单（下单前的取餐方式校验会用到 calculate-price）
    READ_ONLY = {"calculate-price", "query-nearby-stores", "now-time-info"}

    def call_tool(self, name, args=None):
        if name in self.READ_ONLY:
            return {
                "success": True,
                "code": 200,
                "data": {
                    "takeWayList": [
                        {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
                        {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"},
                    ]
                },
            }
        # 安全闸必须在写操作真正发出前触发
        raise AssertionError(f"未授权时不应调用写入工具 {name}")


def test_place_order_live_blocked_without_flag(big_mac_combo, test_store, monkeypatch):
    """MCD_ALLOW_REAL_ORDER 未置 1 时，live 模式真实下单必须被 WriteBlocked 拦截。"""
    monkeypatch.delenv("MCD_ALLOW_REAL_ORDER", raising=False)
    with pytest.raises(mcp_client.WriteBlocked, match="真实下单"):
        place_order(big_mac_combo, "take-in-store", store=test_store, client=FakeLiveClient())


def test_place_order_live_allowed_with_flag(big_mac_combo, test_store, monkeypatch):
    """MCD_ALLOW_REAL_ORDER=1 时，live 模式才放行。"""
    monkeypatch.setenv("MCD_ALLOW_REAL_ORDER", "1")

    class FakeLiveClient:
        mode = "live"
        @staticmethod
        def call_tool(name, args=None):
            return {
                "success": True,
                "code": 200,
                "traceId": "LIVE123",
                "data": {
                    "orderId": "LIVE20261015000001",
                    "createTime": "2026-10-15 12:00:00",
                    "storeCode": "1950970",
                    "storeName": "麦当劳北京中粮广场餐厅",
                    "takeWayCode": "take-in-store",
                    "orderStatus": "待取餐",
                },
            }

    order = place_order(big_mac_combo, "take-in-store", store=test_store, client=FakeLiveClient())
    assert order.order_id == "LIVE20261015000001"
    assert order.source == "live"
    assert order.is_demo is False
    assert order.trace_id == "LIVE123"
