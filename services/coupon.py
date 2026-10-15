"""优惠券归一化。

阶段 0 的关键发现（MCP_INTEGRATION.md §2.3 / §3 坑 3 / §4.10）：

- ``query-store-coupons`` 是**唯一**能同时给出 ``couponId`` + ``couponCode`` +
  适用商品编码的接口 → 本模块的**主数据源**；
- ``available-coupons`` / ``query-my-coupons`` 返回 **Markdown**，只有券标题，
  **没有** couponId / couponCode / 门槛 / 优惠额；
- 券与门店/渠道存在匹配关系（卡包有券 ≠ 门店能用），因此可用性以
  ``query-store-coupons`` 为准。

因此 :class:`Coupon` 把「结构完整」与「仅标题」两种情况统一表达：缺失字段为
``None``，并保留 ``source`` 与 ``usable`` 标记，让上层能区分「能算价的券」和
「只用于展示的券」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from services import mcp_client


@dataclass
class Coupon:
    """归一化优惠券。"""

    id: str                       # couponId（Markdown 券不可得时用 title 生成 hash）
    title: str
    code: str | None = None       # couponCode，算价必需
    threshold: Decimal | None = None   # 门槛金额（元）
    discount: Decimal | None = None    # 优惠金额（元）
    applicable_items: list[str] = None  # 适用商品编码
    expire_at: str | None = None       # ISO 或原始字符串
    start_at: str | None = None
    source: str = "store"         # store / my / available
    usable: bool = True           # 是否可用于算价

    def __post_init__(self) -> None:
        if self.applicable_items is None:
            self.applicable_items = []

    @property
    def has_code(self) -> bool:
        """能参与真实算价的券必须 couponId + couponCode 成对。"""
        return bool(self.id and self.code)

    def covers(self, product_code: str) -> bool:
        return str(product_code) in {str(x) for x in self.applicable_items}

    def to_dict(self) -> dict[str, Any]:
        """给前端的 JSON 友好结构（金额转字符串，避免浮点误差）。"""
        return {
            "id": self.id,
            "title": self.title,
            "code": self.code,
            "threshold": str(self.threshold) if self.threshold is not None else None,
            "discount": str(self.discount) if self.discount is not None else None,
            "applicable_items": list(self.applicable_items),
            "expire_at": self.expire_at,
            "source": self.source,
            "usable": self.usable,
            "has_code": self.has_code,
        }


# --------------------------------------------------------------------------- #
# 解析：query-store-coupons（JSON，主数据源）
# --------------------------------------------------------------------------- #
def parse_trade_datetime(value: Any) -> tuple[str | None, str | None]:
    """``"2026-10-07 00:00:00-2026-11-06 23:59:59"`` -> (start, end)。

    阶段 0 note：这是一个**字符串**，不是两个字段，必须自己 split。
    注意日期本身就含 ``-``，所以用正则按 "日期 时间-日期 时间" 切分。
    """
    if not value or not isinstance(value, str):
        return None, None
    s = value.strip()
    m = re.match(
        r"\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?)\s*-\s*"
        r"(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?)\s*$",
        s,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, s or None


def parse_store_coupons(envelope: Any) -> list[Coupon]:
    """解析 ``query-store-coupons`` 返回。"""
    if isinstance(envelope, dict) and "data" in envelope:
        data = envelope["data"]
    else:
        data = envelope
    if not isinstance(data, list):
        return []

    out: list[Coupon] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        coupon_id = str(item.get("couponId") or "").strip()
        coupon_code = str(item.get("couponCode") or "").strip() or None
        if not coupon_id and not title:
            continue

        start_at, expire_at = parse_trade_datetime(item.get("tradeDateTime"))

        applicable = [
            str(p.get("productCode"))
            for p in (item.get("products") or [])
            if isinstance(p, dict) and p.get("productCode")
        ]

        threshold, discount = infer_amounts_from_title(title)

        out.append(
            Coupon(
                id=coupon_id or f"title::{title}",
                title=title or f"券 {coupon_id}",
                code=coupon_code,
                threshold=threshold,
                discount=discount,
                applicable_items=applicable,
                expire_at=expire_at,
                start_at=start_at,
                source="store",
                usable=bool(coupon_id and coupon_code),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 解析：Markdown 券接口
# --------------------------------------------------------------------------- #
_MD_TITLE_RE = re.compile(r"优惠券标题[:：]\s*(.+?)(?:\s*\\?\s*$|\s*\\$)", re.MULTILINE)
_MD_STATUS_RE = re.compile(r"状态[:：]\s*(.+?)(?:\s*\\?\s*$|\s*\\$)", re.MULTILINE)


def parse_markdown_coupons(envelope: Any, source: str = "available") -> list[Coupon]:
    """解析 Markdown 券列表，仅能拿到标题 + 状态。

    这两个接口**没有** couponId/couponCode，因此 ``usable=False``：
    只能用于「发现有哪些券可领」的展示，不能拿去算价。
    """
    if isinstance(envelope, dict):
        md = envelope.get("_markdown") or envelope.get("data") or ""
    else:
        md = envelope or ""
    if not isinstance(md, str) or not md.strip():
        return []

    # 以「优惠券标题：」为界切块，块内再取状态
    blocks = re.split(r"(?=优惠券标题[:：])", md)
    out: list[Coupon] = []
    for block in blocks:
        m = _MD_TITLE_RE.search(block)
        if not m:
            continue
        title = m.group(1).strip().rstrip("\\").strip()
        if not title:
            continue
        status_m = _MD_STATUS_RE.search(block)
        status = status_m.group(1).strip().rstrip("\\").strip() if status_m else ""
        threshold, discount = infer_amounts_from_title(title)
        out.append(
            Coupon(
                id=f"title::{title}",
                title=title,
                code=None,
                threshold=threshold,
                discount=discount,
                applicable_items=[],
                expire_at=None,
                source=source,
                usable=False,   # 无 couponCode，不能算价
            )
        )
        # 记录状态到 expire_at 之外无字段可用，语义上保留在标题后缀
        if status and status not in ("可领取", "可使用"):
            out[-1].title = f"{title}（{status}）"
    return out


# --------------------------------------------------------------------------- #
# 标题启发式：Markdown 券没有结构化金额，从标题里尽力推断
# --------------------------------------------------------------------------- #
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
_FREE_HINTS = ("免费", "赠", "送", "0元", "0 元")
_BOGO_HINTS = ("买一送一", "买1送1", "第二份", "第二杯", "1+1")


def infer_amounts_from_title(title: str) -> tuple[Decimal | None, Decimal | None]:
    """从券标题粗推 (门槛, 优惠额) 用于展示排序。

    这是**启发式**，不参与算价（算价一律以服务端 calculate-price 为准）。
    """
    if not title:
        return None, None
    t = title
    if any(h in t for h in _FREE_HINTS):
        return None, Decimal("0")   # 免费券：优惠额视为 0（即白拿）
    if any(h in t for h in _BOGO_HINTS):
        return None, None           # 买一送一：折扣额取决于单品价，无法静态推断
    m = _PRICE_RE.search(t)
    if m:
        # "9.9元中杯冰美式" -> 券后价 9.9，门槛与优惠额都无法静态确定
        return None, None
    return None, None


# --------------------------------------------------------------------------- #
# 汇总加载
# --------------------------------------------------------------------------- #
def load_coupons(
    client: mcp_client.BaseMCPClient | None = None,
    *,
    include_unusable: bool = True,
) -> list[Coupon]:
    """加载券列表。

    - 主源：``query-store-coupons``（可算价，usable=True，带 couponId/couponCode）
    - 辅源：``available-coupons``（仅标题，usable=False，用于「可领取」展示）

    返回去重后的列表：以 couponId / 标题为键。
    """
    client = client or mcp_client.get_client()
    result: list[Coupon] = []
    seen: set[str] = set()

    try:
        for c in parse_store_coupons(mcp_client.fetch_store_coupons(client)):
            if c.id not in seen:
                seen.add(c.id)
                result.append(c)
    except Exception:
        pass

    try:
        for c in parse_markdown_coupons(mcp_client.fetch_available_coupons(client), "available"):
            key = f"title::{c.title}"
            if key not in seen:
                seen.add(key)
                result.append(c)
    except Exception:
        pass

    if not include_unusable:
        result = [c for c in result if c.usable]
    return result


def usable_coupons(coupons: list[Coupon]) -> list[Coupon]:
    """能被算价使用的券（有 couponId + couponCode + 适用商品）。"""
    return [c for c in coupons if c.has_code]
