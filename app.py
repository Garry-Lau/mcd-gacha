"""mcd-gacha · Flask 应用入口。

一行跑通（离线、无需任何环境变量）::

    pip install -r requirements.txt && python app.py --demo

路由
----
- ``GET  /``                表单页（身高/体重/性别/年龄/活动系数/门店城市/餐次）
- ``POST /profile``         写入 session 后跳转 ``/gacha``
- ``GET  /gacha``           抽卡主界面（热量预算 + 优惠券 + 卡池）
- ``POST /api/recommend``   接收勾选单品 id → 契合度 ≥ 50 的组合 + 补齐建议
- ``GET  /order/new``       下单确认页（对结果卡上的商品直接算一次组合技）
- ``POST /api/order``       下单（demo 只落本地；live 需显式开关）
- ``GET  /orders``          订单列表（抽卡成功后的结果留档）
- ``GET  /api/coupons``     当前券列表
- ``GET  /health``          健康检查
"""
from __future__ import annotations

import argparse
import os

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from services import coupon as coupon_mod
from services import mcp_client, nutrition
from services import order as order_mod
from services.health import MockHealthProvider
from services.profile import (
    ACTIVITY_LABELS,
    MEAL_SHARE_CHOICES,
    MEAL_SHARE_LABELS,
    UserProfile,
    build_target,
)
from services.recommender import (
    MAX_INPUT_ITEMS,
    MIN_INPUT_ITEMS,
    RARITY_LABELS,
    RecommendError,
    recommend,
    resolve_combo,
    suggest_upgrades,
)

app = Flask(__name__)
#: 仅用于 demo 的 session 签名；生产部署请从环境变量注入。
app.secret_key = os.environ.get("MCD_SECRET_KEY", "mcd-gacha-demo-secret")

# --------------------------------------------------------------------------- #
# 数据缓存：卡池/套餐/券在一次运行内不变，避免每次请求都重新解析 fixture
# --------------------------------------------------------------------------- #
_cache: dict[str, object] = {}


def get_data() -> dict:
    """加载并缓存卡池、官方套餐、优惠券。"""
    if "catalog" not in _cache:
        catalog = nutrition.load_catalog()
        _cache["catalog"] = catalog
        _cache["by_id"] = {i.id: i for i in catalog}
        _cache["set_meals"] = nutrition.load_set_meals(catalog=catalog)
        _cache["coupons"] = coupon_mod.load_coupons()
    return _cache


def current_profile() -> UserProfile:
    """从 session 取画像；没有就用默认值（保证直接访问 /gacha 也不崩）。"""
    data = session.get("profile")
    return UserProfile(**data) if isinstance(data, dict) else UserProfile()


def current_budget():
    """按 session 画像 + 手表（mock）数据算出热量预算。"""
    profile = current_profile()
    burns = MockHealthProvider().get_daily_burn(7)
    return profile, build_target(profile, burns), burns


def parse_ids(raw: object) -> list[str]:
    """把 ``"1100,9900008751"`` 或 ``["1100", ...]`` 统一成 id 列表（保序去重）。"""
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = []
    out: list[str] = []
    for p in parts:
        p = str(p).strip()
        if p and p not in out:
            out.append(p)
    return out


def resolve_selection(ids: list[str]) -> tuple[list, str | None]:
    """按 id 取出单品；返回 ``(items, 错误信息)``。

    下单页与下单接口共用，保证「页面看到的」与「下单买到的」是同一批商品。
    """
    if not ids:
        return [], "缺少商品参数，请回到抽卡页重新选择。"
    by_id = get_data()["by_id"]
    missing = [i for i in ids if i not in by_id]
    if missing:
        return [], f"这些商品不在当前门店菜单里：{'、'.join(missing)}"
    return [by_id[i] for i in ids], None


@app.context_processor
def inject_safety_flags():
    """所有模板都能拿到「是否可能产生真实订单」，用于渲染安全角标。"""
    return {"real_order_allowed": mcp_client.real_order_allowed()}


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    data = get_data()
    return render_template(
        "index.html",
        profile=current_profile(),
        activities=ACTIVITY_LABELS,
        meal_shares=MEAL_SHARE_LABELS,
        catalog_size=len(data["catalog"]),
        setmeal_size=len(data["set_meals"]),
        coupon_size=len(data["coupons"]),
    )


@app.post("/profile")
def post_profile():
    profile = UserProfile.from_form(request.form)
    session["profile"] = profile.to_dict_for_session()
    return redirect(url_for("gacha"))


@app.get("/gacha")
def gacha():
    data = get_data()
    profile, budget, burns = current_budget()

    grouped: dict[str, list] = {c: [] for c in nutrition.CATEGORIES}
    for item in data["catalog"]:
        grouped.setdefault(item.category, []).append(item)

    return render_template(
        "gacha.html",
        profile=profile,
        budget=budget,
        burns=burns,
        grouped=grouped,
        categories=nutrition.CATEGORIES,
        set_meals=data["set_meals"],
        coupons=data["coupons"],
        min_items=MIN_INPUT_ITEMS,
        max_items=MAX_INPUT_ITEMS,
        rarity_labels=RARITY_LABELS,
    )


# --------------------------------------------------------------------------- #
# 下单
# --------------------------------------------------------------------------- #
@app.get("/order/new")
def order_new():
    """下单确认页。

    结果卡上展示的商品 = 用户要买的全部商品，所以这里对**该完整集合**
    直接算一次组合技（:func:`resolve_combo`），不枚举子集。

    查询串 ``?ids=1100,9900008751&sm=9900005466``：
    ``ids`` 必传；``sm`` 是卡片上命中的套餐 id，仅作**意图留痕**——
    服务端不据此查表，而是重算一遍（重算才是权威），避免用户手改 URL
    就把 A 套餐的价格套到 B 套餐的商品上。
    """
    ids = parse_ids(request.args.get("ids"))
    items, err = resolve_selection(ids)

    combo = None
    if err is None:
        _profile, budget, _burns = current_budget()
        data = get_data()
        try:
            combo = resolve_combo(
                items, budget.meal_target, data["coupons"], data["set_meals"]
            )
        except RecommendError as exc:
            err = str(exc)

    return (
        render_template(
            "order_confirm.html",
            ids=ids,
            combo=combo,
            error=err,
            take_ways=mcp_client.take_way_options(),
            store_code=mcp_client.DEMO_STORE_CODE,
            store_name=mcp_client.fetch_store_name(),
            rarity_labels=RARITY_LABELS,
        ),
        200 if err is None else 400,
    )


@app.post("/api/order")
def api_order():
    """下单。

    demo 模式：只写本地 ``data/orders.json``，**不产生任何真实订单**。
    live 模式：真实下单需 ``MCD_ALLOW_REAL_ORDER=1``，否则返回 403 与原因。
    """
    payload = request.get_json(silent=True) or {}
    ids = parse_ids(payload.get("ids"))
    take_way_code = str(payload.get("take_way_code") or "take-in-store").strip()

    items, err = resolve_selection(ids)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    _profile, budget, _burns = current_budget()
    data = get_data()
    try:
        combo = resolve_combo(
            items, budget.meal_target, data["coupons"], data["set_meals"]
        )
    except RecommendError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        order = order_mod.place_order(combo, take_way_code)
    except mcp_client.WriteBlocked as exc:
        # 真实下单被安全闸拦下：不是错误，是保护
        return jsonify({"ok": False, "blocked": True, "error": str(exc)}), 403
    except (mcp_client.MCPToolError, mcp_client.MCPUnavailable, order_mod.OrderError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(
        {"ok": True, "order": order.to_dict(), "redirect": url_for("orders")}
    )


@app.get("/orders")
def orders():
    """订单列表：抽卡成功后落库的结果都在这儿。"""
    store = order_mod.OrderStore()
    rows = store.list()

    # live 模式下额外展示账号的**真实**历史订单（只读工具 order-list）
    real_orders: list[dict] = []
    if mcp_client.mcp_status() == "connected":
        try:
            payload = mcp_client.fetch_order_list()
            real_orders = ((payload or {}).get("data") or {}).get("list") or []
        except Exception:      # noqa: BLE001 - 真实订单取不到不该让页面 500
            real_orders = []

    return render_template(
        "orders.html",
        orders=rows,
        real_orders=real_orders,
        rarity_labels=RARITY_LABELS,
        orders_path=str(order_mod.DEFAULT_ORDERS_PATH),
    )


@app.get("/api/orders")
def api_orders():
    return jsonify({"ok": True, "orders": [o.to_dict() for o in order_mod.OrderStore().list()]})


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.post("/api/recommend")
def api_recommend():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        return jsonify({"ok": False, "error": "ids 必须是数组"}), 400

    data = get_data()
    by_id = data["by_id"]
    items = [by_id[i] for i in ids if i in by_id]
    if len(items) != len(ids):
        missing = [i for i in ids if i not in by_id]
        return jsonify({"ok": False, "error": f"未知单品 id：{missing}"}), 400

    profile, budget, _ = current_budget()

    try:
        results = recommend(
            items,
            budget.meal_target,
            data["coupons"],
            data["set_meals"],
            limit=int(payload.get("limit") or 5),
        )
    except RecommendError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    upgrades = suggest_upgrades(
        items, data["set_meals"], by_id, budget.meal_target, data["coupons"], limit=3
    )

    return jsonify(
        {
            "ok": True,
            "budget": budget.to_dict(),
            "target": round(budget.meal_target, 1),
            "results": [c.to_dict() for c in results],
            "upgrades": [c.to_dict() for c in upgrades],
            "selected_count": len(items),
        }
    )


@app.get("/api/coupons")
def api_coupons():
    return jsonify({"ok": True, "coupons": [c.to_dict() for c in get_data()["coupons"]]})


@app.get("/api/catalog")
def api_catalog():
    data = get_data()
    return jsonify(
        {
            "ok": True,
            "items": [i.__dict__ | {"price": str(i.price)} for i in data["catalog"]],
        }
    )


@app.get("/health")
def health():
    from services import scheduler as scheduler_mod

    running = scheduler_mod.is_running()
    return jsonify(
        {
            "status": "ok",
            "mcp": mcp_client.mcp_status(),
            "scheduler": "running" if running else "stopped",
            "next_run": scheduler_mod.next_run_time(),
            "real_order_allowed": mcp_client.real_order_allowed(),
        }
    )


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="mcd-gacha · 麦当劳抽卡推荐")
    parser.add_argument("--demo", action="store_true", help="离线 demo 模式（默认）")
    parser.add_argument("--live", action="store_true", help="连接真实 MCP 服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-reload", action="store_true", help="关闭热重载")
    args = parser.parse_args()

    demo = not args.live
    if args.demo:
        demo = True

    # 预热缓存 + 初始化 MCP 客户端模式（demo 下不联网）
    mcp_client.get_client(demo=demo, reset=True)
    get_data()

    from services.scheduler import start_scheduler

    # ⚠️ Flask 热重载会让模块执行两次 → 定时任务重复启动。
    #    start_scheduler(use_reloader=...) 内部据此判定「当前进程是否该持有调度器」：
    #      · 关热重载 → 只有一个进程，直接启动；
    #      · 开热重载 → 父进程放弃，只有工作子进程（WERKZEUG_RUN_MAIN=true）启动。
    use_reloader = not args.no_reload
    scheduler = start_scheduler(use_reloader=use_reloader)
    sched_state = "running" if scheduler is not None else "delegated"

    print(f"[mcd-gacha] 模式 = {mcp_client.get_client(demo=demo).mode}  "
          f"定时任务 = {sched_state}  "
          f"http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=use_reloader)


if __name__ == "__main__":
    main()
