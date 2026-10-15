"""Web 层回归测试：路由、模板渲染、API 契约。

这类测试专门用来兜住「模板引用了不存在的属性 → Jinja2 静默渲染成空」
这一类不会报错、只会在页面上留下空白/空标签的缺陷。
"""
from __future__ import annotations

import re

import pytest

from app import app as flask_app

#: demo 下稳定存在的三个单品（data/fixtures/list-nutrition-foods.json）
BIG_MAC = "1100"          # 巨无霸 · 主食 · ¥25.5 · 513 kcal
COKE = "9900008751"       # 可乐   · 饮品 · ¥9.5  · 147 kcal
FRIES = "4810"            # 薯条   · 主食 · ¥13.5 · 289 kcal


@pytest.fixture()
def client():
    flask_app.config.update(TESTING=True)
    with flask_app.test_client() as c:
        yield c


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
def test_index_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "MCD" in html or "麦当劳" in html
    assert "身高" in html and "体重" in html


def test_gacha_ok(client):
    resp = client.get("/gacha")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "抽卡" in html
    # 卡池按四分类分组渲染
    assert html.count('data-id="') >= 30
    assert "官方套餐" in html
    assert "可用优惠券" in html


def test_gacha_renders_watch_average_with_demo_tag(client):
    """阶段 3 验收点：页面必须能看到「近 7 天日均消耗 XXX kcal（演示数据）」。"""
    html = client.get("/gacha").get_data(as_text=True)
    m = re.search(r"近\s*(\d+)\s*天日均消耗\s*<b>(\d+)</b>\s*kcal", html)
    assert m, "页面上找不到「近 N 天日均消耗 X kcal」"
    days, avg = int(m.group(1)), int(m.group(2))
    assert days >= 1
    assert 1000 <= avg <= 4000          # 落在 Mock 生成区间附近
    assert "演示数据" in html


def test_gacha_no_empty_definition_values(client):
    """兜底：预算/画像等 <dd> 不允许出现空值（曾因 basis_label 缺失而空白）。"""
    html = client.get("/gacha").get_data(as_text=True)
    empties = re.findall(r"<dd>\s*</dd>", html)
    assert empties == [], f"存在空的 <dd>：{empties}"
    # 专测当初踩坑的那一项
    assert "公式估算" in html or "智能手表实测" in html


def test_gacha_no_unrendered_jinja(client):
    html = client.get("/gacha").get_data(as_text=True)
    assert "{{" not in html and "{%" not in html


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_recommend_big_mac_plus_coke_reports_setmeal_ratio(client):
    """用户点名的核心场景：巨无霸 + 可乐 → 命中「巨无霸三件套」2/3 = 66.7%。"""
    resp = client.post("/api/recommend", json={"ids": [BIG_MAC, COKE]})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["selected_count"] == 2
    assert payload["results"], "至少要产出一条组合"

    top = payload["results"][0]
    assert 0.0 < top["setmeal_ratio"] <= 1.0
    assert top["setmeal_ratio_pct"] == round(top["setmeal_ratio"] * 100, 1)
    # 原价 vs 组合技价，必须能算出省了多少
    assert float(top["combo_price"]) <= float(top["original_price"])
    assert float(top["saved"]) == pytest.approx(
        float(top["original_price"]) - float(top["combo_price"]), abs=0.01
    )

    # 该场景应当命中一个两件套/三件套
    assert any(r["setmeal_name"] for r in payload["results"])


def test_recommend_results_are_sorted_desc(client):
    payload = client.post(
        "/api/recommend", json={"ids": [BIG_MAC, COKE, FRIES]}
    ).get_json()
    scores = [r["score"] for r in payload["results"]]
    assert scores == sorted(scores, reverse=True)


def test_recommend_all_results_above_floor(client):
    payload = client.post(
        "/api/recommend", json={"ids": [BIG_MAC, COKE, FRIES]}
    ).get_json()
    assert all(r["score"] >= 50.0 for r in payload["results"])


def test_recommend_rejects_unknown_id(client):
    resp = client.post("/api/recommend", json={"ids": [BIG_MAC, "no-such-id"]})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_recommend_rejects_too_few_items(client):
    resp = client.post("/api/recommend", json={"ids": [BIG_MAC]})
    assert resp.status_code == 400
    assert "至少勾选 2 件" in resp.get_json()["error"]


def test_recommend_rejects_non_list_ids(client):
    resp = client.post("/api/recommend", json={"ids": "1100"})
    assert resp.status_code == 400


def test_recommend_handles_empty_body(client):
    resp = client.post("/api/recommend", json={})
    assert resp.status_code == 400


def test_api_coupons_ok(client):
    payload = client.get("/api/coupons").get_json()
    assert payload["ok"] is True
    assert isinstance(payload["coupons"], list)


def test_api_catalog_ok(client):
    payload = client.get("/api/catalog").get_json()
    assert payload["ok"] is True
    assert len(payload["items"]) >= 30
    assert all("id" in i and "name" in i for i in payload["items"])


def test_health_endpoint(client):
    payload = client.get("/health").get_json()
    assert payload["status"] == "ok"
    assert payload["mcp"] in {"demo", "live"}
    assert payload["scheduler"] in {"running", "stopped"}
    assert "next_run" in payload
    assert isinstance(payload.get("real_order_allowed"), bool)


# --------------------------------------------------------------------------- #
# 下单与订单列表（新需求）
# --------------------------------------------------------------------------- #
def test_order_new_renders_combo(client):
    """点结果卡进入的下单确认页必须能重算组合技并渲染价格对比。"""
    resp = client.get(f"/order/new?ids={BIG_MAC},{COKE}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "确认下单" in html
    assert "套餐拼图·差一件达成" in html, "应重算并显示该组合技"
    assert "66.7" in html or "66.70" in html, "套餐匹配度需展示"
    assert "¥26.67" in html
    assert "外带" in html
    assert "演示模式" in html       # demo 模式安全提示


def test_order_new_errors_on_unknown_id(client):
    resp = client.get("/order/new?ids=not-a-real-id")
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    assert "不在当前门店菜单" in html


def test_api_order_creates_demo_order(client, tmp_path, monkeypatch):
    """POST /api/order 产生 demo 订单，只写临时文件，不向外发真实请求。"""
    from services import order as order_mod
    monkeypatch.setattr(order_mod, "DEFAULT_ORDERS_PATH", tmp_path / "orders.json")

    resp = client.post("/api/order", json={"ids": [BIG_MAC, COKE], "take_way_code": "take-in-store"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    order = payload["order"]
    assert order["order_id"].startswith("DEMO")
    assert order["is_demo"] is True
    assert order["rarity"] == "SSR"
    assert float(order["saved"]) > 0
    assert payload["redirect"] == "/orders"

    # 文件确实被写入
    assert (tmp_path / "orders.json").exists()


def test_api_order_rejects_empty_ids(client):
    resp = client.post("/api/order", json={"ids": []})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_order_rejects_unknown_id(client):
    resp = client.post("/api/order", json={"ids": [BIG_MAC, "no-such-id"]})
    assert resp.status_code == 400
    assert "不在当前门店菜单" in resp.get_json()["error"]


def test_api_order_rejects_invalid_take_way(client):
    """非法取餐码应回 400 并说明可选取值，而不是静默下单。"""
    resp = client.post("/api/order", json={"ids": [BIG_MAC, COKE], "take_way_code": "bogus"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "取餐方式不合法" in body["error"]
    assert "take-in-store" in body["error"]


def test_orders_page_shows_demo_order(client, tmp_path, monkeypatch):
    from services import order as order_mod
    monkeypatch.setattr(order_mod, "DEFAULT_ORDERS_PATH", tmp_path / "orders.json")

    # 先下一单
    client.post("/api/order", json={"ids": [BIG_MAC, COKE], "take_way_code": "eat-in"})

    resp = client.get("/orders")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "我的订单" in html
    assert "套餐拼图·差一件达成" in html
    assert "演示订单" in html


def test_api_orders_returns_orders(client, tmp_path, monkeypatch):
    from services import order as order_mod
    monkeypatch.setattr(order_mod, "DEFAULT_ORDERS_PATH", tmp_path / "orders.json")

    client.post("/api/order", json={"ids": [BIG_MAC, COKE], "take_way_code": "take-in-store"})
    payload = client.get("/api/orders").get_json()
    assert payload["ok"] is True
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["is_demo"] is True


# --------------------------------------------------------------------------- #
# 定时任务守卫（阶段 3）
# --------------------------------------------------------------------------- #
def test_scheduler_starts_when_reloader_off(monkeypatch):
    from services import scheduler as sch

    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.delenv("MCD_SCHEDULER_ALWAYS", raising=False)
    assert sch.should_own_scheduler(use_reloader=False) is True


def test_scheduler_delegates_in_reloader_parent(monkeypatch):
    """开热重载 + 父进程（无 WERKZEUG_RUN_MAIN）→ 必须放弃，否则会跑两遍。"""
    from services import scheduler as sch

    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.delenv("MCD_SCHEDULER_ALWAYS", raising=False)
    assert sch.should_own_scheduler(use_reloader=True) is False


def test_scheduler_starts_in_reloader_child(monkeypatch):
    from services import scheduler as sch

    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    monkeypatch.delenv("MCD_SCHEDULER_ALWAYS", raising=False)
    assert sch.should_own_scheduler(use_reloader=True) is True


def test_scheduler_always_escape_hatch(monkeypatch):
    """部署到 gunicorn 等非 Werkzeug 环境时用 MCD_SCHEDULER_ALWAYS=1 强制持有。"""
    from services import scheduler as sch

    monkeypatch.setenv("MCD_SCHEDULER_ALWAYS", "1")
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    assert sch.should_own_scheduler(use_reloader=True) is True


def test_start_scheduler_is_singleton(monkeypatch):
    from services import scheduler as sch

    monkeypatch.setattr(sch, "_scheduler", None, raising=False)
    first = sch.start_scheduler(force=True)
    try:
        assert first is not None
        assert sch.is_running() is True
        assert sch.next_run_time() is not None
        assert sch.start_scheduler(force=True) is first   # 单例：第二次返回同一实例
    finally:
        sch.stop_scheduler()
        assert sch.is_running() is False


def test_refresh_coupons_writes_cache(tmp_path, monkeypatch):
    from services import scheduler as sch

    target = tmp_path / "cache" / "coupons.json"
    monkeypatch.setattr(sch, "CACHE_DIR", target.parent, raising=False)
    monkeypatch.setattr(sch, "COUPON_CACHE", target, raising=False)

    payload = sch.refresh_coupons()
    assert target.exists()
    assert payload["count"] == len(payload["coupons"])
    assert payload["refreshed_at"]
    assert sch.read_cached_coupons()["count"] == payload["count"]
