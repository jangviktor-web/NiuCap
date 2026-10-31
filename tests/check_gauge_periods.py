"""三张半圆仪表的周期切换自检（1分~1月，与 TradingView「指标概览」同序）。

为什么单独一份：`smoke_stock_viz.py` 只守「日线页的仪表长得对」。
周期切换的坑全在「换过去以后数字是不是那个周期的」——
曾经有三个真实缺陷，每一个都能让页面看起来很健康：

  1. 周期条点了没反应，只有 K 线图变了、仪表现在还是日线读数；
  2. /api/indicators_full 收到 '1M' 当成了 1 分钟（'1M'.lower() == '1m'），
     月线仪表悄悄变成「最近 5 天的分钟线」；
  3. 根数不够（月线 28 根、200 周期均线要 300 根）时，少了两项照样出结论，
     总结被静默算偏——数字算得出来，口径完全错了。

跑法（需先启动服务，端口 8899）：
    cd /workspace/tick-stock-panel && .venv/bin/python server/run.py &
    .venv/bin/python tests/check_gauge_periods.py

覆盖：
  A 后端：8 个周期都能返回，period 字段与请求一致（守 '1M' 别被当分钟）
  B 前端：8 个周期逐个点，高亮 / 读数 / 指针角度都跟着换
  C 根数不足的周期给「本周期无评级」+ 原因，而不是空白或旧读数
  D 指针角度能被读数复算出来（避免「换了数字没换针」）
  E 全程无 JS 报错
"""
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
CODE = os.environ.get("CODE", "sz301603")
CHROME = os.environ.get("CHROME", "/usr/bin/chromium")

#: TradingView 的周期顺序，前端按这个排。'1M' 是大写月度，'1m' 是 1 分钟。
TFS = ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M"]

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'✅' if ok else '❌'} {name}  {detail}")


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


# ------------------------------------------------------- A 后端
def test_api():
    print("── A 后端：8 个周期 ──")
    for p in TFS:
        try:
            d = get(f"/api/indicators_full?code={CODE}&period={p}")
        except Exception as e:
            rec(f"A {p} 接口可用", False, str(e)[:80])
            continue
        # '1M' 必须回 '1M'，回成 '1m' 说明被当成 1 分钟了
        rec(f"A {p} period 回显一致", d.get("period") == p,
            f"请求 {p} → 返回 {d.get('period')}")
        g = d.get("rating_gauges")
        bars, need = d.get("bars") or 0, d.get("bars_need") or 0
        if g:
            rec(f"A {p} 三表齐全且 26 项", len(g) == 3 and g[1]["total"] == 26,
                f"bars={bars} · {[x['total'] for x in g]}")
            # avg 是 round(...,4) 后回的，容差按 1e-3 算——1e-6 会
            # 把「1/11 的舍入」误判成算法错，这种测试只会消耗信任。
            rec(f"A {p} 总结 = (震荡均值+均线均值)/2",
                abs(g[1]["avg"] - (g[0]["avg"] + g[2]["avg"]) / 2) < 1e-3,
                f"{g[1]['avg']:.4f} vs {(g[0]['avg'] + g[2]['avg']) / 2:.4f}")
        else:
            rec(f"A {p} 根数不足时不硬给结论", bars < need,
                f"bars={bars} need={need}（有数据却返回 None 就是错）")


# ------------------------------------------------------- B/C/D 前端
def login(pg):
    pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(700)
    pg.evaluate("""async ()=>{
      await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:'bltest',password:'bltest123',display:'x'})}).catch(()=>{});
      await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:'bltest',password:'bltest123'})});}""")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_timeout(1400)


#: 页面里读一次仪表状态。用 evaluate 而不是 selector，是因为换周期会整页重绘，
#: 元素句柄会失效；每次现查 DOM 才拿得到当前那一份。
READ = """()=>{
  var box=document.getElementById('gaugeBox');
  if(!box) return null;
  var on=(document.querySelector('#gaugeTfBtns .btn.on')||{}).dataset;
  var gs=[...box.querySelectorAll('.gauge')].map(function(el){
    var nd=el.querySelector('.g-needle');
    return {k:el.dataset.gauge, val:el.querySelector('.g-val').textContent.trim(),
            tr:!!el.closest('.gauges').dataset.tr,
            rot:(String(nd.style.transform||'').match(/-?[\\d.]+(?=deg)/)||['0'])[0]};
  });
  return {on:on&&on.gtf, gs:gs,
          none:!!box.querySelector('.gauge-none'),
          txt:((box.querySelector('.gauge-none')||{}).textContent||'').trim(),
          p:state.stockPeriod, m:state.stockMinute};
}"""

#: 后端同一周期的读数，用于核对「页面显示的就是这个周期的数」
SNAP = """async (a)=>{
  var r=await fetch('/api/indicators_full?code='+a.code+'&period='+encodeURIComponent(a.per));
  if(!r.ok) return {err:'HTTP '+r.status};
  var d=await r.json();
  return {avg:(d.rating_gauges||[]).map(x=>x.avg), none:!d.rating_gauges};
}"""


def test_ui(page, code):
    print("── B/C/D 前端：8 个周期切换 ──")
    page.evaluate("(c)=>{document.querySelector('[data-tab=\"stock\"]').click(); openStock(c);}", code)
    page.wait_for_selector("#gaugeTfBtns .btn", timeout=60000)
    page.wait_for_timeout(1800)
    # 登录弹窗会拦住点击；它测的是权限不是这里的事，关掉继续
    page.evaluate("()=>{var m=document.getElementById('authModal');"
                  " if(m){m.classList.remove('on'); m.style.display='none';}}")

    prev = None
    for p in TFS:
        page.evaluate(f"()=>{{var b=document.querySelector('#gaugeTfBtns .btn[data-gtf=\"{p}\"]');"
                      f" if(b) b.click();}}")
        page.wait_for_timeout(2600)
        st = page.evaluate(READ)
        if st is None:
            rec(f"B {p} 仪表区存在", False, "gaugeBox 缺失（整页被错误状态覆盖？）")
            continue

        rec(f"B {p} 周期条高亮", st["on"] == p, f"on={st['on']}")
        rec(f"B {p} 周期状态已切", st["p"] == p and not st["m"],
            f"stockPeriod={st['p']} stockMinute={st['m']}")

        # 单参数传：Playwright 会把**一个** list 参数当 JS 数组解成多个实参，
        # 这里包一层对象才稳定（踩过：truth 恒为 undefined 路径，静默走 else）
        truth = page.evaluate(SNAP, {"code": code, "per": p})
        if truth.get("err"):
            rec(f"{p} 后端可查", False, truth["err"])
            prev = st
            continue
        if truth["none"]:
            rec(f"C {p} 根数不足时给出原因", st["none"] and len(st["txt"]) > 10,
                st["txt"][:60])
            rec(f"C {p} 不残留上一周期读数", not st["gs"],
                f"仍有 {len(st['gs'])} 张表")
        else:
            rec(f"B {p} 三张表都画出来", len(st["gs"]) == 3, f"{len(st['gs'])} 张")
            # 角度 = avg*90，是前端唯一的口径换算，错了针就是歪的
            ok = all(abs(float(g["rot"]) - a * 90) < 0.6
                     for g, a in zip(st["gs"], truth["avg"]))
            rec(f"D {p} 指针角度 = avg×90", ok,
                " / ".join(f"{g['rot']}° vs {a * 90:.1f}°"
                           for g, a in zip(st["gs"], truth["avg"])))
            rec(f"D {p} 指针有过渡（不是瞬间闪现）",
                all(g["tr"] for g in st["gs"]), "")

        # 「不是没换」的正确写法：证明仪表显示的是**本周期**的读数，而不是上一周期
        # 的残留。原来这里比的是「与上一个周期的角度必须不同」——那是赌行情：
        # 2026-10-29 实测 15m 与 30m 读数恰好完全相等（[0, .2667, .5333]），
        # 角度自然一样，测试就假红。数据巧合不是代码缺陷，不能拿它当断言。
        # 现在比的是「屏幕上的值 == 本周期后端返回的值」，这条永远不会因行情撞车。
        if prev and prev["gs"] and st["gs"] and not truth.get("none"):
            match = all(abs(float(g["rot"]) - a * 90) < 0.6
                        for g, a in zip(st["gs"], truth["avg"]))
            same_as_prev = all(abs(float(a["rot"]) - float(b["rot"])) < 0.5
                               for a, b in zip(prev["gs"], st["gs"]))
            rec(f"B {p} 显示的是本周期读数（不是上一周期残留）", match,
                f"{prev['on']}→{p}" + ("（与上一周期数值恰好相同，属行情巧合）"
                                       if same_as_prev else ""))
        prev = st


def test_narrow():
    """390px 下 8 个周期标签要一屏放得下。

    放不下时浏览器只横向滚动不报错，「1月」被切一半——用户会以为只有 7 个周期。
    """
    print("── F 窄屏：周期条不溢出 ──")
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, executable_path=CHROME)
        pg = br.new_page(viewport={"width": 390, "height": 844})
        login(pg)
        pg.evaluate("(c)=>{document.querySelector('[data-tab=\"stock\"]').click(); openStock(c);}", CODE)
        pg.wait_for_selector("#gaugeTfBtns .btn", timeout=60000)
        pg.wait_for_timeout(1500)
        m = pg.evaluate("""()=>{
          var b=document.getElementById('gaugeTfBtns');
          var last=b.lastElementChild.getBoundingClientRect();
          var first=b.firstElementChild.getBoundingClientRect();
          return {n:b.children.length, scrollW:b.scrollWidth, clientW:b.clientWidth,
                  firstL:first.left, lastR:last.right, vw:innerWidth};
        }""")
        rec("F 8 个周期都在", m["n"] == 8, f"{m['n']} 个")
        rec("F 周期条不需要横向滚动", m["scrollW"] <= m["clientW"] + 1,
            f"scrollW={m['scrollW']} clientW={m['clientW']}")
        rec("F 最后一个标签完整在视口内", m["lastR"] <= m["vw"],
            f"right={m['lastR']:.0f} vw={m['vw']}")
        rec("F 整页无横向溢出",
            pg.evaluate("()=>document.documentElement.scrollWidth"
                        "-document.documentElement.clientWidth") == 0, "")
        br.close()


def main():
    test_api()

    errs = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, executable_path=CHROME)
        pg = br.new_page(viewport={"width": 1440, "height": 1000})
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        # 401/429/400 是权限与配额，不是这三个表的事，别让它污染 E
        pg.on("console", lambda m: errs.append(m.text[:160])
              if m.type == "error" and not any(k in m.text for k in ("401", "429", "400"))
              else None)
        login(pg)
        test_ui(pg, CODE)
        rec("E 全程无 JS 报错", not errs, "; ".join(errs[:2]))
        br.close()

    test_narrow()

    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} PASS")
    if bad:
        print("失败项：")
        for n, _ok, d in bad:
            print(f"  · {n}  {d}")
        sys.exit(1)


if __name__ == "__main__":
    main()
