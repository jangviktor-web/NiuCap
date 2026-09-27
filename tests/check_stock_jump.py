"""个股分析 → 策略回测 一键直达按钮 回归测试。

验证点：
1. 个股页渲染出 #gotoBtBtn 按钮（renderStock 注入）
2. 点击后：切到回测页 + btCode 预填当前个股代码 + 弹 toast
3. 换一只股票再验一次，确认带的是"当前打开的股"而非固定值
4. 全程无 JS 运行时错误

用法：python3 tests/check_stock_jump.py（需要服务已运行在 :8899）
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("  ✅ " if ok else "  ❌ ") + name + (f"（{detail}）" if detail else ""))


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1560, "height": 1200})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_timeout(1200)

    # ---- 1) 打开茅台，按钮存在 ----
    pg.evaluate("openStock('sh600519')")
    pg.wait_for_timeout(2500)
    rec("个股页渲染出 #gotoBtBtn",
        pg.evaluate("!!document.getElementById('gotoBtBtn')"))

    # ---- 2) 点击 → 回测页激活 + 代码预填 + 自动跑出图 ----
    pg.evaluate("document.getElementById('gotoBtBtn').click()")
    pg.wait_for_timeout(4500)   # 等自动回测跑完
    rec("回测页签激活",
        pg.evaluate("document.querySelector('button[data-tab=backtest]').classList.contains('on')"))
    rec("回测页可见",
        pg.evaluate("document.getElementById('tab-backtest').classList.contains('on')"))
    rec("btCode 预填 sh600519",
        pg.evaluate("document.getElementById('btCode').value") == "sh600519")
    rec("点击后自动回测出图（btOutput 有 canvas）",
        pg.evaluate("!!document.querySelector('#btOutput canvas')"))

    # ---- 3) 换五粮液再验，确认带的是当前股 + 自动出图 ----
    pg.evaluate("openStock('sz000858')")
    pg.wait_for_timeout(2200)
    pg.evaluate("document.getElementById('gotoBtBtn').click()")
    pg.wait_for_timeout(4500)
    rec("换股后 btCode 预填 sz000858",
        pg.evaluate("document.getElementById('btCode').value") == "sz000858")
    rec("换股后自动回测出图",
        pg.evaluate("!!document.querySelector('#btOutput canvas')"))

    # ---- 4) 无 JS 错误 ----
    rec("无 JS 运行时错误", not errs, "; ".join(errs[:3]))
    b.close()

n_ok = sum(1 for _, ok in results if ok)
print(f"\n汇总：{n_ok}/{len(results)} 通过")
sys.exit(0 if n_ok == len(results) else 1)
