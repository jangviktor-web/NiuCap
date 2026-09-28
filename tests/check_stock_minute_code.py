"""个股页分钟线「未找到代码 null」回归自检。

起因：进个股页有两条路——`openStock()`（榜单/选股/搜索）写 `state.stockCode`，
`goStock()`（自选股、新手页「看详情」）只写 `state.code`。分钟周期按钮读的是
`state.stockCode`，于是从自选股进来点「1分」会拿着 null 去请求，后端 400 回
「未找到代码 null」。

修法：goStock 委托 openStock（一份入口），自动刷新也改读 stockCode，
并在 loadStockMinute 里对空代码给一句人话。

跑法（需先启动服务）：
    cd /workspace/tick-stock-panel && .venv/bin/python server/run.py &
    python3 tests/check_stock_minute_code.py

覆盖：
  A 走真实路径（自选股 → 个股 → 1分），state.stockCode 必须被填上
  B 1 分钟线真的加载成功，提示里不再出现「未找到代码」
  C 空代码守卫：不发请求，且给出「还没选个股」提示
  D 页面控制台无报错
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
HEADLESS = os.environ.get("HEADED", "") != "1"

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'✅' if ok else '❌'} {name}  {detail}")


# ------------------------------------------------------ A/B 真实路径走一遍
def test_watch_to_minute(page, errors):
    print("── A/B 自选股 → 个股 → 1分 ──")

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    # 进自选股
    page.click('button[data-tab="watch"]')
    page.wait_for_timeout(1500)

    btns = page.query_selector_all("[data-wtgo]")
    if not btns:
        rec("自选股列表有『进个股』入口", False, "没有 [data-wtgo]，跳过后续")
        return None

    code = btns[0].get_attribute("data-wtgo")
    rec("自选股列表有『进个股』入口", True, f"首行代码 {code}")

    btns[0].click()
    page.wait_for_selector("#perMinBtns .btn", timeout=15000)
    page.wait_for_timeout(800)

    st = page.evaluate("({sc: state.stockCode, old: state.code})")
    rec("goStock 会写 state.stockCode（根因）",
        st["sc"] == code, f"stockCode={st['sc']} 期望={code}")
    rec("不再写旧的 state.code（双轨已消除）",
        st["old"] is None, f"state.code={st['old']!r}")

    # 点「1分」
    page.click('#perMinBtns .btn[data-minperiod="1m"]')
    page.wait_for_timeout(6000)  # 1m 要拉 2400 根，给足时间

    note = page.inner_text("#stockPerNote")
    rec("1 分钟线提示不含『未找到代码』", "未找到代码" not in note, note[:110])
    rec("1 分钟线提示为正常状态（1m 周期）", "1m 周期" in note, note[:110])

    bars = page.evaluate("(state.stockMinuteData && state.stockMinuteData.klines || []).length")
    rec("1 分钟线真的取到了数据", bars > 0, f"{bars} 根")
    page.screenshot(path="shots_minute_fix/minute_1m.png", full_page=False)
    return code


# ------------------------------------------------------------ C 空代码守卫
def test_empty_guard(page):
    print("── C 空代码守卫 ──")
    hits = []
    page.on("request", lambda r: hits.append(r.url) if "kline_intraday" in r.url else None)

    page.evaluate("loadStockMinute(null, '1m')")
    page.wait_for_timeout(800)
    note = page.inner_text("#stockPerNote")
    rec("空代码给出人话提示", "还没选个股" in note, note[:80])
    rec("空代码不再发请求", len(hits) == 0, f"发出 {len(hits)} 次")


def main():
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS,
                                     args=["--no-sandbox"])
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        try:
            test_watch_to_minute(page, errors)
            test_empty_guard(page)
        finally:
            print("── D 控制台 ──")
            rec("页面无 JS 报错", not errors, "; ".join(errors[:3])[:200])
            browser.close()

    ok = sum(1 for _, o, _ in results if o)
    print(f"\n{'=' * 52}\n通过 {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    os.makedirs("shots_minute_fix", exist_ok=True)
    sys.exit(main())
