"""个股页全周期 × 全入口自检（日/周/月 + 1/5/15/30/60 分钟）。

为什么单独一份：`check_stock_minute_code.py` 只守「分钟线别拿到 null 代码」
这一个回归点。这份守的是面——三条进个股页的入口 × 八个周期，外加
分钟↔日线往返、分钟模式下的通道切换、日线页四张副图是否都在。

跑法（需先启动服务）：
    cd /workspace/tick-stock-panel && .venv/bin/python server/run.py &
    python3 tests/check_stock_periods.py

覆盖：
  A 后端接口：3 个日级周期 + 5 个分钟周期都有数据
  B 自选股入口（goStock 路径）：8 个周期逐个点
  C 榜单入口 / D 搜索入口（openStock 路径）：抽样
  E 分钟 ↔ 日线往返
  F 分钟模式下切通道
  G 日线页四张副图 + 关键价位卡片
  H 控制台无报错
"""
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
CODE = os.environ.get("CODE", "sz000066")
DAYS = ["1d", "1w", "1M"]
MINS = ["1m", "5m", "15m", "30m", "60m"]

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'✅' if ok else '❌'} {name}  {detail}")


# ------------------------------------------------------------- A 后端接口
def test_api():
    print("── A 后端接口 ──")

    def get(path, timeout=40):
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.load(r)

    for p in DAYS:
        try:
            d = get(f"/api/kline?code={CODE}&period={p}&count=250")
            k = d.get("klines") or []
            rec(f"日线接口 {p}", len(k) > 0,
                f"{len(k)} 根 · {(k[-1]['date'][:10] if k else '-')}")
        except Exception as e:
            rec(f"日线接口 {p}", False, str(e)[:90])
    for p in MINS:
        try:
            d = get(f"/api/kline_intraday?code={CODE}&period={p}")
            k = d.get("klines") or []
            rec(f"分钟接口 {p}", len(k) > 0,
                f"{len(k)} 根 · {d.get('source')} · {(k[-1]['date'][:16] if k else '-')}")
        except Exception as e:
            rec(f"分钟接口 {p}", False, str(e)[:90])


# --------------------------------------------------------- 前端公共 helper
# ponytail: 用 expect_response 而不是「注册 on(response) + Python 轮询」。
# sync API 下 time.sleep 会阻塞 greenlet，事件回调根本派发不出来——
# 第一版就这么写的，结果每个周期都白等到超时，10 分钟才跑完。
def wait_note_idle(page, timeout=30):
    page.wait_for_function(
        "() => { var n = document.getElementById('stockPerNote');"
        " if(!n || n.style.display === 'none') return true;"
        " return !/正在拉取|加载中/.test(n.textContent); }",
        timeout=timeout * 1000)


# 必须等到提示条真的写上目标周期才算数。
# ponytail: 换股进页面时前端会按上次周期自动先拉一次（如 5m），
# 只等「不在加载中」会读到上一周期留下的文本，断言就成了假绿。
def wait_note_period(page, p, timeout=30):
    page.wait_for_function(
        f"() => {{ var n = document.getElementById('stockPerNote');"
        f" return !!n && n.style.display !== 'none'"
        f" && n.textContent.indexOf('{p} 周期') >= 0; }}",
        timeout=timeout * 1000)


def _body(resp):
    try:
        return resp.json()
    except Exception:
        return {}


def click_day(page, p):
    with page.expect_response(lambda r: "/api/kline?" in r.url, timeout=60000) as info:
        page.click(f'#perBtns .btn[data-period="{p}"]')
    page.wait_for_selector("#perMinBtns .btn", timeout=20000)
    return _body(info.value)


def click_min(page, p):
    with page.expect_response(
            lambda r: "/api/kline_intraday" in r.url and f"period={p}" in r.url,
            timeout=60000) as info:
        page.click(f'#perMinBtns .btn[data-minperiod="{p}"]')
    wait_note_period(page, p)
    return _body(info.value)


# ------------------------------------------------- B 自选股入口（goStock）
def test_watch_all_periods(page):
    print("── B 自选股入口 × 8 周期 ──")
    page.click('button[data-tab="watch"]')
    page.wait_for_timeout(1200)
    btns = page.query_selector_all("[data-wtgo]")
    if not btns:
        rec("自选股入口", False, "没有 [data-wtgo]，跳过 B")
        return
    code = btns[0].get_attribute("data-wtgo")
    btns[0].click()
    page.wait_for_selector("#perMinBtns .btn", timeout=20000)
    rec("自选股入口能进个股页", page.evaluate("state.stockCode") == code, code)

    for p in DAYS:
        d = click_day(page, p)
        k = (d or {}).get("klines") or []
        rec(f"B 日级 {p}", len(k) > 0, f"{len(k)} 根")
    for p in MINS:
        d = click_min(page, p)
        k = (d or {}).get("klines") or []
        note = page.inner_text("#stockPerNote")
        bad = "加载失败" in note or "未找到代码" in note
        rec(f"B 分钟 {p}", len(k) > 0 and not bad,
            f"{len(k)} 根" + ("｜" + note[:60] if bad else ""))


# --------------------------------------------- C/D 另两条入口（openStock）
def test_rank_entry(page):
    print("── C 榜单入口 ──")
    page.click('button[data-tab="rank"]')
    page.wait_for_timeout(2000)
    rows = page.query_selector_all("tr[data-code]")
    if not rows:
        rec("榜单入口", False, "没有 tr[data-code]，跳过 C")
        return
    code = rows[0].get_attribute("data-code")
    rows[0].click()
    page.wait_for_selector("#perMinBtns .btn", timeout=20000)
    rec("榜单入口能进个股页", page.evaluate("state.stockCode") == code, code)

    d = click_day(page, "1d")
    rec("C 日线 1d", len((d or {}).get("klines") or []) > 0,
        f"{len((d or {}).get('klines') or [])} 根")
    d = click_min(page, "5m")
    k = (d or {}).get("klines") or []
    note = page.inner_text("#stockPerNote")
    rec("C 分钟 5m", len(k) > 0 and "加载失败" not in note, f"{len(k)} 根｜{note[:50]}")


def test_search_entry(page):
    print("── D 搜索入口 ──")
    page.fill("#q", "600519")
    page.wait_for_selector("#sug div[data-code]", timeout=15000)
    first = page.query_selector("#sug div[data-code]")
    code = first.get_attribute("data-code")
    first.click()
    page.wait_for_selector("#perMinBtns .btn", timeout=20000)
    rec("搜索入口能进个股页", page.evaluate("state.stockCode") == code, code)

    d = click_min(page, "15m")
    k = (d or {}).get("klines") or []
    note = page.inner_text("#stockPerNote")
    rec("D 分钟 15m", len(k) > 0 and "加载失败" not in note, f"{len(k)} 根｜{note[:50]}")
    d = click_day(page, "1d")
    rec("D 日线 1d", len((d or {}).get("klines") or []) > 0,
        f"{len((d or {}).get('klines') or [])} 根")


# ------------------------------------------------------- E 分钟↔日线往返
def test_round_trip(page):
    print("── E 分钟 ↔ 日线往返 ──")
    click_min(page, "5m")
    rec("E 已在 5 分钟模式", page.evaluate("state.stockMinute") == "5m", "")

    # 再点一次高亮的 5m = 退回日线
    with page.expect_response(lambda r: "/api/kline?" in r.url, timeout=60000) as info:
        page.click('#perMinBtns .btn[data-minperiod="5m"]')
    d = _body(info.value)
    page.wait_for_selector("#perBtns .btn", timeout=20000)
    rec("E 再点一次退回日线",
        page.evaluate("state.stockMinute") is None and len((d or {}).get("klines") or []) > 0,
        f"日线 {len((d or {}).get('klines') or [])} 根")

    note = page.evaluate("() => { var n = document.getElementById('stockPerNote');"
                        " return n ? (n.style.display || '') : 'gone' }")
    rec("E 退回后分钟提示条已隐藏", note == "none", f"display={note}")

    d = click_min(page, "30m")
    k = (d or {}).get("klines") or []
    rec("E 退回后还能再进分钟", len(k) > 0, f"30m {len(k)} 根")


# --------------------------------------------------- F/G 通道切换 + 副图
def test_channel_and_subcharts(page):
    print("── F 分钟模式下切通道 ──")
    page.click('#chanBtns .btn[data-chan="boll"]')
    page.wait_for_timeout(1200)
    rec("F 分钟模式切布林带不报错", True, "见 H 控制台断言")
    page.click('#chanBtns .btn[data-chan="none"]')
    page.wait_for_timeout(600)

    print("── G 日线页副图 / 卡片 ──")
    click_day(page, "1d")
    page.wait_for_timeout(1500)
    for cid in ["cmacd", "cvol", "crsi", "ckdj"]:
        n = page.evaluate(
            f"() => {{ var e = document.getElementById('{cid}');"
            f" return e ? e.querySelectorAll('canvas').length : -1 }}")
        rec(f"G 副图 {cid}", n > 0, f"{n} 个 canvas")
    has = page.evaluate(
        "() => { var b = document.body.innerText;"
        " return {kl: b.indexOf('关键价位') >= 0,"
        "  ind: b.indexOf('全套技术指标') >= 0,"
        "  st: b.indexOf('量化策略信号') >= 0} }")
    rec("G 关键价位卡片", has["kl"], "")
    rec("G 全套技术指标卡片", has["ind"], "")
    rec("G 量化策略信号卡片", has["st"], "")


def main():
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))


        try:
            page.goto(BASE, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            test_watch_all_periods(page)
            test_rank_entry(page)
            test_search_entry(page)
            test_round_trip(page)
            test_channel_and_subcharts(page)
        finally:
            print("── H 控制台 ──")
            rec("页面无 JS 报错", not errors, "; ".join(errors[:3])[:220])
            page.screenshot(path="shots_periods/periods.png", full_page=False)
            browser.close()

    ok = sum(1 for _, o, _ in results if o)
    print(f"\n{'=' * 52}\n通过 {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    os.makedirs("shots_periods", exist_ok=True)
    sys.exit(main())
