#!/usr/bin/env python3
# 验证导航条：标签溢出时可横向滚动、右侧时钟不被挤掉、无控制台报错
import subprocess, sys, time, json
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
SHOT = "shots_navscroll"

def req(method, path, body=None):
    import requests
    h = {"Content-Type": "application/json"}
    r = requests.request(method, BASE + path, json=body, headers=h, timeout=20)
    try: j = r.json()
    except Exception: j = r.text
    return r.status_code, j, r.cookies.get("tick_sid")

def main():
    # 自注册一个一次性测试账号（不依赖任何真实账号密码；匿名也能测滚动，
    # 登录只为让标签条尽可能满——普通用户没有「后台管理」页签）
    import secrets
    u = "nav_" + secrets.token_hex(4)
    st, d, sid = req("POST", "/api/auth/register",
                     {"username": u, "password": "test1234", "display": u})
    print(f"注册测试账号: {st} cookie={'有' if sid else '无'}")

    results, fails = [], 0
    def rec(name, ok, detail=""):
        nonlocal fails
        results.append((name, ok, detail))
        if not ok: fails += 1
        print(f"{'✅' if ok else '❌'} {name}  {detail}")

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1100, "height": 800})
        if sid:
            ctx.add_cookies([{"name": "tick_sid", "value": sid, "url": BASE}])
        pg = ctx.new_page()
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(BASE + "/", wait_until="networkidle")
        # #190 起桌面端也走「☰ 菜单」抽屉：.tabs 容器默认 display:none，
        # 先等汉堡按钮可见，点开抽屉再断言（原「横排标签滚动」场景已不存在）
        pg.wait_for_selector("#navToggle", timeout=15000)

        # 1) 抽屉导航项数量（普通用户 16 项：跳过隐藏 hot/moves + 仅管理员的 admin）
        pg.click("#navToggle")
        pg.wait_for_selector(".navdrawer.open .nd-item", timeout=10000)
        pg.wait_for_timeout(400)
        vis = pg.eval_on_selector_all(".navdrawer .nd-item",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.dataset.tab)")
        rec("抽屉导航项=16(普通用户；后台管理仅管理员可见)",
            len(vis) == 16 and "admin" not in vis,
            f"可见 {len(vis)} 项: {vis}")

        # 2) 点抽屉项能切页（验证 #190 抽屉 JS 绑定未坏）
        pg.click('.nd-item[data-tab="newbie"]')
        pg.wait_for_timeout(600)
        on_ok = pg.evaluate(
            "() => document.getElementById('tab-newbie').classList.contains('on')")
        rec("点抽屉项切页生效(小白选股)", on_ok)
        # 抽屉点项后自动关闭
        closed = pg.evaluate(
            "() => !document.getElementById('navDrawer').classList.contains('open')")
        rec("点项后抽屉自动关闭", closed)

        # 3) 顶栏时钟与汉堡同处 header 一行（#190 吸顶布局不被挤走）
        same = pg.evaluate("""() => {
            const c = document.getElementById('mktClock').getBoundingClientRect();
            const t = document.getElementById('navToggle').getBoundingClientRect();
            const h = document.querySelector('header').getBoundingClientRect();
            return {clockTop:c.top, toggleTop:t.top, headerBottom:h.bottom,
                    sameRow: c.top >= h.top - 2 && c.bottom <= h.bottom + 2 &&
                            Math.abs(c.top - t.top) < 40};
        }""")
        rec("交易时钟与菜单按钮同处顶栏一行", same["sameRow"], f"{same}")

        # 4) 页面滚动后 header 吸顶、时钟仍在视口内
        pg.evaluate("window.scrollTo(0, 600)")
        pg.wait_for_timeout(400)
        stuck = pg.evaluate("""() => {
            const c = document.getElementById('mktClock').getBoundingClientRect();
            return {clockTop:c.top, inView: c.top >= 0 && c.top < 120};
        }""")
        rec("滚动后时钟仍吸顶可见", stuck["inView"], f"{stuck}")

        # 5) 控制台无报错
        rec("页面无 JS 报错", not errors, "; ".join(errors[:2])[:160])

        pg.screenshot(path=f"{SHOT}/nav_1100.png", full_page=False)
        b.close()

    print(f"\n{'通过' if fails==0 else '失败'} {len(results)-fails}/{len(results)}")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
