"""订单层：把一次「组合技」抽卡结果落成一张可回看的订单。

职责
----
1. 组装 ``create-order`` 入参（单品编码 + 取餐方式）；
2. 把 MCP 响应 + 本次推荐结果归一化成 :class:`Order`；
3. 本地持久化到 ``data/orders.json``（demo 模式），供「订单列表」回看。

安全边界（重要）
----------------
- **demo 模式**：只读本地骨架 + 只写本地 JSON，**永远不产生真实订单**；
- **live 模式**：真实下单由 :func:`mcp_client.create_order` 把关，
  必须先设置 ``MCD_ALLOW_REAL_ORDER=1``，默认拦截。

金额口径
--------
订单的应付金额取自**本次推荐结果**（即用户在确认页看到的价格），
而不是 MCP 响应里的金额 —— 避免"页面显示一个价、订单记另一个价"。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from services import mcp_client
from services.recommender import Combo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ORDERS_PATH = ROOT / "data" / "orders.json"

#: 单次列表最多返回多少张订单（防止文件越攒越大拖慢页面）
DEFAULT_PAGE_LIMIT = 50


class OrderError(RuntimeError):
    """订单操作失败（重复订单号 / 数据损坏等）。"""


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #
@dataclass
class OrderLine:
    """订单里的一行商品。"""

    product_code: str
    name: str
    price: Decimal
    quantity: int = 1
    calories: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_code": self.product_code,
            "name": self.name,
            "price": str(self.price),
            "quantity": self.quantity,
            "calories": round(self.calories, 1),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OrderLine:
        return cls(
            product_code=str(d.get("product_code") or ""),
            name=str(d.get("name") or ""),
            price=Decimal(str(d.get("price") or "0")),
            quantity=int(d.get("quantity") or 1),
            calories=float(d.get("calories") or 0.0),
        )


@dataclass
class Order:
    """一张已落库的订单。"""

    order_id: str
    created_at: str                 # "2026-10-15 14:23:17"
    lines: list[OrderLine]
    original_price: Decimal         # 单点原价之和
    pay_amount: Decimal             # 实际支付（组合技价）
    store_code: str = mcp_client.DEMO_STORE_CODE
    store_name: str = mcp_client.DEMO_STORE_NAME
    take_way_code: str = "take-in-store"
    take_way_label: str = "外带"
    status: str = "待取餐"
    # 抽卡上下文（保留稀有度，订单列表里能看出"这是抽出来的哪张卡"）
    rarity: str = "N"
    score: float = 0.0
    skill_name: str = ""
    setmeal_id: str | None = None
    setmeal_name: str | None = None
    setmeal_ratio_pct: float = 0.0
    coupon_ids: list[str] = field(default_factory=list)
    # 溯源
    source: str = "demo"            # demo / live
    trace_id: str = ""

    # ---- 派生 ----
    @property
    def saved(self) -> Decimal:
        return self.original_price - self.pay_amount

    @property
    def saved_pct(self) -> float:
        if self.original_price <= 0:
            return 0.0
        return float(self.saved / self.original_price)

    @property
    def total_calories(self) -> float:
        return sum(l.calories * l.quantity for l in self.lines)

    @property
    def item_count(self) -> int:
        return sum(l.quantity for l in self.lines)

    @property
    def is_demo(self) -> bool:
        return self.source != "live"

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "created_at": self.created_at,
            "store_code": self.store_code,
            "store_name": self.store_name,
            "take_way_code": self.take_way_code,
            "take_way_label": self.take_way_label,
            "status": self.status,
            "lines": [l.to_dict() for l in self.lines],
            "original_price": str(self.original_price),
            "pay_amount": str(self.pay_amount),
            "saved": str(self.saved),
            "saved_pct": round(self.saved_pct, 4),
            "total_calories": round(self.total_calories, 1),
            "item_count": self.item_count,
            "rarity": self.rarity,
            "score": round(self.score, 1),
            "skill_name": self.skill_name,
            "setmeal_id": self.setmeal_id,
            "setmeal_name": self.setmeal_name,
            "setmeal_ratio_pct": round(self.setmeal_ratio_pct, 1),
            "coupon_ids": self.coupon_ids,
            "source": self.source,
            "is_demo": self.is_demo,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Order:
        return cls(
            order_id=str(d.get("order_id") or ""),
            created_at=str(d.get("created_at") or ""),
            lines=[OrderLine.from_dict(x) for x in (d.get("lines") or [])],
            original_price=Decimal(str(d.get("original_price") or "0")),
            pay_amount=Decimal(str(d.get("pay_amount") or "0")),
            store_code=str(d.get("store_code") or ""),
            store_name=str(d.get("store_name") or ""),
            take_way_code=str(d.get("take_way_code") or ""),
            take_way_label=str(d.get("take_way_label") or ""),
            status=str(d.get("status") or ""),
            rarity=str(d.get("rarity") or "N"),
            score=float(d.get("score") or 0.0),
            skill_name=str(d.get("skill_name") or ""),
            setmeal_id=d.get("setmeal_id"),
            setmeal_name=d.get("setmeal_name"),
            setmeal_ratio_pct=float(d.get("setmeal_ratio_pct") or 0.0),
            coupon_ids=list(d.get("coupon_ids") or []),
            source=str(d.get("source") or "demo"),
            trace_id=str(d.get("trace_id") or ""),
        )


# --------------------------------------------------------------------------- #
# 本地持久化（demo）
# --------------------------------------------------------------------------- #
class OrderStore:
    """``data/orders.json`` 的读写。

    写入是**原子**的（先写 ``.tmp`` 再 ``os.replace``），中途崩溃不会留下
    半截文件 —— 订单是用户唯一会真正在意"丢了没有"的数据。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DEFAULT_ORDERS_PATH)

    # ---- 读 ----
    def load(self) -> list[Order]:
        """读全部订单。文件缺失 / 损坏一律视为空，不抛栈。"""
        if not self.path.exists():
            return []
        try:
            with self.path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return []
        rows = payload.get("orders") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        out: list[Order] = []
        for row in rows:
            if isinstance(row, dict) and row.get("order_id"):
                try:
                    out.append(Order.from_dict(row))
                except (TypeError, ValueError):
                    continue          # 单条坏数据不该拖垮整张列表
        return out

    def list(self, limit: int | None = DEFAULT_PAGE_LIMIT) -> list[Order]:
        """按时间**倒序**返回（最新在前）。

        排序键用 ``(created_at, order_id)``：同一秒内下的多笔订单
        ``created_at`` 完全相同，只按时间排会退化成"插入顺序"（Python 排序稳定），
        看起来像旧的排在了前面。订单号尾部带自增序号，正好用作次级键。
        """
        orders = sorted(self.load(), key=lambda o: (o.created_at, o.order_id), reverse=True)
        return orders[:limit] if limit else orders

    def get(self, order_id: str) -> Order | None:
        for order in self.load():
            if order.order_id == order_id:
                return order
        return None

    def has(self, order_id: str) -> bool:
        return self.get(order_id) is not None

    # ---- 写 ----
    def save(self, orders: list[Order]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_meta": {
                "note": "本地演示订单。demo 模式下的下单只落盘到这里，"
                        "不会产生任何真实交易。",
                "count": len(orders),
            },
            "orders": [o.to_dict() for o in orders],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)          # 原子替换

    def add(self, order: Order) -> Order:
        """追加一张订单。订单号重复视为错误（宁可报错也不要静默覆盖）。"""
        orders = self.load()
        if any(o.order_id == order.order_id for o in orders):
            raise OrderError(f"订单号已存在：{order.order_id}")
        orders.append(order)
        self.save(orders)
        return order

    def clear(self) -> None:
        self.save([])


# --------------------------------------------------------------------------- #
# 下单
# --------------------------------------------------------------------------- #
def order_from_combo(
    combo: Combo,
    order_id: str,
    created_at: str,
    *,
    take_way_code: str = "take-in-store",
    take_way_label: str = "外带",
    store_code: str = mcp_client.DEMO_STORE_CODE,
    store_name: str = mcp_client.DEMO_STORE_NAME,
    status: str = "待取餐",
    source: str = "demo",
    trace_id: str = "",
) -> Order:
    """把推荐结果 + MCP 响应归一化成 :class:`Order`（纯函数，便于测试）。"""
    return Order(
        order_id=str(order_id),
        created_at=created_at,
        lines=[
            OrderLine(
                product_code=it.id,
                name=it.name,
                price=it.price,
                quantity=1,
                calories=it.calories,
            )
            for it in combo.items
        ],
        original_price=combo.total_price,
        pay_amount=combo.combo_price,
        store_code=store_code,
        store_name=store_name,
        take_way_code=take_way_code,
        take_way_label=take_way_label,
        status=status,
        rarity=combo.rarity,
        score=combo.score,
        skill_name=combo.skill_name or combo.name,
        setmeal_id=combo.setmeal_id,
        setmeal_name=combo.setmeal_name,
        setmeal_ratio_pct=combo.setmeal_ratio * 100.0,
        coupon_ids=list(combo.coupon_ids),
        source=source,
        trace_id=trace_id,
    )


def place_order(
    combo: Combo,
    take_way_code: str = "take-in-store",
    *,
    store_code: str = mcp_client.DEMO_STORE_CODE,
    store: OrderStore | None = None,
    client=None,
) -> Order:
    """下单并落库，返回 :class:`Order`。

    抛 :class:`mcp_client.MCPToolError` / :class:`mcp_client.WriteBlocked`
    / :class:`OrderError`，由 Web 层翻译成用户可读的提示。
    """
    store = store or OrderStore()

    # 取餐方式必须先校验：``create-order`` 的 ``takeWayCode`` 只接受
    # ``takeWayList`` 里的取值，传别的会被接口拒绝。这里提前拦下，
    # 避免把「非法但静默回落」的订单写进本地库。
    # 空值仍交给 ``create_order`` 报错（错误信息更贴近接口约定）。
    ways = {w["code"]: w for w in mcp_client.take_way_options(client=client)}
    code = str(take_way_code or "").strip()
    if code and code not in ways:
        raise OrderError(
            "取餐方式不合法：" + code + "（可选：" + "、".join(ways) + "）"
        )
    way = ways.get(code, {})

    # 入参严格按接口约定：calculate-price / create-order 的 items 只认
    # productCode + quantity（+ 券），不要塞额外字段。
    payload_items = [{"productCode": it.id, "quantity": 1} for it in combo.items]

    resp = mcp_client.create_order(
        payload_items,
        code,
        client=client,
        store_code=store_code,
    )

    data = (resp or {}).get("data") or {}
    order_id = str(data.get("orderId") or "")
    if not order_id:
        raise OrderError("下单接口未返回订单号")

    order = order_from_combo(
        combo,
        order_id=order_id,
        created_at=str(data.get("createTime") or ""),
        take_way_code=code,
        take_way_label=str(way.get("title") or code),
        store_code=str(data.get("storeCode") or store_code),
        store_name=str(data.get("storeName") or mcp_client.fetch_store_name(store_code)),
        status=str(data.get("orderStatus") or "待取餐"),
        source=str(getattr(client or mcp_client.get_client(), "mode", "demo")),
        trace_id=str((resp or {}).get("traceId") or ""),
    )
    return store.add(order)
