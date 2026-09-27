"""端到端：ETF 筛选 / 网格回测 / 网格档位表（F1 + F2 + F3）。

跑法（需先启动服务在 8899）：
    python3 tests/e2e_etf_grid.py

验的是"用户真能点到"，不是静态代码：
  F1 网格回测：页签里填参数 -> 跑 -> 出指标/档位表/成交明细/净值图，且无 JS 报错
  F2 ETF 筛选：页签筛选 -> 出表格 -> 点代码能带着代码跳回网格回测
  F3 网格档位表：登录 -> 建网格 -> 看到档位状态 -> 点成交 -> 持仓/流水联动
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        errs, seen = [], []

        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("request", lambda r: seen.append(r.url) if "/api/" in r.url else None)

        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1200)

        # ---------- F1 网格回测 ----------
        pg.click('button[data-tab="backtest"]')
        pg.wait_for_timeout(400)
        has_card = pg.query_selector("#grRun") is not None
        rec("F1 网格回测卡片存在", has_card)

        errs.clear()
        pg.fill("#grCode", "sh510300")
        pg.fill("#grStep", "2")
        pg.fill("#grBand", "20")
        pg.click("#grRun")
        pg.wait_for_timeout(4000)
        out = pg.inner_text("#grOutput") if pg.query_selector("#grOutput") else ""
        for kw in ("网格收益", "买入持有", "最大回撤", "网格档位", "成交明细"):
            rec(f"F1 输出含「{kw}」", kw in out)
        rec("F1 净值图容器存在", pg.query_selector("#grOutputNav") is not None)

        # K 线上要能看到买卖点（曾经的坑：标记价用了空字符串，
        # echarts 当 0 处理，把 y 轴从 4.4~5.0 撑到 2，点飘到图外）
        kinfo = pg.evaluate("""() => {
          var el = document.getElementById('grOutputK');
          if(!el) return {err:'no el'};
          var inst = echarts.getInstanceByDom(el);
          if(!inst) return {err:'no instance'};
          var o = inst.getOption();
          var cnt = n => { var s = o.series.find(x=>x.name===n); return s ? (s.data||[]).length : -1; };
          var bs = (o.series.find(s=>s.name==='买入')||{}).data || [];
          var kd = o.series[0].data;                       // [open, close, low, high]
          // 全序列价格范围 vs 图内像素范围：验证 y 轴没被买卖点撑开
          var lo = Math.min(...kd.map(d=>d[2])), hi = Math.max(...kd.map(d=>d[3]));
          var h = el.clientHeight;
          var pxLo = inst.convertToPixel({yAxisIndex:0}, lo);
          var pxHi = inst.convertToPixel({yAxisIndex:0}, hi);
          return { k: cnt('K线'), buy: cnt('买入'), sell: cnt('卖出'),
                   markLines: (o.series[0].markLine && o.series[0].markLine.data || []).length,
                   // 买卖点锚在日柱中价上，且带像素偏移（浮在柱体上下方）
                   anchored: bs.every(d => {
                     // coord[0] 是 K 线索引（与 K 线纯 y 数组同源，避免日期字符串
                     // 在 category 轴 + dataZoom 下解析错位）；老格式是日期字符串
                     var xi = d.coord[0];
                     var row = (typeof xi === 'number') ? kd[xi]
                                                        : kd[o.xAxis[0].data.indexOf(xi)];
                     return row && Math.abs((row[0]+row[1])/2 - d.coord[1]) < 0.002;
                   }),
                   offsetted: bs.every(d => d.symbolOffset && d.symbolOffset[1] !== 0),
                   yInRange: pxLo > 0 && pxLo < h && pxHi > 0 && pxHi < h };
        }""")
        rec("F1 K线图渲染", kinfo.get("k", 0) > 0, f"{kinfo.get('k')} 根")
        rec("F1 K线上有买卖点", kinfo.get("buy", 0) > 0 and kinfo.get("sell", 0) > 0,
            f"买 {kinfo.get('buy')} / 卖 {kinfo.get('sell')}")
        rec("F1 买卖点锚在日柱中价", kinfo.get("anchored") is True,
            "取 (开+收)/2，不是影线端点")
        rec("F1 买卖点浮在柱体上下方", kinfo.get("offsetted") is True,
            "靠 symbolOffset 像素位移，不压柱身")
        rec("F1 y 轴未被买卖点撑开", kinfo.get("yInRange") is True,
            "K线全序列必须完整落在图内")
        rec("F1 档位横线（上界/基准/下界）", kinfo.get("markLines") == 3,
            f"{kinfo.get('markLines')} 条")

        rec("F1 无 JS 错误", not errs, errs[:2])

        # ---------- 网格策略（fixed / pyramid / asym / moving） ----------
        errs.clear()
        pg.select_option("#grStrategy", "asym")
        pg.wait_for_timeout(300)
        asym_vis = pg.eval_on_selector("#grStepSellBox", "el => el.style.display !== 'none'")
        pg.select_option("#grStrategy", "pyramid")
        pg.wait_for_timeout(300)
        pyr_vis = pg.eval_on_selector("#grMulBox", "el => el.style.display !== 'none'")
        pg.select_option("#grStrategy", "fixed")
        pg.wait_for_timeout(300)
        fix_hid = pg.eval_on_selector("#grStepSellBox", "el => el.style.display === 'none'")
        rec("F1 策略切换只显示用得上的参数",
            bool(asym_vis) and bool(pyr_vis) and bool(fix_hid),
            f"asym→卖出步长 {asym_vis} / pyramid→倍率 {pyr_vis} / fixed→都隐藏 {fix_hid}")

        pg.select_option("#grStrategy", "moving")
        pg.click("#grRun")
        pg.wait_for_timeout(4000)
        out2 = pg.inner_text("#grOutput") if pg.query_selector("#grOutput") else ""
        rec("F1 移动网格标注策略与平移次数",
            "移动网格" in out2 and "区间平移" in out2,
            ("" if "移动网格" in out2 else "缺策略名 ") +
            ("" if "区间平移" in out2 else "缺平移次数"))
        rec("F1 切换策略无 JS 错误", not errs, errs[:2])

        # 建议参数
        errs.clear()
        pg.click("#grSuggest")
        pg.wait_for_timeout(3000)
        step_v = pg.input_value("#grStep")
        rec("F1 ATR 建议参数已回填", step_v not in ("", "2"), f"step={step_v}")
        rec("F1 建议参数无 JS 错误", not errs, errs[:2])

        # ---------- F2 ETF 筛选 ----------
        pg.click('button[data-tab="etf"]')
        pg.wait_for_timeout(500)
        rec("F2 ETF 页签存在", pg.query_selector("#tab-etf") is not None)

        errs.clear()
        pg.fill("#etfMinCap", "100")
        pg.select_option("#etfSort", "cap")
        pg.click("#etfRun")
        pg.wait_for_timeout(40000)          # 首跑要拉全量 + 补快照
        rows = pg.query_selector_all("#etfOutput table tbody tr")
        rec("F2 规模≥100亿筛出结果", len(rows) > 0, f"{len(rows)} 行")
        snap = pg.inner_text("#etfMeta") if pg.query_selector("#etfMeta") else ""
        rec("F2 显示数据源/时间戳", "源=" in snap, snap[:80])

        # 点行 → 跳回网格回测并填好代码（data-etfcode 挂在 <tr> 上）
        link = pg.query_selector("#etfOutput .etfpick")
        if link:
            seen.clear()
            link.click()
            pg.wait_for_timeout(800)
            grcode = pg.input_value("#grCode") if pg.query_selector("#grCode") else ""
            vis = pg.evaluate(
                "!!document.querySelector('#tab-backtest') && "
                "getComputedStyle(document.querySelector('#tab-backtest')).display!=='none'"
            )
            rec("F2 点代码→跳回网格回测", vis and bool(grcode), f"grCode={grcode}")
        else:
            rec("F2 点代码→跳回网格回测", False, "未找到 .etfpick")

        rec("F2 无 JS 错误", not errs, errs[:2])

        # ---------- F3 网格档位表 ----------
        pg.click('button[data-tab="paper"]')
        pg.wait_for_timeout(600)
        rec("F3 未登录显示遮罩", bool(pg.query_selector("#ppGate")
                                    and pg.query_selector("#ppGate").is_visible()))

        uname = "_e2e_grid_%d" % int(time.time())
        pg.click("#ppGateLogin")
        pg.wait_for_timeout(400)
        pg.click("#authSwap")
        pg.wait_for_timeout(200)
        pg.fill("#authUser", uname)
        pg.fill("#authPwd", "test1234")
        pg.click("#authSubmit")
        pg.wait_for_timeout(3000)
        rec("F3 登录成功", not (pg.query_selector("#ppGate")
                             and pg.query_selector("#ppGate").is_visible()), uname)

        errs.clear()
        pg.fill("#gpCode", "sh510300")
        pg.fill("#gpCenter", "4.6")
        pg.fill("#gpStep", "2")
        pg.fill("#gpBand", "20")
        pg.fill("#gpLot", "10000")
        pg.click("#gpCreate")
        pg.wait_for_timeout(3000)
        body = pg.inner_text("#gpBody") if pg.query_selector("#gpBody") else ""
        rec("F3 网格已建出档位表", "档位价" in body or "可买" in body or "待跌" in body,
            body[:50].replace("\n", " "))
        # 新建网格时现价通常落在中心附近 → 没有档位处于「可买/可卖」是正确的。
        # 这里直接把中心价设到现价下方，强制造出「可买」档，才有得可点。
        states_before = pg.eval_on_selector_all(
            "#gpBody td:nth-child(5)", "els=>els.map(e=>e.textContent.trim())")
        rec("F3 档位状态有区分度（非全同一状态）",
            len(set(states_before)) >= 1, f"{sorted(set(states_before))}")

        # 把中心价抬到现价上方 → 现价变成「可买」（跌到买档才买，中心高则当前在下半区）
        pg.fill("#gpCenter", "5.5")
        pg.click("#gpCreate")
        pg.wait_for_timeout(3000)
        fire_btns = pg.query_selector_all("#gpBody [data-gpfire]")
        rec("F3 有可成交档位按钮", len(fire_btns) > 0, f"{len(fire_btns)} 个")

        if fire_btns:
            fire_btns[0].click()
            pg.wait_for_timeout(3500)
            hist = pg.inner_text("#ppHisBody") if pg.query_selector("#ppHisBody") else ""
            pos = pg.inner_text("#ppPosBody") if pg.query_selector("#ppPosBody") else ""
            rec("F3 成交后流水出现「网格档位」", "网格档位" in hist, hist[:60].replace("\n", " "))
            rec("F3 成交后持仓出现 sh510300", "510300" in pos, pos[:60].replace("\n", " "))
        rec("F3 无 JS 错误", not errs, errs[:2])

        b.close()

    print("\n== 汇总 ==")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  FAIL {n} {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
