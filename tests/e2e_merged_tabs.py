#!/usr/bin/env python3
"""
「市场榜单 / 市场热点 / 异动监控」合并后的浏览器端到端校验。

跑法（需先起服务在 8899）：
    python3 tests/e2e_merged_tabs.py

验的是"用户真能点到"，不是静态代码：
  1. 页签栏只剩一个「📊 市场榜单」，热点/异动页签不可见
  2. 下拉里有 3 个分组、共 19 项
  3. 逐项切换 → 对应卡片显示、其余隐藏、无 console 报错
  4. 列表里的股票可点（跳个股分析）
  5. 「展开全部」把当前分组平铺成按钮
"""
import re
import sys
import time

BASE = "http://127.0.0.1:8899"
ok_n = 0
fail: list[str] = []


def check(cond, good, bad):
    global ok_n
    if cond:
        ok_n += 1
        print(f"  ✅ {good}")
    else:
        print(f"  ❌ {bad}")
        fail.append(bad)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：sudo pip3 install playwright && playwright install chromium")
        return 2

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1440, "height": 950})
        errs: list[str] = []
        bad_urls: list[tuple[int, str]] = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: bad_urls.append((r.status, r.url)) if r.status >= 400 else None)

        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # ---- 1. 页签栏 ----
        print("\n[1] 页签栏")
        vis = pg.eval_on_selector_all(
            ".tabs button",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.dataset.tab)")
        check("hot" not in vis, "「市场热点」页签已隐藏", f"热点页签仍可见: {vis}")
        check("moves" not in vis, "「异动监控」页签已隐藏", f"异动页签仍可见: {vis}")
        check("rank" in vis, "「市场榜单」页签在", "市场榜单页签缺失")

        # ---- 2. 下拉结构 ----
        print("\n[2] 下拉结构")
        groups = pg.eval_on_selector_all(
            "#rankSel optgroup", "els => els.map(e => e.label)")
        check(len(groups) == 3, f"3 个分组：{groups}", f"分组数异常: {groups}")
        n_opts = pg.eval_on_selector("#rankSel", "e => e.options.length")
        check(n_opts == 19, f"共 19 个内容项（实为 {n_opts}）", f"内容项数异常: {n_opts}")

        # ---- 3. 逐项切换 ----
        print("\n[3] 逐项切换")
        vals = pg.eval_on_selector_all("#rankSel option", "els => els.map(e => e.value)")
        cases = [
            ("rank:gainers", "rankCard", "📈 涨幅榜"),
            ("rank:pe", "rankCard", "💎 低估值榜"),
            ("hot:hotrank", "hotCard", "🔥 人气热榜"),
            ("hot:ladder", "hotCard", "🪜 连板天梯"),
            ("hot:limitup", "hotCard", "📈 涨停池"),
            ("hot:anomaly", "hotCard", "⚡ 异动解读"),
            ("moves:涨停", "movesCard", "异动监控 · 🔴 涨停"),
            ("moves:放量", "movesCard", "异动监控 · 📊 放量"),
        ]
        for val, card, expect_title in cases:
            check(val in vals, f"下拉含 {val}", f"下拉缺 {val}")
            pg.select_option("#rankSel", val)
            pg.wait_for_timeout(2600)
            shown = pg.eval_on_selector_all(
                "#rankCard, #hotCard, #movesCard",
                "els => els.filter(e => e.offsetParent !== null).map(e => e.id)")
            check(shown == [card], f"{val} → 只显示 {card}", f"{val} 显示了 {shown}")
            title = pg.eval_on_selector(
                f"#{card} h2", "e => e.textContent.trim()")
            check(expect_title in title, f"{val} 标题 = {title}", f"{val} 标题异常: {title}")

        # ---- 4. 可点击 ----
        print("\n[4] 股票可点击跳转")
        pg.select_option("#rankSel", "rank:gainers")
        pg.wait_for_timeout(2500)
        rows = pg.eval_on_selector_all("#rankBody tr[data-code]", "e => e.length")
        check(rows > 0, f"涨幅榜有 {rows} 行", "涨幅榜无数据")
        if rows:
            code = pg.eval_on_selector("#rankBody tr[data-code]", "e => e.dataset.code")
            pg.click("#rankBody tr[data-code]")
            pg.wait_for_timeout(2600)
            tab = pg.eval_on_selector(".tabs button.on", "e => e.dataset.tab")
            check(tab == "stock", f"点击 {code} 跳到个股分析", f"点击后停在 {tab}")

        # 热点卡片里的 a.slink 也要能点（历史 bug 回归点）
        pg.click(".tabs button[data-tab='rank']")
        pg.select_option("#rankSel", "hot:hotrank")
        pg.wait_for_timeout(2800)
        links = pg.eval_on_selector_all("#hotBody a[data-code]", "e => e.length")
        check(links > 0, f"人气热榜有 {links} 个可点链接", "人气热榜无可点链接")
        if links:
            pg.click("#hotBody a[data-code]")
            pg.wait_for_timeout(2600)
            tab = pg.eval_on_selector(".tabs button.on", "e => e.dataset.tab")
            check(tab == "stock", "热榜链接可跳个股分析", f"热榜链接点击后停在 {tab}")

        # ---- 5. 展开全部 ----
        print("\n[5] 展开全部")
        pg.click(".tabs button[data-tab='rank']")
        pg.select_option("#rankSel", "hot:lhb")
        pg.wait_for_timeout(1200)
        lab = pg.eval_on_selector("#rankAll", "e => e.textContent.trim()")
        check("热点" in lab, f"热点分组按钮文案：{lab}", f"热点文案异常: {lab}")
        pg.click("#rankAll")
        pg.wait_for_timeout(500)
        n_btn = pg.eval_on_selector_all(".hotexpand button", "e => e.length")
        check(n_btn == 7, f"热点分组平铺出 {n_btn} 个按钮", f"热点平铺按钮数异常: {n_btn}")

        pg.select_option("#rankSel", "moves:涨停")
        pg.wait_for_timeout(1200)
        lab2 = pg.eval_on_selector("#rankAll", "e => e.textContent.trim()")
        check("异动" in lab2, f"异动分组按钮文案：{lab2}", f"异动文案异常: {lab2}")
        pg.click("#rankAll")
        pg.wait_for_timeout(400)
        n_btn2 = pg.eval_on_selector_all(".hotexpand button", "e => e.length")
        check(n_btn2 == 6, f"异动分组平铺出 {n_btn2} 个按钮", f"异动平铺数异常: {n_btn2}")

        # 平铺按钮点一下应该直接切换，并且自动收起（避免遮挡）
        if n_btn2:
            txt = pg.eval_on_selector(".hotexpand button:nth-child(4)", "e => e.textContent")
            pg.click(".hotexpand button:nth-child(4)")
            pg.wait_for_timeout(2400)
            title = pg.eval_on_selector("#movesTitle", "e => e.textContent.trim()")
            check("异动监控" in title, f"点平铺按钮切到「{txt}」", f"平铺按钮点击后标题异常: {title}")
            n_after = pg.eval_on_selector_all(".hotexpand", "e => e.length")
            check(n_after == 0, "切换后自动收起", f"切换后未收起，还剩 {n_after} 个")

        # 展开 → 再点按钮 → 收起
        pg.click("#rankAll")
        pg.wait_for_timeout(400)
        check(pg.eval_on_selector_all(".hotexpand", "e => e.length") == 1, "展开成功", "展开失败")
        pg.click("#rankAll")
        pg.wait_for_timeout(400)
        check(pg.eval_on_selector_all(".hotexpand", "e => e.length") == 0,
              "再点一次收起展开区", "再点一次未能收起")

        # ---- 6. console ----
        print("\n[6] 运行时报错")
        # 已知的良性 404：次新股（C 字头，上市 5 日内）历史 K 线不足 20 根，
        # /api/keylevels 与 /api/indicators_full 会返回 404，前端已 catch 成 null 降级。
        # 这不是本次合并引入的，属于既有语义问题（用 404 表达"数据不足"）。
        BENIGN = ("/api/keylevels", "/api/indicators_full")
        benign_urls = [u for _, u in bad_urls if any(b in u for b in BENIGN)]
        other_bad = [(s, u) for s, u in bad_urls if not any(b in u for b in BENIGN)]
        check(not other_bad, "无其他 4xx/5xx", f"出现非预期错误: {other_bad[:3]}")
        if benign_urls:
            print(f"   ℹ️ 良性 404 × {len(benign_urls)}（次新股 K 线不足，前端已降级）")
        # console 里的资源报错文案不含 URL，按"良性 404 条数"抵扣
        net404 = [e for e in errs if "404" in e]
        real = [e for e in errs if e not in net404 and "favicon" not in e.lower()]
        check(len(net404) <= len(benign_urls),
              f"资源 404 均为良性（{len(net404)} 条）",
              f"有 {len(net404) - len(benign_urls)} 条非良性 404")
        check(not real, "无 JS 运行时报错", f"有 {len(real)} 条报错: {real[:3]}")

        pg.screenshot(path="/tmp/merged_tabs.png", full_page=False)
        b.close()

    print("\n" + "=" * 56)
    if fail:
        print(f"❌ {len(fail)} 项未通过：")
        for f in fail:
            print(f"   · {f}")
        return 1
    print(f"✅ 全部通过（{ok_n} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
