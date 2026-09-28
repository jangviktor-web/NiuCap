#!/usr/bin/env python3.11
# Task#101 前端优化 · 回归+根因修复测试（桌面+手机双视口）
# 运行: python3.11 tests/check_frontend_opt.py
import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8899/"
failures = []

def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures.append(msg)

def probe(pg, css_prop, decl):
    # 注入隔离探针，读解析后的计算值（不受祖先污染）
    return pg.evaluate("""(o) => {
      const s = document.createElement('span');
      s.style.cssText = o.d + ';position:absolute;left:-9999px';
      document.body.appendChild(s);
      const v = getComputedStyle(s).getPropertyValue(o.p).trim();
      s.remove();
      return v;
    }""", {"p": css_prop, "d": decl})

def run_desktop(p, b):
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(1200)
    print("[桌面] 市场榜单默认加载")
    rank = pg.evaluate("() => { const c=document.getElementById('tab-rank'); return c?c.innerText.length:0; }")
    ok(rank > 50, "市场榜单有内容 (%d 字)" % rank)

    # R1: --muted / --tx1 / --border / --bg2 令牌解析（隔离探针）
    muted = probe(pg, "color", "color:var(--muted)")
    ok(muted == "rgb(139, 147, 161)", "--muted 解析为灰 rgb(139,147,161)（实际=%s）" % muted)
    tx1 = probe(pg, "color", "color:var(--tx1)")
    ok(tx1 == "rgb(26, 29, 35)", "--tx1 解析为主文字 rgb(26,29,35)（实际=%s）" % tx1)
    border = probe(pg, "border-bottom-color", "border-bottom:1px solid var(--border)")
    ok(border == "rgb(230, 232, 236)", "--border 解析为浅边框 rgb(230,232,236)（实际=%s）" % border)
    bg2 = probe(pg, "background-color", "background:var(--bg2)")
    ok(bg2 == "rgb(247, 248, 250)", "--bg2 解析为 rgb(247,248,250)（实际=%s）" % bg2)

    # O2: .tag 变体保留（去重后 .tag.n/.tag.bj 等变体规则仍在）。
    # 真实使用的变体在默认市场榜单的股票名单元格里就有，无需切 tab。
    tagn = pg.evaluate("""() => {
      const el = document.querySelector('.tag.n') || document.querySelector('.tag.bj');
      return el ? getComputedStyle(el).backgroundColor : null;
    }""")
    ok(tagn is not None, "市场榜单含 .tag.n/.tag.bj 变体")
    ok(tagn in ("rgb(243, 244, 247)", "rgb(254, 246, 232)"), ".tag 变体背景有效（实际=%s）" % tagn)

    # 切回市场榜单（非回归）
    pg.click('.tabs button[data-tab="rank"]'); pg.wait_for_timeout(600)
    ok(pg.evaluate("() => document.getElementById('tab-rank').classList.contains('on')"),
       "切回市场榜单正常（非回归）")
    ok(len(errs) == 0, "桌面无 JS 报错（%d）" % len(errs))
    if errs: print("    JS错误:", errs[:3])
    pg.close()

def run_mobile(p, b):
    pg = b.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=3)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(1000)
    print("[手机] 导航")
    ok(pg.evaluate("() => getComputedStyle(document.getElementById('navToggle')).display !== 'none'"),
       "汉堡按钮可见")
    ok(pg.evaluate("() => getComputedStyle(document.querySelector('.tabs')).display === 'none'"),
       "inline tabs 隐藏（零回归）")

    # 场景A: O4 Esc 关闭 + 滚动锁
    pg.click('#navToggle'); pg.wait_for_timeout(400)
    ok(pg.evaluate("() => document.getElementById('navDrawer').classList.contains('open')"), "抽屉打开")
    ok(pg.evaluate("() => getComputedStyle(document.body).overflow === 'hidden'"), "抽屉打开时 body 滚动被锁")
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    ok(pg.evaluate("() => !document.getElementById('navDrawer').classList.contains('open')"), "Esc 关闭抽屉生效")
    ok(pg.evaluate("() => getComputedStyle(document.body).overflow !== 'hidden'"), "关闭后 body 滚动还原")

    # 场景B: 点模块切换（先确保关→开，避免基线抽屉状态混乱；force 规避关闭动画期命中 flake）
    if pg.evaluate("() => document.getElementById('navDrawer').classList.contains('open')"):
        pg.click('#navClose'); pg.wait_for_timeout(300)
    pg.click('#navToggle'); pg.wait_for_timeout(400)
    n = pg.evaluate("() => document.querySelectorAll('.nd-item').length")
    ok(n > 0, "抽屉列出 %d 个模块" % n)
    try:
        pg.click('.nd-item[data-tab="rank"]', force=True, timeout=4000); pg.wait_for_timeout(800)
        ok(pg.evaluate("() => document.getElementById('tab-rank').classList.contains('on')"),
           "点击模块切换正常（非回归）")
    except Exception as e:
        ok(False, "点击模块被拦截: %s" % str(e)[:60])
    ok(len(errs) == 0, "手机无 JS 报错（%d）" % len(errs))
    if errs: print("    JS错误:", errs[:3])
    pg.close()

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    run_desktop(p, b)
    print("----")
    run_mobile(p, b)
    b.close()

print("\n==== 结果 ====")
if failures:
    print("失败 %d 项:" % len(failures))
    for f in failures: print("  -", f)
    sys.exit(1)
print("全部通过 ✓")
