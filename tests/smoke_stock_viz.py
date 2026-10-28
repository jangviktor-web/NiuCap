# -*- coding: utf-8 -*-
"""个股页可视化改造的端到端检查。

覆盖四类改动，每一条都是"改了会静默坏掉"的地方：

  1. 页头：大字价格、涨跌、日内区间位置、涨跌停幅度
     —— 涨跌停幅度必须基于**昨收**算。用现价算会把对称的 ±10%
        算成 +11.48% / -8.83%，看着像规则错了。
  2. 主图：关键价位线、现价线、区间统计
     —— 价位线会因彼此太近而叠字，所以选线要按像素间距筛；
        不能退回"每侧都画 4 条"。
  3. 分区导航：9 个锚点都要跳准、跳完标题不能被吸顶栏盖住、高亮跟着滚动走
     —— 高亮必须按纵向位置取，不能按 DOM 顺序（两者在本页不一致）。
  4. 主题：明暗两套下关键元素都不能"看不见"
     —— tooltip 文字曾经写死近白色，白天模式下白底白字。

用法：cd /workspace/tick-stock-panel && .venv/bin/python tests/smoke_stock_viz.py
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
CHROME = "/usr/bin/chromium"
SHOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shots")

passes, fails = [], []


def ck(name, cond, extra=""):
    (passes if cond else fails).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          ((" :: " + str(extra)) if (extra and not cond) else ""))


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


def open_stock(pg, code="000001"):
    pg.evaluate("(c)=>{document.querySelector('[data-tab=\"stock\"]').click(); openStock(c);}", code)
    pg.wait_for_selector("#ckline canvas", timeout=40000)
    pg.wait_for_timeout(2600)          # 等 K 线 / 指标全画完


def main():
    errs = []
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROME,
                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1500, "height": 1000}, device_scale_factor=2)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append("PAGEERROR " + str(e)))
        # 401 = 登录前探活；400 = register 已存在该用户（登录脚本每次都先注册）；
        # 403 = 非管理员点 admin 页，/api/admin/* 守卫按设计返回（app.py:3553）。
        # 三者都是预期内的接口应答，不是页面 JS 异常，滤掉噪声否则永远红。
        def _noise(t):
            return ("401" in t or "400 (Bad Request)" in t or "403 (Forbidden)" in t)
        pg.on("console", lambda m: errs.append("CONSOLE " + m.text)
              if m.type == "error" and not _noise(m.text) else None)
        login(pg)
        open_stock(pg, "000001")

        # ── 1. 页头
        hd = pg.evaluate("""()=>{
          const q=state.chartCache.stock.quote;
          const price=document.querySelector('.hd-price');
          const chg=[...document.querySelectorAll('.hd-chg span')].map(x=>x.textContent.trim());
          const stats=[...document.querySelectorAll('.hd-stats .k')].map(x=>x.textContent.trim());
          const vals=[...document.querySelectorAll('.hd-stats .v')].map(x=>x.textContent.trim());
          const bar=document.querySelector('.hd-range-bar i');
          const name=document.querySelector('.hd-name');
          return {price:price&&price.textContent.trim(), chg, stats, vals,
                  dot:bar?bar.style.left:null, name:name?name.textContent.trim():null,
                  q:{price:q.price, prev:q.prev_close, up:q.limit_up, down:q.limit_down}};
        }""")
        ck("页头显示股票名", bool(hd["name"]) and len(hd["name"]) > 2, hd["name"])
        ck("页头大字价格与行情一致", hd["price"] == "%.2f" % hd["q"]["price"], hd["price"])
        ck("页头同时给出涨跌额与涨跌幅", len(hd["chg"]) == 2, hd["chg"])
        ck("页头有日内区间圆点", hd["dot"] is not None and hd["dot"].endswith("%"), hd["dot"])
        ck("页头三格是 振幅/涨停价/跌停价",
           hd["stats"] == ["振幅", "涨停价", "跌停价"], hd["stats"])

        # 涨跌停幅度必须以昨收为基准 —— 这是本次修掉的真 bug
        base = hd["q"]["prev"] or hd["q"]["price"]
        want_up = (hd["q"]["up"] / base - 1) * 100
        want_dn = (hd["q"]["down"] / base - 1) * 100
        up_txt, dn_txt = hd["vals"][1], hd["vals"][2]
        ck("涨停幅度按昨收算（不是现价）",
           ("%+.2f%%" % want_up) in up_txt, up_txt)
        ck("跌停幅度按昨收算（不是现价）",
           ("%+.2f%%" % want_dn) in dn_txt, dn_txt)
        ck("涨跌停幅度基本对称（主板 ±10%）",
           abs(abs(want_up) - abs(want_dn)) < 0.5, (want_up, want_dn))

        # ── 2. 主图
        ch = pg.evaluate("""()=>{
          const o=state.charts['ckline'].getOption();
          const s=o.series[0];
          const ml=(s.markLine&&s.markLine.data)||[];
          const hasPriv=(o.graphic||[]).length>0;
          return {lines:ml.map(x=>({y:x.yAxis, name:x.name})),
                  hasStat:hasPriv,
                  candleColor:s.itemStyle.color, candleColor0:s.itemStyle.color0,
                  right:o.grid[0].right};
        }""")
        names = [x["name"] for x in ch["lines"]]
        ck("主图画了现价线", "现价" in names, names)
        ck("主图有支撑/压力价位线", len([n for n in names if n != "现价"]) >= 1, names)
        ck("价位线数量受控（不糊成一团）", len(ch["lines"]) <= 4, len(ch["lines"]))
        ck("主图左上角有区间统计", ch["hasStat"])
        ck("有价签时右侧留白（否则标签被裁）", ch["right"] >= 70, ch["right"])
        ck("蜡烛涨色 = 主题 --ec-up",
           ch["candleColor"].lower() in ("#f6465d", "#d9342b"), ch["candleColor"])
        ck("蜡烛跌色 = 主题 --ec-down",
           ch["candleColor0"].lower() in ("#0ecb81", "#12a15c"), ch["candleColor0"])
        ck("涨跌色不是同一个值（曾出现同页两种红）",
           ch["candleColor"].lower() != ch["candleColor0"].lower())

        # 价签右侧留白必须按**实测最长标签**算。
        # 曾写死 78px，「成交密集 1292.34」实测 79px+padding，右半截被 canvas 裁掉。
        pad = pg.evaluate("""()=>{
          const ch=state.charts['ckline'], o=ch.getOption(), g=o.grid[0];
          const ml=(o.series[0].markLine&&o.series[0].markLine.data)||[];
          const mc=document.createElement('canvas').getContext('2d');
          let worst=null;
          ml.forEach(x=>{
            const t=(x.label&&x.label.formatter)||'';
            mc.font=((x.label&&x.label.fontSize)||10)+'px sans-serif';
            const w=Math.ceil(mc.measureText(t).width)+6;   // 6 = padding [1,3]
            if(!worst||w>worst.w) worst={txt:t,w};
          });
          if(!worst) return {none:true};
          // 价签从 grid 右边界起、向右画，所以实际终点 = 绘图区右缘 + 标签宽
          const rightEdge = ch.getWidth() - g.right;
          return {padR:g.right, cw:ch.getWidth(), worst,
                  end: worst.w + rightEdge,
                  fits: worst.w + rightEdge <= ch.getWidth()};
        }""")
        ck("价签留白容得下最长标签（否则右侧被裁）",
           pad.get("fits") is True, pad)

        # 区间统计块必须在绘图区上方，不能压在 Y 轴刻度上。
        # Y 轴 scale:true 时刻度位置随行情变，所以它是横排一行 + grid.top 让带，
        # 不能再靠调 top 躲。
        # 注意 getOption() 会把 graphic 的子节点规范化成 elements（不是 children），
        # 且会丢掉 left/top 的最终值——所以这里只验结构与排版意图，
        # 视觉上「没压住刻度」由截图人工确认。
        stat = pg.evaluate("""()=>{
          const ch=state.charts['ckline'], o=ch.getOption();
          const g=o.grid[0];
          const gr=(o.graphic||[])[0];
          const kids=(gr&&(gr.elements||gr.children))||[];
          // elements[0] 是 group 本身；后面才是文字项。
          // 竖排时文字项带 top 偏移，横排时 top 全为 null。
          const texts=kids.filter(c=>c.type==='text');
          const stacked=texts.filter(c=>c.top&&c.top>0).length;
          return {hasGr:!!gr, texts:texts.length, stacked, gridTop:g.top};
        }""")
        ck("区间统计块存在（横排 5 段文字）",
           stat["hasGr"] and stat["texts"] >= 4, stat)
        ck("统计块横排且为绘图区让出顶部带（不压刻度）",
           stat["stacked"] == 0 and stat["gridTop"] >= 48, stat)

        # x 轴日期要抽稀：250+ 根 K 线全画就是一团重叠
        xa = pg.evaluate("""()=>{
          const ch=state.charts['ckline'], o=ch.getOption();
          const ax=o.xAxis[0];
          return {hasFn: typeof (ax.axisLabel&&ax.axisLabel.interval)==='function',
                  hide: !!(ax.axisLabel&&ax.axisLabel.hideOverlap),
                  n:(ax.data||[]).length};
        }""")
        ck("x 轴日期做了抽稀（长序列不重叠）",
           xa["hasFn"] and xa["hide"] and xa["n"] > 100, xa)

        # ── 2b. 技术分析三表（震荡/均线/总结）
        # 数据来自后端 rating_gauges，指针角度 = (score-50)/50*90。
        # 三个表必须都在、且指针角度与 score 对得上——只看「有没有画出来」
        # 是不够的，角度算错的话指针会指向错误的结论。
        gg = pg.evaluate("""()=>{
          const boxes=[...document.querySelectorAll('.gauge')];
          return boxes.map(b=>{
            const g=b.querySelector('.g-needle');
            const m=g && /rotate\\((-?[\\d.]+)deg\\)/.exec(g.getAttribute('style')||'');
            const key=b.dataset.gauge;
            const src=(state._gauges||[]).find(x=>x.key===key)||{};
            return {key, title:(b.querySelector('.g-title')||{}).textContent,
                    val:(b.querySelector('.g-val')||{}).textContent,
                    deg:m?parseFloat(m[1]):null,
                    score:src.score, rating:src.rating,
                    segs:b.querySelectorAll('.g-seg').length,
                    votes:(b.querySelector('.g-votes')||{}).textContent||''};
          });
        }""")
        ck("技术指标卡片有三张仪表", len(gg) == 3, [x["key"] for x in gg])
        ck("仪表顺序是 震荡/均线/总结",
           [x["key"] for x in gg] == ["osc", "ma", "sum"], [x["key"] for x in gg])
        ck("每张表都画了 5 段弧", all(x["segs"] == 5 for x in gg), [x["segs"] for x in gg])
        # 角度必须与 score 一致（容差 1.5°，取整误差）
        bad_ang = [x for x in gg
                   if x["score"] is not None and x["deg"] is not None
                   and abs(x["deg"] - max(-90, min(90, (x["score"] - 50) / 50 * 90))) > 1.5]
        ck("指针角度与评分一致", not bad_ang, bad_ang[:2])
        ck("三表都有票数说明", all(x["votes"] for x in gg), [x["votes"] for x in gg])
        # 均线表不能永远 0 票：MA 系列按「现价 vs 均线」评级，曾是空白
        ma = next((x for x in gg if x["key"] == "ma"), None)
        ck("移动平均线表有评级票（不是永远 0 票）",
           ma and ma["votes"] and "无可评级" not in ma["votes"], ma and ma["votes"])

        vol = pg.evaluate("""()=>{
          const o=state.charts['cvol'].getOption();
          const names=(o.series||[]).map(s=>s.name);
          const bars=(o.series||[])[0].data||[];
          const colors=[...new Set(bars.map(d=>d.itemStyle&&d.itemStyle.color).filter(Boolean))];
          return {names, nColors:colors.length, colors};
        }""")
        ck("成交量按涨跌双色着色", vol["nColors"] == 2, vol["colors"])
        ck("成交量叠加 5 日均量线", "MA5量" in vol["names"], vol["names"])

        # ── 3. 分区导航
        nav = pg.evaluate("""()=>[...document.querySelectorAll('#secNav a')].map(a=>
          ({t:a.textContent.trim(), d:a.dataset.sec}))""")
        ck("分区导航有 9 个锚点", len(nav) == 9, [x["t"] for x in nav])
        # 导航项顺序必须与页面真实纵向顺序一致
        yorder = pg.evaluate("""(ids)=>{
          const y={};
          ids.forEach(id=>{const e=document.getElementById(id);
            if(e) y[id]=Math.round(e.getBoundingClientRect().top+window.scrollY);});
          return y;
        }""", [x["d"] for x in nav])
        seq = [yorder.get(x["d"]) for x in nav]
        ck("导航顺序与页面纵向顺序一致",
           all(isinstance(v, int) for v in seq) and seq == sorted(seq), seq)

        # 逐项跳转：目标不能被吸顶栏吃掉，且高亮要对
        bad_jump = []
        for x in nav:
            pg.evaluate("(sec)=>{[...document.querySelectorAll('#secNav a')]"
                        ".find(a=>a.dataset.sec===sec).click()}", x["d"])
            pg.wait_for_timeout(950)
            r = pg.evaluate("""(sec)=>{
              const el=document.getElementById(sec), nav=document.getElementById('secNav');
              return {top:Math.round(el.getBoundingClientRect().top),
                      navB:Math.round(nav.getBoundingClientRect().bottom),
                      on:[...document.querySelectorAll('#secNav a.on')].map(a=>a.dataset.sec)};
            }""", x["d"])
            if r["top"] < r["navB"] or r["on"] != [x["d"]]:
                bad_jump.append((x["t"], r))
        ck("9 个锚点全部跳准且高亮正确", not bad_jump, bad_jump[:2])

        # 手动滚动时高亮也要跟着走（不能只在点击时更新）
        pg.evaluate("()=>window.scrollTo(0, document.body.scrollHeight*0.75)")
        pg.wait_for_timeout(700)
        on_late = pg.evaluate("""()=>[...document.querySelectorAll('#secNav a.on')]
          .map(a=>a.textContent.trim())""")
        pg.evaluate("()=>window.scrollTo(0,0)")
        pg.wait_for_timeout(700)
        on_top = pg.evaluate("""()=>[...document.querySelectorAll('#secNav a.on')]
          .map(a=>a.textContent.trim())""")
        ck("滚动到页尾时高亮靠后", on_late and on_late != on_top, (on_top, on_late))

        # ── 4. 明暗主题
        for theme, bg in (("light", "#ffffff"), ("dark", "#181a20")):
            pg.evaluate("(t)=>{document.documentElement.dataset.theme=t}", theme)
            pg.wait_for_timeout(500)
            r = pg.evaluate("""()=>{
              const root=getComputedStyle(document.documentElement);
              const g=k=>root.getPropertyValue(k).trim();
              const o=state.charts['ckline'].getOption();
              return {tipFg:g('--ec-tip-fg'), tipBg:g('--ec-tip-bg'),
                      ecUp:g('--ec-up'), ecDown:g('--ec-down'),
                      candle:o.series[0].itemStyle.color,
                      candle0:o.series[0].itemStyle.color0,
                      hdr:g('--hdr-h')};
            }""")
            ck("[%s] tooltip 文字色与背景不同（不是白底白字）" % theme,
               r["tipFg"] and r["tipFg"].lower() != bg, (r["tipFg"], r["tipBg"]))
            ck("[%s] 图表涨跌色已解析为真实色值（不是 var() 字面量）" % theme,
               not r["candle"].startswith("var(") and not r["candle0"].startswith("var("),
               (r["candle"], r["candle0"]))
            ck("[%s] --hdr-h 已写入实测高度" % theme,
               r["hdr"].endswith("px") and int(r["hdr"][:-2]) > 0, r["hdr"])
        pg.evaluate("()=>{document.documentElement.dataset.theme='light'}")

        # ── 5. 窄屏不溢出
        pg.set_viewport_size({"width": 390, "height": 900})
        # resize 到窄屏后要等两件事：图表 resize（120ms 防抖）和
        # bindMobFold 重算（在 resize 回调里，且要量 scrollHeight）。
        # 只等 600ms 会卡在两者之间，卡片还没被折叠就去做断言。
        pg.wait_for_timeout(1600)
        ovf = pg.evaluate("""()=>{
          const bad=[];
          document.querySelectorAll('#stockWrap *').forEach(el=>{
            const cs=getComputedStyle(el);
            if(el.scrollWidth-el.clientWidth>2 && cs.overflowX!=='auto' && cs.overflowX!=='scroll')
              bad.push({tag:el.tagName, cls:String(el.className).slice(0,36),
                        over:el.scrollWidth-el.clientWidth});
          });
          return {doc:document.documentElement.scrollWidth-window.innerWidth, bad:bad.slice(0,4)};
        }""")
        ck("390px 下无横向溢出", ovf["doc"] <= 0 and not ovf["bad"], ovf)
        cols = pg.evaluate("""()=>{
          const g=document.querySelector('.quotegrid');
          return g?getComputedStyle(g).gridTemplateColumns.split(' ').length:0;
        }""")
        ck("390px 报价网格为 3 列（省高度）", cols == 3, cols)

        # ── 6. 移动端可用性
        # 触摸目标：导航项曾只有 28px 高，手指点不准（规范建议 ≥44，取 40 折中）
        tgt = pg.evaluate("""()=>{
          const bad=[];
          document.querySelectorAll('#secNav a, #secNav button').forEach(e=>{
            const r=e.getBoundingClientRect();
            if(r.height>0 && r.height<40) bad.push({t:e.textContent.trim().slice(0,8),
                                                    h:Math.round(r.height)});
          });
          return bad;
        }""")
        ck("移动端分区导航触摸目标 ≥40px", not tgt, tgt[:4])

        # 长卡片折叠：桌面端不折（按钮隐藏），移动端按实测高度自动折
        fold = pg.evaluate("""()=>{
          const on=[...document.querySelectorAll('#stockWrap .card.mobfold-on')];
          return {n:on.length,
                  withBtn:on.filter(c=>c.querySelector(':scope > .mobfold-btn')).length,
                  ids:on.map(c=>c.id),
                  pageH:document.body.scrollHeight};
        }""")
        ck("移动端长卡片已折叠（≥3 张，含全套指标）",
           fold["n"] >= 3 and fold["withBtn"] == fold["n"], fold)
        ck("折叠卡片都带展开按钮", fold["withBtn"] == fold["n"], fold)
        # 折叠后整页应显著变短（改造前 12212px）
        ck("折叠后整页高度明显下降（<9000px）", fold["pageH"] < 9000, fold["pageH"])

        # 展开必须能真的看到内容，且图表要重新 resize（否则画出半张图）
        exp = pg.evaluate("""()=>{
          const card=document.querySelector('#secInd');
          if(!card || !card.classList.contains('mobfold-on')) return {skip:true};
          const before=card.querySelector('.card-b').clientHeight;
          card.querySelector('.mobfold-btn').click();
          return {before, opened:card.classList.contains('mobfold-open')};
        }""")
        pg.wait_for_timeout(700)
        exp2 = pg.evaluate("""()=>{
          const card=document.querySelector('#secInd');
          const b=card.querySelector('.card-b');
          const cv=document.querySelector('#cvol canvas');
          return {after:b.clientHeight,
                  styleMax:getComputedStyle(b).maxHeight,
                  volW:cv?cv.clientWidth:0, volH:cv?cv.clientHeight:0};
        }""")
        ck("展开后内容区变高（不是点了没反应）",
           exp.get("skip") or exp2["after"] > exp["before"], (exp, exp2))
        ck("展开后图表有正常尺寸（不是 0 宽）",
           exp.get("skip") or (exp2["volW"] > 100 and exp2["volH"] > 50), exp2)

        ck("全程无 JS 报错", not errs, errs[:3])
        b.close()

    print("\n%d/%d PASS  (%.1fs)" % (len(passes), len(passes) + len(fails), time.time() - t0))
    if fails:
        print("FAILED:", fails)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
