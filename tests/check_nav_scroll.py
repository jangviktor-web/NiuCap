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
    # 登录拿 admin（让「后台管理」显示，标签最多）；token 在 cookie 里
    st, d, sid = req("POST", "/api/auth/login", {"username": "admin", "password": "femkerr"})
    print(f"登录: {st} cookie={'有' if sid else '无'}")

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
        pg.wait_for_selector(".tabs button", timeout=15000)

        # 1) 全部可见标签数量（后台管理应显示）
        vis = pg.eval_on_selector_all(".tabs button",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.dataset.tab)")
        rec("标签数量=14(含后台管理，热/异动刻意隐藏)",
            len(vis) == 14 and "admin" in vis,
            f"可见 {len(vis)} 个: {vis[-2:]}")

        # 2) tabs 容器横向可滚动（内容宽 > 可视宽）
        info = pg.evaluate("""() => {
            const t = document.querySelector('.tabs');
            const ni = document.querySelector('.navinner');
            return {scroll:t.scrollWidth, client:t.clientWidth,
                    niw:ni.getBoundingClientRect().width,
                    vw:window.innerWidth};
        }""")
        rec("标签条可横向滚动(scrollWidth>clientWidth)",
            info["scroll"] > info["client"], f"{info}")

        # 3) 滚动到最右后，末尾标签进入可视区（之前被切掉）
        pg.evaluate("document.querySelector('.tabs').scrollLeft = 99999")
        time.sleep(0.3)
        last_ok = pg.evaluate("""() => {
            const btns=[...document.querySelectorAll('.tabs button')].filter(e=>e.offsetParent!==null);
            const last=btns[btns.length-1].getBoundingClientRect();
            const tabs=document.querySelector('.tabs').getBoundingClientRect();
            return {right:last.right, tabsRight:tabs.right, visible:last.right<=tabs.right+1 && last.left>=tabs.left-1};
        }""")
        rec("滚动后末尾标签进入可视区", last_ok["visible"], f"{last_ok}")

        # 4) 右侧交易时钟与标签同处一行（不被挤到第二行）
        same = pg.evaluate("""() => {
            const t=document.querySelector('.tabs').getBoundingClientRect();
            const c=document.getElementById('mktClock').getBoundingClientRect();
            const a=document.querySelector('.tabs button').getBoundingClientRect();
            // 同处一行 = 时钟顶部位在「标签行」带内（允许垂直居中的高度差）
            const bandBottom = t.top + a.height + 4;
            return {tabsTop:t.top, clockTop:c.top, btnTop:a.top, btnH:a.height,
                    sameRow: c.top >= t.top - 2 && c.top <= bandBottom};
        }""")
        rec("交易时钟与标签同处一行", same["sameRow"], f"{same}")

        # 5) 控制台无报错
        rec("页面无 JS 报错", not errors, "; ".join(errors[:2])[:160])

        pg.screenshot(path=f"{SHOT}/nav_1100.png", full_page=False)
        b.close()

    print(f"\n{'通过' if fails==0 else '失败'} {len(results)-fails}/{len(results)}")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
