"""端到端：模拟净值（方案 3）+ 市场宽度（方案 4）。

跑法（需先启动服务在 8899）：
    python3 tests/e2e_sim_breadth.py

验的是「用户真能点到并看懂」：
  1. 市场宽度卡片：默认页签直接渲染，摘要数字合理、双图存在
  2. 模拟净值：体检完成后可选策略回放，SVG 曲线 + 指标出现（15 个可评估策略）
  3. 换持有天数自动重算
  4. 全程无 JS 报错、无长请求（防网关 504）

用 days=40 跑体检（先 curl 预热，页面交互秒出）。

跑之前的前置条件（踩过两次，别再排查第三遍）：
本脚本第 68 行要的是 **days=120** 的体检结果，依赖服务启动时的 120 天预热。
而策略体检缓存 `_HITS_CACHE` 是**单槽**（按 days|forward|as_of 整份替换），
所以只要先跑过 `e2e_strategy_eval.py`（它全程只用 days=40），120 就会被挤掉，
这里 4 条「模拟 API / 曲线 / 指标 / 笔数」会一起挂——不是功能坏了。
顺序：单独跑（等启动预热完成，或手动 POST /api/strategy_eval/job?days=120&forward=5
预热完再跑）；跑完再跑 e2e_strategy_eval，别反过来。
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def api(path, timeout=120):
    """GET 并解析 JSON。4xx 也是「预期响应」之一（如 need_eval 的 409），
    同样解析 body 返回，由调用方判断字段。"""
    try:
        with urllib.request.urlopen(BASE.rstrip("/") + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise


def main():
    # ================= API 直测 =================
    # ---- 市场宽度 ----
    t0 = time.time()
    d = api("/api/market_breadth?days=120")
    dt = time.time() - t0
    rec("宽度API ok", d.get("ok") is True, str(d.get("error", ""))[:60])
    rec("宽度序列 >=100 天", d.get("ok") and len(d["series"]) >= 100,
        f"{len(d.get('series', []))} 天")
    L = d.get("latest", {})
    rec("宽度最新读数字段齐",
        all(k in L for k in ("adv", "dec", "above20_ratio",
                             "above20_pct", "nh60", "nl60")),
        str({k: L.get(k) for k in ("adv", "dec", "above20_ratio")}))
    rec("涨跌家数加总合理（总样本>1000）", (L.get("adv", 0) + L.get("dec", 0)
                                        + L.get("flat", 0)) > 1000,
        f"{L.get('adv')}+{L.get('dec')}+{L.get('flat')}")
    rec("分位在 0~100", (L.get("above20_pct") is not None
                         and 0 <= L["above20_pct"] <= 100),
        f"above20_pct={L.get('above20_pct')}")
    rec("宽度首算/缓存 <120s（防网关；服务刚重启时与体检预热抢 CPU 会慢）",
        dt < 120, f"{dt:.1f}s")

    # ---- 模拟净值（120 天体检启动时已预热）----
    t0 = time.time()
    s = api("/api/equity_sim?key=oversold_rebound&days=120&hold=5")
    dt = time.time() - t0
    rec("模拟API ok", s.get("ok") is True, str(s.get("error", ""))[:60])
    st = s.get("stats", {})
    rec("模拟曲线 >50 点", s.get("ok") and len(s.get("curve", [])) > 50,
        f"{len(s.get('curve', []))} 点")
    rec("模拟指标齐",
        all(k in st for k in ("total_ret", "max_dd", "win_rate",
                              "n_trades", "limit_skipped")),
        f"total={st.get('total_ret')} dd={st.get('max_dd')} "
        f"win={st.get('win_rate')}")
    rec("模拟笔数 >0", (st.get("n_trades") or 0) > 0,
        f"{st.get('n_trades')} 笔 · 涨停跳过 {st.get('limit_skipped')}")
    rec("模拟响应 <5s（纯内存计算）", dt < 5, f"{dt:.2f}s")

    # need_eval：没预热过的参数应提示先体检，而不是悄悄重算
    s2 = api("/api/equity_sim?key=oversold_rebound&days=40&hold=5")
    rec("未预热参数返回 need_eval",
        s2.get("ok") is False and s2.get("need_eval") is True,
        str(s2.get("error", ""))[:40])

    # ---- 预热 40 天体检（页面交互要秒出）----
    print("… 预热 40 天体检（约 30~60s）")
    t0 = time.time()
    j = urllib.request.urlopen(
        urllib.request.Request(
            BASE.rstrip("/") + "/api/strategy_eval/job?days=40&forward=5",
            method="POST", data=b""), timeout=30)
    jid = json.loads(j.read().decode())["job"]
    while True:
        p = api(f"/api/strategy_eval/job/{jid}")
        if not p.get("running"):
            break
        time.sleep(2)
    rec("40天体检预热完成", p.get("ok") and (p.get("result") or {}).get("ok"),
        f"{time.time() - t0:.0f}s")

    # ================= 页面交互 =================
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console",
              lambda m: errs.append(m.text) if m.type == "error" else None)

        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # ---- 市场宽度卡片（默认页签内）----
        card = pg.query_selector("#breadthCard")
        rec("宽度卡片存在且可见", card is not None and card.is_visible())
        try:
            pg.wait_for_selector("#breadthBody svg", timeout=15000)
            ok_svg = True
        except Exception:
            ok_svg = False
        rec("宽度图已渲染", ok_svg)
        txt = pg.inner_text("#breadthBody") if ok_svg else ""
        rec("宽度摘要含涨跌家数", "涨" in txt and "跌" in txt,
            txt.split("\n")[0][:40])
        rec("宽度摘要含 MA20 比例", "20日线" in txt)
        rec("宽度摘要含新高新低", "新高" in txt and "新低" in txt)
        # 方案 D：市况读数行 + Chop 震荡指数折线
        rec("宽度摘要含市况读数（方案D）", "市况" in txt,
            txt.split("\n")[0][:60])
        rec("宽度图含 Chop 震荡指数（方案D）", "震荡指数" in txt or "Chop" in txt)
        meta = pg.inner_text("#breadthMeta")
        rec("宽度元信息含交易日数", "个交易日" in meta, meta[:40])

        # ---- 策略体检 → 模拟净值 ----
        pg.click('button[data-tab="strategy"]')
        pg.wait_for_timeout(1200)
        # 关键：把回看天数设为 40 再点体检。_CACHE 是单槽（按参数整份替换），
        # 上面预热 40 天时已把启动时预热的 120 天挤掉；页面默认 120 会
        # 触发一次真算（114s）导致本步超时。设成与预热一致的 40 天 → 秒出。
        pg.evaluate("""() => {
          var s = document.getElementById('evalDays');
          if(s){ s.value = '40'; }
        }""")
        pg.click("#evalBtn")
        try:
            pg.wait_for_selector("#evalBody table.evt", timeout=30000)
            ok_tbl = True
        except Exception:
            ok_tbl = False
            # 失败现场：把面板状态与 console 错误吐出来，方便定位
            _diag = pg.evaluate("""() => ({
                body: ((document.getElementById('evalBody')||{}).textContent||'').slice(0,120),
                meta: (document.getElementById('evalMeta')||{}).textContent||'',
                panelShown: !!document.getElementById('evalPanel') &&
                            document.getElementById('evalPanel').style.display !== 'none',
                hasEvt: !!document.querySelector('#evalBody table.evt'),
            })""")
            print("  [诊断] 面板状态:", _diag)
            print("  [诊断] console 错误:", errs[:3])
        rec("体检表格已渲染（40 天已预热应秒出）", ok_tbl)

        try:
            pg.wait_for_selector("#simBox", state="visible", timeout=5000)
            ok_sim = True
        except Exception:
            ok_sim = False
        rec("模拟净值小节已展开", ok_sim)
        if ok_sim:
            n_opts = pg.evaluate(
                "() => document.getElementById('simKey').options.length")
            rec("策略下拉有 15 项（含方案A 6个经典指标）", n_opts == 15,
                f"{n_opts} 项")

            # 选体检里超额最强的策略回放（oversold_rebound 在 120 天为正，
            # 40 天窗口不保证——只要曲线和指标出来即可）
            pg.select_option("#simKey", "oversold_rebound")
            pg.click("#simRun")
            try:
                pg.wait_for_selector("#simBody svg", timeout=10000)
                ok_curve = True
            except Exception:
                ok_curve = False
            rec("模拟净值曲线已渲染", ok_curve)
            stxt = pg.inner_text("#simBody")
            rec("模拟指标行齐", all(k in stxt for k in
                                  ("窗口总收益", "最大回撤", "逐笔胜率",
                                   "Sharpe", "Sortino", "Calmar", "盈亏比",
                                   "涨停跳过")))
            rec("模拟含基准说明", "全市场等权" in stxt)

            # 换持有天数 → 自动重算（请求快，曲线应更新且不再显示回放中）
            errs.clear()
            pg.select_option("#simHold", "10")
            try:
                pg.wait_for_function(
                    "() => /持有 10 日/.test((document.getElementById('simBody')||{}).innerText||'')",
                    timeout=10000)
                ok_hold = True
            except Exception:
                ok_hold = False
            rec("换持有天数自动重算（10 日）", ok_hold)
            rec("模拟交互无 JS 错误", len(errs) == 0, str(errs[:2]))

        rec("全程无 JS 错误", len(errs) == 0, str(errs[:2]))
        b.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print("== 汇总 ==")
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
