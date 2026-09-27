"""波动率三件套交付截图。

三张图：
  1. 虚拟盘持仓表的「建议止损」列（吊灯止损价 + 距线幅度，跌破标红）
  2. 个股 K 线主图叠加「吊灯止损」线（橙实线）
  3. 全套技术指标新增的「波动率」分组

用法：python3 tests/shot_vol_stop.py
前置：服务在 8899。
"""
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899/"
OUT_POS = "docs/shot-vol-stop-position.png"
OUT_KLINE = "docs/shot-vol-stop-kline.png"
OUT_IND = "docs/shot-vol-stop-indicators.png"


def mark_card(pg, title, cid):
    """按标题找到所在卡片并打上 id（卡片本身没有 id，只能这样定位）。"""
    pg.evaluate(
        """([title, cid]) => {
             var h = Array.from(document.querySelectorAll('h2'))
                        .find(x => (x.textContent || '').indexOf(title) >= 0);
             if (!h) return false;
             var card = h.closest('.card');
             if (!card) return false;
             card.id = cid;
             return true;
           }""",
        [title, cid],
    )


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1560, "height": 1500})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1500)

        # ---------- 1) 个股页：K 线 + 吊灯止损线 + 全套指标 ----------
        pg.evaluate("openStock('sh600519')")
        # 主图是 canvas 渲染（ECharts 默认），不是 svg
        pg.wait_for_selector("#ckline canvas", timeout=120000)
        pg.wait_for_timeout(2500)

        # 切到「吊灯止损」叠加模式
        pg.click('button[data-chan="stop"]')
        pg.wait_for_timeout(1500)
        on = pg.evaluate(
            """() => {
                 var b = document.querySelector('button[data-chan="stop"]');
                 return !!b && b.classList.contains('on');
               }"""
        )
        if not on:
            print("! 吊灯止损按钮未高亮，图可能没切换")
        mark_card(pg, "K线走势", "shotKlineCard")
        pg.locator("#shotKlineCard").screenshot(path=OUT_KLINE)
        print("已保存", OUT_KLINE)

        # 全套技术指标（含新增波动率组）
        pg.wait_for_function(
            """() => {
                 var h = Array.from(document.querySelectorAll('.indgt'))
                            .find(x => (x.textContent || '').indexOf('波动率') >= 0);
                 return !!h;
               }""",
            timeout=30000,
        )
        mark_card(pg, "全套技术指标", "shotIndCard")
        pg.wait_for_timeout(500)
        pg.locator("#shotIndCard").screenshot(path=OUT_IND)
        print("已保存", OUT_IND)

        # ---------- 2) 虚拟盘：持仓表的建议止损列 ----------
        pg.click('button[data-tab="paper"]')
        pg.wait_for_timeout(600)
        pg.click("#ppGateLogin")
        pg.wait_for_timeout(400)
        pg.click("#authSwap")
        pg.wait_for_timeout(200)
        uname = "_shot_vol_%d" % int(time.time())
        pg.fill("#authUser", uname)
        pg.fill("#authPwd", "test1234")
        pg.click("#authSubmit")
        pg.wait_for_timeout(3000)

        for nm in ("贵州茅台", "五粮液"):
            pg.fill("#ppBuyCode", nm)
            pg.fill("#ppBuyQty", "100")
            pg.click("#ppBuy")
            pg.wait_for_timeout(2500)

        pg.wait_for_function(
            """() => {
                 var ths = Array.from(document.querySelectorAll('#ppPosBody th'));
                 return ths.some(t => (t.textContent || '').indexOf('建议止损') >= 0);
               }""",
            timeout=20000,
        )
        pg.wait_for_timeout(800)
        mark_card(pg, "我的持仓", "shotPosCard")
        pg.locator("#shotPosCard").screenshot(path=OUT_POS)
        print("已保存", OUT_POS, f"（测试账号 {uname}）")

        print("JS 错误:", errs if errs else "无")
        b.close()


if __name__ == "__main__":
    main()
