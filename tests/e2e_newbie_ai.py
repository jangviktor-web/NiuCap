import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from webp_shot import shoot
"""小白选股 AI 讲解 登录守卫 + 配额 + 渲染 端到端验证（#224）。

链路：
  1. 匿名直打 /api/newbie/explain → 401（后端才是真边界）
  2. /api/access 含 newbieai 开关且默认上锁（10 模块）
  3. 登录 → 小白选股出结果 → 点「✨ AI 讲解」→ 渲染大白话/注意点/今日配额行
  4. 配额直连 DB 打到 0 → 再点 → 429 文案友好呈现；随即恢复
  5. 全程 JS 零报错；截图留档（shots/newbie_ai.png）

用法：python3.11 tests/e2e_newbie_ai.py
（需普通测试账号：export E2E_USER=xxx E2E_PWD=xxx，账号不存在会自动注册）
"""
from playwright.sync_api import sync_playwright
import os
import sqlite3
import sys

BASE = "http://127.0.0.1:8899"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "tick.db")
USER = os.environ.get("E2E_USER", "")
PWD = os.environ.get("E2E_PWD", "")

errors = []
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" | " + str(detail)) if detail else ""))


def watch(pg):
    pg.on("console", lambda m: None if (
        m.type != "error" or "status of 401" in m.text or "status of 429" in m.text
    ) else errors.append(m.text))
    pg.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))


def mask_names(pg):
    """截图中性化：登录态用户名/头像不露出测试账号名。"""
    pg.evaluate("""() => {
      let u = document.querySelector('#authUser span, #uArea .uname b');
      if(u) u.textContent = '演示用户';
      let i = document.querySelector('#authUser .ava, #uArea .ava');
      if(i) i.style.filter = 'blur(6px)';
    }""")


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


def set_quota(username, quota):
    """直连 DB 改配额（e2e 专用，恢复现场在 finally）。"""
    con = sqlite3.connect(DB, timeout=10)
    try:
        con.execute("UPDATE users SET ai_quota=? WHERE username=?", (quota, username))
        con.commit()
    finally:
        con.close()


def open_newbie(pg):
    pg.click('#navToggle')
    pg.wait_for_timeout(300)
    pg.click('.nd-item[data-tab="newbie"]')
    pg.wait_for_timeout(2500)
    # 方案卡需点一下才跑（页面只懒加载预设，不自动选股）
    pg.wait_for_selector('.nbcard[data-nbpick]', timeout=20000)
    pg.locator('.nbcard[data-nbpick]').first.click()
    # 后台预热可能要几秒；.nbrow 出来才算有结果
    pg.wait_for_selector('#nbBody .nbrow', timeout=90000)


def main():
    if not USER or not PWD:
        print("请先设置环境变量 E2E_USER / E2E_PWD（普通测试账号），账号不存在会自动注册")
        sys.exit(2)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)

        # ---------- 1. 匿名 API 层：401 ----------
        print("\n=== 1. 匿名直打讲解接口 → 401 ===")
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        pg = ctx.new_page()
        watch(pg)
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        r = pg.evaluate("""async()=>{
          let r = await fetch('/api/newbie/explain', {method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({item:{code:'sh600519'}, preset:{}})});
          return {status: r.status, body: await r.text()};
        }""")
        check("匿名 POST /api/newbie/explain → 401", r["status"] == 401, r["body"][:60])
        check("401 文案带功能名", "AI 讲解" in r["body"], r["body"][:80])

        rules = pg.evaluate("async()=>{ let d=await (await fetch('/api/access')).json(); "
                            "return d.require_login; }")
        check("/api/access 含 newbieai 且默认上锁", rules.get("newbieai") is True,
              rules.get("newbieai"))
        check("开放策略共 10 个模块", len(rules) == 10, len(rules))
        ctx.close()

        # ---------- 2. 登录 → 点 AI 讲解 → 渲染 ----------
        print("\n=== 2. 登录后讲解渲染 ===")
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        pg = ctx.new_page()
        watch(pg)
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        pg.wait_for_function("typeof authState !== 'undefined' && authState.loaded", timeout=20000)
        ensure_login(pg, USER, PWD)
        open_newbie(pg)

        pg.locator('#nbBody [data-nbai]').first.click()
        # 真调 LLM，给足超时；渲染落在行内 .nbai 卡片
        pg.wait_for_selector('#nbBody .nbai .nbai-ex', timeout=60000)
        box_txt = pg.locator('#nbBody .nbai').first.inner_text()
        check("讲解卡片含大白话正文（≥20字）", len(box_txt) >= 20, box_txt.replace("\n", " ")[:60])
        check("讲解卡片含风格标签行", "AI 讲解" in box_txt)
        check("讲解卡片含今日配额行", "今日配额" in box_txt and "已用" in box_txt,
              [ln for ln in box_txt.split("\n") if "配额" in ln][:1])
        n_watch = pg.locator('#nbBody .nbai .nbai-wt li').count()
        check("含 ≥1 条买前注意点", n_watch >= 1, f"{n_watch} 条")
        mask_names(pg)
        shoot(pg, path="shots/newbie_ai.webp", full_page=False)

        # ---------- 3. 配额 0 → 429 友好呈现 → 恢复 ----------
        print("\n=== 3. 超额 429 呈现（DB 置 0 后恢复）===")
        set_quota(USER, 0)
        try:
            # 先收起再点开（同按钮 toggle）
            pg.locator('#nbBody [data-nbai]').first.click()
            pg.wait_for_timeout(400)
            pg.locator('#nbBody [data-nbai]').first.click()
            pg.wait_for_function(
                """() => { let b = document.querySelector('#nbBody .nbai');
                  return b && (b.textContent.indexOf('已用完') >= 0
                            || b.textContent.indexOf('讲解失败') >= 0); }""",
                timeout=20000)
            dead = pg.locator('#nbBody .nbai').first.inner_text()
            check("超额时呈现 429 文案（已用完/次数）", "已用完" in dead or "次数" in dead,
                  dead.replace("\n", " ")[:70])
        finally:
            set_quota(USER, 10)

        print("\n=== JS 错误 ===")
        check("全程零 JS 报错", not errors, errors[:3])
        ctx.close()
        b.close()

    ok_n = sum(1 for _, c in checks if c)
    print(f"\ne2e_newbie_ai: {ok_n}/{len(checks)} 通过")
    sys.exit(0 if ok_n == len(checks) else 1)


if __name__ == "__main__":
    main()
