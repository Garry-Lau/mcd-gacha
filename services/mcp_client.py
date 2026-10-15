"""MCP 调用的**唯一出口**（项目硬性约束 #2）。

其他任何模块（nutrition / coupon / recommender / app ...）都不允许自行发起
MCP 调用，必须通过本文件暴露的 `fetch_*` 便捷函数或 `get_client().call_tool()`。

两种运行模式
------------
- ``demo``（默认，``python app.py --demo`` 走这条路）
  完全离线：不联网、不读环境变量、不消费任何真实账户额度。
  直接读取阶段 0 真实抓取落盘的 ``data/fixtures/*.json``。
- ``live``
  通过 MCP stdio 服务器真实调用。需要环境变量 ``MCD_MCP_COMMAND``
  指向已授权的 mcd-mcp 启动命令（JSON 数组或普通命令行字符串）。
  未配置 / 依赖缺失时抛 :class:`MCPUnavailable`，由上层自动降级到 demo。

阶段 0 结论（详见 MCP_INTEGRATION.md）：
- 服务端**不返回 session-id**，会话由 MCP 传输层托管 → 无需每次 initialize；
- 因此本文件只做「单例连接 + 超时重试 1 次」，不做额外握手。
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "data" / "fixtures"
CACHE_DIR = ROOT / "data" / "cache"

#: demo 门店：北京中粮广场店。菜单随门店变化，demo 固定此门店。
DEMO_STORE_CODE = "1950970"
#: 门店名（来自 query-nearby-stores 的真实返回，按 storeCode 匹配）。
DEMO_STORE_NAME = "麦当劳北京中粮广场餐厅"
#: 到店自提。本账号配送地址为空，主链路锁定到店自提（见 MCP_INTEGRATION.md §4.11）
DEMO_ORDER_TYPE = 1
DEMO_BE_TYPE = 1


#: 真实下单总开关。live 模式下必须显式设置 ``MCD_ALLOW_REAL_ORDER=1``
#: 才允许调用 ``create-order`` —— 因为它是**唯一会花真钱**的工具。
#: 默认关闭：即使配好了 live 环境，忘记打开也只是报错，不会误下单。
ALLOW_REAL_ORDER_ENV = "MCD_ALLOW_REAL_ORDER"


class MCPToolError(RuntimeError):
    """工具调用返回业务失败（success=False）。"""


class MCPUnavailable(RuntimeError):
    """live 模式不可用（未配置命令 / 依赖缺失 / 连接失败）。"""


class WriteBlocked(RuntimeError):
    """被写操作安全闸拦下（live 未显式授权真实下单）。"""


#: demo 订单号自增序号，保证同一进程内不重复。
_demo_order_seq = itertools.count(1)


def gen_demo_order_id(now: datetime | None = None) -> str:
    """生成 demo 订单号，如 ``DEMO20261015142317001``。

    刻意做成"一眼看出是演示数据"的前缀 —— 即使被截图传播，
    也不会被误认为真实订单号。
    """
    now = now or datetime.now()
    return f"DEMO{now:%Y%m%d%H%M%S}{next(_demo_order_seq):03d}"


# --------------------------------------------------------------------------- #
# fixture 路由表：工具名 -> 文件
# --------------------------------------------------------------------------- #
_FIXTURE_FILES: dict[str, str] = {
    "list-nutrition-foods": "list-nutrition-foods.json",
    "query-meals": "query-meals.json",
    "query-meal-detail": "query-meal-detail.json",
    "query-store-coupons": "query-store-coupons.json",
    "available-coupons": "available-coupons.json",
    "query-my-coupons": "query-my-coupons.json",
    "query-nearby-stores": "query-nearby-stores.json",
    "now-time-info": "now-time-info.json",
    "query-my-account": "query-my-account.json",
    "query-lottery-info": "query-lottery-info.json",
    "mall-points-products": "mall-points-products.json",
    "campaign-calendar": "campaign-calendar.json",
    # calculate-price 没有独立 fixture：用「成对券成功」样本，
    # 缺 couponCode 的失败样本走 errors.json 记录（见 MCP_INTEGRATION.md §3 坑 3）
    "calculate-price": "calculate-price-with-coupon.json",
    # 订单（见 MCP_INTEGRATION.md §1.4）
    #   order-list    —— 真实抓取（订单号已脱敏）
    #   create-order  —— **合成骨架**，本项目从未真实调用（会真花钱）
    "order-list": "order-list.json",
    "create-order": "create-order.json",
}


class BaseMCPClient:
    """客户端抽象。"""

    mode: str = "base"

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    # 便于 /health 统一判断
    @property
    def connected(self) -> bool:
        return self.mode == "live"


class DemoMCPClient(BaseMCPClient):
    """离线模式：读取阶段 0 落盘的真实 fixture。"""

    mode = "demo"

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = Path(fixture_dir or FIXTURE_DIR)

    def _load(self, filename: str) -> dict[str, Any]:
        path = self.fixture_dir / filename
        if not path.exists():
            raise MCPToolError(f"fixture 缺失：{path}（请勿删除 data/fixtures/）")
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        if name not in _FIXTURE_FILES:
            raise MCPToolError(f"demo 模式未收录工具：{name}")

        args = args or {}
        if name == "query-meal-detail":
            return self._meal_detail(args.get("code"))
        if name == "create-order":
            return self._create_order(args)

        payload = self._load(_FIXTURE_FILES[name])
        payload.pop("_fixture_meta", None)

        # 统一约定：
        #   JSON 型工具 -> 直接返回 raw 包裹体（含 success/code/message/data）
        #   Markdown 型工具 -> 返回 {"_markdown": "..."}，由解析层识别
        if "raw" in payload:
            return payload["raw"]
        if "raw_markdown" in payload:
            return {"_markdown": payload["raw_markdown"]}
        return payload

    def _meal_detail(self, code: Any) -> Any:
        """按套餐编码路由到 ``query-meal-detail-<code>.json``。

        ⚠️ 回落时**必须校验回落文件里的 ``data.code`` 与请求一致**，
        否则会出现「请求 A 套餐、拿到 B 套餐组成」的张冠李戴，
        推荐算法会静默算错匹配度。
        """
        code = str(code or "").strip()

        if code:
            candidate = f"query-meal-detail-{code}.json"
            if (self.fixture_dir / candidate).exists():
                payload = self._load(candidate)
                payload.pop("_fixture_meta", None)
                return payload["raw"]

        legacy = self._load("query-meal-detail.json")
        legacy.pop("_fixture_meta", None)
        real_code = str(((legacy.get("raw") or {}).get("data") or {}).get("code") or "")
        if code and real_code != code:
            raise MCPToolError(
                f"demo 未抓取套餐 {code} 的详情（现有回落样本是 {real_code}）"
            )
        return legacy["raw"]

    # -- 下单（demo 模拟） -------------------------------------------------- #
    def _create_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """demo 下单：回放**合成骨架**并填入本次请求的动态字段。

        这里**不会**、也无法产生任何真实订单：`create-order.json` 是按接口
        结构合成的（真实下单会花钱，本项目从未调用过该工具）。

        动态填充的字段（其余字段来自骨架）：
        ``orderId`` / ``createTime`` / ``datetime`` / ``storeCode`` /
        ``takeWayCode`` / ``orderProductList``。
        金额不在这里算 —— 由业务层按**本次推荐结果的价格**记账，
        避免"响应里的钱"和"页面上展示的钱"不一致。
        """
        payload = self._load("create-order.json")
        payload.pop("_fixture_meta", None)
        raw = copy.deepcopy(payload["raw"])

        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        data = raw["data"]
        data["orderId"] = gen_demo_order_id(now)
        data["createTime"] = stamp
        raw["datetime"] = stamp
        data["storeCode"] = str(args.get("storeCode") or DEMO_STORE_CODE)
        data["storeName"] = fetch_store_name(str(data["storeCode"]))
        data["takeWayCode"] = str(args.get("takeWayCode") or "")
        data["orderProductList"] = [
            {
                "productCode": str(it.get("productCode") or ""),
                "productName": str(it.get("productName") or ""),
                "quantity": int(it.get("quantity") or 1),
                "comboItemList": [],
            }
            for it in (args.get("items") or [])
        ]
        return raw


class LiveMCPClient(BaseMCPClient):
    """真实模式：通过 MCP stdio 服务器调用。

    只有当环境变量 ``MCD_MCP_COMMAND`` 存在且 ``mcp`` 包可导入时才可用。
    实现刻意保持"薄"——真正的 MCP 交互只发生在这里，其他模块无感。
    """

    mode = "live"

    def __init__(self, command: str | None = None, timeout: float = 20.0) -> None:
        cmd = command or os.environ.get("MCD_MCP_COMMAND", "").strip()
        if not cmd:
            raise MCPUnavailable(
                "未设置 MCD_MCP_COMMAND，无法使用 live 模式。"
                "请配置已授权的 mcd-mcp 启动命令，或改用 --demo。"
            )
        self.command = cmd
        self.timeout = timeout
        self._argv = self._parse_command(cmd)
        try:
            import mcp  # noqa: F401  (探测依赖)
        except Exception as exc:  # pragma: no cover - 依赖缺失分支
            raise MCPUnavailable(
                "缺少 mcp 依赖。执行 `pip install mcp` 后重试，或改用 --demo。"
            ) from exc

    @staticmethod
    def _parse_command(command: str) -> list[str]:
        command = command.strip()
        if command.startswith("["):
            parsed = json.loads(command)
            if not isinstance(parsed, list):
                raise MCPUnavailable("MCD_MCP_COMMAND 的 JSON 形式必须是数组")
            return [str(x) for x in parsed]
        return shlex.split(command)

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        """真实调用。超时重试 1 次（阶段 0：无需手动 initialize）。"""
        import asyncio

        last_exc: Exception | None = None
        for _ in range(2):
            try:
                return asyncio.run(self._call_once(name, args or {}))
            except Exception as exc:  # pragma: no cover - 需真实 MCP 环境
                last_exc = exc
        raise MCPUnavailable(f"调用 {name} 失败：{last_exc}")

    async def _call_once(self, name: str, args: dict[str, Any]) -> Any:  # pragma: no cover
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=self._argv[0], args=self._argv[1:])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, args)
                for block in result.content:
                    text = getattr(block, "text", None)
                    if text:
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return {"_markdown": text}
                return {}


# --------------------------------------------------------------------------- #
# 单例与模式解析
# --------------------------------------------------------------------------- #
_client: BaseMCPClient | None = None


def resolve_mode(demo: bool | None = None) -> str:
    """决定运行模式。

    显式 ``--demo`` > 显式 ``--live``(demo=False) > 默认 demo。
    默认 demo 是刻意的：保证「无网络、无环境变量也能一行跑通」。
    """
    if demo is True:
        return "demo"
    if demo is False:
        return "live"
    env = os.environ.get("MCD_MODE", "").strip().lower()
    return "live" if env == "live" else "demo"


def get_client(demo: bool | None = None, *, reset: bool = False) -> BaseMCPClient:
    """获取全局单例客户端，live 不可用时**静默降级**到 demo。"""
    global _client
    if _client is not None and not reset:
        return _client

    if resolve_mode(demo) == "live":
        try:
            _client = LiveMCPClient()
            return _client
        except MCPUnavailable:
            # 降级：保证页面永远能跑起来，而不是抛栈给用户
            _client = DemoMCPClient()
            return _client

    _client = DemoMCPClient()
    return _client


def mcp_status(demo: bool | None = None) -> str:
    """供 /health 使用：connected（真实）/ demo（离线）。"""
    return "connected" if get_client(demo).mode == "live" else "demo"


def real_order_allowed(demo: bool | None = None) -> bool:
    """当前是否可能产生**真实订单**。

    只有 live 模式 + 显式 ``MCD_ALLOW_REAL_ORDER=1`` 才为真。
    页面据此决定是否显示"不会产生真实订单"的安全角标。
    """
    return get_client(demo).mode == "live" and os.environ.get(ALLOW_REAL_ORDER_ENV) == "1"


# --------------------------------------------------------------------------- #
# 便捷访问器：其他模块只认这些函数，不认 MCP 细节
# --------------------------------------------------------------------------- #
def fetch_nutrition(client: BaseMCPClient | None = None) -> Any:
    return (client or get_client()).call_tool("list-nutrition-foods", {})


def fetch_meals(
    client: BaseMCPClient | None = None,
    store_code: str = DEMO_STORE_CODE,
    order_type: int = DEMO_ORDER_TYPE,
    be_type: int = DEMO_BE_TYPE,
) -> Any:
    return (client or get_client()).call_tool(
        "query-meals",
        {"storeCode": store_code, "orderType": order_type, "beType": be_type},
    )


def fetch_meal_detail(
    code: str,
    client: BaseMCPClient | None = None,
    store_code: str = DEMO_STORE_CODE,
    order_type: int = DEMO_ORDER_TYPE,
    be_type: int = DEMO_BE_TYPE,
) -> Any:
    return (client or get_client()).call_tool(
        "query-meal-detail",
        {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "code": code,
        },
    )


def fetch_store_coupons(
    client: BaseMCPClient | None = None,
    store_code: str = DEMO_STORE_CODE,
    order_type: int = DEMO_ORDER_TYPE,
    be_type: int = DEMO_BE_TYPE,
) -> Any:
    """唯一的"三全"券接口：含 couponId + couponCode + 适用商品编码。

    可用性一律以此接口为准（`query-my-coupons` 不校验门店，见 §4.10）。
    """
    return (client or get_client()).call_tool(
        "query-store-coupons",
        {"storeCode": store_code, "orderType": order_type, "beType": be_type},
    )


def fetch_available_coupons(client: BaseMCPClient | None = None) -> Any:
    return (client or get_client()).call_tool("available-coupons", {})


def fetch_my_coupons(client: BaseMCPClient | None = None) -> Any:
    return (client or get_client()).call_tool("query-my-coupons", {})


def fetch_now_time(client: BaseMCPClient | None = None) -> Any:
    return (client or get_client()).call_tool("now-time-info", {})


def fetch_nearby_stores(client: BaseMCPClient | None = None) -> Any:
    """附近门店。demo 下返回阶段 0 抓取的真实快照。"""
    return (client or get_client()).call_tool("query-nearby-stores", {})


def fetch_store_name(
    store_code: str = DEMO_STORE_CODE, client: BaseMCPClient | None = None
) -> str:
    """按门店编码查名称。查不到返回空串（调用方自行兜底）。

    demo 门店直接常量返回，避免为了一个名字多走一次数据加载。
    """
    code = str(store_code or "").strip()
    if code == DEMO_STORE_CODE:
        return DEMO_STORE_NAME
    try:
        payload = fetch_nearby_stores(client)
    except Exception:          # noqa: BLE001 - 名称只是展示用，失败不该影响下单
        return ""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    for row in rows or []:
        if isinstance(row, dict) and str(row.get("storeCode") or "") == code:
            return str(row.get("storeName") or "")
    return ""


def take_way_options(client: BaseMCPClient | None = None) -> list[dict[str, str]]:
    """取餐方式选项（``eat-in`` 堂食 / ``take-in-store`` 外带）。

    来源：``calculate-price`` 返回的 ``data.takeWayList`` —— 这是
    ``create-order`` 的 ``takeWayCode`` 唯一合法取值来源（见 §3 坑 2）。
    """
    payload = calculate_price(
        [{"productCode": "515837", "quantity": 1}],       # 用最便宜的单品探路
        client=client,
    )
    rows = ((payload or {}).get("data") or {}).get("takeWayList") or []
    out: list[dict[str, str]] = []
    for row in rows:
        if isinstance(row, dict) and row.get("code"):
            out.append(
                {
                    "code": str(row["code"]),
                    "title": str(row.get("title") or row["code"]),
                    "subtitle": str(row.get("subtitle") or ""),
                }
            )
    if not out:       # fixture 异常时的兜底，保证页面永远有可选项
        out = [
            {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
            {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"},
        ]
    return out


def fetch_order_list(client: BaseMCPClient | None = None) -> Any:
    """真实历史订单列表（只读）。

    注意：这是**账号真实订单**，与 demo 本地模拟订单是两回事。
    demo 模式下返回阶段 0 之后的真实快照（供结构参考）。
    """
    return (client or get_client()).call_tool("order-list", {})


def create_order(
    items: list[dict[str, Any]],
    take_way_code: str,
    client: BaseMCPClient | None = None,
    store_code: str = DEMO_STORE_CODE,
    order_type: int = DEMO_ORDER_TYPE,
    be_type: int = DEMO_BE_TYPE,
) -> Any:
    """下单。

    ⚠️ **唯一会花真钱的工具。** 安全设计：

    - ``demo`` 模式：回放 ``create-order.json`` 合成骨架 + 客户端填充订单号，
      **不联网、不产生真实订单**；
    - ``live`` 模式：真实调用，但必须先设 ``MCD_ALLOW_REAL_ORDER=1``，
      否则抛 :class:`WriteBlocked`。默认关闭 —— 忘记打开只会报错，不会误下单。

    参数校验前置：``items`` 不能为空、``orderType=1`` 时 ``takeWayCode`` 必传
    （阶段 0 实测结论，见 errors.json 的 ``create-order-empty-items``）。
    """
    if not items:
        raise MCPToolError("下单至少需要 1 个商品（错误码 600010：检查必填参数）")
    if order_type == 1 and not str(take_way_code or "").strip():
        raise MCPToolError(
            "orderType=1（到店自提）时 takeWayCode 必传，"
            "取值来自 calculate-price 的 takeWayList（eat-in / take-in-store）"
        )

    client = client or get_client()
    if client.mode == "live" and os.environ.get(ALLOW_REAL_ORDER_ENV) != "1":
        raise WriteBlocked(
            "live 模式下真实下单会立即产生真实订单与费用，已默认拦截。"
            f"如确需下单，请显式设置 {ALLOW_REAL_ORDER_ENV}=1 后重试。"
        )

    return client.call_tool(
        "create-order",
        {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "takeWayCode": take_way_code,
            "items": items,
        },
    )


def calculate_price(
    items: list[dict[str, Any]],
    client: BaseMCPClient | None = None,
    store_code: str = DEMO_STORE_CODE,
    order_type: int = DEMO_ORDER_TYPE,
    be_type: int = DEMO_BE_TYPE,
) -> Any:
    """算价。

    注意（§3 坑 3）：``couponId`` 与 ``couponCode`` 必须**成对**出现，
    否则整个算价请求直接失败（不是降级为原价）。
    返回价格单位为**分**。
    """
    for it in items:
        has_id = bool(it.get("couponId"))
        has_code = bool(it.get("couponCode"))
        if has_id != has_code:
            raise MCPToolError(
                "使用优惠券时 couponId 与 couponCode 必须成对出现（错误码 600010）"
            )
    return (client or get_client()).call_tool(
        "calculate-price",
        {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "items": items,
        },
    )
