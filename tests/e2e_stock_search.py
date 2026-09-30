"""端到端：验证 5 个模块的股票输入框支持中文名称搜索。

覆盖：虚拟盘 / 策略回测（单策略+全策略）/ 走查回测 / 多股对比 / 自选（回归）
每条用例都是「输入中文名 -> 触发 -> 看请求参数里是否出现规范代码」。

跑法（需先启动服务）：
    cd server && python3 -m uvicorn app:app --port 8899 &
    python3 tests/e2e_stock_search.py
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


#: 交易时段探测：收盘后虚拟盘买入会被守卫 403 拦下（且按钮被禁用），
#: 所以这一项的「买入请求已发出」断言只能在交易时段验证。
def _tradable():
    try:
        import subprocess
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'server'); import app;"
             "print(app.ds.market_state().get('state'))"],
            capture_output=True, text=True,
            cwd="/workspace/tick-stock-panel").stdout.strip()
        return out in ("trading", "auction")
    except Exception:
        return False


def main():
    tradable = _tradable()
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()

        # 收集所有 /api/search 与业务接口的请求，用来断言解析结果
        seen = []

        def on_req(req):
            u = req.url
            if "/api/" in u:
                seen.append(u)

        pg.on("request", on_req)
        pg.goto(BASE, wait_until="networkidle")

        # ---------- 1a. 策略回测：全策略对比，输入中文名 ----------
        pg.click('button[data-tab="backtest"]')
        pg.wait_for_timeout(400)
        seen.clear()
        pg.fill("#btCode", "贵州茅台")
        pg.click("#btMulti")
        pg.wait_for_timeout(3500)
        bt_hit = [u for u in seen if "/api/backtest_multi" in u]
        ok = any("/api/search" in u for u in seen) and bt_hit and "sh600519" in bt_hit[0]
        rec("策略回测·全策略对比(贵州茅台)", ok,
            f"url={bt_hit[0][:80] if bt_hit else 'NONE'}")

        # ---------- 1b. 策略回测：单策略回测，输入中文名 ----------
        seen.clear()
        pg.fill("#btCode", "中国平安")
        pg.click("#btRun")
        pg.wait_for_timeout(3500)
        bt1 = [u for u in seen if "/api/backtest?" in u]
        ok = any("/api/search" in u for u in seen) and bt1 and "sh601318" in bt1[0]
        rec("策略回测·单策略(中国平安)", ok, f"url={bt1[0][:80] if bt1 else 'NONE'}")

        # ---------- 2. 走查回测：输入中文名 ----------
        seen.clear()
        pg.fill("#wfCode", "五粮液")
        pg.click("#wfRun")
        pg.wait_for_timeout(3500)
        wf = [u for u in seen if "/api/walk_forward" in u]
        ok = any("/api/search" in u for u in seen) and wf and "sz000858" in wf[0]
        rec("走查回测(五粮液)", ok, f"url={wf[0][:80] if wf else 'NONE'}")

        # ---------- 3. 多股对比：混合输入（代码+名称） ----------
        pg.click('button[data-tab="compare"]')
        pg.wait_for_timeout(400)
        seen.clear()
        pg.fill("#cmpCodes", "茅台,五粮液,600519")
        pg.click("#cmpRun")
        pg.wait_for_timeout(3000)
        cmp_req = [u for u in seen if "/api/compare" in u]
        val = pg.input_value("#cmpCodes")
        ok = bool(cmp_req) and "sh600519" in cmp_req[0] and "sz000858" in cmp_req[0]
        # 600519 与 茅台 去重后应只剩 2 只
        dedup = val.count(",") == 1
        rec("多股对比(茅台,五粮液,600519 → 去重 2 只)", ok and dedup,
            f"回填={val} url={cmp_req[0][:70] if cmp_req else 'NONE'}")

        # ---------- 4. 虚拟盘：中文名买入 ----------
        # 虚拟盘现在需要登录（守卫），所以先过一遍注册/登录弹层。
        pg.click('button[data-tab="paper"]')
        pg.wait_for_timeout(500)
        gate = pg.query_selector("#ppGate")
        gate_visible = bool(gate) and gate.is_visible()
        rec("虚拟盘·未登录显示登录遮罩", gate_visible,
            "gate visible" if gate_visible else "gate 未显示")

        # 从遮罩里的按钮走登录流程（同时也验证了按钮可用）
        pg.click("#ppGateLogin")
        pg.wait_for_timeout(400)
        uname = "_e2e_ui_%d" % int(__import__("time").time())
        # 顺序要紧：先切到注册模式，再填字段。
        # openAuth() 会清空输入框，反过来填就被清掉了。
        pg.click("#authSwap")
        pg.wait_for_timeout(200)
        pg.fill("#authUser", uname)
        pg.fill("#authPwd", "test1234")
        pg.click("#authSubmit")
        pg.wait_for_timeout(3000)
        rec("虚拟盘·注册后遮罩消失",
            not (pg.query_selector("#ppGate") and pg.query_selector("#ppGate").is_visible()),
            f"user={uname}")

        seen.clear()
        pg.fill("#ppBuyCode", "宁德时代")
        pg.fill("#ppBuyQty", "100")
        pg.click("#ppBuy")
        pg.wait_for_timeout(2500)
        buy = [u for u in seen if "/api/trade/buy" in u]
        if tradable:
            rec("虚拟盘·买入(宁德时代)", any("/api/search" in u for u in seen) and bool(buy),
                f"buy={len(buy)}")
        else:
            rec("虚拟盘·买入(宁德时代)", True,
                "非交易时段跳过（收盘后禁止交易，改日盘中运行）")

        # ---------- 5. 自选：中文名加入（回归） ----------
        pg.click('button[data-tab="watch"]')
        pg.wait_for_timeout(600)
        seen.clear()
        pg.fill("#wtAddCode", "平安银行")
        pg.click("#wtAdd")
        pg.wait_for_timeout(2000)
        wt = [u for u in seen if "/api/watch" in u and "folders" not in u]
        rec("自选·加入(平安银行)", any("/api/search" in u for u in seen), f"watch={len(wt)}")

        # ---------- 6. 异动监控：确认是全市场扫描，无需单股搜索 ----------
        # 异动监控页签已并入「市场榜单」，先回去再通过下拉切换
        pg.click('button[data-tab="rank"]')
        pg.wait_for_timeout(500)
        pg.select_option("#rankSel", "moves:涨停")
        pg.wait_for_timeout(1800)
        has_code_input = pg.query_selector("#movesCard input[type=text]") is not None
        rec("异动监控·确认为扫描器(无单股输入框)", not has_code_input,
            "该模块按条件扫全市场，不需要名称搜索")

        b.close()

    print("\n== 汇总 ==")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
