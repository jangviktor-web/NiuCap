"""ETF 规模占比卡的浏览器自检：渲染、两个图、单位不混轴、无 JS 报错、窄屏不溢出。

2026-10-29 线上故障回归：接口曾报「ETF 池为空」，真实原因是上游清单接口
超时（新浪连续 3 次 26s 超时、东财高频断连）。现在数据源换成腾讯搜索 +
静态种子表，本脚本除了 UI 还守一条：**池子不能是空的**，且错误信息不能
把「拉取失败」说成「没有沪深300 标的」——后者会让人以为市场上真没这产品。
"""
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
CHROME = os.environ.get("CHROME", "/usr/bin/chromium")
res = []


def rec(name, ok, detail=""):
    res.append(bool(ok))
    print(("✅" if ok else "❌") + f" {name}  {detail}")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.load(r)


def test_api():
    """后端：池子非空、比例合理、失败信息不误导。"""
    print("── A 后端 ──")
    try:
        d = api("/api/hs300_etf_ratio")
    except Exception as e:
        rec("A 接口可用", False, f"{type(e).__name__}: {e}")
        return
    if not d.get("ok"):
        err = str(d.get("error") or "")
        # 失败可以接受（网络抖动），但**不能说成池为空**：那会把
        # 「上游挂了」误报成「市场上没有沪深300 ETF」。
        rec("A 失败信息不谎称无标的", "池为空" not in err and "没有沪深300" not in err, err[:80])
        rec("A 失败信息带真实原因", len(err) > 8, err[:80])
        # 但「一只候选都没有」不算网络抖动——那是枚举环节自己断了，
        # 就是这次 P0 的形状。必须红，否则注入这个缺陷时整组照样报绿
        # （第一次注入实测：只有 UI 那几条红，A 组两条全绿，等于没守住）。
        rec("A 枚举环节没断（候选表非空）", "候选 0 只" not in err, err[:80])
        return

    rec("A 池子非空", (d.get("pool_n") or 0) >= 10, f"pool_n={d.get('pool_n')}")
    t = d["today"]
    # 量级守卫：实测 0.78%。掉到 0.1% 以下通常是池子漏了一大半，
    # 涨到 5% 以上通常是把指数市值当成了别的口径。
    rec("A 比例量级合理", 0.1 < t["ratio"] * 100 < 5,
        f"{t['ratio'] * 100:.4f}%")
    rec("A 分母是万亿级（指数流通市值）", t["index_cap"] > 300000,
        f"{t['index_cap']:.0f} 亿")
    top = (d.get("pool") or [{}])[0]
    rec("A 头号 ETF 是华泰柏瑞 510300", top.get("code") == "510300",
        str(top.get("code")))
    rec("A 季报有历史", len(d.get("hist") or []) >= 20,
        f"{len(d.get('hist') or [])} 期")


SNAP = """()=>{
  var box=document.getElementById('erBody');
  var hc=document.getElementById('erHistChart');
  var tc=document.getElementById('erTodayChart');
  var hero=box?box.querySelector('.bbhero .n'):null;
  // 「两个不同容器」必须比 DOM 节点本身，不能比 id 字符串——id 恒不同，
  // 那样写无论是两个元素还是一个元素都会通过（第一版就是这么假绿的）。
  var distinct = !!(hc && tc && hc !== tc && !hc.contains(tc) && !tc.contains(hc));
  return {
    has: !!box,
    text: box ? box.innerText.slice(0,400) : '',
    hero: hero ? hero.textContent : null,
    histChart: !!hc && hc.clientHeight>0,
    todayChart: !!tc && tc.clientHeight>0,
    distinctDom: distinct,
    canvases: box ? box.querySelectorAll('canvas').length : 0,
    err: window.__erErr || null
  };
}"""

# 后端组先跑：它不需要浏览器，上游一挂能立刻报出原因，不用等 90s 页面超时。
test_api()

with sync_playwright() as p:
    br = p.chromium.launch(headless=True, executable_path=CHROME)
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(1200)
    pg.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg.wait_for_selector("#erBody .bbhero, #erBody .empty", timeout=90000)
    pg.wait_for_timeout(2500)

    d = pg.evaluate(SNAP)
    rec("卡片已渲染", d["has"])
    rec("主读数形如百分比", bool(d["hero"]) and d["hero"].strip().endswith("%"), str(d["hero"]))
    rec("季报图有高度", d["histChart"])
    rec("今日图有高度", d["todayChart"])
    rec("两图是两个独立 DOM 节点", d["distinctDom"])
    rec("两张 echarts 画布", d["canvases"] >= 2, f"canvas={d['canvases']}")
    # 单位说明必须出现在正文里，否则「亿份」会被当「%」
    rec("正文说明了两套单位", "亿份" in d["text"] and "%" in d["text"])

    # 主题切换后必须重绘（走 chartCache.er）
    pg.evaluate("()=>{toggleTheme();}")
    pg.wait_for_timeout(1500)
    d2 = pg.evaluate(SNAP)
    rec("切夜间后两图仍在", d2["histChart"] and d2["todayChart"])
    rec("切夜间后主读数不变", d2["hero"] == d["hero"], f'{d["hero"]} vs {d2["hero"]}')

    # C 新功能（参考 baifenwei 优化）：百分位表 + 跨度切换 + 双轴
    d3 = pg.evaluate("""()=>{
      var pt=document.querySelector('.pcttbl');
      var bar=document.getElementById('erSpanBar');
      var c=bar?bar.querySelectorAll('.erseg').length:0;
      var leg=null;
      try{ leg=(echarts.getInstanceByDom(document.getElementById('erHistChart'))
        .getOption().legend||[])[0]; }catch(e){}
      return {pctTable: !!pt, pctText: pt?pt.innerText.replace(/\\s+/g,' ').slice(0,120):'',
        segN: c, legend: leg? (leg.data||[]) : null};
    }""")
    rec("百分位表存在", d3["pctTable"], d3["pctText"][:60])
    rec("百分位有 4 个窗口读数", d3["pctText"].count("%") >= 4, d3["pctText"][:80])
    rec("跨度条有 4 档", d3["segN"] == 4, f"seg={d3['segN']}")
    rec("上图是双轴（占比+指数收盘）",
        d3["legend"] and d3["legend"] == ["510300占比", "沪深300收盘"],
        str(d3["legend"]))
    # 切「近3年」必须真的重画：点数变少（季频 3 年约 12 点 vs 全部 58 点）
    n_all = pg.evaluate("""()=>echarts.getInstanceByDom(
      document.getElementById('erHistChart')).getOption().series[0].data.length""")
    pg.evaluate("""()=>{document.querySelector('#erSpanBar .erseg[data-s="3"]').click();}""")
    pg.wait_for_timeout(600)
    n_3y = pg.evaluate("""()=>echarts.getInstanceByDom(
      document.getElementById('erHistChart')).getOption().series[0].data.length""")
    rec("切近3年后点数变少（真重画，不是按钮摆设）",
        0 < n_3y < n_all, f"{n_all} → {n_3y}")
    rec("切回全部", (pg.evaluate(
        """()=>{document.querySelector('#erSpanBar .erseg[data-s=\"all\"]').click();
        return echarts.getInstanceByDom(document.getElementById('erHistChart'))
          .getOption().series[0].data.length;}""")) == n_all)

    # 窄屏不横向溢出
    pg2 = br.new_page(viewport={"width": 390, "height": 844})
    pg2.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg2.wait_for_timeout(1200)
    pg2.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg2.wait_for_selector("#erBody .bbhero, #erBody .empty", timeout=90000)
    pg2.wait_for_timeout(2000)
    ov = pg2.evaluate("()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
    rec("390px 无横向溢出", ov["sw"] <= ov["cw"] + 1, f'scrollW={ov["sw"]} clientW={ov["cw"]}')

    # 只断言「本功能相关」的报错。页面里还有需要登录的接口（如 /api/screen/fields
    # 在未登录时 401），那是权限设计、不是这里要守的东西——把它算进来只会让
    # 这条检查长期假红，然后被人忽略。
    mine = [e for e in errs if "hs300_etf_ratio" in e or "erBody" in e or "drawEr" in e
            or ("401" not in e and "Failed to load resource" not in e)]
    rec("本功能无 JS 报错", len(mine) == 0, "; ".join(mine[:3]))
    br.close()

ok = sum(res)
print(f"\n{ok}/{len(res)} PASS")
sys.exit(0 if ok == len(res) else 1)
