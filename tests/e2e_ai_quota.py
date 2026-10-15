"""AI 深度分析登录守卫 + 每日配额 + 后台「AI 用量」面板 端到端验证（#213~#219）。

链路：
  1. 未登录点「开始分析」→ 弹登录框（guardScreen 复用选股守卫范式）
  2. 登录成功 → 自动续跑被拦的那次分析，结果区出现「今日配额」行
  3. 后台「📈 AI 用量」面板：渲染每账号已用/配额、改配额保存即生效
  4. 前端 /api/access 已含 stockai 开关（9 模块）
  5. 全程 JS 零报错；截图留档（后台 AI 用量面板）

用法：python3.11 tests/e2e_ai_quota.py
（需管理员账号：export E2E_ADMIN_USER=xxx E2E_ADMIN_PWD=xxx，
  并确保服务以 TICK_ADMIN_USERS=$E2E_ADMIN_USER 启动；账号不存在会自动注册）
"""
from playwright.sync_api import sync_playwright
import os
import sys

BASE = "http://127.0.0.1:8899"
USER = os.environ.get("E2E_ADMIN_USER", "")
PWD = os.environ.get("E2E_ADMIN_PWD", "")

errors = []
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" | " + str(detail)) if detail else ""))


def modal_open(pg):
    return pg.evaluate(
        "!!document.querySelector('#authModal') && "
        "document.querySelector('#authModal').classList.contains('on')"
    )


def ensure_login(pg, user, pwd):
    """幂等拿到会话：先登录、失败再注册。"""
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


def mask_names(pg):
    """截图前把真实用户名中性化（#185/#212 惯例）：头部 chip + 后台表格。"""
    pg.evaluate(
        """() => {
          let un = document.querySelector('#uArea .uname b');
          if(un) un.textContent = '演示用户';
          document.querySelectorAll('#admBody table.tbl tbody tr').forEach((tr, i) => {
            let b = tr.querySelector('td b');
            if(b) b.textContent = '用户' + (i + 1);
            let sub = tr.querySelector('td div');
            if(sub) sub.textContent = '';
          });
        }""")


def open_admin_tab(pg, key):
    pg.click('#navToggle')
    pg.wait_for_timeout(300)
    pg.click(f'.nd-item[data-tab="admin"]')
    pg.wait_for_timeout(800)
    pg.click(f'#admTabs button[data-adm="{key}"]')


def main():
    if not USER or not PWD:
        print("请先设置环境变量 E2E_ADMIN_USER / E2E_ADMIN_PWD（管理员账号），"
              "并确保服务以 TICK_ADMIN_USERS=$E2E_ADMIN_USER 启动")
        sys.exit(2)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)

        # ---------- 1. 匿名：点「开始分析」被拦，弹登录框 ----------
        print("\n=== 1. 匿名：AI 分析入口被拦 ===")
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        pg = ctx.new_page()
        watch(pg)
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState !== 'undefined' && authState.loaded", timeout=20000)
        check("确实未登录", pg.evaluate("authState.user === null"))

        rules = pg.evaluate("async()=>{ let d=await (await fetch('/api/access')).json(); "
                            "return d.require_login; }")
        check("/api/access 含 stockai 且默认上锁", rules.get("stockai") is True, rules.get("stockai"))
        check("开放策略共 9 个模块", len(rules) == 9, len(rules))

        # 进个股页（rank 抽屉 → 点第一行），等 AI 卡片出现
        pg.click('#navToggle')
        pg.wait_for_timeout(300)
        pg.click('.nd-item[data-tab="rank"]')
        pg.wait_for_timeout(2500)
        pg.click('#rankBody tbody tr[data-code]:first-child')
        pg.wait_for_selector('#stockAiBtn', timeout=20000)
        pg.evaluate("loadAccess(true);")
        pg.wait_for_function("needLogin('stockai') === true", timeout=15000)

        pg.evaluate("document.querySelector('#stockAiBtn').click();")
        pg.wait_for_selector("#authModal.on", timeout=10000)
        check("未登录点分析弹登录框", modal_open(pg))
        tip = pg.locator("#authNeedTip").inner_text()
        check("提示条带功能名", "AI 深度分析" in tip, tip.replace("\n", " / ")[:80])
        check("提示条说明登录后续跑", "自动继续" in tip)
        mask_names(pg)
        pg.screenshot(path="shots/ai_quota_need_login.png", full_page=False)
        pg.evaluate("closeAuth(true);")
        ctx.close()

        # ---------- 2. 登录后自动续跑 + 配额行 ----------
        print("\n=== 2. 登录后续跑 + 配额回显 ===")
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        pg = ctx.new_page()
        watch(pg)
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState !== 'undefined' && authState.loaded", timeout=20000)
        ensure_login(pg, USER, PWD)

        pg.click('#navToggle')
        pg.wait_for_timeout(300)
        pg.click('.nd-item[data-tab="rank"]')
        pg.wait_for_timeout(2500)
        pg.click('#rankBody tbody tr[data-code]:first-child')
        pg.wait_for_selector('#stockAiBtn', timeout=20000)

        # 先记管理端当前用量，供事后核对
        before = pg.evaluate("async(u)=>{ let d=await (await fetch('/api/admin/ai_usage')).json(); "
                             "let m=(d.items||[]).find(x=>x.username===u); "
                             "return m ? m.used : -1; }", USER)
        check("管理端用量接口可读", before >= 0, "used=%s" % before)

        pg.evaluate("document.querySelector('#stockAiBtn').click();")
        pg.wait_for_selector("#stockAiOut .intentbox", timeout=60000)
        quota_txt = pg.locator("#stockAiOut").inner_text()
        check("结果区显示今日配额行", "今日配额" in quota_txt and "剩余" in quota_txt,
              [ln for ln in quota_txt.split("\n") if "配额" in ln][:1])
        # 把「今日配额」行滚进视口再截图（卡片在技术面结论下方，首屏拍不到）
        pg.evaluate(
            """() => { let rs = document.querySelectorAll('#stockAiOut .row');
              for(let r of rs){ if(r.textContent.indexOf('今日配额') >= 0){ r.scrollIntoView({block:'center'}); return; } } }""")
        pg.wait_for_timeout(400)
        mask_names(pg)
        pg.screenshot(path="shots/ai_quota_result.png", full_page=False)

        after = pg.evaluate("async(u)=>{ let d=await (await fetch('/api/admin/ai_usage')).json(); "
                            "let m=(d.items||[]).find(x=>x.username===u); "
                            "return m ? m.used : -1; }", USER)
        check("点一次分析用量 +1", after == before + 1, "before=%s after=%s" % (before, after))

        # ---------- 3. 后台「AI 用量」面板 ----------
        print("\n=== 3. 后台「📈 AI 用量」面板 ===")
        open_admin_tab(pg, "aiusage")
        pg.wait_for_selector("[data-aqs]", timeout=20000)
        head = pg.locator("#admBody .card-h .meta").first.inner_text()
        check("标题条显示日期与合计", "今日合计" in head, head)
        rows = pg.locator("[data-aq]").count()
        check("每账号一行配额输入框", rows >= 1, "%d 行" % rows)
        body_txt = pg.locator("#admBody").inner_text()
        check("面板写明计费口径", "失败不退款" in body_txt and "0 点自动重置" in body_txt)
        check("面板提示 0 = 停用", "停用" in body_txt)

        # 滚到面板标题（留出吸顶头部），让标题/计费口径/已用计数行同框
        pg.evaluate(
            """() => { let ch = document.querySelector('#admBody .card-h');
              if(ch){ ch.scrollIntoView({block:'start'}); window.scrollBy(0, -80); } }""")
        pg.wait_for_timeout(400)
        mask_names(pg)
        pg.screenshot(path="shots/admin_ai_usage.png", full_page=False)

        # 改配额：把测试账号改成 3，保存后管理端即时生效
        uid_row = pg.evaluate("async(u)=>{ let d=await (await fetch('/api/admin/users')).json(); "
                              "let m=(d.items||[]).find(x=>x.username===u); return m ? m.id : 0; }", USER)
        pg.fill(f'[data-aq="{uid_row}"]', "3")
        pg.click(f'[data-aqs="{uid_row}"]')
        # 等管理端「AI 用量」账本回显 quota=3（quota 只在 ai_usage 端点，users 端点不带此字段）
        pg.wait_for_function(
            "async(u)=>{ let d=await (await fetch('/api/admin/ai_usage')).json(); "
            "let m=(d.items||[]).find(x=>x.username===u); return m && m.quota===3; }", arg=USER,
            timeout=15000)
        check("面板改配额后即时生效", True)

        # 配额改成 3 后：当日已用可能已 >= 3 → 再点分析应 429（或放行后剩 0）
        used_now = pg.evaluate("async(u)=>{ let d=await (await fetch('/api/admin/ai_usage')).json(); "
                               "let m=(d.items||[]).find(x=>x.username===u); return m.used; }", USER)
        pg.evaluate("document.querySelector('#stockAiBtn').click();")
        pg.wait_for_timeout(4000)
        out_txt = pg.locator("#stockAiOut").inner_text()
        if used_now >= 3:
            check("超额时前端友好提示（含 429 文案）", "已用完" in out_txt or "次数" in out_txt,
                  out_txt.replace("\n", " / ")[:100])
        else:
            check("配额 3 内仍放行", "今日配额" in out_txt, out_txt.replace("\n", " / ")[:80])

        # 还原配额 10，别给运行库留脏值
        pg.fill(f'[data-aq="{uid_row}"]', "10")
        pg.click(f'[data-aqs="{uid_row}"]')
        pg.wait_for_function(
            "async(u)=>{ let d=await (await fetch('/api/admin/ai_usage')).json(); "
            "let m=(d.items||[]).find(x=>x.username===u); return m && m.quota===10; }", arg=USER,
            timeout=15000)
        check("还原配额 10", True)

        # ---------- 4. 后台开关联动：放开 stockai → 匿名可调 ----------
        print("\n=== 4. 后台开关：放开 stockai 后匿名放行 ===")
        open_admin_tab(pg, "access")
        pg.wait_for_selector('[data-accc="TICK_AUTH_REQ_STOCKAI"]', timeout=20000)
        pg.uncheck('[data-accc="TICK_AUTH_REQ_STOCKAI"]')
        pg.wait_for_function(
            "async()=>{ let d=await (await fetch('/api/access')).json(); "
            "return d.require_login.stockai === false; }", timeout=25000)
        check("后台关掉登录要求后 /api/access 即时放开", True)

        ctx2 = b.new_context(viewport={"width": 1440, "height": 1000})
        pg2 = ctx2.new_page()
        watch(pg2)
        pg2.goto(BASE + "/", wait_until="load", timeout=25000)
        pg2.wait_for_function("typeof needLogin === 'function'", timeout=20000)
        pg2.evaluate("loadAccess(true);")
        pg2.wait_for_function("needLogin('stockai') === false", timeout=20000)
        st = pg2.evaluate("async()=>{ let r=await fetch('/api/stock/analyze?code=sh600519'); "
                          "return r.status; }")
        check("放开后匿名调 analyze 返回 200", st == 200, "HTTP %s" % st)
        ctx2.close()

        # 还原开关（删 meta 记录回默认）
        open_admin_tab(pg, "access")
        pg.wait_for_selector('[data-accc="TICK_AUTH_REQ_STOCKAI"]', timeout=20000)
        pg.click('[data-accr="TICK_AUTH_REQ_STOCKAI"]')
        pg.wait_for_function(
            "async()=>{ let d=await (await fetch('/api/access')).json(); "
            "return d.require_login.stockai === true; }", timeout=25000)
        check("开关已还原为默认（要求登录）", True)

        b.close()

    print("\nJS_ERRORS:", len(errors))
    for e in errors[:10]:
        print("   -", e[:220])

    failed = [n for n, ok in checks if not ok]
    print("\n通过 %d / %d" % (len(checks) - len(failed), len(checks)))
    if failed:
        print("未通过:", ", ".join(failed))
    print("RESULT:", "PASS" if (not failed and not errors) else "FAIL")
    sys.exit(0 if (not failed and not errors) else 1)


if __name__ == "__main__":
    main()
