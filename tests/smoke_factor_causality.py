# -*- coding: utf-8 -*-
"""A2+A3 前端端到端验证：因果性说明带、快照说明带、判定列。
用法：cd /workspace/tick-stock-panel && .venv/bin/python tests/smoke_factor_causality.py
"""
import sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
CHROME = "/usr/bin/chromium"
fails, passes = [], []


def ck(name, cond, extra=""):
    (passes if cond else fails).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))


def main():
    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROME,
                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1500, "height": 1200}, device_scale_factor=2)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errs.append("PAGEERROR " + str(e)))
        pg.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        pg.evaluate("""async ()=>{
          await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:'bltest',password:'bltest123',display:'x'})}).catch(()=>{});
          await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:'bltest',password:'bltest123'})});}""")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)

        tabs = pg.eval_on_selector_all("button[data-tab]", "e=>e.map(x=>x.dataset.tab)")
        pg.evaluate("(t)=>{document.querySelectorAll('button[data-tab=\"'+t+'\"]').forEach(b=>b.click())}",
                    "factorlab")
        pg.wait_for_timeout(2500)  # 等 /api/pools + availability 填完下拉

        sel = pg.eval_on_selector("#flPool", "e=>e.options.length")
        ck("股票池下拉已填充", sel > 0, sel)
        pg.select_option("#flPool", "000300")
        pg.evaluate("()=>{document.getElementById('flDays').value='500';}")

        t0 = time.time()
        pg.click("#flRun")
        pg.wait_for_selector("#flTbl", timeout=120000)
        pg.wait_for_timeout(400)
        took = time.time() - t0

        # ── 两条说明带
        bars = pg.eval_on_selector_all("#flBody .flbar", "e=>e.map(x=>({cls:x.className,txt:x.innerText}))")
        ck("表格上方有 2 条说明带", len(bars) == 2, len(bars))
        causal = next((x for x in bars if "因果性" in x["txt"]), None)
        snap = next((x for x in bars if "数据快照" in x["txt"]), None)
        ck("因果性说明带存在且为通过态", causal is not None and "ok" in causal["cls"], causal and causal["cls"])
        ck("因果性文案含「因果性检查通过」", causal is not None and "因果性检查通过" in causal["txt"])
        ck("因果性文案含检查窗口天数", causal is not None and "检查窗口" in causal["txt"])
        ck("快照说明带存在", snap is not None)
        ck("快照带含 SHA-256 前缀", snap is not None and "SHA-256" in snap["txt"])
        ck("快照带含行数/跨度", snap is not None and "行数" in snap["txt"] and "跨度" in snap["txt"])
        ck("快照带含「先比哈希」复现说明", snap is not None and "先比哈希" in snap["txt"])
        # 说明带必须在表格之前 —— 顺序是刻意的：先看能不能用，再看数字多少
        order = pg.evaluate("""()=>{
          const body=document.getElementById('flBody');
          const bar=body.querySelector('.flbar'), tbl=document.getElementById('flTbl');
          return !!(bar && tbl) && (bar.compareDocumentPosition(tbl) & Node.DOCUMENT_POSITION_FOLLOWING) > 0;
        }""")
        ck("说明带排在表格之前", order)

        # ── 判定列
        rows = pg.eval_on_selector_all("#flTbl tbody tr",
                                       "e=>e.map(r=>r.lastElementChild.innerText.replace(/\\s+/g,' ').trim())")
        ck("因子表 12 行", len(rows) == 12, len(rows))
        ck("每行判定列都标明「只看过去」", all("只看过去" in r for r in rows), rows[:3])
        ck("无「⚠ 用了未来」/「✕ 前视」误报",
           not any(("用了未来" in r) or ("前视" in r) for r in rows))
        sha_in_row = pg.eval_on_selector_all("#flTbl tbody tr", "e=>e.length")
        ck("表格行数 > 0", sha_in_row > 0)

        # ── 快照哈希可复制（显示只露前 32 位，复制的必须是完整 64 位）
        pg.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="http://127.0.0.1:8899")
        full_sha = pg.eval_on_selector(".flsha", "e=>e.getAttribute('data-sha') || ''")
        ck("data-sha 是完整 64 位", len(full_sha) == 64, len(full_sha))
        ck("元素文本只露前 32 位加省略号",
           pg.eval_on_selector(".flsha", "e=>e.textContent").endswith("…"))
        pg.click(".flsha")
        pg.wait_for_timeout(400)
        clip = pg.evaluate("()=>navigator.clipboard.readText()")
        ck("点击后剪贴板是完整哈希", clip == full_sha, clip[:16])
        ck("点击有「已复制」反馈", "已复制" in pg.eval_on_selector(".flsha", "e=>e.textContent"))
        pg.wait_for_timeout(900)

        # ── 截图：明/暗 + 窄屏
        pg.locator("#flBody").screenshot(path="/workspace/tick-stock-panel/shots/A2A3-因果性与快照-明.png")
        pg.evaluate("()=>{document.documentElement.dataset.theme='dark';}")
        pg.wait_for_timeout(350)
        pg.locator("#flBody").screenshot(path="/workspace/tick-stock-panel/shots/A2A3-因果性与快照-暗.png")
        pg.evaluate("()=>{document.documentElement.dataset.theme='';}")

        # 窄屏溢出检查：sha 是 32 位十六进制，最容易撑破。
        # 布局在 ~820px 断点切换成手机版，所以这里直接缩视口量 .flbar 的
        # scrollWidth-clientWidth（不靠元素截图，元素截图会把超出的部分裁掉，
        # 看起来"没溢出"）。
        pg.set_viewport_size({"width": 390, "height": 1400})
        pg.wait_for_timeout(500)
        ovf = pg.evaluate("""()=>{
          const bars=[...document.querySelectorAll('.flbar')];
          if(!bars.length) return -1;
          return Math.max(...bars.map(b=>b.scrollWidth-b.clientWidth));
        }""")
        ck("390px 下说明带无横向溢出", ovf <= 1, ovf)
        # 说明带的盒子必须落在视口内（不能靠被裁切来“看起来没溢出”）
        box = pg.evaluate("""()=>{
          const bars=[...document.querySelectorAll('.flbar')];
          return bars.map(b=>{const r=b.getBoundingClientRect();
            return {l:Math.round(r.left), r:Math.round(r.right), w:Math.round(r.width)}});
        }""")
        ck("说明带盒子不超出视口宽度", all(x["r"] <= 391 and x["l"] >= -1 for x in box), box)
        pg.screenshot(path="/workspace/tick-stock-panel/shots/A2A3-因果性与快照-窄屏.png",
                      clip={"x": 0, "y": 0, "width": 390, "height": 760})

        ck("无 JS 运行时异常", not errs, errs[:3])
        print("\n耗时 %.1fs" % took)
        b.close()

    print("\n%d/%d PASS" % (len(passes), len(passes) + len(fails)))
    if fails:
        print("FAILED:", fails)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
