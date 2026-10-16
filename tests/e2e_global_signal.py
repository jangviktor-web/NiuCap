"""全球领先信号层 端到端验证（#225）。

链路：
  1. GET /api/newsfeed/global → 200 + 结构正确（sectors/meta/points 溯源 URL）
  2. 快讯页「板块舆情」tab 内出现「🌐 全球领先信号」折叠区，展开后渲染要点
  3. 源数标注「N源」渲染（多源共识信号可见）
  4. 折叠交互：默认收起 → 点击展开 → 再点收起
  5. 脚本自检：server/global_signal.py 的 selfcheck 全绿
  6. 全程 JS 零报错；截图留档（shots/global_signal.png）

用法：python3.11 tests/e2e_global_signal.py
"""
from playwright.sync_api import sync_playwright
import subprocess
import os
import sys

BASE = "http://127.0.0.1:8899"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

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
    pg.evaluate("""() => {
      let u = document.querySelector('#authUser span, #uArea .uname b');
      if(u) u.textContent = '演示用户';
      let i = document.querySelector('#authUser .ava, #uArea .ava');
      if(i) i.style.filter = 'blur(6px)';
    }""")


def main():
    # ---------- 0. 模块自检 ----------
    print("\n=== 0. global_signal selfcheck ===")
    r = subprocess.run([os.path.join(ROOT, ".venv", "bin", "python"),
                        os.path.join(ROOT, "server", "global_signal.py")],
                       capture_output=True, text=True, cwd=os.path.join(ROOT, "server"))
    check("selfcheck 全绿", r.returncode == 0, (r.stdout or "").strip().split("\n")[-1])

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 1100})
        pg = ctx.new_page()
        watch(pg)

        # ---------- 1. API 层 ----------
        print("\n=== 1. /api/newsfeed/global 结构 ===")
        pg.goto(BASE + "/", wait_until="load", timeout=25000)
        d = pg.evaluate("async()=>{ let r=await fetch('/api/newsfeed/global'); return {st:r.status, j:await r.json()}; }")
        check("HTTP 200", d["st"] == 200, d["st"])
        j = d["j"]
        check("返回 ok:true 且含 sectors", j.get("ok") is True and isinstance(j.get("sectors"), list),
              f"{len(j.get('sectors', []))} 板块")
        check("meta 含源统计与抓取时间",
              (j.get("meta") or {}).get("sources_total", 0) > 0
              and bool((j.get("meta") or {}).get("fetched_at")), j.get("meta", {}).get("fetched_at"))
        pt_with_url = 0
        pt_total = 0
        for s in j.get("sectors", []):
            for pt in s.get("points", []):
                pt_total += 1
                if pt.get("url"):
                    pt_with_url += 1
        # 容差 10%：LLM 偶尔给一条 refs 为空的「纯归纳」要点（不引用具体故事），
        # digest_sector 对这种情况显式兜底 url="" —— 属设计内，不是丢数据。
        # 卡 100% 会随每次抓取随机翻红，噪声大于信号。
        miss_pct = (pt_total - pt_with_url) * 100.0 / max(pt_total, 1)
        check("要点带溯源 URL（容差≤10%）",
              pt_total > 0 and miss_pct <= 10.0,
              f"{pt_with_url}/{pt_total}（缺 {miss_pct:.1f}%）")

        # ---------- 2. 前端渲染 ----------
        print("\n=== 2. 板块舆情内折叠区渲染 ===")
        pg.click('#navToggle')
        pg.wait_for_timeout(300)
        pg.click('.nd-item[data-tab="news"]')
        pg.wait_for_timeout(1200)
        pg.click('button[data-ntab="sector"]')
        pg.wait_for_selector('#gsWrap', timeout=20000)
        check("板块舆情内出现全球信号折叠区", pg.locator('#gsWrap').count() == 1)
        check("默认收起（无 open 类）",
              not pg.evaluate("document.getElementById('gsWrap').classList.contains('open')"))

        pg.click('#gsHead')
        pg.wait_for_timeout(300)
        check("点击后展开", pg.evaluate("document.getElementById('gsWrap').classList.contains('open')"))
        pg.wait_for_selector('#gsBody .gs-sec', timeout=15000)
        n_sec = pg.locator('#gsBody .gs-sec').count()
        n_pt = pg.locator('#gsBody .gs-pt').count()
        check("渲染 ≥1 个板块分区", n_sec >= 1, f"{n_sec} 板块")
        check("渲染 ≥1 条中文要点", n_pt >= 1, f"{n_pt} 条要点")
        body_txt = pg.locator('#gsBody').inner_text()
        check("含源统计 footer", "故事" in body_txt and "抓取于" in body_txt)
        check("含「N源」共识标注或源名", ("源" in body_txt), body_txt[:60].replace("\n", " "))

        # ---------- 2b. 方向判定（利好/利空） ----------
        print("\n=== 2b. 要点方向徽标渲染 ===")
        n_dir = pg.locator('#gsBody .gs-pt .gs-dir').count()
        check("每条要点带方向徽标（利好/利空/中性）", n_dir == n_pt, f"{n_dir}/{n_pt}")
        n_net = pg.locator('#gsBody .gs-sec-h .gs-net').count()
        check("每个板块头带净方向徽标", n_net == n_sec, f"{n_net}/{n_sec}")
        dir_txt = pg.locator('#gsBody').inner_text()
        check("方向词出现在页面上", any(k in dir_txt for k in ("利好", "利空", "中性")))
        check("footer 说明 AI 判定方向口径", "产业基本面" in dir_txt)

        # 收起验证
        pg.click('#gsHead')
        pg.wait_for_timeout(250)
        check("再点收起", not pg.evaluate("document.getElementById('gsWrap').classList.contains('open')"))
        pg.click('#gsHead')          # 展开回来，便于截图
        pg.wait_for_timeout(400)
        mask_names(pg)
        pg.evaluate("document.getElementById('gsWrap').scrollIntoView({block:'start'})")
        pg.wait_for_timeout(400)
        pg.screenshot(path="shots/global_signal.png", full_page=False)

        print("\n=== JS 错误 ===")
        check("全程零 JS 报错", not errors, errors[:3])
        ctx.close()
        b.close()

    ok_n = sum(1 for _, c in checks if c)
    print(f"\ne2e_global_signal: {ok_n}/{len(checks)} 通过")
    sys.exit(0 if ok_n == len(checks) else 1)


if __name__ == "__main__":
    main()
