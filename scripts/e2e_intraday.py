#!/usr/bin/env python3
"""分钟级功能端到端测试。

覆盖两处入口（2026-09 整合后的架构，已无独立「分时看盘」页签）：
  1. 个股分析页的 K 线卡片：日K/周K/月K + 1/5/15/30/60 分周期按钮、深度指标摘要行
  2. 条件选股页的「分钟级选股」：全市场扫描、点行跳个股页并带入周期

用法： python3 scripts/e2e_intraday.py [--base http://127.0.0.1:8899]
退出码 0 = 全通过。
"""
import argparse
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
SHOT = "/tmp/e2e"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    a = ap.parse_args()
    base = a.base

    errs, pageerrs = [], []
    steps = []

    def step(name, ok, detail=""):
        steps.append((name, ok, detail))
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))

    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
              if m.type == "error" else None)
        pg.on("pageerror", lambda e: pageerrs.append(str(e)))

        pg.goto(base, wait_until="networkidle", timeout=60000)
        step("打开首页", pg.title() is not None, pg.title())

        # ---------- 0. 旧页签确已下线 ----------
        step("已无「分时看盘」页签",
             pg.locator('button[data-tab="intraday"]').count() == 0)
        step("已无 tab-intraday 面板",
             pg.locator("#tab-intraday").count() == 0)

        # ---------- 1. 个股页 K 线卡片 ----------
        pg.click('button[data-tab="stock"]')
        pg.wait_for_timeout(500)
        pg.evaluate("() => openStock('sh600519')")
        pg.wait_for_timeout(6000)

        step("日/周/月按钮齐备", pg.locator("#perBtns .btn").count() == 3)
        step("分钟按钮齐备（5 个）", pg.locator("#perMinBtns .btn").count() == 5)
        hl = pg.evaluate(
            "() => [...document.querySelectorAll('#perBtns .btn.on')].map(b=>b.textContent)")
        step("默认高亮日K", hl == ["日K"], str(hl))
        step("日线图已渲染", pg.locator("#ckline canvas").count() > 0)

        # 逐个分钟周期：高亮、提示条、摘要行、图表实例不泄漏
        for p, label in [("1m", "1分"), ("5m", "5分"),
                         ("15m", "15分"), ("30m", "30分"), ("60m", "60分")]:
            pg.click(f'#perMinBtns button[data-minperiod="{p}"]')
            pg.wait_for_timeout(5000)
            on = pg.evaluate(
                "() => [...document.querySelectorAll('#perMinBtns .btn.on')]"
                ".map(b=>b.textContent)")
            note = pg.inner_text("#stockPerNote").replace("\n", " ")[:70]
            daily = pg.evaluate(
                "() => document.querySelectorAll('#perBtns .btn.on').length")
            canv = pg.locator("#ckline canvas").count()
            ok = (on == [label] and daily == 0 and canv == 1
                  and "根" in note and "失败" not in note)
            step(f"分钟周期 {label}", ok, f"高亮={on} 日线高亮={daily} canvas={canv}")

        # 摘要行（原分时看盘的深度指标，现压缩成一行）
        summ = pg.inner_text("#stockMtSummary").replace("\n", " ")
        step("深度指标摘要行有内容", len(summ) > 30, summ[:110])
        for kw in ["区间位置", "量比", "均线排列"]:
            step(f"摘要含「{kw}」", kw in summ)

        # 再点一次同一周期 -> 退回日线
        pg.click('#perMinBtns button[data-minperiod="60m"]')
        pg.wait_for_timeout(6000)
        back = pg.evaluate(
            "() => [...document.querySelectorAll('#perBtns .btn.on')]"
            ".map(b=>b.textContent)")
        min_on = pg.evaluate(
            "() => document.querySelectorAll('#perMinBtns .btn.on').length")
        hidden = pg.evaluate(
            "() => getComputedStyle(document.getElementById('stockPerNote')).display")
        step("再点一次退回日线", back == ["日K"] and min_on == 0 and hidden == "none",
             f"日线={back} 分钟高亮={min_on} 提示条={hidden}")
        step("摘要行已清空", pg.inner_text("#stockMtSummary").strip() == "")

        pg.screenshot(path=f"{SHOT}_stock_minute.png")

        # ---------- 2. 条件选股页的分钟级选股 ----------
        pg.click('button[data-tab="screen"]')
        pg.wait_for_timeout(5000)
        step("扫描器已迁入条件选股页", pg.locator("#idScan").count() == 1)
        n_opt = pg.locator("#idPreset option").count()
        step("预设条件已加载", n_opt > 1, f"{n_opt} 项")

        pg.click('#idPerBtns button[data-iper="30m"]')
        pg.wait_for_timeout(600)
        meta = pg.inner_text("#idScanMeta")
        step("切换周期后元信息联动", "30分" in meta, meta[-26:])

        t0 = time.time()
        pg.click("#idScan")
        for _ in range(120):
            s = pg.inner_text("#idScanStatus")
            if s.strip() and "扫描" not in pg.inner_text("#idScanBody"):
                break
            pg.wait_for_timeout(1000)
        pg.wait_for_timeout(1500)
        cost = time.time() - t0
        s = pg.inner_text("#idScanStatus").replace("\n", " ")[:130]
        step("全市场扫描完成",
             "扫过" in s and "失败" not in s, f"{cost:.1f}s | {s}")
        rows = pg.locator("#idScanBody tr[data-code]").count()
        step("扫描有结果行", rows > 0, f"{rows} 行")

        # 点行 -> 进个股页并带入扫描周期
        if rows > 0:
            code = pg.evaluate(
                "() => document.querySelector('#idScanBody tr[data-code]').dataset.code")
            pg.locator("#idScanBody tr[data-code]").first.click()
            pg.wait_for_timeout(9000)
            on_tab = pg.evaluate(
                "() => document.querySelector('.tabs button.on')?.textContent")
            on_min = pg.evaluate(
                "() => [...document.querySelectorAll('#perMinBtns .btn.on')]"
                ".map(b=>b.textContent)")
            on_daily = pg.evaluate(
                "() => document.querySelectorAll('#perBtns .btn.on').length")
            step("点行跳个股分析页", "个股分析" in (on_tab or ""), str(on_tab))
            step("跳转后带入扫描周期 30分",
                 on_min == ["30分"] and on_daily == 0,
                 f"分钟={on_min} 日线={on_daily} ({code})")
            note = pg.inner_text("#stockPerNote")
            step("跳转后分钟图正常", "30m 周期" in note and "失败" not in note,
                 note[:70])

        pg.screenshot(path=f"{SHOT}_screen_scan.png")

        # ---------- 3. 其他页签回归 ----------
        steps_tabs = []
        for nm in ["rank", "newbie", "watch", "paper", "hot", "strategy",
                   "backtest", "moves", "compare", "about"]:
            try:
                pg.click(f'button[data-tab="{nm}"]')
                pg.wait_for_timeout(1500)
                on = pg.evaluate(
                    f"() => document.querySelector('#tab-{nm}')"
                    ".classList.contains('on')")
                ok = bool(on)
                steps_tabs.append((nm, ok))
            except Exception as e:
                step(f"页签 {nm}", False, str(e)[:70])
        bad = [n for n, o in steps_tabs if not o]
        step(f"其余 {len(steps_tabs)} 个页签正常", not bad, str(bad) if bad else "")

        pg.screenshot(path=f"{SHOT}_final.png")
        b.close()

    # 主动触发的 400/404 是预期内的，浏览器会记成 console error，这里只关心脚本错误
    real_errs = [e for e in errs if "Failed to load resource" not in e]
    step("无意外的 console error", not real_errs, "; ".join(real_errs[:3]))
    step("无 pageerror", not pageerrs, "; ".join(pageerrs[:3]))
    if errs:
        print(f"  （另有 {len(errs)} 条资源加载错误，为主动触发的 400/404，属预期）")

    ok = all(s[1] for s in steps)
    print(f"\n结论：{'全部通过 ✓' if ok else '存在问题 ✗'} "
          f"（{sum(1 for s in steps if s[1])}/{len(steps)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
