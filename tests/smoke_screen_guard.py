"""选股登录守卫冒烟测试。

覆盖 7 件事，任何一条不通过都算失败：
  1. 未登录点「执行选股」→ 弹登录框 + 守卫提示条（文案带操作名）
  2. 登录 → 自动续跑被拦下的那次操作，直接出结果
  3. 续跑用的还是原参数（不是空跑一遍）
  4. 其它 4 个选股入口（一句话 / 策略 / 盘中 / 一键）未登录时同样被拦
  5. 已登录时这些入口零影响（不弹框，直接跑）
  6. 退出登录后重新被拦
  7. 全程 JS 零报错

用法：python3 tests/smoke_screen_guard.py
"""
from playwright.sync_api import sync_playwright
import sys

BASE = "http://127.0.0.1:8899"
USER = "guardtest"
PWD = "Guard123456"

errors = []
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" | " + str(detail)) if detail else ""))


def act_tab(pg, tab):
    """切页签。

    不能直接 click('button[data-tab=x]')：抽屉里有一份隐藏副本，
    .first 会选到不可见那个，然后等到超时。这里照 index.html 的 actTab 手搓一遍。
    """
    pg.evaluate(
        """(function(x){
          document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
          document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
          document.querySelectorAll('button[data-tab="'+x+'"]').forEach(b=>b.classList.add('on'));
          var tab=document.querySelector('#tab-'+x); if(tab) tab.classList.add('on');
        })('%s')""" % tab
    )


def modal_open(pg):
    return pg.evaluate(
        "!!document.querySelector('#authModal') && "
        "document.querySelector('#authModal').classList.contains('on')"
    )


def login_if_open(pg):
    pg.fill("#authUser", USER)
    pg.fill("#authPwd", PWD)
    pg.click("#authSubmit")
    pg.wait_for_function(
        "!document.querySelector('#authModal').classList.contains('on')",
        timeout=15000,
    )


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1366, "height": 1000})
        def on_console(m):
            if m.type != "error":
                return
            # 401 是守卫的预期结果，浏览器对每个失败请求都会记一条 console error，
            # 它是「资源没取到」而不是 JS 异常 —— 混进来会让断言永远红。
            if "status of 401" in m.text:
                return
            errors.append(m.text)

        pg.on("console", on_console)
        pg.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState !== 'undefined' && authState.loaded",
                             timeout=15000)

        # ---------- 1. 未登录点「执行选股」 ----------
        print("\n=== 1. 未登录点「执行选股」 ===")
        check("初始态确实未登录", pg.evaluate("authState.user === null"))
        pg.evaluate("document.querySelector('#f_run').click();")
        pg.wait_for_selector("#authModal.on", timeout=8000)
        check("弹出登录框", modal_open(pg))
        tip = pg.locator("#authNeedTip").inner_text()
        check("守卫提示带操作名", "条件选股" in tip and "自动继续" in tip, tip)
        check("已记住待续跑动作",
              pg.evaluate("screenPending !== null && screenPending.label === '条件选股'"),
              pg.evaluate("screenPending && screenPending.label"))

        # ---------- 2. 登录 → 自动续跑 ----------
        print("\n=== 2. 登录并自动续跑 ===")
        login_if_open(pg)
        # ponytail: 用 attached 而不是默认 visible —— 结果表格确实渲染出来了，
        # 只是选股页签还没激活（父层 display:none），visible 会一直等到超时。
        pg.wait_for_selector("#screenBody table", state="attached", timeout=60000)
        rows = pg.locator("#screenBody tbody tr").count()
        cnt = pg.locator("#screenCount").inner_text()
        check("登录后续跑出结果", rows > 0, "%d 行 %s" % (rows, cnt))
        check("续跑队列已清空", pg.evaluate("screenPending === null"))
        check("结果区没被误渲染成『请登录』", "需要登录后使用" not in pg.locator("#screenBody").inner_text())

        # ---------- 3. 其它入口：未登录也要被拦 ----------
        print("\n=== 3. 其它选股入口的拦截 ===")
        pg.evaluate("doLogout();")
        pg.wait_for_function("authState.user === null", timeout=10000)

        # (标签, 触发点击的 JS, 点击前要准备好的东西)
        cases = [
            ("一句话选股", "document.querySelector('#nlScreenParse').click();", None),
            ("策略选股", "document.querySelector('#stratRun').click();", None),
            ("盘中扫描", "document.querySelector('#idScan').click();", None),
            ("一键选股", "document.querySelector('[data-nbpick]').click();",
             # 方案卡片是 JS 异步渲染的，先等它出现再点，否则选择器是 null
             ("loadNewbiePresets();",
              "document.querySelectorAll('[data-nbpick]').length > 0")),
            ("加载指标评级", "document.querySelector('#screenRateBtn').click();", None),
        ]
        for label, js, pre in cases:
            if pre:
                pg.evaluate(pre[0])
                pg.wait_for_function(pre[1], timeout=15000)
            el_ok = pg.evaluate(
                "(function(){ try{ %s ; return true; }catch(e){ return 'ERR:'+e.message; } })()" % js
            )
            opened = modal_open(pg)
            pend = pg.evaluate("screenPending && screenPending.label")
            check("%s → 弹框拦截" % label, opened and pend == label,
                  "clicked=%s pending=%s" % (el_ok, pend))
            pg.evaluate("closeAuth(true);")   # 关掉，别让上一个弹框影响下一个

        # ---------- 4. 已登录：入口零影响 ----------
        print("\n=== 4. 已登录时入口不被拦 ===")
        pg.evaluate("openAuth('login');")
        login_if_open(pg)
        check("已登录", pg.evaluate("!!authState.user"), pg.evaluate("authState.user && authState.user.username"))
        pg.evaluate("document.querySelector('#nlScreenParse').click();")
        pg.wait_for_timeout(1200)
        check("一句话选股未被拦", not modal_open(pg))
        pg.evaluate("document.querySelector('#stratRun').click();")
        pg.wait_for_timeout(1200)
        check("策略选股未被拦", not modal_open(pg))

        # ---------- 5. 会话过期的异步分支（loadMe 兜底） ----------
        print("\n=== 5. 清 cookie 后的异步判态分支 ===")
        pg.context.clear_cookies()
        pg.reload(wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState !== 'undefined'", timeout=15000)
        pg.evaluate("authState.loaded = false; authState.user = null;")  # 模拟首屏竞态
        pg.evaluate("document.querySelector('#f_run').click();")
        pg.wait_for_selector("#authModal.on", timeout=10000)
        check("loaded=false 时先问会话再弹框", modal_open(pg))
        check("异步分支也记住了动作",
              pg.evaluate("screenPending !== null && screenPending.label === '条件选股'"))

        b.close()

    print("\nJS_ERRORS:", len(errors))
    for e in errors[:10]:
        print("   -", e[:220])

    failed = [n for n, ok in checks if not ok]
    print("\n通过 %d / %d" % (len(checks) - len(failed), len(checks)))
    ok = not failed and len(errors) == 0
    if failed:
        print("未通过:", ", ".join(failed))
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
