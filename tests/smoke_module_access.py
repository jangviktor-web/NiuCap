"""模块登录权限冒烟测试（后台开关 + 前端联动 + 文案）。

这次改造最关键的一条：**管理员在后台关掉某个模块的开关后，前端必须跟着放行**。
后台改了、界面还拦着，等于功能是坏的 —— 所以脚本要把这条链路真正走一遍：

  1. /api/access 匿名可读，8 个模块默认全锁
  2. 未登录点选股入口 → 弹登录框，提示条里讲清算力/存储这笔账
  3. 管理员登录 → 后台「🔒 登录权限」面板渲染 8 个开关 + 来源标记
  4. 「运行参数」面板不再重复出现这批开关（两处都能改 = 记不住改哪儿）
  5. 面板上取消「指标评级」开关 → /api/access 立即变「匿名可用」
  6. 匿名浏览器里点同一个按钮 → 不再弹框，接口真的通了
  7. 把开关勾回来 → 匿名再次被拦（还原有效）
  8. 全程 JS 零报错

用法：python3 tests/smoke_module_access.py
（服务需以管理员白名单启动：cd server && TICK_ADMIN_USERS=guardadmin python3 run.py）
"""
from playwright.sync_api import sync_playwright
import sys

BASE = "http://127.0.0.1:8899"
USER = "guardadmin"
PWD = "GuardAdmin123456"
MODS = ["screen", "newbie", "nlpick", "strategy", "intraday", "rating", "grid", "paper"]

errors = []
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" | " + str(detail)) if detail else ""))


def act_tab(pg, tab):
    """切页签。不能直接 click('button[data-tab=x]')：抽屉里有一份隐藏副本。"""
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


def ensure_login(pg, user, pwd):
    """幂等拿到会话：**先登录、失败再注册**。

    顺序很重要：反过来（先注册）第二次跑会因「用户名已存在」收到 400，
    浏览器对每条失败请求记一条 console error —— 那是我们自己制造的噪声，
    会掩盖真正的 JS 报错。
    """
    pg.evaluate(
        """async ([u,p]) => {
          let r = await fetch('/api/auth/login', {method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({username:u, password:p})});
          if(!r.ok){
            await fetch('/api/auth/register', {method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({username:u, password:p})});
          }
          if(typeof loadMe === 'function') await loadMe();
        }""", [user, pwd])
    pg.wait_for_function("!!authState.user", timeout=20000)


def watch(pg):
    pg.on("console", lambda m: None if (
        m.type != "error" or "status of 401" in m.text or "status of 429" in m.text
    ) else errors.append(m.text))
    pg.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))


def open_access_panel(pg):
    act_tab(pg, "admin")
    pg.click('#admTabs button[data-adm="access"]')
    pg.wait_for_selector('[data-accc="TICK_AUTH_REQ_SCREEN"]', timeout=20000)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)

        # ---------- 0. 复位：别让上一次跑到一半留下的脏开关影响这次 ----------
        print("\n=== 0. 复位 8 个开关到默认 ===")
        pg = b.new_page(viewport={"width": 1440, "height": 1100})
        watch(pg)
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState !== 'undefined' && authState.loaded", timeout=20000)
        ensure_login(pg, USER, PWD)
        pg.evaluate(
            """async (mods) => {
              for(const m of mods){
                await fetch('/api/admin/config/TICK_AUTH_REQ_' + m.toUpperCase(),
                            {method:'DELETE'});
              }
            }""", MODS)
        pg.evaluate("doLogout();")
        pg.reload(wait_until="load", timeout=25000)
        pg.wait_for_function(
            "async()=>{ let d=await (await fetch('/api/access')).json(); "
            "return Object.keys(d.require_login).length===8 && "
            "Object.values(d.require_login).every(v=>v===true); }", timeout=25000)
        check("8 个开关都已复位到默认（全要求登录）", True)

        # ---------- 1. 匿名：开放策略可读、入口被拦 ----------
        print("\n=== 1. 匿名：/api/access + 拦截文案 ===")
        check("确实处于未登录状态", pg.evaluate("authState.user === null"))

        rules = pg.evaluate("async()=>{ let r=await fetch('/api/access'); "
                            "let d=await r.json(); return d.require_login; }")
        check("匿名可读开放策略", isinstance(rules, dict) and len(rules) == 8, str(rules))
        check("默认 8 个模块全部要求登录",
              all(rules.get(m) is True for m in MODS),
              [m for m in MODS if rules.get(m) is not True])
        reason = pg.evaluate("async()=>{ let d=await (await fetch('/api/access')).json(); "
                             "return d.reason; }")
        check("统一原因说明带上了算力/存储", "算力" in reason and "存储" in reason, reason[:60])

        pg.evaluate("document.querySelector('#screenRateBtn').click();")
        pg.wait_for_selector("#authModal.on", timeout=10000)
        tip = pg.locator("#authNeedTip").inner_text()
        check("提示条讲清了为什么", "算力" in tip and "自动继续" in tip, tip.replace("\n", " / "))
        check("提示条带操作名", "加载指标评级" in tip)
        pg.screenshot(path="shots/auth_need_tip.png", full_page=False)
        pg.evaluate("closeAuth(true);")

        # ---------- 2. 管理员：后台面板 ----------
        print("\n=== 2. 后台「登录权限」面板 ===")
        ensure_login(pg, USER, PWD)
        is_admin = pg.evaluate("async()=>{ let d=await (await fetch('/api/admin/whoami')).json(); "
                               "return !!d.is_admin; }")
        check("拿到管理员身份", is_admin)
        open_access_panel(pg)
        rows = pg.locator("[data-accc]").count()
        check("面板渲染 8 个模块开关", rows == 8, "%d 行" % rows)
        head = pg.locator("#admBody .card-h .meta").first.inner_text()
        check("标题条显示上锁数量", "/ 8" in head, head)
        body_txt = pg.locator("#admBody").inner_text()
        check("面板写明了放开代价与生效方式", "放开的代价" in body_txt and "无需重启" in body_txt,
              body_txt[-80:].replace("\n", " / "))
        pg.screenshot(path="shots/admin_access_panel.png", full_page=False)

        print("\n=== 3. 「运行参数」面板不重复这批开关 ===")
        pg.click('#admTabs button[data-adm="config"]')
        pg.wait_for_selector("[data-cfgs]", timeout=15000)
        keys = pg.evaluate("Array.from(document.querySelectorAll('[data-cfgs]'))"
                           ".map(b=>b.dataset.cfgs)")
        leaked = [k for k in keys if k.startswith("TICK_AUTH_REQ_")]
        check("运行参数里没有权限开关", not leaked, "泄漏 %s" % leaked)
        check("运行参数仍有常规项", len(keys) >= 10, "%d 项" % len(keys))

        # ---------- 4. 关掉「指标评级」开关 → 前端放行 ----------
        print("\n=== 4. 取消「指标评级」开关 ===")
        open_access_panel(pg)
        pg.uncheck('[data-accc="TICK_AUTH_REQ_RATING"]')
        # 后端 config 有 5s 读缓存，这里轮询等它过期后再断言
        pg.wait_for_function(
            "async()=>{ let d=await (await fetch('/api/access')).json(); "
            "return d.require_login.rating === false; }", timeout=25000)
        check("开关写入后立即生效", True)
        pg.wait_for_function("needLogin('rating') === false", timeout=15000)
        check("前端 needLogin 跟着变 false", True)

        # ---------- 5. 匿名浏览器：同一个按钮不再被拦 ----------
        print("\n=== 5. 匿名上下文：不再弹框、接口真的通 ===")
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        pg2 = ctx.new_page()
        watch(pg2)
        pg2.goto(BASE + "/", wait_until="load", timeout=25000)
        pg2.wait_for_function("typeof accessState === 'object' && "
                              "typeof needLogin === 'function'", timeout=20000)
        pg2.evaluate("loadAccess(true);")
        pg2.wait_for_function("needLogin('rating') === false && needLogin('grid') === true",
                              timeout=20000)
        pg2.evaluate("document.querySelector('#screenRateBtn').click();")
        pg2.wait_for_timeout(3000)
        check("放开后点击不再弹登录框", not modal_open(pg2),
              pg2.evaluate("screenPending && screenPending.label"))
        st = pg2.evaluate("async()=>{ let r=await fetch('/api/rating_batch?codes=600519'); "
                          "return r.status; }")
        check("匿名调评级接口 200", st == 200, "HTTP %s" % st)
        st2 = pg2.evaluate("async()=>{ let r=await fetch('/api/grid/plans'); return r.status; }")
        check("未放开的模块仍被拦", st2 == 401, "HTTP %s" % st2)
        ctx.close()

        # ---------- 6. 还原 ----------
        print("\n=== 6. 勾回来，还原到默认 ===")
        open_access_panel(pg)
        pg.check('[data-accc="TICK_AUTH_REQ_RATING"]')
        pg.wait_for_function(
            "async()=>{ let d=await (await fetch('/api/access')).json(); "
            "return d.require_login.rating === true; }", timeout=25000)
        check("还原后重新上锁", True)
        # 用「还原」按钮把 meta 记录删掉，别在运行库里留下非默认值
        pg.click('[data-accr="TICK_AUTH_REQ_RATING"]')
        pg.wait_for_function(
            "async()=>{ let d=await (await fetch('/api/admin/config')).json(); "
            "let it=(d.items||[]).find(x=>x.key==='TICK_AUTH_REQ_RATING'); "
            "return it && it.source === 'default'; }", timeout=20000)
        check("meta 记录已清，回落默认来源", True)

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
