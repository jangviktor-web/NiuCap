"""恐贪指数（并进牛熊卡）自检：口径断言 + 页面渲染断言。

2026-10-30 新增。恐贪 = 波动率/相对换手/RSI（K 线 10 年滚动分位）
+ 市场宽度/涨跌停比（当日快照绝对映射），两融无源跳过。
守四条关键口径：
  1. 分数 0-100，缺子指标权重再分配而不是补 50；
  2. RSI 全平盘 / 样本不足必须 None，不能冒充 50（50=中性=假结论）；
  3. 涨跌停比映射两端截断（r<0.2→0，r>5→100），除零安全；
  4. 与 13 维温度语义相反，前端必须并排展示且标注「别混算」。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import bullbear as bb  # noqa: E402

BASE = os.environ.get("BASE", "http://127.0.0.1:8899")
CHROME = os.environ.get("CHROME", "/usr/bin/chromium")
res = []


def rec(name, ok, detail=""):
    res.append(bool(ok))
    print(("✅" if ok else "❌") + f" {name}  {detail}")


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


print("── A 纯逻辑（不联网）──")
rec("A 模块自检", bb.selfcheck() == 0)

print("── B 后端 API ──")
try:
    r = api("/api/bullbear")
    fg = r.get("fear_greed") or {}
    rec("B /api/bullbear 带 fear_greed", fg.get("ok") is True,
        f"score={fg.get('score')}")
    sc = fg.get("score")
    rec("B 分数量级 0-100", sc is not None and 0 <= sc <= 100, f"score={sc}")
    rec("B 五档有名字有注", bool((fg.get("band") or {}).get("name"))
        and bool((fg.get("band") or {}).get("note")),
        (fg.get("band") or {}).get("name"))
    items = {x["name"]: x["score"] for x in fg.get("items", [])}
    rec("B 子指标齐全 6 项", len(items) == 6, "/".join(items))
    # 历史分位三项今天就该有真分位（K 线 10 年），缺了说明 K 线路径坏了
    rec("B 分位三项可用", all(items.get(k) is not None
                              for k in ("波动率", "相对换手", "RSI(14)")),
        str({k: items.get(k) for k in ("波动率", "相对换手", "RSI(14)")}))
    rec("B 两融子指标可用（东财源）", items.get("两融情绪") is not None,
        f"两融情绪={items.get('两融情绪')}")
    rec("B 分位三项量级", all(0 <= items[k] <= 100
                              for k in ("波动率", "相对换手", "RSI(14)")
                              if items.get(k) is not None))
    rec("B 两融口径如实标注",
        any("两融" in c for c in fg.get("caveats", [])),
        f"caveats={len(fg.get('caveats', []))}")
    rec("B 缺失项如实列出",
        (fg.get("have") == fg.get("total")) == (not fg.get("missing")),
        f"have={fg.get('have')}/{fg.get('total')}")
    # 语义相反：与 13 维温度同时返回但不相等混算
    rec("B 与 13 维温度并存", (r.get("data") or {}).get("ok") is True,
        f"13维={(r.get('data') or {}).get('score')} 恐贪={sc}")
except Exception as e:  # noqa: BLE001
    rec("B 接口可用", False, f"{type(e).__name__}: {e}")

print("── C 页面渲染 ──")


def isNaNCheck(s):
    try:
        v = float(s)
        return v == v  # NaN != NaN
    except Exception:   # noqa: BLE001
        return False


from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    br = p.chromium.launch(headless=True, executable_path=CHROME)
    pg = br.new_page(viewport={"width": 1440, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_timeout(1200)
    pg.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg.wait_for_selector("#bbBody .bbhero, #bbBody .empty", timeout=120000)
    pg.wait_for_timeout(1500)

    d = pg.evaluate("""()=>{
      var box=document.getElementById('bbBody');
      var secs=box?Array.from(box.querySelectorAll('.bbsec')):[];
      var fg=secs.find(s=>s.querySelector('.bbh2') && s.querySelector('.bbh2').textContent.indexOf('恐贪指数')===0);
      return {has: !!box,
        fgTitle: fg? fg.querySelector('.bbh2').textContent : null,
        fgHero: fg? (fg.querySelector('.bbhero .n')||{}).textContent : null,
        fgRows: fg? fg.querySelectorAll('.bbdim').length : 0,
        fgPin: fg? !!fg.querySelector('.bbpin') : false,
        text: box? box.innerText.replace(/\\s+/g,' ').slice(0,1200):''};
    }""")
    rec("C 恐贪子块已渲染", bool(d["fgTitle"]), str(d["fgTitle"]))
    rec("C 主读数是数字", bool(d["fgHero"]) and isNaNCheck(d["fgHero"]),
        str(d["fgHero"]))
    rec("C 5 行子指标明细", d["fgRows"] >= 5, f"rows={d['fgRows']}")
    rec("C 有档位定位针", d["fgPin"])
    rec("C 标注与温度语义相反（别混算）", "混算" in d["text"])
    rec("C 两融跳过如实上页", "两融" in d["text"])

    pg2 = br.new_page(viewport={"width": 390, "height": 844})
    pg2.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    pg2.wait_for_timeout(1200)
    pg2.evaluate("()=>{document.querySelector('[data-tab=\"bullbear\"]').click();}")
    pg2.wait_for_selector("#bbBody .bbhero, #bbBody .empty", timeout=120000)
    pg2.wait_for_timeout(1000)
    ov = pg2.evaluate("()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
    rec("C 390px 无横向溢出", ov["sw"] <= ov["cw"] + 1,
        f'scrollW={ov["sw"]} clientW={ov["cw"]}')

    mine = [e for e in errs if "fear" in e.lower() or "bbBody" in e
            or ("401" not in e and "Failed to load resource" not in e)]
    rec("C 本功能无 JS 报错", len(mine) == 0, "; ".join(mine[:3]))
    br.close()


ok = sum(res)
print(f"\n{ok}/{len(res)} PASS")
sys.exit(0 if ok == len(res) else 1)
