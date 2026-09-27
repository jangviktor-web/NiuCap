"""风险过滤器 + 公告风险着色 交付截图。

两张图：
  1. 策略扫描（pullback_ma20，默认过滤开）：提示条显示「🛡 风险过滤剔除 N 只」，
     结果列表为过滤后的命中（对比接口层实测：397 → 306）。
  2. 个股页公告区：标题按语义着色（风险红 / 事件橙 / 普通默认）。
     （过滤开关 tune={"risk_filters":false} 是 API 参数，前端无入口，故不截对比图）

用法：python3 tests/shot_risk_filters.py
前置：服务在 8899；需 admin 账号。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import store  # noqa: E402

BASE = "http://127.0.0.1:8899/"
OUT_SCAN = "docs/shot-riskfilter-scan.png"
OUT_NOTICES = "docs/shot-notice-tone.png"


def admin_token():
    adm = next((u for u in store.list_users()
                if (u.get("username") or "").lower() == "admin"), None)
    if not adm:
        raise SystemExit("需要 admin 账号")
    return store.create_session(adm["id"])


def main():
    tok = admin_token()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1560, "height": 1500})
        ctx.add_cookies([{"name": "tick_sid", "value": tok, "url": BASE}])
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # ---- 1) 策略扫描：pullback_ma20，过滤开 ----
        pg.evaluate("syncTabs('strategy')")
        pg.wait_for_timeout(400)
        pg.evaluate("stratState.picked = ['pullback_ma20']; runStrategyScan()")
        pg.wait_for_timeout(15000)          # 全市场扫描 + 引擎预热
        pg.locator("#stratResultCard").screenshot(path=OUT_SCAN)
        print("已保存", OUT_SCAN)

        # ---- 2) 个股公告着色 ----
        pg.evaluate("openStock('sh600519')")
        pg.wait_for_timeout(8000)           # 等扩展数据（含公告）加载
        pg.locator("#stockExtra").screenshot(path=OUT_NOTICES)
        print("已保存", OUT_NOTICES)

        print("页面 JS 错误:", len(errs), errs[:3])
        b.close()


if __name__ == "__main__":
    main()
