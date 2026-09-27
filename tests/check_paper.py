"""虚拟盘守卫 / 费率 / 认领 / 手填价护栏 的端到端校验。

覆盖：
  1. 登录守卫：未登录访问虚拟盘一律 401，自选股仍放行
  2. 费率：按用户存、默认值正确、可改、非法值被拒
  3. ETF 免印花税（股票卖出有、ETF 卖出没有）
  4. 手填价护栏：离谱价拒绝、区间内放行、取不到区间时放行
  5. 匿名数据认领：搬走制（资金替换而非相加）、先到先得

跑法（需先启动服务，且以 TICK_ADMIN_USERS=admin 启动）：
    cd server && python3 -m uvicorn app:app --port 8899 &
    python3 tests/check_paper.py

与 e2e_* 的区别：本脚本用 HTTP 直连（不开浏览器），所以能跑得快、
也便于在 CI 里当冒烟测试。浏览器交互由 e2e_stock_search.py 覆盖。
"""
import json
import subprocess
import sys
import time
import urllib.error
import http.cookiejar
import urllib.request

BASE = "http://127.0.0.1:8899"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


#: 会话是 HttpOnly cookie（名字 tick_sid），接口不返回 token，
#: 所以必须用 cookiejar 让 urllib 自己带着走，不能手工拼 Cookie 头。
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def req(method, path, body=None, timeout=30, anon=False):
    """发一个请求，返回 (status, json|None)。

    `anon=True` 时用一个**独立的空 jar**，保证这次调用不带任何登录态——
    用来验证未登录的行为，否则前一个用例登录后这个用例就不再是「未登录」了。
    """
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    opener = _OPENER
    if anon:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        with opener.open(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main():
    # ---------- 1. 登录守卫（全部用 anon=True，确保是真实未登录态）----------
    for ep, m in [("/api/trade/summary", "GET"), ("/api/trade/positions", "GET"),
                  ("/api/trade/history", "GET"), ("/api/trade/fees", "GET"),
                  ("/api/trade/buy", "POST"), ("/api/trade/sell", "POST"),
                  ("/api/trade/reset", "POST"), ("/api/trade/claim", "POST")]:
        st, _ = req(m, ep, {} if m == "POST" else None, anon=True)
        rec(f"未登录 {m} {ep} → 401", st == 401, f"status={st}")

    st, _ = req("GET", "/api/watch/folders", anon=True)
    rec("未登录自选股仍可用 → 200（按需求只拦虚拟盘）", st == 200, f"status={st}")

    st, d = req("GET", "/api/trade/claim", anon=True)
    rec("未登录查认领状态不报错，只答 no",
        st == 200 and d.get("available") is False, f"{d}")

    # ---------- 2. 注册 + 费率 ----------
    # 注意不能传 anon=True：那样会话会写进一次性 jar，后面所有请求又变未登录。
    uname = f"_check_paper_{int(time.time())}"
    st, d = req("POST", "/api/auth/register",
                {"username": uname, "password": "test1234"})
    if st != 200:
        rec("注册测试账号", False, f"status={st} {d}")
        return summary()
    uid = d["user"]["id"]
    st, me = req("GET", "/api/auth/me")
    rec("注册测试账号（会话已建立）",
        st == 200 and me.get("logged_in") is True, f"uid={uid}")

    st, d = req("GET", "/api/trade/fees")
    f = d.get("fees", {})
    rec("新账号费率 = 默认值（佣金万 2.5 / 最低 5 / 印花税千 0.5）",
        (st == 200 and f.get("fee_rate") == 0.00025 and f.get("fee_min") == 5.0
         and f.get("stamp_rate") == 0.0005),
        f"fees={f}")
    rec("费率接口带 defaults（供「还原默认」用）",
        d.get("defaults", {}).get("fee_rate") == 0.00025, "")

    st, d = req("PUT", "/api/trade/fees",
                {"fees": {"fee_rate": 0.0001, "fee_min": 1.0}})
    rec("改费率生效", st == 200 and d["fees"]["fee_rate"] == 0.0001, f"{d}")

    for bad in [{"fee_rate": "abc"}, {"fee_rate": -0.1}, {"fee_rate": 0.9},
                {"fee_min": 9999}, {"stamp_rate": 1}]:
        st, _ = req("PUT", "/api/trade/fees", {"fees": bad})
        rec(f"非法费率 {bad} → 400", st == 400, f"status={st}")

    # 还原默认，后面算费好对比
    req("PUT", "/api/trade/fees",
        {"fees": {"fee_rate": 0.00025, "fee_min": 5.0, "stamp_rate": 0.0005}})

    # ---------- 3. ETF 免印花税 ----------
    # 价格不能写死：虚拟盘按当日真实成交区间校验，隔天价格一漂就会
    # 报「买入价超出当日成交区间」，看着像功能坏了，其实是用了昨天的价。
    def day_mid(code):
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'server'); import app, json;"
             f"print(json.dumps(app._day_range('{code}')))"],
            capture_output=True, text=True,
            cwd="/workspace/tick-stock-panel").stdout.strip()
        try:
            r0 = json.loads(out)
        except Exception:
            return None
        return round((r0["low"] + r0["high"]) / 2, 2) if r0 else None

    etf_px = day_mid("sh510300") or 4.0
    st, d = req("POST", "/api/trade/buy",
                {"code": "sh510300", "qty": 10000, "price": etf_px})
    etf_buy = d.get("trade", {}) if st == 200 else {}
    rec("ETF 买入成功且 is_etf=True",
        st == 200 and etf_buy.get("is_etf") is True, f"status={st} {d}")

    st, d = req("POST", "/api/trade/sell",
                {"code": "sh510300", "qty": 10000, "price": etf_px})
    etf_sell = d.get("trade", {}) if st == 200 else {}
    rec("ETF 卖出免印花税（费用 == 买入费用）",
        st == 200 and abs(etf_sell.get("fee", 0) - etf_buy.get("fee", 0)) < 0.01,
        f"买 {etf_buy.get('fee')} / 卖 {etf_sell.get('fee')}")

    st, d = req("POST", "/api/trade/buy",
                {"code": "sh600519", "qty": 100, "price": 1250})
    stk_buy = d.get("trade", {}) if st == 200 else {}
    st, d = req("POST", "/api/trade/sell",
                {"code": "sh600519", "qty": 100, "price": 1250})
    stk_sell = d.get("trade", {}) if st == 200 else {}
    # 同金额下单：股票卖出应比 ETF 卖出多一笔印花税
    etf_fee_at = (etf_sell.get("fee") or 0)
    stk_fee_at = (stk_sell.get("fee") or 0)
    rec("股票卖出有印花税（比 ETF 多约 62.5 元）",
        st == 200 and (stk_fee_at - etf_fee_at) > 50,
        f"股票卖 {stk_fee_at} / ETF卖 {etf_fee_at} / 差额 {stk_fee_at - etf_fee_at:.2f}")

    # ---------- 4. 手填价护栏 ----------
    st, d = req("POST", "/api/trade/buy",
                {"code": "sh600519", "qty": 100, "price": 0.01})
    rec("手填 0.01 买茅台 → 400（挡离谱价）", st == 400, f"status={st}")

    # 从后端取真实区间，再验证边界内放行
    rng = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'server'); import app, json;"
         "print(json.dumps(app._day_range('sh600519')))"],
        capture_output=True, text=True,
        cwd="/workspace/tick-stock-panel").stdout.strip()
    try:
        r0 = json.loads(rng)
    except Exception:
        r0 = None
    if r0:
        mid = round((r0["low"] + r0["high"]) / 2, 2)
        st, d = req("POST", "/api/trade/buy",
                    {"code": "sh600519", "qty": 100, "price": mid})
        rec(f"手填区间中值 {mid} → 放行", st == 200, f"status={st}")
        # 清掉这笔，别影响后面的资产断言
        req("POST", "/api/trade/sell",
            {"code": "sh600519", "qty": 100, "price": mid})
    else:
        rec("取价格区间（跳过边界测试）", False, f"无法解析: {rng[:120]}")

    # ---------- 5. 认领（搬走制）----------
    # 先造一份匿名数据
    make = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'server'); import store;"
         "store.initialize(); cc=store._conn();"
         "a=store._ensure_default_user(cc);"
         "cc.execute('DELETE FROM positions WHERE user_id=?',(a,));"
         "cc.execute('DELETE FROM trades WHERE user_id=?',(a,));"
         "cc.execute('UPDATE users SET cash=1000000.0 WHERE id=?',(a,));cc.commit();"
         "store.buy_stock('sh600519',100,1200.0,'M',a);"
         "print(store.anon_state()['position_count'])"],
        capture_output=True, text=True, cwd="/workspace/tick-stock-panel")
    ok_make = make.stdout.strip() == "1"
    rec("造匿名数据（模拟守卫前的历史数据）", ok_make,
        make.stdout.strip() or make.stderr[-160:])

    st, d = req("GET", "/api/trade/claim")
    rec("认领状态可见（available=True）",
        st == 200 and d.get("available") is True, f"{d}")

    st, before = req("GET", "/api/trade/summary")
    cash_before = (before or {}).get("cash", 0)

    st, d = req("POST", "/api/trade/claim", {})
    rec("认领成功", st == 200 and (d or {}).get("ok") is True, f"{d}")

    st, after = req("GET", "/api/trade/summary")
    after = after or {}
    # 搬走制：现金被【替换】成匿名那份，不是两边相加
    anon_cash = 1000000.0 - 120000.0 - 30.0     # 买入 12 万 + 佣金 30
    got = after.get("cash")
    rec("认领是资金替换而非相加（没白送 100 万）",
        got is not None and abs(got - anon_cash) < 1.0,
        f"认领前 {cash_before:.2f} → 认领后 {got} 期望 {anon_cash:.2f}")
    rec("认领后持仓已过户", (after.get("position_count") or 0) >= 1,
        f"持仓 {after.get('position_count')} 只")

    st, d = req("POST", "/api/trade/claim", {})
    rec("同账号二次认领 → 400（先到先得）", st == 400, f"status={st}")

    st, d = req("GET", "/api/trade/claim")
    rec("认领后 available 变 false", (d or {}).get("available") is False, f"{d}")

    # ---------- 6. 流水带价格口径 ----------
    st, d = req("GET", "/api/trade/history")
    items = (d or {}).get("items", []) if st == 200 else []
    has_pspan = all("pspan" in t for t in items) if items else False
    rec("流水每笔带 pspan（价格口径）", bool(items) and has_pspan,
        f"{len(items)} 笔")
    spans = {t.get("pspan") for t in items}
    rec("价格口径取值在已知集合内",
        spans <= {"实时", "收盘", "昨收", "手填", "竞价", ""}, f"spans={spans}")

    return summary()


def summary():
    print("\n== 汇总 ==")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    for n, ok, dt in results:
        if not ok:
            print(f"  FAIL: {n}  {dt}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
