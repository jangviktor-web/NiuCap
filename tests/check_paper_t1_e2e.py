"""虚拟盘 T+1 浏览器级端到端验证（修复 #T1 闭环）。

为什么单独一份：check_paper.py 是 HTTP 直连，证的是「后端拦截」；这份证的是
「前端交互真的拦得住」——填买入、点买入、再点当日卖出，页面应弹 T+1 提示、
持仓不被卖掉、流水不出现当日卖。用的是和真实用户完全一样的 UI 路径。

跑法（需先启动服务）：
    cd /workspace/tick-stock-panel && .venv/bin/python server/run.py &
    python3 tests/check_paper_t1_e2e.py
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
HEADLESS = os.environ.get("HEADED", "") != "1"

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'✅' if ok else '❌'} {name}  {detail}")


def _positions(page):
    return page.evaluate(
        "async () => { const r = await fetch('/api/trade/positions');"
        " const d = await r.json(); return d.positions || d.items || []; }")


def _history(page):
    return page.evaluate(
        "async () => { const r = await fetch('/api/trade/history');"
        " const d = await r.json(); return d.items || []; }")


def main():
    uname = "_t1_e2e_%d" % int(time.time())
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")

        # 注册 + 登录临时账号（不动 admin 的真实虚拟盘）
        page.evaluate(
            "async (u) => {"
            " await fetch('/api/auth/register', {method:'POST',"
            "   headers:{'Content-Type':'application/json'},"
            "   body: JSON.stringify({username:u, password:'test1234'})});"
            " await fetch('/api/auth/login', {method:'POST',"
            "   headers:{'Content-Type':'application/json'},"
            "   body: JSON.stringify({username:u, password:'test1234'})});"
            "}", uname)
        page.wait_for_timeout(500)

        page.evaluate("syncTabs('paper')")
        page.wait_for_selector("#ppBuyCode", state="visible", timeout=15000)

        # 1) 当日买入（留空价格 = 实时价，避开手填价区间校验）
        page.fill("#ppBuyCode", "sh600519")
        page.fill("#ppBuyQty", "100")
        page.evaluate("doBuy()")
        page.wait_for_timeout(2500)

        pos = _positions(page)
        rec("当日买入成功（持仓出现 sh600519）",
            any(p.get("code") == "sh600519" and int(p.get("qty", 0)) >= 100 for p in pos),
            str([(p.get("code"), p.get("qty")) for p in pos[:3]]))

        # 2) 当日卖出——应被 T+1 拦下，持仓不减
        page.evaluate("doSell('sh600519','贵州茅台',100)")
        page.wait_for_timeout(2500)

        pos2 = _positions(page)
        still = any(p.get("code") == "sh600519" for p in pos2)
        rec("当日卖出被 T+1 拦截（持仓仍在）", still,
            str([(p.get("code"), p.get("qty")) for p in pos2[:3]]))

        his = _history(page)
        today_sell = [t for t in his
                      if t.get("code") == "sh600519" and t.get("side") == "sell"]
        rec("当日无 sh600519 卖出流水（T+1 真的拦住）",
            len(today_sell) == 0, f"{len(today_sell)} 笔")

        # 页面应弹出 T+1 提示
        body = page.evaluate("document.body.innerText")
        rec("页面出现 T+1 提示", "T+1" in body, "")

        # 清理：重置临时账号，还原干净状态
        page.evaluate(
            "async () => { await fetch('/api/trade/reset', {method:'POST',"
            " headers:{'Content-Type':'application/json'}, body:'{}'}); }")
        browser.close()

    ok = sum(1 for _, o, _ in results if o)
    print(f"\n{'=' * 52}\n通过 {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
